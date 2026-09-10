#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
Fetch one remote branch or tag as cheaply as possible, then check it out.

Local refs that share the name are deleted (prompt unless -f). After that,
the remote side always wins: no merge, uncommitted changes are discarded.

Examples:
  git-pull.py                     # current branch, depth 1
  git-pull.py -b master           # prompt before deleting local master
  git-pull.py -f -b master        # delete local master, shallow fetch
  git-pull.py -b release/v3.4     # last 2 years
  git-pull.py -b release/v3.4 -t 6m
  git-pull.py --depth 20
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

RELEASE_PREFIX = "release/"
DEFAULT_RELEASE_SINCE = "2y"
SHORTHAND = re.compile(r"^(\d+)([ymd])$", re.IGNORECASE)
SINCE_UNITS = {"y": "year", "m": "month", "d": "day"}


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


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_PAGER"] = "cat"
    return env


def _fmt_git(args: list[str]) -> str:
    return "git " + " ".join(shlex.quote(a) for a in args)


def git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    capture: bool = False,
    check: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-c", "advice.detachedHead=false", *args]
    if not quiet:
        loc = f"  (in {cwd})" if cwd else ""
        print(f"  -> {_fmt_git(args)}{loc}", flush=True)

    kw: dict[str, Any] = {
        "cwd": str(cwd) if cwd is not None else None,
        "env": _git_env(),
        "check": False,
        "text": True,
        "encoding": "utf-8",
        "errors": "surrogateescape",
    }
    if capture:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.PIPE
    proc = subprocess.run(cmd, **kw)
    if check and proc.returncode != 0:
        err = ((proc.stderr or proc.stdout or "") if capture else "").strip()
        pretty = _fmt_git(args)
        if err:
            raise RuntimeError(f"{pretty}\n{err}")
        raise RuntimeError(f"{pretty} failed ({proc.returncode})")
    return proc


def git_out(args: list[str], *, cwd: Path | str | None = None) -> str:
    return git(args, cwd=cwd, capture=True, quiet=True).stdout.strip()


def git_ok(args: list[str], *, cwd: Path | str | None = None) -> bool:
    return git(args, cwd=cwd, capture=True, check=False, quiet=True).returncode == 0


# ---------------------------------------------------------------------------
# Repo / remote / refs
# ---------------------------------------------------------------------------

def find_repo_root() -> Path | None:
    proc = git(["rev-parse", "--show-toplevel"], capture=True,
               check=False, quiet=True)
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    return Path(text) if text else None


def _git_dir(repo: Path) -> Path:
    raw = git_out(["rev-parse", "--git-dir"], cwd=repo)
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def find_remote(repo: Path) -> str:
    remotes = git_out(["remote"], cwd=repo).split()
    if not remotes:
        _die("no git remote configured")
    if "origin" in remotes:
        return "origin"
    return remotes[0]


def current_branch(repo: Path) -> str:
    name = git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    if not name or name == "HEAD":
        _die("detached HEAD; pass -b <branch-or-tag>")
    return name


def local_branch_exists(repo: Path, name: str) -> bool:
    return git_ok(["show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
                  cwd=repo)


def local_tag_exists(repo: Path, name: str) -> bool:
    return git_ok(["show-ref", "--verify", "--quiet", f"refs/tags/{name}"],
                  cwd=repo)


def is_shallow_repo(repo: Path) -> bool:
    return git_out(["rev-parse", "--is-shallow-repository"], cwd=repo) == "true"


def local_branch_names(repo: Path) -> list[str]:
    text = git_out(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=repo,
    )
    return [line for line in text.splitlines() if line]


def other_local_branches(repo: Path, target: str | None) -> list[str]:
    return [name for name in local_branch_names(repo) if name != target]


def ls_remote_exact(repo: Path, remote: str, ref: str) -> bool:
    proc = git(["ls-remote", remote, ref], cwd=repo, capture=True,
               check=False, quiet=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        pretty = _fmt_git(["ls-remote", remote, ref])
        if err:
            raise RuntimeError(f"{pretty}\n{err}")
        raise RuntimeError(f"{pretty} failed ({proc.returncode})")
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        _sha, name = line.split("\t", 1)
        if name == ref:
            return True
    return False


def classify_ref(repo: Path, remote: str, name: str, *, as_branch: bool) -> str:
    """Return 'branch' or 'tag'."""
    _info(f"looking up {name} on {remote}")
    if as_branch:
        if ls_remote_exact(repo, remote, f"refs/heads/{name}"):
            return "branch"
        _die(f"remote {remote} has no branch {name!r}")

    has_branch = ls_remote_exact(repo, remote, f"refs/heads/{name}")
    has_tag = ls_remote_exact(repo, remote, f"refs/tags/{name}")
    if has_branch and has_tag:
        _warn(f"{name!r} is both a branch and a tag on {remote}; using branch")
        return "branch"
    if has_branch:
        return "branch"
    if has_tag:
        return "tag"
    _die(f"remote {remote} has no branch or tag {name!r}")


# ---------------------------------------------------------------------------
# History window
# ---------------------------------------------------------------------------

def parse_since(raw: str) -> str:
    text = raw.strip()
    if not text:
        _die("-t/--since requires a duration or date")
    match = SHORTHAND.fullmatch(text)
    if not match:
        return text
    count = int(match.group(1))
    if count < 1:
        _die(f"invalid duration {raw!r}; count must be >= 1")
    unit = SINCE_UNITS[match.group(2).lower()]
    if count != 1:
        unit += "s"
    return f"{count} {unit} ago"


def since_to_epoch(since: str) -> int | None:
    proc = subprocess.run(
        ["date", "-d", since, "+%s"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def oldest_reachable_epoch(repo: Path) -> int | None:
    """Committer time of the oldest root (or shallow-boundary) commit."""
    proc = git(
        ["log", "--all", "--max-parents=0", "--format=%ct"],
        cwd=repo, capture=True, check=False, quiet=True,
    )
    epochs: list[int] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            epochs.append(int(line))
    return min(epochs) if epochs else None


def would_shorten_shallow(repo: Path, since: str) -> bool:
    """True if --shallow-since would drop commits we already have."""
    oldest = oldest_reachable_epoch(repo)
    cutoff = since_to_epoch(since)
    if oldest is None or cutoff is None:
        return False
    return cutoff > oldest + 86400


def resolve_history(
    name: str,
    depth: int | None,
    since_raw: str | None,
) -> tuple[int | None, str | None, bool]:
    if depth is not None and since_raw is not None:
        _die("--depth and -t/--since are mutually exclusive")
    if depth is not None:
        if depth < 1:
            _die("--depth must be >= 1")
        return depth, None, False
    if since_raw is not None:
        return None, parse_since(since_raw), True
    if name.startswith(RELEASE_PREFIX):
        return None, parse_since(DEFAULT_RELEASE_SINCE), False
    return 1, None, False


def _fmt_branch_list(names: list[str], *, limit: int = 4) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    shown = ", ".join(names[:limit])
    return f"{shown}, ... ({len(names)} total)"


def fetch_limit_args(
    repo: Path,
    *,
    kind: str,
    name: str,
    depth: int | None,
    since: str | None,
    since_explicit: bool = False,
) -> list[str]:
    """Return --depth/--shallow-since/--update-shallow flags.

    Git stores shallow boundaries in .git/shallow for the whole repository.
    An explicit -t/--since is always honored (with a warning). Implicit
    defaults will not shorten other local branches or convert a full clone
    that still has other branches.
    """
    keep = name if kind == "branch" else None
    others = other_local_branches(repo, keep)
    shallow = is_shallow_repo(repo)

    if since is not None:
        flags = [f"--shallow-since={since}", "--update-shallow"]
        if since_explicit:
            if others:
                _warn(
                    "git shallow history is repository-wide; "
                    f"--shallow-since={since} applies to all local branches "
                    f"({_fmt_branch_list(others)})"
                )
            return flags
        if not others:
            return flags
        if not shallow:
            _warn(
                "not using --shallow-since; converting a full clone to shallow "
                f"would affect other local branches ({_fmt_branch_list(others)})"
            )
            return []
        if would_shorten_shallow(repo, since):
            _warn(
                f"not using --shallow-since={since}; it would shorten other "
                f"local branches ({_fmt_branch_list(others)}). git shallow "
                "history is repository-wide"
            )
            return []
        _info(f"deepening shallow history to {since} (repository-wide)")
        return flags

    assert depth is not None
    if not shallow and others:
        _warn(
            "not using --depth; converting a full clone to shallow would "
            f"affect other local branches ({_fmt_branch_list(others)})"
        )
        return []
    return ["--depth", str(depth), "--update-shallow"]


# ---------------------------------------------------------------------------
# Local overwrite
# ---------------------------------------------------------------------------

def confirm_delete(kind: str, name: str, *, force: bool) -> None:
    if force:
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        _die(f"local {kind} {name!r} exists (not a TTY; pass -f to delete)")
    try:
        ans = input(
            f"local {kind} {name!r} exists. delete it and continue [y/N]? "
        ).strip().lower()
    except EOFError:
        _die("cancelled")
    if ans not in ("y", "yes"):
        _die("cancelled")


def abort_in_progress(repo: Path) -> None:
    gd = _git_dir(repo)
    if (gd / "rebase-merge").exists():
        _info("aborting rebase")
        git(["rebase", "--abort"], cwd=repo, check=False)
    elif (gd / "rebase-apply").exists():
        _info("aborting rebase/am")
        if git(["rebase", "--abort"], cwd=repo, check=False).returncode != 0:
            git(["am", "--abort"], cwd=repo, check=False)
    if (gd / "MERGE_HEAD").exists():
        _info("aborting merge")
        git(["merge", "--abort"], cwd=repo, check=False)
    if (gd / "CHERRY_PICK_HEAD").exists():
        _info("aborting cherry-pick")
        git(["cherry-pick", "--abort"], cwd=repo, check=False)
    if (gd / "REVERT_HEAD").exists():
        _info("aborting revert")
        git(["revert", "--abort"], cwd=repo, check=False)
    if (gd / "BISECT_LOG").exists():
        _info("resetting bisect")
        git(["bisect", "reset"], cwd=repo, check=False)


def discard_local(repo: Path) -> None:
    abort_in_progress(repo)
    git(["reset", "--hard"], cwd=repo)


def delete_local_branch(repo: Path, name: str) -> None:
    current = git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    if current == name:
        git(["checkout", "--detach"], cwd=repo)
    if local_branch_exists(repo, name):
        _info(f"deleting local branch {name}")
        git(["branch", "-D", name], cwd=repo)


def delete_local_tag(repo: Path, name: str) -> None:
    if local_tag_exists(repo, name):
        _info(f"deleting local tag {name}")
        git(["tag", "-d", name], cwd=repo)


def checkout_forced(repo: Path, args: list[str]) -> None:
    proc = git(["checkout", *args], cwd=repo, check=False)
    if proc.returncode != 0:
        git(["checkout", "-f", *args], cwd=repo)


# ---------------------------------------------------------------------------
# Fetch / checkout
# ---------------------------------------------------------------------------

def fetch_ref(
    repo: Path,
    remote: str,
    kind: str,
    name: str,
    *,
    depth: int | None,
    since: str | None,
    since_explicit: bool = False,
) -> None:
    if kind == "branch":
        spec = f"+refs/heads/{name}:refs/remotes/{remote}/{name}"
    else:
        spec = f"+refs/tags/{name}:refs/tags/{name}"
    limit = fetch_limit_args(
        repo, kind=kind, name=name, depth=depth, since=since,
        since_explicit=since_explicit,
    )
    if "--depth" in limit:
        window = f"depth {depth}"
    elif any(a.startswith("--shallow-since=") for a in limit):
        window = f"since {since}"
    else:
        window = "single ref, full negotiation"
    _info(f"fetch {kind} {name} from {remote} ({window})")
    cmd = [
        "fetch",
        *limit,
        "--no-tags",
        "--no-recurse-submodules",
        "--no-auto-maintenance",
        remote,
        spec,
    ]
    git(cmd, cwd=repo)


def checkout_ref(repo: Path, remote: str, kind: str, name: str) -> None:
    if kind == "branch":
        checkout_forced(repo, ["-B", name, "FETCH_HEAD"])
        proc = git(
            ["branch", "--set-upstream-to", f"{remote}/{name}", name],
            cwd=repo,
            check=False,
        )
        if proc.returncode != 0:
            _warn(f"could not set upstream to {remote}/{name}")
    else:
        checkout_forced(repo, ["--detach", "FETCH_HEAD"])


def show_head(repo: Path) -> None:
    head = git_out(["rev-parse", "HEAD"], cwd=repo)
    describe = git(
        ["describe", "--tags", "--always"],
        cwd=repo,
        capture=True,
        check=False,
        quiet=True,
    ).stdout.strip() or head[:12]
    _info(f"HEAD {head}")
    _info(f"describe {describe}")
    git(["log", "-n", "1", "--oneline"], cwd=repo)


def pull_ref(
    repo: Path,
    *,
    requested: str | None,
    force: bool,
    depth: int | None,
    since_raw: str | None,
) -> None:
    remote = find_remote(repo)
    as_branch = requested is None
    name = current_branch(repo) if requested is None else requested
    kind = classify_ref(repo, remote, name, as_branch=as_branch)
    depth, since, since_explicit = resolve_history(name, depth, since_raw)

    if kind == "branch" and local_branch_exists(repo, name):
        confirm_delete("branch", name, force=force)
    elif kind == "tag" and local_tag_exists(repo, name):
        confirm_delete("tag", name, force=force)

    # Branches: fetch first so a failed fetch leaves the local branch intact.
    # Tags: drop the local tag first so the force refspec writes the remote one.
    if kind == "tag":
        discard_local(repo)
        delete_local_tag(repo, name)
        fetch_ref(
            repo, remote, kind, name,
            depth=depth, since=since, since_explicit=since_explicit,
        )
    else:
        fetch_ref(
            repo, remote, kind, name,
            depth=depth, since=since, since_explicit=since_explicit,
        )
        discard_local(repo)
        delete_local_branch(repo, name)

    checkout_ref(repo, remote, kind, name)
    show_head(repo)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="git-pull.py",
        description=(
            "Fetch one remote branch or tag as cheaply as possible, "
            "then check it out. Local same-named refs are deleted "
            "(prompt unless -f); the remote side always wins."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s                     # current branch, depth 1
  %(prog)s -b master           # prompt before deleting local master
  %(prog)s -f -b master        # delete local master, then shallow fetch
  %(prog)s -b release/v3.4     # last 2 years
  %(prog)s -b release/v3.4 -t 6m
  %(prog)s -t 1y               # current branch, last year
  %(prog)s --depth 20          # 20 commits (--depth and -t are exclusive)

-t/--since accepts 2y, 6m, 30d, or any git date (2 years ago, 2024-01-01).
Explicit -t is always applied (git shallow history is repository-wide).
Implicit --depth / release/ window will not convert a full clone that
still has other local branches.
""",
    )
    p.add_argument(
        "-b",
        metavar="REF",
        dest="ref",
        default=None,
        help="branch or tag to fetch (default: current branch)",
    )
    p.add_argument(
        "-f", "--force",
        action="store_true",
        help="delete a same-named local branch/tag without prompting",
    )
    hist = p.add_mutually_exclusive_group()
    hist.add_argument(
        "-t", "--since",
        metavar="DURATION",
        default=None,
        help="shallow-since window (e.g. 2y, 6m, 30d, or 2024-01-01)",
    )
    hist.add_argument(
        "--depth",
        type=int,
        default=None,
        metavar="N",
        help="shallow history of N commits (mutually exclusive with -t)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = find_repo_root()
    if root is None:
        _die("not a git repository (or any of the parent directories)")
    try:
        pull_ref(
            root,
            requested=args.ref,
            force=args.force,
            depth=args.depth,
            since_raw=args.since,
        )
    except RuntimeError as exc:
        _die(str(exc))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _error("interrupted")
        raise SystemExit(130)
