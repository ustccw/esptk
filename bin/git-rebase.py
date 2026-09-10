#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
Squash the latest N commits into one, keeping the oldest commit's message,
author, and author date. The committer date is now.

Examples:
  git-rebase.py              # squash latest 2 commits
  git-rebase.py -c 5         # squash latest 5 commits, do not push
  git-rebase.py -p           # squash latest 2 commits and force-push
  git-rebase.py -c 3 -p      # squash latest 3 commits and force-push
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

DEFAULT_COUNT = 2


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


def current_branch(repo: Path) -> str:
    name = git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    if not name or name == "HEAD":
        _die("detached HEAD; cannot force-push")
    return name


def index_has_staged_changes(repo: Path) -> bool:
    """True if the index differs from HEAD (would mix into a soft-reset commit)."""
    return not git_ok(["diff", "--cached", "--quiet"], cwd=repo)


def list_remotes(repo: Path) -> list[str]:
    text = git_out(["remote"], cwd=repo)
    return [name for name in text.split() if name]


def remote_url(repo: Path, name: str) -> str:
    return git_out(["remote", "get-url", name], cwd=repo)


def select_remote(repo: Path) -> str:
    remotes = list_remotes(repo)
    if not remotes:
        _die("no git remote configured")
    if len(remotes) == 1:
        return remotes[0]

    print("available remotes:", flush=True)
    for i, name in enumerate(remotes, 1):
        print(f"  {i}) {name}  ({remote_url(repo, name)})", flush=True)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        _die("multiple remotes; run in a TTY to choose one")

    prompt = f"select remote [1-{len(remotes)}]: "
    try:
        raw = input(prompt).strip()
    except EOFError:
        _die("cancelled")
    if not raw.isdigit():
        _die("cancelled")
    idx = int(raw)
    if idx < 1 or idx > len(remotes):
        _die(f"invalid selection {idx}")
    return remotes[idx - 1]


# ---------------------------------------------------------------------------
# Squash / push
# ---------------------------------------------------------------------------

def squash_commits(repo: Path, count: int) -> None:
    if index_has_staged_changes(repo):
        _die("index has staged changes; unstage them first")
    if not git_ok(["rev-parse", "--verify", f"HEAD~{count}"], cwd=repo):
        _die(f"HEAD~{count} not found; history is too short")

    first_sha = git_out(["rev-parse", f"HEAD~{count - 1}"], cwd=repo)
    subject = git_out(["log", "-1", "--format=%s", first_sha], cwd=repo)
    _info(f"squash last {count} commits into one ({subject})")
    git(["reset", "--soft", f"HEAD~{count}"], cwd=repo)
    git(["commit", "-C", first_sha], cwd=repo)
    git(["log", "-1", "--format=fuller"], cwd=repo)


def force_push(repo: Path) -> None:
    branch = current_branch(repo)
    remote = select_remote(repo)
    _info(f"force-push {branch} to {remote}")
    git(["push", "--force", remote, branch], cwd=repo)
    _info(f"pushed {branch} to {remote}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="git-rebase.py",
        description=(
            "Squash the latest N commits into one. The new commit reuses "
            "the oldest commit's message, author, and author date; the "
            "committer date is now. Optionally force-push the result."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s              # squash latest 2 commits
  %(prog)s -c 5         # squash latest 5 commits, do not push
  %(prog)s -p           # squash latest 2 commits and force-push
  %(prog)s -c 3 -p      # squash latest 3 commits and force-push

With -p, a single remote is used as-is; multiple remotes are chosen
interactively (TTY required).
""",
    )
    p.add_argument(
        "-c",
        metavar="COUNT",
        dest="count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"number of commits to squash (default: {DEFAULT_COUNT}, min: 2)",
    )
    p.add_argument(
        "-p",
        dest="push",
        action="store_true",
        help="force-push the squashed commit to a remote",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.count < 2:
        _die("-c must be >= 2")
    root = find_repo_root()
    if root is None:
        _die("not a git repository (or any of the parent directories)")
    try:
        squash_commits(root, args.count)
        if args.push:
            force_push(root)
    except RuntimeError as exc:
        _die(str(exc))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _error("interrupted")
        raise SystemExit(130)
