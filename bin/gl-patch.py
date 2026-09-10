#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
Download a git patch from GitLab (MR URL, commit URL, or SHA).

Examples:
  gl-patch.py https://gitlab.espressif.cn:6688/espressif/esp-idf/-/merge_requests/50758
  gl-patch.py https://gitlab.espressif.cn:6688/espressif/esp-idf/-/commit/296b6eab
  gl-patch.py -o patches/ 296b6eab
  gl-patch.py --combine -o mqtt.patch <MR-URL>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

DEFAULT_GITLAB = "https://gitlab.espressif.cn:6688"
DEFAULT_PROJECT = "espressif/esp-idf"
DEFAULT_TOKEN_FILES = (
    "~/.esptk/gitlab_oauth_token",
    "~/.gitlab_oauth_token",
)
USER_AGENT = "gl-patch.py"
SLUG_MAX = 80
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
SHA40 = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

# Internal GitLab often uses a cert curl treated with --insecure.
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


def _interrupted() -> NoReturn:
    if sys.stderr.isatty():
        sys.stderr.write("\n")
        sys.stderr.flush()
    _die("Interrupted.", code=130)


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def load_token_optional(token_file: str | None) -> tuple[str | None, str]:
    """Return (token or None, source_description)."""
    env = os.environ.get("GITLAB_TOKEN", "").strip()
    if env:
        return env, "env:GITLAB_TOKEN"

    candidates = ([token_file] if token_file else []) + list(DEFAULT_TOKEN_FILES)
    for raw in candidates:
        path = os.path.expanduser(raw)
        try:
            text = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as e:
            _die(f"Cannot read token file {path}: {e}")
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line, path
        _die(f"Token file {path} has no non-empty token line.")
    return None, "none"


def _need_token_hint(host: str) -> str:
    return (
        "GitLab authentication failed (HTTP 401).\n"
        "  Token is missing, expired, revoked, or malformed.\n"
        f"  Create a PAT: {host}/-/user_settings/personal_access_tokens\n"
        "  Scopes: api, read_api.\n"
        f"  Save to {DEFAULT_TOKEN_FILES[0]} or set GITLAB_TOKEN."
    )


def format_gitlab_api_error(
    status: int, body: str, url: str, host: str
) -> str:
    text = body.strip()
    message = ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("error") or "")
            if not message and "error_description" in data:
                message = str(data["error_description"])
    except json.JSONDecodeError:
        message = text[:500]
    detail = message or text[:300]

    if status == 401:
        return _need_token_hint(host) + f"\n  Detail: {detail}"
    if status == 403:
        return f"GitLab API forbidden (HTTP 403) for {url}\n  Detail: {detail}"
    if status == 404:
        return f"GitLab resource not found (HTTP 404) for {url}\n  Detail: {detail}"
    return f"GitLab API HTTP {status} for {url}: {message or text[:500]}"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def gitlab_request(
    url: str,
    token: str | None,
    host: str,
    *,
    die_on_error: bool = True,
    timeout: int = 120,
) -> tuple[int, bytes, dict[str, str]]:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    req = Request(url, method="GET", headers=headers)
    try:
        with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            body = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.getcode() or 200, body, hdrs
    except HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        if die_on_error:
            _die(format_gitlab_api_error(e.code, err, url, host))
        return e.code, err.encode("utf-8"), {}
    except URLError as e:
        if die_on_error:
            _die(f"GitLab request failed: {e}")
        return 0, str(e).encode("utf-8"), {}
    except TimeoutError:
        if die_on_error:
            _die(f"GitLab request timed out: {url}")
        return 0, b"timeout", {}


def gitlab_json(url: str, token: str | None, host: str) -> Any:
    _, body, _ = gitlab_request(url, token, host)
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        preview = body[:300].decode("utf-8", errors="replace")
        _die(f"Invalid JSON from {url}: {e}\n  Preview: {preview}")


def gitlab_json_pages(
    url: str, token: str | None, host: str
) -> list[Any]:
    """GET a paginated GitLab list endpoint."""
    items: list[Any] = []
    page = 1
    sep = "&" if "?" in url else "?"
    while True:
        page_url = f"{url}{sep}per_page=100&page={page}"
        data = gitlab_json(page_url, token, host)
        if not isinstance(data, list):
            _die(f"Expected a JSON list from {page_url}")
        items.extend(data)
        if len(data) < 100:
            break
        page += 1
    return items


# ---------------------------------------------------------------------------
# Source parsing
# ---------------------------------------------------------------------------

@dataclass
class Source:
    kind: str  # "mr" | "commit" | "sha"
    host: str
    project: str
    iid: int | None = None
    sha: str | None = None


def _normalize_host(scheme: str, netloc: str) -> str:
    return f"{scheme}://{netloc}".rstrip("/")


def _strip_git_suffix(path: str) -> str:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path


def project_from_remote_url(url: str) -> str | None:
    """Return 'group/project' without .git, or None."""
    u = url.strip()
    if not u:
        return None
    if u.startswith("git@"):
        try:
            path = u.split(":", 1)[1]
        except IndexError:
            return None
    else:
        path = urlsplit(u).path
    path = _strip_git_suffix(path)
    segs = [p for p in path.split("/") if p]
    if len(segs) < 2:
        return None
    return "/".join(segs)


def https_host_from_remote_url(url: str) -> str | None:
    """HTTPS GitLab host from an origin URL, or None."""
    u = url.strip()
    parts = urlsplit(u)
    if parts.scheme in ("http", "https") and "gitlab" in (parts.netloc or "").lower():
        return _normalize_host(parts.scheme, parts.netloc)
    return None


def origin_remote_url() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    return url or None


def resolve_host_project(
    *,
    host: str | None,
    project: str | None,
    project_flag: str | None,
) -> tuple[str, str]:
    """Fill host/project for a bare SHA (or incomplete source)."""
    origin = origin_remote_url()
    origin_project = None
    origin_host = None
    if origin and "gitlab" in origin.lower():
        origin_project = project_from_remote_url(origin)
        origin_host = https_host_from_remote_url(origin)

    resolved_project = (
        (project_flag.strip().strip("/") if project_flag else None)
        or project
        or origin_project
        or DEFAULT_PROJECT
    )
    if resolved_project.endswith(".git"):
        resolved_project = resolved_project[:-4]
    resolved_host = host or origin_host or DEFAULT_GITLAB
    return resolved_host.rstrip("/"), resolved_project


def parse_source(raw: str, project_flag: str | None) -> Source:
    text = raw.strip()
    if SHA_RE.fullmatch(text):
        host, project = resolve_host_project(
            host=None, project=None, project_flag=project_flag
        )
        return Source(kind="sha", host=host, project=project, sha=text.lower())

    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        _die(
            f"Unrecognized source {raw!r}. Pass a GitLab MR URL, "
            "commit URL, or 7-40 character SHA."
        )

    host = _normalize_host(parts.scheme, parts.netloc)
    path = parts.path.strip("/")
    marker = "/-/"
    idx = path.find(marker)
    if idx < 0:
        _die(f"Not a GitLab MR or commit URL: {raw}")
    project = path[:idx]
    rest = path[idx + len(marker):]
    if not project:
        _die(f"Missing project path in URL: {raw}")

    mr = re.match(
        r"merge_requests/(\d+)(?:\.(?:patch|diff)|/.*)?$",
        rest,
        re.IGNORECASE,
    )
    if mr:
        if project_flag:
            _warn(f"--project ignored; using {project} from URL")
        return Source(
            kind="mr", host=host, project=project, iid=int(mr.group(1))
        )

    commit = re.match(
        r"commits?/([0-9a-f]{7,40})(?:\.(?:patch|diff)|/.*)?$",
        rest,
        re.IGNORECASE,
    )
    if commit:
        if project_flag:
            _warn(f"--project ignored; using {project} from URL")
        return Source(
            kind="commit",
            host=host,
            project=project,
            sha=commit.group(1).lower(),
        )

    _die(f"Not a GitLab MR or commit URL: {raw}")


def api_root(src: Source) -> str:
    return f"{src.host}/api/v4/projects/{quote(src.project, safe='')}"


# ---------------------------------------------------------------------------
# Naming / output
# ---------------------------------------------------------------------------

def slugify(title: str) -> str:
    first = ""
    for line in title.splitlines():
        first = line.strip()
        if first:
            break
    if not first:
        first = "patch"
    s = first.lower()
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = "patch"
    if len(s) > SLUG_MAX:
        s = s[:SLUG_MAX].rstrip("_")
    return s


def default_filename(title: str, sha: str) -> str:
    return f"{slugify(title)}_{sha[:8].lower()}.patch"


def looks_like_patch(body: bytes) -> bool:
    text = body[:4000].decode("utf-8", errors="replace")
    lowered = text.lstrip().lower()
    if lowered.startswith("<!doctype") or lowered.startswith("<html"):
        return False
    return "diff --git" in text or text.startswith("From ")


def _is_output_dir(path: Path, raw: str) -> bool:
    if raw.endswith("/") or raw.endswith(os.sep):
        return True
    return path.is_dir()


def resolve_single_path(out: str | None, filename: str) -> Path:
    if not out:
        return Path.cwd() / filename
    raw = out
    path = Path(raw).expanduser()
    if _is_output_dir(path, raw):
        return path / filename
    return path


def resolve_split_dir(out: str | None) -> Path:
    if not out:
        return Path.cwd()
    raw = out
    path = Path(raw).expanduser()
    if _is_output_dir(path, raw):
        return path
    _die(
        f"--split requires -o to be a directory, not a file path ({out}). "
        "Example: -o patches/"
    )


def write_patch(path: Path, body: bytes, force: bool) -> None:
    if path.exists() and not force:
        _die(f"Refusing to overwrite {path} (pass -f/--force)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    _info(f"wrote {path}")


# ---------------------------------------------------------------------------
# GitLab data
# ---------------------------------------------------------------------------

def fetch_commit(src: Source, token: str | None, sha: str) -> dict[str, Any]:
    url = f"{api_root(src)}/repository/commits/{quote(sha, safe='')}"
    data = gitlab_json(url, token, src.host)
    if not isinstance(data, dict):
        _die(f"Unexpected commit payload from {url}")
    full = str(data.get("id") or "").strip().lower()
    if not SHA40.match(full):
        _die(f"Could not expand SHA {sha!r} in {src.project}")
    if not full.startswith(sha.lower()):
        _die(f"GitLab returned {full} which does not match prefix {sha}")
    return data


def fetch_mr(src: Source, token: str | None) -> dict[str, Any]:
    url = f"{api_root(src)}/merge_requests/{src.iid}"
    data = gitlab_json(url, token, src.host)
    if not isinstance(data, dict):
        _die(f"Unexpected merge request payload from {url}")
    return data


def fetch_mr_commits(src: Source, token: str | None) -> list[dict[str, Any]]:
    url = f"{api_root(src)}/merge_requests/{src.iid}/commits"
    items = gitlab_json_pages(url, token, src.host)
    commits = [c for c in items if isinstance(c, dict)]
    if not commits:
        _die(f"MR !{src.iid} has no commits")
    # Oldest first so a combined mailbox applies in order.
    commits.sort(
        key=lambda c: str(c.get("authored_date") or c.get("created_at") or "")
    )
    return commits


def fetch_commit_diffs(
    src: Source, token: str | None, sha: str
) -> list[dict[str, Any]]:
    # Web /-/commit/<sha>.patch redirects to sign-in on internal GitLab
    # even with PRIVATE-TOKEN. Build a mailbox patch from this API instead.
    url = (
        f"{api_root(src)}/repository/commits/{quote(sha, safe='')}"
        f"/diff?unidiff=true"
    )
    items = gitlab_json_pages(url, token, src.host)
    return [x for x in items if isinstance(x, dict)]


def _format_git_date(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return format_datetime(datetime.now().astimezone())
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return text
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return format_datetime(dt)


def _format_file_diff(item: dict[str, Any]) -> str:
    old_path = str(item.get("old_path") or item.get("new_path") or "unknown")
    new_path = str(item.get("new_path") or item.get("old_path") or "unknown")
    diff = (item.get("diff") or "").replace("\r\n", "\n")
    if diff and not diff.endswith("\n"):
        diff += "\n"

    if item.get("too_large"):
        _warn(f"diff too large, omitted by GitLab: {new_path}")
        return ""
    if item.get("collapsed") and not diff:
        _warn(f"diff collapsed, omitted by GitLab: {new_path}")
        return ""
    if not diff:
        if item.get("renamed_file") and old_path != new_path:
            return (
                f"diff --git a/{old_path} b/{new_path}\n"
                f"rename from {old_path}\n"
                f"rename to {new_path}\n"
            )
        _warn(f"empty diff for {new_path}")
        return ""

    if diff.startswith("diff --git "):
        return diff

    headers: list[str] = []
    if item.get("new_file"):
        headers.append(f"diff --git a/{new_path} b/{new_path}")
        headers.append(f"new file mode {item.get('b_mode') or '100644'}")
    elif item.get("deleted_file"):
        headers.append(f"diff --git a/{old_path} b/{old_path}")
        headers.append(f"deleted file mode {item.get('a_mode') or '100644'}")
    elif item.get("renamed_file") and old_path != new_path:
        headers.append(f"diff --git a/{old_path} b/{new_path}")
        headers.append(f"rename from {old_path}")
        headers.append(f"rename to {new_path}")
    else:
        headers.append(f"diff --git a/{old_path} b/{new_path}")
        a_mode = item.get("a_mode")
        b_mode = item.get("b_mode")
        if a_mode and b_mode and a_mode != b_mode:
            headers.append(f"old mode {a_mode}")
            headers.append(f"new mode {b_mode}")
    return "\n".join(headers) + "\n" + diff


def format_mailbox_patch(
    commit: dict[str, Any], diffs: list[dict[str, Any]]
) -> bytes:
    sha = _commit_sha(commit)
    title = _commit_title(commit).strip().splitlines()[0]
    author = str(commit.get("author_name") or "unknown")
    email = str(commit.get("author_email") or "unknown")
    date = _format_git_date(
        str(commit.get("authored_date") or commit.get("created_at") or "")
    )
    message = str(commit.get("message") or title)
    parts = message.split("\n", 1)
    body = parts[1].strip("\n") if len(parts) > 1 else ""

    file_diffs = [_format_file_diff(item) for item in diffs]
    file_diffs = [chunk for chunk in file_diffs if chunk]
    if not file_diffs:
        _die(f"No diffs for commit {sha[:8]}")

    lines = [
        f"From {sha} Mon Sep 17 00:00:00 2001",
        f"From: {author} <{email}>",
        f"Date: {date}",
        f"Subject: [PATCH] {title}",
        "",
    ]
    if body:
        lines.append(body)
        lines.append("")
    lines.append("---")
    lines.append("")
    text = "\n".join(lines) + "\n" + "".join(file_diffs) + "-- \ngl-patch.py\n"
    return text.encode("utf-8")


def patch_for_commit(
    src: Source, token: str | None, commit: dict[str, Any]
) -> bytes:
    sha = _commit_sha(commit)
    _info(f"fetching diffs for {sha[:8]}")
    diffs = fetch_commit_diffs(src, token, sha)
    body = format_mailbox_patch(commit, diffs)
    if not looks_like_patch(body):
        _die(f"Assembled patch for {sha[:8]} does not look like a git patch")
    return body


def patch_for_mr_combined(
    src: Source, token: str | None, commits: list[dict[str, Any]]
) -> bytes:
    chunks = [patch_for_commit(src, token, commit) for commit in commits]
    return b"".join(chunks)


def _commit_title(commit: dict[str, Any]) -> str:
    return str(commit.get("title") or commit.get("message") or "patch")


def _commit_sha(commit: dict[str, Any]) -> str:
    sha = str(commit.get("id") or commit.get("short_id") or "").strip().lower()
    if not sha:
        _die("Commit payload has no id")
    return sha


# ---------------------------------------------------------------------------
# Combine / split
# ---------------------------------------------------------------------------

def prompt_combine_or_split(commits: list[dict[str, Any]]) -> str:
    print()
    for i, commit in enumerate(commits, 1):
        sha = _commit_sha(commit)[:8]
        title = _commit_title(commit).strip().splitlines()[0]
        print(f"[{i}] {sha}  {title}")
    print()
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        _die(
            f"MR has {len(commits)} commits; pass --combine or --split "
            "(non-interactive)."
        )
    while True:
        try:
            raw = input(
                "Combine into one patch, or one patch per commit? [c/s] "
            ).strip().lower()
        except EOFError:
            _die(
                f"MR has {len(commits)} commits; pass --combine or --split."
            )
        except KeyboardInterrupt:
            _interrupted()
        if raw in ("c", "combine"):
            return "combine"
        if raw in ("s", "split"):
            return "split"
        print("Please enter c (combine) or s (split).")


def choose_mode(
    n_commits: int,
    combine: bool,
    split: bool,
    commits: list[dict[str, Any]],
) -> str:
    if n_commits <= 1:
        return "combine"
    if combine:
        return "combine"
    if split:
        return "split"
    return prompt_combine_or_split(commits)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def run_sha_or_commit(
    src: Source,
    token: str | None,
    args: argparse.Namespace,
) -> int:
    assert src.sha
    if args.combine or args.split:
        _warn("--combine/--split ignored for a single commit")
    commit = fetch_commit(src, token, src.sha)
    sha = _commit_sha(commit)
    title = _commit_title(commit)
    _info(f"resolved {src.sha} -> {sha} ({src.project})")
    body = patch_for_commit(src, token, commit)
    path = resolve_single_path(args.output, default_filename(title, sha))
    write_patch(path, body, args.force)
    return 0


def run_mr(
    src: Source,
    token: str | None,
    args: argparse.Namespace,
) -> int:
    mr = fetch_mr(src, token)
    commits = fetch_mr_commits(src, token)
    mr_title = str(mr.get("title") or "merge_request")
    head = str(mr.get("sha") or "").strip().lower()
    if not head:
        head = _commit_sha(commits[-1])
    _info(
        f"MR !{src.iid} {src.project}: {len(commits)} commit(s), "
        f"HEAD {head[:8]}"
    )

    mode = choose_mode(len(commits), args.combine, args.split, commits)
    if mode == "combine":
        title = mr_title if len(commits) > 1 else _commit_title(commits[0])
        sha = head if len(commits) > 1 else _commit_sha(commits[0])
        body = patch_for_mr_combined(src, token, commits)
        path = resolve_single_path(args.output, default_filename(title, sha))
        write_patch(path, body, args.force)
        return 0

    dest = resolve_split_dir(args.output)
    for commit in commits:
        sha = _commit_sha(commit)
        title = _commit_title(commit)
        body = patch_for_commit(src, token, commit)
        path = dest / default_filename(title, sha)
        write_patch(path, body, args.force)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gl-patch.py",
        description=(
            "Download a git patch from GitLab: merge request URL, "
            "commit URL, or SHA."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
token (optional; tried in order):
  env GITLAB_TOKEN
  ~/.esptk/gitlab_oauth_token
  ~/.gitlab_oauth_token

  Requests are sent anonymously when no token is found. A 401 then
  prints how to create a PAT.

examples:
  %(prog)s https://gitlab.espressif.cn:6688/espressif/esp-idf/-/merge_requests/50758
  %(prog)s https://gitlab.espressif.cn:6688/espressif/esp-idf/-/commit/296b6eab
  %(prog)s -o patches/ 296b6eab
  %(prog)s --combine -o mqtt.patch <MR-URL>
  %(prog)s --split -o patches/ <MR-URL>
""",
    )
    p.add_argument(
        "source",
        help="GitLab MR URL, commit URL, or 7-40 character SHA",
    )
    p.add_argument(
        "-o", "--output",
        metavar="PATH",
        help=(
            "Output file, or directory if PATH exists / ends with '/'. "
            "Default: ./<title>_<sha8>.patch"
        ),
    )
    p.add_argument(
        "-f", "--force",
        action="store_true",
        help="Overwrite an existing patch file",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--combine",
        action="store_true",
        help="One patch for the whole MR (skip the prompt)",
    )
    mode.add_argument(
        "--split",
        action="store_true",
        help="One patch per MR commit (skip the prompt)",
    )
    p.add_argument(
        "--project",
        metavar="ORG/REPO",
        help=(
            "GitLab project for a bare SHA "
            f"(default: origin remote or {DEFAULT_PROJECT})"
        ),
    )
    p.add_argument(
        "-t", "--token-file",
        metavar="PATH",
        help="GitLab token file (overrides default search paths)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    src = parse_source(args.source, args.project)
    token, token_source = load_token_optional(args.token_file)
    if token:
        _info(f"using GitLab token from {token_source}")
    else:
        _info("no GitLab token; trying anonymous request")

    _info(f"{src.kind} {src.host}/{src.project}")
    if src.kind == "mr":
        return run_mr(src, token, args)
    return run_sha_or_commit(src, token, args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _interrupted()
