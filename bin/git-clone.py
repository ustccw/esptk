#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
Shallow clone of a git repository and its submodules.

Examples:
  git-clone.py ssh://git@gitlab.espressif.cn:27227/sdk/ESP8266_RTOS_SDK.git release/v3.4
  git-clone.py https://github.com/espressif/ESP8266_RTOS_SDK.git v3.4
  git-clone.py <url> a1b2c3d4e5f6... -o my-sdk
  git-clone.py <url> --depth 20 release/v3.4
  git-clone.py <url> --no-recurse
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

DEFAULT_JOBS = 8
DEFAULT_GITLAB = "https://gitlab.espressif.cn:6688"
DEFAULT_TOKEN_FILES = (
    "~/.esptk/gitlab_oauth_token",
    "~/.gitlab_oauth_token",
)
USER_AGENT = "git-clone.py"

SHA40 = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
SHA_PREFIX = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)

_SSL_CTX = ssl._create_unverified_context()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _color_enabled(stream: Any) -> bool:
    return (not os.environ.get("NO_COLOR")
            and hasattr(stream, "isatty")
            and stream.isatty())


def _c(code: str, text: str, *, stream: Any = sys.stdout) -> str:
    if not _color_enabled(stream):
        return text
    return f"\033[{code}m{text}\033[0m"


def _info(msg: str) -> None:
    print(f"{_c('1;32', '[INFO]')} {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"{_c('1;33', '[WARN]', stream=sys.stderr)} {msg}",
          file=sys.stderr, flush=True)


def _error(msg: str) -> None:
    print(f"{_c('1;31', '[ERROR]', stream=sys.stderr)} {msg}",
          file=sys.stderr, flush=True)


def _die(msg: str, code: int = 1) -> NoReturn:
    _error(msg)
    raise SystemExit(code)


def _human_size(n: int) -> str:
    step = 1024.0
    units = ["B", "K", "M", "G", "T"]
    val = float(n)
    for unit in units:
        if val < step or unit == units[-1]:
            if unit == "B":
                return f"{int(val)}B"
            return f"{val:.1f}{unit}"
        val /= step
    return f"{n}B"


def _du_bytes(path: Path) -> int:
    if not path.exists() or path.is_symlink():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            fp = Path(root) / name
            try:
                st = fp.lstat()
            except OSError:
                continue
            if not stat.S_ISLNK(st.st_mode):
                total += st.st_size
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
    return total


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    return env


def git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-c", "advice.detachedHead=false", *args]
    kw: dict[str, Any] = {
        "cwd": str(cwd) if cwd is not None else None,
        "env": _git_env(),
        "check": False,
        "text": True,
    }
    if capture:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.PIPE
    proc = subprocess.run(cmd, **kw)
    if check and proc.returncode != 0:
        err = ((proc.stderr or proc.stdout or "") if capture else "").strip()
        pretty = "git " + " ".join(args)
        if err:
            raise RuntimeError(f"{pretty}\n{err}")
        raise RuntimeError(f"{pretty} failed ({proc.returncode})")
    return proc


def git_out(args: list[str], *, cwd: Path | str | None = None) -> str:
    return git(args, cwd=cwd, capture=True).stdout.strip()


# ---------------------------------------------------------------------------
# URL / token
# ---------------------------------------------------------------------------

def parse_org_repo(url: str) -> tuple[str, str] | None:
    u = url.strip()
    if u.startswith("git@"):
        try:
            path = u.split(":", 1)[1]
        except IndexError:
            return None
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            return None
        org, repo = parts[-2], parts[-1]
        if not repo.endswith(".git"):
            repo += ".git"
        return org, repo
    parts = urlsplit(u)
    segs = [p for p in parts.path.strip("/").split("/") if p]
    if len(segs) < 2:
        return None
    org, repo = segs[-2], segs[-1]
    if not repo.endswith(".git"):
        repo += ".git"
    return org, repo


def repo_dir_name(url: str) -> str:
    parsed = parse_org_repo(url)
    if parsed:
        repo = parsed[1]
        return repo[:-4] if repo.endswith(".git") else repo
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if ":" in name and "/" not in name:
        name = name.rsplit(":", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    if not name:
        _die("cannot derive directory name from URL; pass -o")
    return name


def gitlab_api_root(url: str) -> str | None:
    u = url.strip()
    if u.startswith("git@"):
        host = u.split(":", 1)[0][len("git@"):]
    else:
        parts = urlsplit(u)
        host = parts.hostname or ""
        if parts.scheme in ("http", "https") and "gitlab" in host.lower():
            netloc = parts.netloc
            if "@" in netloc:
                netloc = netloc.split("@", 1)[1]
            return f"{parts.scheme}://{netloc}"
    if "gitlab" not in host.lower():
        return None
    if host == "gitlab.espressif.cn" or host.startswith("gitlab.espressif.cn"):
        return DEFAULT_GITLAB
    return f"https://{host}"


def load_token() -> tuple[str | None, str]:
    env = os.environ.get("GITLAB_TOKEN", "").strip()
    if env:
        return env, "env:GITLAB_TOKEN"
    for raw in DEFAULT_TOKEN_FILES:
        path = os.path.expanduser(raw)
        try:
            text = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            _die(f"cannot read token file {path}: {exc}")
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line, path
        _die(f"token file {path} has no non-empty token line")
    return None, "none"


def gitlab_json(url: str, token: str) -> dict[str, Any]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "PRIVATE-TOKEN": token,
    }
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=20, context=_SSL_CTX) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        _die(f"GitLab API HTTP {exc.code} for {url}\n{body.strip()[:400]}")
    except (URLError, TimeoutError, OSError) as exc:
        _die(f"GitLab request failed: {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _die(f"invalid JSON from {url}: {exc}")
    if not isinstance(data, dict):
        _die(f"expected a JSON object from {url}")
    return data


def expand_short_sha(url: str, prefix: str) -> str:
    prefix = prefix.lower()
    token, source = load_token()
    if not token:
        _die(
            f"short SHA {prefix!r} cannot be fetched from remotes.\n"
            f"  Pass the full 40-character commit, or set GITLAB_TOKEN / "
            f"{DEFAULT_TOKEN_FILES[0]} / {DEFAULT_TOKEN_FILES[1]} "
            f"to expand it."
        )
    parsed = parse_org_repo(url)
    api_root = gitlab_api_root(url)
    if not parsed or not api_root:
        _die(
            f"short SHA expansion is only supported for GitLab URLs.\n"
            f"  Pass the full 40-character commit."
        )
    _info(f"using GitLab token from {source}")
    org, repo = parsed
    stem = repo[:-4] if repo.endswith(".git") else repo
    project = quote(f"{org}/{stem}", safe="")
    api = (
        f"{api_root}/api/v4/projects/{project}/repository/commits/{prefix}"
    )
    _info(f"looking up {prefix} as {org}/{stem}")
    data = gitlab_json(api, token)
    sha = str(data.get("id") or data.get("sha") or "").strip().lower()
    if not SHA40.match(sha) or not sha.startswith(prefix):
        _die(f"GitLab did not expand {prefix!r} to a unique commit")
    _info(f"expanded {prefix} -> {sha}")
    return sha


# ---------------------------------------------------------------------------
# Destination
# ---------------------------------------------------------------------------

def output_path(url: str, output: str | None) -> Path:
    name = output if output else repo_dir_name(url)
    dest = Path(name)
    if dest.is_absolute() or ".." in dest.parts:
        dest = Path(dest.name)
    return (Path.cwd() / dest).resolve()


def confirm_delete(dest: Path) -> None:
    if not dest.exists() and not dest.is_symlink():
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        _die(f"{dest} already exists (not a TTY; refusing to delete)")
    try:
        ans = input(f"{dest} exists. delete and continue [y/N]? ").strip().lower()
    except EOFError:
        _die("cancelled")
    if ans not in ("y", "yes"):
        _die("cancelled")
    _info(f"removing {dest}")
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    else:
        shutil.rmtree(dest)


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------

def fetch_ok(dest: Path, spec: str, depth: int) -> bool:
    _info(f"fetch --depth {depth} origin {spec}")
    proc = git(
        ["fetch", "--depth", str(depth), "origin", spec],
        cwd=dest,
        check=False,
    )
    return proc.returncode == 0


def default_branch_name(dest: Path) -> str | None:
    proc = git(
        ["ls-remote", "--symref", "origin", "HEAD"],
        cwd=dest,
        capture=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        # ref: refs/heads/master	HEAD
        if line.startswith("ref:") and line.endswith("HEAD"):
            ref = line.split("\t", 1)[0][len("ref:"):].strip()
            if ref.startswith("refs/heads/"):
                return ref[len("refs/heads/"):]
    return None


def clone_repo(
    url: str,
    ref: str | None,
    dest: Path,
    *,
    depth: int,
    jobs: int,
    recurse: bool,
    submodule_depth: int,
) -> None:
    dest.mkdir(parents=True)
    git(["init"], cwd=dest)
    git(["remote", "add", "origin", url], cwd=dest)

    kind = "head"
    name = ref
    sha: str | None = None

    if ref is None:
        branch = default_branch_name(dest)
        if branch:
            kind, name = "branch", branch
            _info(f"default branch {branch}")
        else:
            kind, name = "head", "HEAD"
    elif SHA40.match(ref):
        kind, sha = "sha", ref.lower()
    elif SHA_PREFIX.match(ref):
        kind, sha = "sha", expand_short_sha(url, ref)
    else:
        kind, name = "unknown", ref

    fetched = False
    if kind == "sha":
        assert sha is not None
        fetched = fetch_ok(dest, sha, depth)
        if not fetched:
            fetched = fetch_ok(dest, f"{sha}:refs/tags/clone-{sha[:12]}", depth)
    elif kind == "head":
        fetched = fetch_ok(dest, "HEAD", depth)
    elif kind == "branch":
        assert name is not None
        fetched = (
            fetch_ok(dest, f"+refs/heads/{name}:refs/heads/{name}", depth)
            or fetch_ok(dest, name, depth)
        )
    else:
        assert name is not None
        if fetch_ok(dest, f"refs/tags/{name}:refs/tags/{name}", depth):
            kind = "tag"
            fetched = True
        elif fetch_ok(dest, f"+refs/heads/{name}:refs/heads/{name}", depth):
            kind = "branch"
            fetched = True
        elif fetch_ok(dest, name, depth):
            kind = "branch"
            fetched = True

    if not fetched:
        _die(f"could not fetch {ref or 'HEAD'!r} from {url}")

    head = git_out(["rev-parse", "FETCH_HEAD"], cwd=dest).lower()
    if kind == "branch" and name:
        git(["checkout", "-B", name, "FETCH_HEAD"], cwd=dest)
    else:
        git(["checkout", "--detach", "FETCH_HEAD"], cwd=dest)

    if recurse and (dest / ".gitmodules").is_file():
        _info(
            f"submodule update --init --recursive --depth {submodule_depth} "
            f"--jobs {jobs}"
        )
        git(
            [
                "submodule", "update", "--init", "--recursive",
                "--depth", str(submodule_depth),
                "--jobs", str(jobs),
            ],
            cwd=dest,
        )
    elif recurse:
        _info("no .gitmodules; skipping submodules")

    describe = git(
        ["describe", "--tags", "--always"],
        cwd=dest,
        capture=True,
        check=False,
    ).stdout.strip() or head[:12]
    size = _human_size(_du_bytes(dest))
    _info(f"HEAD {head}")
    _info(f"describe {describe}")
    _info(f"size {size}")
    _info(f"cloned {dest}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="git-clone.py",
        description="Shallow clone a git repository and its submodules.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s ssh://git@gitlab.espressif.cn:27227/sdk/ESP8266_RTOS_SDK.git release/v3.4
  %(prog)s https://github.com/espressif/ESP8266_RTOS_SDK.git v3.4
  %(prog)s <url> <40-char-sha> -o my-sdk
  %(prog)s <url> --depth 20 release/v3.4
  %(prog)s <url> --no-recurse
""",
    )
    p.add_argument("url", help="Repository URL (ssh or https)")
    p.add_argument(
        "ref",
        nargs="?",
        default=None,
        help="Tag, branch, or commit (default: remote HEAD)",
    )
    p.add_argument(
        "-o", "--output",
        default=None,
        metavar="DIR",
        help="Directory name under cwd (default: repository name)",
    )
    p.add_argument(
        "--depth",
        type=int,
        default=1,
        metavar="N",
        help="Shallow history of the superproject (default: 1)",
    )
    p.add_argument(
        "--submodule-depth",
        type=int,
        default=1,
        metavar="N",
        help="Shallow history of submodules (default: 1)",
    )
    p.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        metavar="N",
        help=f"Parallel submodule fetches (default: {DEFAULT_JOBS})",
    )
    p.add_argument(
        "--no-recurse",
        action="store_true",
        help="Do not clone submodules",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.depth < 1:
        _die("--depth must be >= 1")
    if args.submodule_depth < 1:
        _die("--submodule-depth must be >= 1")
    if args.jobs < 1:
        _die("--jobs must be >= 1")

    dest = output_path(args.url, args.output)
    confirm_delete(dest)

    started = False
    ok = False
    try:
        started = True
        clone_repo(
            args.url,
            args.ref,
            dest,
            depth=args.depth,
            jobs=args.jobs,
            recurse=not args.no_recurse,
            submodule_depth=args.submodule_depth,
        )
        ok = True
    except RuntimeError as exc:
        _die(str(exc))
    finally:
        if started and not ok and dest.exists():
            _warn(f"removing incomplete {dest}")
            shutil.rmtree(dest, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _error("interrupted")
        raise SystemExit(130)
