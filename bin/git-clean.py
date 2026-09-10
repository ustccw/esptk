#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
Restore the current git repository (including submodules) to an unmodified
working tree matching local HEAD.

Must be run inside a git working tree. Does not rewrite the superproject's
commit history. Submodules are checked out locally when the recorded commit
already exists; otherwise that single commit is fetched with --depth 1.

Examples:
  git-clean.py           # restore working tree, keep ignored files
  git-clean.py -f        # also remove git-ignored files
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn


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
    git_dir: Path | str | None = None,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "git",
        "-c", "advice.detachedHead=false",
        "-c", "protocol.file.allow=always",
    ]
    extra: list[str] = []
    if git_dir is not None:
        extra.extend(["--git-dir", str(git_dir)])
    extra.extend(args)
    cmd.extend(extra)
    if not quiet:
        loc = f"  (in {cwd})" if cwd else ""
        print(f"  -> {_fmt_git(extra)}{loc}", flush=True)

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
        pretty = _fmt_git(extra)
        if err:
            raise RuntimeError(f"{pretty}\n{err}")
        raise RuntimeError(f"{pretty} failed ({proc.returncode})")
    return proc


def git_out(args: list[str], *, cwd: Path | str | None = None,
            git_dir: Path | str | None = None) -> str:
    return git(args, cwd=cwd, git_dir=git_dir, capture=True, quiet=True).stdout.strip()


def git_ok(args: list[str], *, cwd: Path | str | None = None,
           git_dir: Path | str | None = None) -> bool:
    return git(args, cwd=cwd, git_dir=git_dir, capture=True,
               check=False, quiet=True).returncode == 0


# ---------------------------------------------------------------------------
# Paths / probes
# ---------------------------------------------------------------------------

def _resolve_git_path(repo: Path, spec: str) -> Path:
    raw = git_out(["rev-parse", "--git-path", spec], cwd=repo)
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def _git_dir(repo: Path) -> Path:
    raw = git_out(["rev-parse", "--git-dir"], cwd=repo)
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def is_git_worktree(path: Path) -> bool:
    """True only if *path* itself is a work tree root, not a subdir of another repo."""
    if not path.is_dir():
        return False
    proc = git(["rev-parse", "--show-toplevel"], cwd=path,
               capture=True, check=False, quiet=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    top = Path(proc.stdout.strip())
    try:
        return top.resolve() == path.resolve()
    except OSError:
        return False


def has_commit(repo: Path, sha: str) -> bool:
    return git_ok(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo)


def find_repo_root() -> Path | None:
    proc = git(["rev-parse", "--show-toplevel"], capture=True,
               check=False, quiet=True)
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    return Path(text) if text else None


def _rmtree(path: Path) -> None:
    def _onerror(func: Any, p: str, _exc: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)


# ---------------------------------------------------------------------------
# In-progress operations
# ---------------------------------------------------------------------------

def in_progress_kind(repo: Path) -> str | None:
    gd = _git_dir(repo)
    checks = (
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase/am"),
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
    )
    for filename, kind in checks:
        if (gd / filename).exists():
            return kind
    return None


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


# ---------------------------------------------------------------------------
# Submodule metadata
# ---------------------------------------------------------------------------

@dataclass
class Submodule:
    name: str
    path: str
    url: str
    sha: str
    branch: str | None


def _parse_gitmodules(repo: Path) -> dict[str, dict[str, str]]:
    """Return {name: {path, url, branch}} from .gitmodules."""
    gm = repo / ".gitmodules"
    if not gm.is_file():
        return {}
    proc = git(["config", "-f", ".gitmodules", "--list", "-z"],
               cwd=repo, capture=True, check=False, quiet=True)
    if proc.returncode != 0 or not proc.stdout:
        return {}

    by_name: dict[str, dict[str, str]] = {}
    # --list -z emits "key\\nvalue\\0"
    for item in proc.stdout.split("\0"):
        if not item or "\n" not in item:
            continue
        key, value = item.split("\n", 1)
        if not key.startswith("submodule.") or not value:
            continue
        rest = key[len("submodule."):]
        if rest.endswith(".path"):
            name, field = rest[: -len(".path")], "path"
        elif rest.endswith(".url"):
            name, field = rest[: -len(".url")], "url"
        elif rest.endswith(".branch"):
            name, field = rest[: -len(".branch")], "branch"
        else:
            continue
        by_name.setdefault(name, {})[field] = value
    return by_name


def _gitlinks(repo: Path) -> dict[str, str]:
    """Return {path: sha} for gitlink entries in HEAD."""
    proc = git(["ls-tree", "-r", "-z", "HEAD"], cwd=repo,
               capture=True, check=False, quiet=True)
    if proc.returncode != 0 or not proc.stdout:
        return {}
    links: dict[str, str] = {}
    for entry in proc.stdout.split("\0"):
        if not entry or "\t" not in entry:
            continue
        meta, path = entry.split("\t", 1)
        parts = meta.split()
        if len(parts) != 3:
            continue
        mode, typ, sha = parts
        if mode == "160000" and typ == "commit":
            links[path] = sha
    return links


def list_submodules(repo: Path) -> list[Submodule]:
    gitlinks = _gitlinks(repo)
    if not gitlinks:
        return []

    by_name = _parse_gitmodules(repo)
    by_path: dict[str, tuple[str, dict[str, str]]] = {}
    for name, fields in by_name.items():
        path = fields.get("path")
        if path:
            by_path[path] = (name, fields)

    result: list[Submodule] = []
    for path, sha in gitlinks.items():
        if path in by_path:
            name, fields = by_path[path]
            url = fields.get("url", "")
            branch = fields.get("branch") or None
        else:
            name, url, branch = path, "", None
        resolved = git(["config", "--get", f"submodule.{name}.url"],
                       cwd=repo, capture=True, check=False, quiet=True)
        if resolved.returncode == 0 and resolved.stdout.strip():
            url = resolved.stdout.strip()
        result.append(Submodule(name=name, path=path, url=url,
                                sha=sha, branch=branch))
    return result


def _modules_root(repo: Path) -> Path:
    return _resolve_git_path(repo, "modules")


def _submodule_git_dir(repo: Path, name: str) -> Path:
    return _modules_root(repo) / name


def _iter_module_git_dirs(modules_root: Path) -> list[tuple[str, Path]]:
    """Git dirs immediately owned by this repo's modules/ (not nested deeper)."""
    found: list[tuple[str, Path]] = []
    if not modules_root.is_dir():
        return found
    for dirpath, dirnames, _files in os.walk(modules_root):
        p = Path(dirpath)
        if (p / "HEAD").exists() and (p / "objects").is_dir():
            rel = str(p.relative_to(modules_root)).replace("\\", "/")
            found.append((rel, p))
            dirnames.clear()
    return found


# ---------------------------------------------------------------------------
# Submodule checkout / fetch
# ---------------------------------------------------------------------------

def _sync_origin(worktree: Path, url: str) -> None:
    if not url:
        return
    remotes = git_out(["remote"], cwd=worktree).split()
    if "origin" in remotes:
        git(["remote", "set-url", "origin", url], cwd=worktree)
    else:
        git(["remote", "add", "origin", url], cwd=worktree)


def _fetch_sha(worktree: Path, sm: Submodule) -> None:
    if not sm.url:
        raise RuntimeError(
            f"submodule {sm.path}: commit {sm.sha} is missing locally "
            f"and no URL is configured")
    _info(f"fetching {sm.path} {sm.sha[:12]} (depth 1)")
    proc = git(["fetch", "--depth", "1", "origin", sm.sha],
               cwd=worktree, check=False)
    if proc.returncode == 0 and has_commit(worktree, sm.sha):
        return
    if sm.branch:
        _warn(f"SHA fetch failed for {sm.path}; trying branch {sm.branch}")
        proc = git(["fetch", "--depth", "1", "origin", sm.branch],
                   cwd=worktree, check=False)
        if proc.returncode == 0 and has_commit(worktree, sm.sha):
            return
    raise RuntimeError(
        f"submodule {sm.path}: cannot obtain {sm.sha} from {sm.url}")


def _write_gitfile(worktree: Path, git_dir: Path) -> None:
    rel = os.path.relpath(git_dir, worktree)
    (worktree / ".git").write_text(f"gitdir: {rel}\n", encoding="utf-8")
    rel_wt = os.path.relpath(worktree, git_dir)
    git(["config", "core.worktree", rel_wt], git_dir=git_dir, quiet=True,
        check=False)


def _prepare_worktree(repo: Path, sm: Submodule) -> Path:
    """Make sm.path a usable work tree. Reuse .git/modules/<name> when present."""
    worktree = repo / sm.path
    git_dir = _submodule_git_dir(repo, sm.name)

    if is_git_worktree(worktree):
        return worktree

    if worktree.exists():
        _warn(f"replacing non-git path {sm.path}")
        if worktree.is_dir():
            _rmtree(worktree)
        else:
            worktree.unlink()

    worktree.mkdir(parents=True, exist_ok=True)
    git_dir.parent.mkdir(parents=True, exist_ok=True)

    if (git_dir / "HEAD").exists() and (git_dir / "objects").is_dir():
        _info(f"reconnecting {sm.path} to existing module git dir")
        _write_gitfile(worktree, git_dir)
        return worktree

    git(["init", f"--separate-git-dir={git_dir.resolve()}"], cwd=worktree)
    return worktree


def _register_parent_config(repo: Path, sm: Submodule) -> None:
    if sm.url:
        git(["config", f"submodule.{sm.name}.url", sm.url],
            cwd=repo, quiet=True, check=False)
    git(["config", "--bool", f"submodule.{sm.name}.active", "true"],
        cwd=repo, quiet=True, check=False)


def _ensure_submodule(repo: Path, sm: Submodule, clean_flags: list[str]) -> None:
    _register_parent_config(repo, sm)
    worktree = _prepare_worktree(repo, sm)
    if not is_git_worktree(worktree):
        raise RuntimeError(
            f"submodule {sm.path}: failed to create a work tree at {worktree}")
    if sm.url:
        _sync_origin(worktree, sm.url)
    abort_in_progress(worktree)

    if has_commit(worktree, sm.sha):
        _info(f"checkout {sm.path} {sm.sha[:12]} (local)")
    else:
        _fetch_sha(worktree, sm)

    git(["reset", "--hard", sm.sha], cwd=worktree)
    git(["clean"] + clean_flags, cwd=worktree)
    restore_submodules(worktree, clean_flags)


def _prune_stale_modules(repo: Path, expected: list[Submodule]) -> None:
    expected_names = {sm.name for sm in expected}
    for name, git_dir in _iter_module_git_dirs(_modules_root(repo)):
        if name in expected_names:
            continue
        _info(f"removing leftover module git dir {name}")
        _rmtree(git_dir)
        git(["config", "--remove-section", f"submodule.{name}"],
            cwd=repo, check=False, quiet=True)

    proc = git(["config", "--name-only", "--get-regexp",
                r"^submodule\..*\.url$"],
               cwd=repo, capture=True, check=False, quiet=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return
    for line in proc.stdout.splitlines():
        # submodule.<name>.url
        key = line.strip()
        if not key.startswith("submodule.") or not key.endswith(".url"):
            continue
        name = key[len("submodule."):-len(".url")]
        if name not in expected_names:
            git(["config", "--remove-section", f"submodule.{name}"],
                cwd=repo, check=False, quiet=True)


def restore_submodules(repo: Path, clean_flags: list[str]) -> None:
    git(["submodule", "sync"], cwd=repo, check=False)
    expected = list_submodules(repo)
    errors: list[str] = []
    for sm in expected:
        try:
            _ensure_submodule(repo, sm, clean_flags)
        except (RuntimeError, OSError) as exc:
            _error(str(exc))
            errors.append(sm.path)
    _prune_stale_modules(repo, expected)
    if errors:
        raise RuntimeError("failed to restore submodules: " + ", ".join(errors))


# ---------------------------------------------------------------------------
# Clean / verify
# ---------------------------------------------------------------------------

def reset_and_clean(repo: Path, clean_flags: list[str]) -> None:
    abort_in_progress(repo)
    git(["reset", "--hard", "HEAD"], cwd=repo)
    git(["clean"] + clean_flags, cwd=repo)


def is_repo_clean(repo: Path) -> bool:
    if in_progress_kind(repo):
        return False
    proc = git(["status", "--porcelain", "--ignore-submodules=none"],
               cwd=repo, capture=True, quiet=True)
    return not (proc.stdout or "").strip()


def show_status(repo: Path) -> None:
    kind = in_progress_kind(repo)
    if kind:
        _warn(f"in-progress operation: {kind}")
    proc = git(["status", "--porcelain", "--ignore-submodules=none"],
               cwd=repo, capture=True, quiet=True)
    text = (proc.stdout or "").rstrip()
    if text:
        print(text)


def clean_repo(repo: Path, *, force_ignored: bool) -> bool:
    clean_flags = ["-ffdx"] if force_ignored else ["-ffd"]
    _info(f"restoring {repo}")
    reset_and_clean(repo, clean_flags)
    restore_submodules(repo, clean_flags)

    if is_repo_clean(repo):
        _info("repository is clean")
        return True

    _warn("not clean after first pass; deinit submodules and retry")
    git(["submodule", "deinit", "--all", "-f"], cwd=repo, check=False)
    git(["clean"] + clean_flags, cwd=repo)
    restore_submodules(repo, clean_flags)

    if is_repo_clean(repo):
        _info("repository is clean (after retry)")
        return True

    _error("repository still not clean:")
    show_status(repo)
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Restore the current git repository (including submodules) "
            "to an unmodified working tree matching local HEAD."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s          # restore working tree, keep ignored files
  %(prog)s -f       # also remove git-ignored files (build artifacts, etc.)

Must be run inside a git working tree. Superproject commit history,
branches and stash are left untouched.
""",
    )
    parser.add_argument(
        "-f", "--force", action="store_true",
        help="also remove git-ignored files",
    )
    args = parser.parse_args()

    root = find_repo_root()
    if root is None:
        _die("not a git repository (or any of the parent directories)")

    try:
        ok = clean_repo(root, force_ignored=args.force)
    except RuntimeError as exc:
        _die(str(exc))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
