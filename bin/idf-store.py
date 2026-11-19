#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
Shallow shared ESP-IDF store.

Keeps one working tree per commit under ~/esp/idf/trees/<8-char-sha>
and points project ./esp-idf symlinks at it. Git objects are shared;
working-tree files for different SHAs stay isolated. Directory names
lengthen past 8 hex chars only when two installed SHAs would collide.

Examples:
  idf-store.py init
  idf-store.py get    // pick an installed tree
  idf-store.py get 296b6eab
  idf-store.py get v5.4.4 --note custom
  idf-store.py get v5.4.4 --clean
  idf-store.py clean  // clean ./esp-idf
  idf-store.py clean v5.4.4
  idf-store.py clean 296b6eab
  idf-store.py status
  idf-store.py doctor
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator, NoReturn
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

STORE_VERSION = 1
DEFAULT_STORE = Path.home() / "esp" / "idf"
DEFAULT_JOBS = 8
TREE_ABBREV = 8
LINK_NAME = "esp-idf"

DEFAULT_REMOTES = [
    "ssh://git@gitlab.espressif.cn:27227/espressif/esp-idf.git",
    "https://gitlab.espressif.cn:6688/espressif/esp-idf.git",
    "https://github.com/espressif/esp-idf.git",
]

SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA_PREFIX = re.compile(r"^[0-9a-f]{7,40}$")
HEX = re.compile(r"^[0-9a-f]+$")
HEX_DESCRIBE = re.compile(r"^[0-9a-f]{7,40}$")
DESCRIBE_DISTANCE = re.compile(r"^(.+)-(\d+)-g[0-9a-f]+$")
PRE_RELEASE_TAG = re.compile(r"-(?:rc|beta|dev|alpha)\d*$", re.I)
STABLE_TAG = re.compile(r"^v\d+\.\d+(?:\.\d+)?$")
VERSION_TAG = re.compile(r"^v\d+\.\d+")
DESCRIBE_DEPTHS = (1, 32, 128, 512, 2048, 8192)
DESCRIBE_BUDGET_S = 60.0
FILTER_PROBE_MAX = 2 * 1024 * 1024
DESCRIBE_PACK_MAX = 32 * 1024 * 1024


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


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

_GIT_ENV = None


def _git_env() -> dict[str, str]:
    global _GIT_ENV
    if _GIT_ENV is None:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_PAGER"] = "cat"
        _GIT_ENV = env
    return _GIT_ENV


def git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    capture: bool = False,
    check: bool = True,
    quiet: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "git",
        "-c", "protocol.file.allow=always",
        "-c", "advice.detachedHead=false",
        *args,
    ]
    kw: dict[str, Any] = {
        "cwd": str(cwd) if cwd is not None else None,
        "env": _git_env(),
        "check": False,
        "text": True,
    }
    if capture or quiet:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.PIPE
    try:
        proc = subprocess.run(cmd, timeout=timeout, **kw)
    except subprocess.TimeoutExpired as exc:
        pretty = "git " + " ".join(args)
        raise RuntimeError(f"{pretty} timed out after {timeout:.0f}s") from exc
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        pretty = "git " + " ".join(args)
        if err:
            raise RuntimeError(f"{pretty}\n{err}")
        raise RuntimeError(f"{pretty} failed ({proc.returncode})")
    return proc


def git_out(args: list[str], *, cwd: Path | str | None = None) -> str:
    return git(args, cwd=cwd, capture=True).stdout.strip()


def git_ok(args: list[str], *, cwd: Path | str | None = None) -> bool:
    return git(args, cwd=cwd, capture=True, check=False).returncode == 0


def _pick_tag(names: list[str]) -> str:
    """Prefer a final release name over rc/beta/dev tags on the same commit."""
    def rank(name: str) -> tuple[int, int, int, str]:
        pre = 1 if PRE_RELEASE_TAG.search(name) else 0
        stable = 0 if STABLE_TAG.fullmatch(name) else 1
        return (pre, stable, -len(name), name)

    return min(names, key=rank)


def _parse_peeled_tags(text: str) -> dict[str, list[str]]:
    """Map peeled commit SHA -> tag names from `git ls-remote --tags`."""
    pointed: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, ref = line.partition("\t")
        if not ref:
            sha, _, ref = line.partition(" ")
        sha = sha.strip().lower()
        ref = ref.strip()
        if not SHA40.fullmatch(sha) or not ref.startswith("refs/tags/"):
            continue
        if ref.endswith("^{}"):
            peeled[ref[len("refs/tags/"):-3]] = sha
        else:
            pointed[ref[len("refs/tags/"):]] = sha
    by_commit: dict[str, list[str]] = {}
    for name, tag_sha in pointed.items():
        commit = peeled.get(name, tag_sha)
        by_commit.setdefault(commit, []).append(name)
    return by_commit


def _version_tag_map(tags: dict[str, list[str]]) -> dict[str, list[str]]:
    """Keep release tags (v5.4, v5.4.4, v6.1-rc1). Drop qa-test and similar."""
    out: dict[str, list[str]] = {}
    for sha, names in tags.items():
        kept = [name for name in names if VERSION_TAG.match(name)]
        if kept:
            out[sha] = kept
    return out


def _pack_bytes(repo: Path) -> int:
    pack = repo / "objects" / "pack"
    if not pack.is_dir():
        return 0
    return sum(p.stat().st_size for p in pack.iterdir() if p.is_file())


def _strip_describe(text: str) -> str:
    """`v5.0.1-86-g24b9d38` -> `v5.0.1-86`. Exact tags stay as-is."""
    match = DESCRIBE_DISTANCE.fullmatch(text.strip())
    if not match:
        return text.strip()
    return f"{match.group(1)}-{match.group(2)}"


# ---------------------------------------------------------------------------
# Paths / JSON
# ---------------------------------------------------------------------------

def _lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        _die(f"corrupt JSON {path}: {exc}")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _du_bytes(path: Path) -> int:
    if not path.exists() or path.is_symlink():
        return 0
    total = 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    for root, dirs, files in os.walk(path, followlinks=False):
        # do not walk into nested .git/modules twice via follow
        for name in files:
            fp = Path(root) / name
            try:
                st = fp.lstat()
            except OSError:
                continue
            if not stat.S_ISLNK(st.st_mode):
                total += st.st_size
        keep = []
        for d in dirs:
            dp = Path(root) / d
            if dp.is_symlink():
                continue
            keep.append(d)
        dirs[:] = keep
    return total


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _basename_git(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if ":" in name and "/" not in url.rstrip("/").rsplit("@", 1)[-1]:
        # git@host:org/repo.git already split by rsplit /
        pass
    if name.endswith(".git"):
        return name
    return name + ".git"


def parse_org_repo(url: str) -> tuple[str, str] | None:
    """Return (org, repo.git) if the URL looks like org/repo."""
    u = url.strip()
    if u.startswith("git@"):
        # git@host:org/repo.git
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
    path = parts.path.strip("/")
    segs = [p for p in path.split("/") if p]
    if len(segs) < 2:
        return None
    org, repo = segs[-2], segs[-1]
    if not repo.endswith(".git"):
        repo += ".git"
    return org, repo


def mirror_urls(url: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def add(u: str) -> None:
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    parsed = parse_org_repo(url)
    if parsed:
        org, repo = parsed
        add(f"ssh://git@gitlab.espressif.cn:27227/{org}/{repo}")
        add(f"https://gitlab.espressif.cn:6688/{org}/{repo}")
    add(url)
    if parsed:
        org, repo = parsed
        add(f"https://github.com/{org}/{repo}")
    return out


def resolve_relative_url(parent: str, url: str) -> str:
    if not url.startswith("."):
        return url
    if parent.startswith("git@"):
        prefix, path = parent.split(":", 1)
        new_path = posixpath.normpath(
            posixpath.join(posixpath.dirname(path), url)
        )
        return prefix + ":" + new_path
    parts = urlsplit(parent)
    new_path = posixpath.normpath(
        posixpath.join(posixpath.dirname(parts.path), url)
    )
    return urlunsplit((parts.scheme, parts.netloc, new_path, "", ""))


def cache_dir_name(url: str) -> str:
    base = _basename_git(url)
    stem = base[:-4] if base.endswith(".git") else base
    stem = re.sub(r"[^A-Za-z0-9._+-]+", "_", stem)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}.git"


def _http_json(url: str, *, timeout: int = 20) -> dict[str, Any] | None:
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "idf-store.py",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve() if root.exists() else root.expanduser()
        self.config_path = self.root / "config.json"
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / ".lock"
        self.objects = self.root / "objects.git"
        self.modules = self.root / "modules"
        self.trees = self.root / "trees"
        self._cfg: dict[str, Any] | None = None
        self._idx: dict[str, Any] | None = None
        self._lock_fd: Any = None
        self._bare_lock = threading.RLock()
        self._tag_map: dict[str, list[str]] | None = None

    # -- layout --

    def initialized(self) -> bool:
        return self.config_path.is_file() and self.objects.is_dir()

    def load_config(self) -> dict[str, Any]:
        if self._cfg is None:
            self._cfg = _read_json(self.config_path, {})
        return self._cfg

    def load_index(self) -> dict[str, Any]:
        if self._idx is None:
            self._idx = _read_json(
                self.index_path, {"version": STORE_VERSION, "trees": {}}
            )
        return self._idx

    def save_config(self) -> None:
        assert self._cfg is not None
        _write_json(self.config_path, self._cfg)

    def save_index(self) -> None:
        assert self._idx is not None
        _write_json(self.index_path, self._idx)

    def remotes(self) -> list[str]:
        cfg = self.load_config()
        remotes = cfg.get("remotes") or list(DEFAULT_REMOTES)
        return [str(x) for x in remotes]

    def tree_path(self, sha: str) -> Path:
        scanned = self._scan_trees()
        existing = scanned.get(sha)
        if existing is not None:
            return existing
        others = [s for s in scanned if s != sha]
        return self.trees / self._abbrev(sha, others)

    def _abbrev(self, sha: str, others: list[str]) -> str:
        n = TREE_ABBREV
        while n < 40 and any(o[:n] == sha[:n] for o in others):
            n += 1
        return sha[:n]

    def _scan_trees(self) -> dict[str, Path]:
        """Map full 40-char SHA -> working-tree directory."""
        found: dict[str, Path] = {}
        if not self.trees.is_dir():
            return found
        for p in sorted(self.trees.iterdir()):
            if not p.is_dir() or p.name.startswith("."):
                continue
            name = p.name.lower()
            if not HEX.match(name) or not (7 <= len(name) <= 40):
                continue
            if SHA40.match(name):
                sha = name
            else:
                sha = self._commit_sha(p, "HEAD")
                if not sha:
                    continue
                if not sha.startswith(name):
                    _warn(
                        f"{p.name}: HEAD {sha[:12]}… does not match "
                        f"directory name"
                    )
            if sha in found:
                _die(f"duplicate tree for {sha}:\n  {found[sha]}\n  {p}")
            found[sha] = p
        return found

    def _index_shas(self) -> list[str]:
        trees = self.load_index().get("trees") or {}
        out: list[str] = []
        for key in trees:
            if not isinstance(key, str):
                continue
            sha = key.strip().lower()
            if SHA40.match(sha):
                out.append(sha)
        return out

    def _pinned_trees(self) -> dict[str, Path]:
        """Map full SHA -> tree directory, ignoring a drifted HEAD.

        The directory name is a prefix of the installed commit. The full
        SHA comes from the index when that prefix is unique.
        """
        found: dict[str, Path] = {}
        if not self.trees.is_dir():
            return found
        index_shas = self._index_shas()
        for p in sorted(self.trees.iterdir()):
            if not p.is_dir() or p.name.startswith("."):
                continue
            name = p.name.lower()
            if not HEX.match(name) or not (7 <= len(name) <= 40):
                continue
            sha = self._sha_for_tree_dir(p, name, index_shas)
            if not sha:
                continue
            if sha in found:
                _die(f"duplicate tree for {sha}:\n  {found[sha]}\n  {p}")
            found[sha] = p
        return found

    def _sha_for_tree_dir(
        self, path: Path, name: str, index_shas: list[str]
    ) -> str | None:
        hits = [s for s in index_shas if s.startswith(name)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            resolved = self._commit_sha(path, name)
            if resolved and resolved in hits:
                return resolved
            _die(
                "ambiguous tree directory {0}: {1}".format(
                    path, ", ".join(h[:12] for h in hits)
                )
            )
        if SHA40.match(name):
            return name
        resolved = self._commit_sha(path, name)
        if resolved and resolved.startswith(name):
            return resolved
        return None

    def _pinned_sha_at(self, repo: Path) -> str | None:
        try:
            target = repo.resolve()
        except OSError:
            return None
        for sha, path in self._pinned_trees().items():
            try:
                if path.resolve() == target:
                    return sha
            except OSError:
                continue
        return None

    def _local_ref_sha(self, ref: str) -> str | None:
        """Resolve ref inside objects.git. Does not fetch."""
        if not self.objects.is_dir():
            return None
        names = [ref]
        if not HEX.match(ref.lower()):
            names.append("refs/idf-store/branch-" + ref.replace("/", "-"))
        for name in names:
            sha = self._commit_sha(self.objects, name)
            if sha:
                return sha
        return None

    def resolve_installed_ref(self, ref: str) -> tuple[str, Path]:
        """Map a local ref to an installed tree. Does not fetch or clone."""
        ref = ref.strip()
        pinned = self._pinned_trees()
        if HEX.match(ref.lower()) and len(ref) <= 40:
            low = ref.lower()
            hits = [s for s in pinned if s.startswith(low)]
            if len(hits) > 1:
                _die(
                    "ambiguous SHA prefix {0!r}: {1}".format(
                        ref, ", ".join(h[:12] for h in hits)
                    )
                )
            if len(hits) == 1:
                return hits[0], pinned[hits[0]]
            _die(f"no installed tree for {ref!r}")

        describe_hits: list[str] = []
        lowered: dict[str, Any] = {}
        for key, meta in (self.load_index().get("trees") or {}).items():
            if isinstance(key, str) and isinstance(meta, dict):
                lowered[key.strip().lower()] = meta
        for sha in pinned:
            text = str((lowered.get(sha) or {}).get("describe") or "")
            if text and text == ref:
                describe_hits.append(sha)
        if len(describe_hits) > 1:
            _die(
                "ambiguous describe {0!r}: {1}".format(
                    ref, ", ".join(h[:12] for h in describe_hits)
                )
            )
        if len(describe_hits) == 1:
            sha = describe_hits[0]
            return sha, pinned[sha]

        sha = self._local_ref_sha(ref)
        if sha and sha in pinned:
            return sha, pinned[sha]
        _die(f"no installed tree for {ref!r}")

    def _retarget_links(self, sha: str, dest: Path) -> None:
        dest_s = str(dest.resolve())
        dest_res = dest.resolve()
        for item in list(self._link_list(sha)):
            p = Path(item)
            if not p.is_symlink():
                continue
            try:
                if p.resolve() == dest_res:
                    continue
            except OSError:
                pass
            tmp = p.with_name(p.name + ".idf-store-new")
            if _lexists(tmp):
                tmp.unlink()
            os.symlink(dest_s, str(tmp), target_is_directory=True)
            os.replace(str(tmp), str(p))
            _info(f"relinked {p} -> {dest_s}")

    def _migrate_tree_names(self) -> None:
        """Rename trees/<40-char-sha> (or other non-canonical names) to 8+."""
        entries = self._scan_trees()
        if not entries:
            return
        shas = list(entries)
        planned: dict[str, Path] = {}
        for sha in shas:
            others = [s for s in shas if s != sha]
            planned[sha] = self.trees / self._abbrev(sha, others)
        for sha, src in entries.items():
            dest = planned[sha]
            if src.resolve() == dest.resolve():
                continue
            if dest.exists():
                _die(f"cannot rename {src} -> {dest}: destination exists")
            _info(f"renaming tree {src.name} -> {dest.name}")
            src.rename(dest)
            self._retarget_links(sha, dest)

    def module_cache(self, url: str) -> Path:
        return self.modules / cache_dir_name(url)

    # -- lock --

    def lock(self) -> "Store":
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self.config_path = self.root / "config.json"
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / ".lock"
        self.objects = self.root / "objects.git"
        self.modules = self.root / "modules"
        self.trees = self.root / "trees"
        self._lock_fd = open(self.lock_path, "a+", encoding="utf-8")
        fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX)
        return self

    def unlock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_fd.close()
                self._lock_fd = None

    def __enter__(self) -> "Store":
        return self.lock()

    def __exit__(self, *exc: Any) -> None:
        self.unlock()

    # -- bare repos --

    def _init_bare(self, path: Path, urls: list[str]) -> None:
        with self._bare_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not (path / "HEAD").exists():
                git(["init", "--bare", str(path)])
            git(["config", "gc.auto", "0"], cwd=path)
            git(["config", "core.logAllRefUpdates", "false"], cwd=path)
            existing = set()
            listing = git(["remote"], cwd=path, capture=True, check=False)
            if listing.returncode == 0:
                existing = set(listing.stdout.split())
            for i, url in enumerate(urls):
                name = "origin" if i == 0 else f"r{i}"
                if name in existing:
                    git(["remote", "set-url", name, url], cwd=path)
                else:
                    git(["remote", "add", name, url], cwd=path)
                git(["config", "--unset-all", f"remote.{name}.fetch"],
                    cwd=path, check=False)

    def _remote_names(self, bare: Path) -> list[str]:
        listing = git(["remote"], cwd=bare, capture=True, check=False)
        if listing.returncode != 0:
            return []
        return [n for n in listing.stdout.split() if n]

    def _commit_sha(self, repo: Path, ref: str) -> str | None:
        spec = ref if ref.endswith("^{commit}") else f"{ref}^{{commit}}"
        proc = git(
            ["rev-parse", "--verify", spec],
            cwd=repo, capture=True, check=False,
        )
        if proc.returncode != 0:
            return None
        sha = proc.stdout.strip().lower()
        return sha if SHA40.match(sha) else None

    def _pin_ref(self, bare: Path, sha: str) -> None:
        git(["update-ref", f"refs/idf-store/{sha}", sha], cwd=bare)
        git(["symbolic-ref", "HEAD", f"refs/idf-store/{sha}"], cwd=bare)

    def _refspecs(self, ref: str) -> list[str]:
        specs = [ref]
        if SHA_PREFIX.match(ref.lower()):
            low = ref.lower()
            specs.append(f"{low}:refs/idf-store/{low}")
        else:
            specs.extend(
                [
                    f"refs/tags/{ref}:refs/tags/{ref}",
                    f"+refs/tags/{ref}:refs/tags/{ref}",
                    f"refs/heads/{ref}:refs/idf-store/branch-{ref.replace('/', '-')}",
                    f"+refs/heads/{ref}:refs/idf-store/branch-{ref.replace('/', '-')}",
                ]
            )
        # unique preserve order
        seen: set[str] = set()
        out: list[str] = []
        for s in specs:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def fetch_into_bare(self, bare: Path, ref: str) -> str:
        """Fetch ref (sha / tag / branch) into a bare store repo. Return full sha."""
        with self._bare_lock:
            have = self._commit_sha(bare, ref)
            if have:
                self._pin_ref(bare, have)
                return have
            last_err = ""
            for remote in self._remote_names(bare):
                skip_remote = False
                for spec in self._refspecs(ref):
                    if skip_remote:
                        break
                    _info(f"fetch --depth 1 {remote} {spec}")
                    proc = git(
                        ["fetch", "--depth", "1", remote, spec],
                        cwd=bare, capture=True, check=False,
                    )
                    text = (proc.stderr or "") + (proc.stdout or "")
                    if proc.returncode == 0:
                        have = (
                            self._commit_sha(bare, ref)
                            or self._commit_sha(bare, "FETCH_HEAD")
                        )
                        if have:
                            self._pin_ref(bare, have)
                            return have
                        continue
                    last_err = text.strip() or f"git fetch {remote} {spec}"
                    low = last_err.lower()
                    if any(
                        needle in low
                        for needle in (
                            "could not be found",
                            "repository not found",
                            "could not read username",
                            "authentication failed",
                            "permission denied",
                        )
                    ):
                        _warn(f"{remote} has no {ref[:12]}…; trying next remote")
                        skip_remote = True
            extra = f"\n{last_err}" if last_err else ""
            _die(f"could not fetch {ref!r} into {bare}{extra}")

    # -- superproject --

    def ensure_objects(self) -> None:
        self._init_bare(self.objects, self.remotes())

    def resolve_super_ref(self, ref: str) -> str:
        self.ensure_objects()
        ref = ref.strip()
        if SHA_PREFIX.match(ref.lower()):
            low = ref.lower()
            installed = self._match_installed(low)
            if installed:
                return installed
            have = self._commit_sha(self.objects, low)
            if have:
                return have
            if not SHA40.match(low):
                expanded = self._expand_sha_prefix(low)
                if not expanded:
                    _die(
                        f"short SHA {ref!r} is not in the store and could "
                        f"not be expanded. Pass the full 40-character "
                        f"commit (remotes do not advertise abbreviated SHAs)."
                    )
                _info(f"expanded {low} -> {expanded}")
                low = expanded
            return self.fetch_into_bare(self.objects, low)
        return self.fetch_into_bare(self.objects, ref)

    def _expand_sha_prefix(self, prefix: str) -> str | None:
        """Turn a unique abbreviated SHA into 40 hex chars via GitLab/GitHub API."""
        seen: set[str] = set()
        for url in self.remotes():
            parsed = parse_org_repo(url)
            if not parsed:
                continue
            org, repo = parsed
            stem = repo[:-4] if repo.endswith(".git") else repo
            key = f"{org}/{stem}".lower()
            if key in seen:
                continue
            seen.add(key)
            _info(f"looking up {prefix} as {org}/{stem}")
            sha = (
                self._lookup_sha_gitlab(org, stem, prefix)
                or self._lookup_sha_github(org, stem, prefix)
            )
            if sha:
                return sha
        return None

    def _gitlab_http_roots(self) -> list[str]:
        roots: list[str] = []
        for url in self.remotes():
            parts = urlsplit(url)
            host = (parts.netloc or "").lower()
            if parts.scheme in ("http", "https") and "gitlab" in host:
                root = f"{parts.scheme}://{parts.netloc}"
                if root not in roots:
                    roots.append(root)
        fallback = "https://gitlab.espressif.cn:6688"
        if fallback not in roots:
            roots.append(fallback)
        return roots

    def _lookup_sha_gitlab(self, org: str, repo: str, prefix: str) -> str | None:
        project = quote(f"{org}/{repo}", safe="")
        for root in self._gitlab_http_roots():
            data = _http_json(
                f"{root}/api/v4/projects/{project}/repository/commits/{prefix}"
            )
            sha = self._sha_from_payload(data, prefix)
            if sha:
                return sha
        return None

    def _lookup_sha_github(self, org: str, repo: str, prefix: str) -> str | None:
        data = _http_json(
            f"https://api.github.com/repos/{org}/{repo}/commits/{prefix}"
        )
        return self._sha_from_payload(data, prefix)

    @staticmethod
    def _sha_from_payload(data: dict[str, Any] | None, prefix: str) -> str | None:
        if not data:
            return None
        for key in ("id", "sha"):
            val = data.get(key)
            if not isinstance(val, str):
                continue
            sha = val.strip().lower()
            if SHA40.match(sha) and sha.startswith(prefix):
                return sha
        return None

    def _match_installed(self, prefix: str) -> str | None:
        names = self.installed_shas()
        hits = [n for n in names if n.startswith(prefix)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            _die("ambiguous SHA prefix {0!r}: {1}".format(
                prefix, ", ".join(h[:12] for h in hits)))
        return None

    def installed_shas(self) -> list[str]:
        return sorted(self._scan_trees())

    # -- submodules --

    def _parent_origin(self, repo: Path) -> str:
        proc = git(["remote", "get-url", "origin"], cwd=repo,
                   capture=True, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        return self.remotes()[0]

    def _set_origin(self, repo: Path, url: str) -> None:
        remotes = git(["remote"], cwd=repo, capture=True, check=False)
        names = remotes.stdout.split() if remotes.returncode == 0 else []
        if "origin" in names:
            git(["remote", "set-url", "origin", url], cwd=repo)
        else:
            git(["remote", "add", "origin", url], cwd=repo)
        git(["config", "--unset-all", "remote.origin.fetch"],
            cwd=repo, check=False)
        git(["config", "gc.auto", "0"], cwd=repo)

    def _gitlinks(self, repo: Path) -> list[tuple[str, str]]:
        proc = git(["ls-tree", "-r", "-z", "HEAD"], cwd=repo, capture=True)
        out: list[tuple[str, str]] = []
        for entry in proc.stdout.split("\0"):
            if not entry:
                continue
            try:
                meta, path = entry.split("\t", 1)
            except ValueError:
                continue
            parts = meta.split()
            if len(parts) != 3:
                continue
            mode, kind, sha = parts
            if mode == "160000" and kind == "commit":
                out.append((path, sha.lower()))
        return out

    def _gitmodules_urls(self, repo: Path) -> dict[str, str]:
        """Map submodule path -> url from .gitmodules."""
        gm = repo / ".gitmodules"
        if not gm.is_file():
            return {}
        listing = git(
            ["config", "-f", ".gitmodules", "--list"],
            cwd=repo, capture=True, check=False,
        )
        if listing.returncode != 0:
            return {}
        names: dict[str, dict[str, str]] = {}
        for line in listing.stdout.splitlines():
            if "=" not in line or not line.startswith("submodule."):
                continue
            key, _, val = line.partition("=")
            body = key[len("submodule."):]
            if body.endswith(".path"):
                name = body[: -len(".path")]
                names.setdefault(name, {})["path"] = val
            elif body.endswith(".url"):
                name = body[: -len(".url")]
                names.setdefault(name, {})["url"] = val
        parent = self._parent_origin(repo)
        result: dict[str, str] = {}
        for meta in names.values():
            path = meta.get("path")
            url = meta.get("url")
            if path and url:
                result[path] = resolve_relative_url(parent, url)
        return result

    def _abs_git_dir(self, repo: Path) -> Path:
        return Path(git_out(["rev-parse", "--absolute-git-dir"], cwd=repo))

    def ensure_module_objects(self, url: str, sha: str) -> Path:
        cache = self.module_cache(url)
        self._init_bare(cache, mirror_urls(url))
        got = self.fetch_into_bare(cache, sha)
        if got != sha:
            _die(f"module {url} resolved to {got}, expected {sha}")
        return cache

    def _clone_submodule(
        self, repo: Path, relpath: str, url: str, sha: str
    ) -> Path:
        work = repo / relpath
        gitdir = self._abs_git_dir(repo) / "modules" / relpath
        cache = self.module_cache(url)
        gitfile = work / ".git"
        already = gitfile.is_file() or gitfile.is_dir()
        if already and git_ok(["rev-parse", "--git-dir"], cwd=work):
            git(["checkout", "--detach", sha, "--force"], cwd=work)
            self._set_origin(work, url)
            if gitdir.is_dir():
                self._point_gitfile(work, gitdir)
            return work
        if work.exists():
            shutil.rmtree(work)
        if gitdir.exists():
            shutil.rmtree(gitdir)
        work.parent.mkdir(parents=True, exist_ok=True)
        gitdir.parent.mkdir(parents=True, exist_ok=True)
        git([
            "clone", "--shared", "--no-checkout",
            "--separate-git-dir", str(gitdir),
            str(cache), str(work),
        ])
        self._point_gitfile(work, gitdir)
        self._set_origin(work, url)
        git(["checkout", "--detach", sha, "--force"], cwd=work)
        return work

    def _point_gitfile(self, work: Path, gitdir: Path) -> None:
        """Keep gitdir pointers relative so trees/ can be renamed from tmp."""
        rel_gd = os.path.relpath(gitdir, start=work)
        (work / ".git").write_text(f"gitdir: {rel_gd}\n")
        rel_wt = os.path.relpath(work, start=gitdir)
        git(["config", "core.worktree", rel_wt], cwd=work)

    def populate_submodules(
        self, repo: Path, *, clean: bool, jobs: int
    ) -> None:
        if not (repo / ".gitmodules").is_file():
            return
        git(["submodule", "init"], cwd=repo)
        links = self._gitlinks(repo)
        urls = self._gitmodules_urls(repo)
        if not links:
            return
        tasks: list[tuple[str, str, str]] = []
        for path, sha in links:
            url = urls.get(path)
            if not url:
                _warn(f"submodule {path} has no URL in .gitmodules; skip")
                continue
            _info(f"submodule {path} @ {sha[:12]}")
            self.ensure_module_objects(url, sha)
            tasks.append((path, url, sha))

        def one(item: tuple[str, str, str]) -> None:
            path, url, sha = item
            work = self._clone_submodule(repo, path, url, sha)
            if clean:
                git(["reset", "--hard", sha], cwd=work)
                git(["clean", "-fdx"], cwd=work)
            if (work / ".gitmodules").is_file():
                self.populate_submodules(work, clean=clean, jobs=1)

        workers = max(1, jobs)
        if workers == 1 or len(tasks) <= 1:
            for item in tasks:
                one(item)
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(one, item) for item in tasks]
            for fut in as_completed(futs):
                fut.result()

    def _assert_alternates_inside(self, gitdir: Path) -> None:
        alt = gitdir / "objects" / "info" / "alternates"
        if not alt.is_file():
            return
        root = str(self.root.resolve())
        for line in alt.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            target = Path(line)
            if not target.is_absolute():
                target = (gitdir / "objects" / "info" / line).resolve()
            else:
                try:
                    target = target.resolve()
                except OSError:
                    _die(f"broken git alternate {line} in {gitdir}")
            if not str(target).startswith(root + os.sep) and str(target) != root:
                _die(
                    f"git alternate points outside the store:\n"
                    f"  {gitdir}\n  -> {target}"
                )

    # -- trees --

    def ensure_tree(self, sha: str, *, clean: bool, jobs: int) -> Path:
        if not SHA40.match(sha):
            _die(f"internal error: not a 40-char sha: {sha}")
        self._migrate_tree_names()
        dest = self.tree_path(sha)
        if dest.is_dir() and git_ok(["rev-parse", "--git-dir"], cwd=dest):
            if clean:
                _warn(
                    f"--clean will discard local changes (including "
                    f"submodules) in shared tree {dest}"
                )
                self._clean_tree(dest, sha, jobs=jobs)
            else:
                _info(f"reusing {dest}")
            return dest

        if dest.exists():
            shutil.rmtree(dest)

        tmp = self.trees / f".tmp-{sha[:12]}-{os.getpid()}"
        if tmp.exists():
            shutil.rmtree(tmp)
        self.trees.mkdir(parents=True, exist_ok=True)
        try:
            _info(f"cloning {sha} -> {tmp}")
            git([
                "clone", "--shared", "--no-checkout",
                str(self.objects), str(tmp),
            ])
            self._set_origin(tmp, self.remotes()[0])
            git(["checkout", "--detach", sha, "--force"], cwd=tmp)
            git(["config", "submodule.recurse", "false"], cwd=tmp)
            git(["config", "fetch.recurseSubmodules", "false"], cwd=tmp)
            self._assert_alternates_inside(tmp / ".git")
            self.populate_submodules(tmp, clean=True, jobs=jobs)
            self._check_submodule_status(tmp, as_error=True)
            if not (tmp / "tools" / "idf.py").is_file():
                _die(f"checkout {sha} is missing tools/idf.py")
            tmp.replace(dest)
        except Exception:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            raise
        self._remember_short_describe(sha, dest)
        self.save_index()
        _info(f"installed {sha} ({self._tree_meta(sha)['describe']})")
        return dest

    def _clean_tree(self, dest: Path, sha: str, *, jobs: int) -> None:
        git(["checkout", "--detach", sha, "--force"], cwd=dest)
        git(["reset", "--hard", sha], cwd=dest)
        git(["clean", "-fdx"], cwd=dest)
        git(["submodule", "sync", "--recursive"], cwd=dest, check=False)
        self.populate_submodules(dest, clean=True, jobs=jobs)
        self._check_submodule_status(dest, as_error=True)

    def _describe(self, repo: Path) -> str:
        proc = git(
            ["describe", "--tags", "--always"],
            cwd=repo, capture=True, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        return git_out(["rev-parse", "--short", "HEAD"], cwd=repo)

    def _remember_short_describe(self, sha: str, repo: Path) -> None:
        """Keep the local short name until a real tag describe is stored."""
        meta = self._tree_meta(sha)
        current = str(meta.get("describe") or "")
        if current and not HEX_DESCRIBE.fullmatch(current):
            return
        meta["describe"] = self._describe(repo)

    def _stored_version(self, sha: str) -> str:
        meta = (self.load_index().get("trees") or {}).get(sha) or {}
        text = str(meta.get("describe") or "")
        if VERSION_TAG.match(text):
            return text
        return ""

    def _abbrev_name(self, repo: Path, sha: str) -> str:
        """Directory name, usually 8 hex chars. Not git's variable --short."""
        name = repo.name.lower()
        if HEX.fullmatch(name) and sha.startswith(name):
            return name
        return sha[:TREE_ABBREV]

    def _rev_parts(self, repo: Path, sha: str) -> tuple[str, str]:
        short = self._abbrev_name(repo, sha)
        version = self._stored_version(sha)
        dirty = self._worktree_dirty(repo)
        if version:
            body = f"{version}-dirty" if dirty else version
            return short, f"({body})"
        if dirty:
            return short, "(dirty)"
        return short, ""

    def _worktree_dirty(self, repo: Path) -> bool:
        """Tracked changes, including submodule checkouts and their files.

        Untracked and ignored files (build/, sdkconfig) do not count.
        """
        proc = git(
            [
                "status", "--porcelain",
                "--untracked-files=no",
                "--ignore-submodules=none",
            ],
            cwd=repo, capture=True, check=False,
        )
        return bool((proc.stdout or "").strip())

    def _rev_label(self, repo: Path, sha: str) -> str:
        short, version = self._rev_parts(repo, sha)
        if version:
            return f"{short} {version}"
        return short

    def _peeled_tag_map(self) -> dict[str, list[str]]:
        if self._tag_map is not None:
            return self._tag_map
        last_err = ""
        for url in self.remotes():
            _info(f"listing tags from {url}")
            proc = git(
                ["ls-remote", "--tags", url],
                capture=True, check=False, timeout=30,
            )
            if proc.returncode != 0:
                last_err = (proc.stderr or proc.stdout or "").strip()
                _warn(f"could not list tags from {url}")
                continue
            mapping = _version_tag_map(_parse_peeled_tags(proc.stdout or ""))
            if mapping:
                self._tag_map = mapping
                return mapping
        if last_err:
            _warn(last_err.splitlines()[-1])
        self._tag_map = {}
        return {}

    def ensure_version(self, sha: str) -> str:
        """Store `v5.4.4` or `v5.0.1-86` for sha. Empty if lookup failed."""
        have = self._stored_version(sha)
        if have:
            return have
        found = self._lookup_version(sha)
        if not found:
            return ""
        self._tree_meta(sha)["describe"] = found
        self.save_index()
        _info(f"version {sha[:12]} -> {found}")
        return found

    def _lookup_version(self, sha: str) -> str:
        tags = self._peeled_tag_map()
        if not tags:
            _warn(f"could not list tags; {sha[:12]} has no version")
            return ""
        names = tags.get(sha)
        if names:
            return _pick_tag(names)
        for url in self.remotes():
            outcome, text = self._version_from_remote(sha, url, tags)
            if outcome == "found":
                return text
            if outcome == "stop":
                return ""
        return ""

    def _version_from_remote(
        self, sha: str, url: str, tags: dict[str, list[str]]
    ) -> tuple[str, str]:
        """Return (found|next|stop, version). History stays in /tmp."""
        tmp = Path(tempfile.mkdtemp(prefix="idf-store-"))
        probed = False
        try:
            self._init_describe_tmp(tmp, url)
            deadline = time.monotonic() + DESCRIBE_BUDGET_S
            prev = 0
            for depth in DESCRIBE_DEPTHS:
                left = deadline - time.monotonic()
                if left <= 0:
                    _warn(
                        f"describe {sha[:12]} exceeded "
                        f"{DESCRIBE_BUDGET_S:.0f}s; version left unset"
                    )
                    return "stop", ""
                if depth == 1:
                    _info(f"describe {sha[:12]}: fetch --depth 1 {url}")
                    ok = self._fetch_describe(
                        tmp, ["--depth", "1"], sha, timeout=left,
                    )
                else:
                    step = depth - prev
                    _info(f"describe {sha[:12]}: fetch --deepen={step}")
                    ok = self._fetch_describe(
                        tmp, [f"--deepen={step}"], sha, timeout=left,
                    )
                prev = depth
                if not ok:
                    if probed:
                        _warn(f"describe {sha[:12]}: fetch failed")
                        return "stop", ""
                    return "next", ""
                size = _pack_bytes(tmp)
                if not probed:
                    probed = True
                    if size > FILTER_PROBE_MAX:
                        _warn(
                            f"{url} did not honor --filter=tree:0 "
                            f"(probe pack {_human_size(size)})"
                        )
                        return "next", ""
                elif size > DESCRIBE_PACK_MAX:
                    _warn(
                        f"describe {sha[:12]} pack grew to "
                        f"{_human_size(size)}; stopped"
                    )
                    return "stop", ""
                version = self._version_in_tmp(tmp, sha, tags)
                if version:
                    return "found", version
            _warn(
                f"describe {sha[:12]}: no tag within "
                f"{DESCRIBE_DEPTHS[-1]} commits"
            )
            return "stop", ""
        except RuntimeError as exc:
            _warn(str(exc).splitlines()[0])
            if probed:
                return "stop", ""
            return "next", ""
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _init_describe_tmp(self, path: Path, url: str) -> None:
        git(["init", "--bare", str(path)], quiet=True)
        git(["remote", "add", "origin", url], cwd=path, quiet=True)
        git(["config", "gc.auto", "0"], cwd=path, quiet=True)
        git(["config", "core.logAllRefUpdates", "false"], cwd=path, quiet=True)
        git(["config", "remote.origin.promisor", "true"], cwd=path, quiet=True)
        git(["config", "remote.origin.partialclonefilter", "tree:0"], cwd=path, quiet=True)
        git(["config", "extensions.partialClone", "origin"], cwd=path, quiet=True)

    def _fetch_describe(
        self, repo: Path, limit: list[str], sha: str, *, timeout: float
    ) -> bool:
        spec = f"{sha}:refs/idf-store/{sha}"
        proc = git(
            ["fetch", "--filter=tree:0", *limit, "origin", spec],
            cwd=repo, capture=True, check=False, timeout=max(1.0, timeout),
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            if err:
                _warn(err.splitlines()[-1])
            return False
        return git_ok(
            ["-c", "remote.origin.promisor=false", "cat-file", "-e", sha],
            cwd=repo,
        )

    def _set_lazy_fetch(self, repo: Path, enabled: bool) -> None:
        if enabled:
            git(["config", "remote.origin.promisor", "true"], cwd=repo)
            git(["config", "extensions.partialClone", "origin"], cwd=repo)
            return
        git(
            ["config", "--unset-all", "extensions.partialClone"],
            cwd=repo, check=False,
        )
        git(["config", "remote.origin.promisor", "false"], cwd=repo)

    def _version_in_tmp(
        self, repo: Path, sha: str, tags: dict[str, list[str]]
    ) -> str:
        self._set_lazy_fetch(repo, False)
        try:
            return self._describe_loaded(repo, sha, tags)
        finally:
            self._set_lazy_fetch(repo, True)

    def _describe_loaded(
        self, repo: Path, sha: str, tags: dict[str, list[str]]
    ) -> str:
        proc = git(
            ["-c", "remote.origin.promisor=false", "rev-list", sha],
            cwd=repo, capture=True, check=False,
        )
        if proc.returncode != 0:
            return ""
        have = set((proc.stdout or "").split())
        hit = False
        for commit, names in tags.items():
            if commit not in have:
                continue
            hit = True
            git(
                ["update-ref", f"refs/tags/{_pick_tag(names)}", commit],
                cwd=repo, check=False,
            )
        if not hit:
            return ""
        described = git(
            ["-c", "remote.origin.promisor=false", "describe", "--tags", sha],
            cwd=repo, capture=True, check=False,
        )
        if described.returncode != 0:
            return ""
        return _strip_describe(described.stdout or "")

    def _current_sha(self, project: Path) -> str | None:
        link = project / LINK_NAME
        if not link.is_symlink():
            return None
        try:
            target = link.resolve()
        except OSError:
            return None
        for sha in self.installed_shas():
            if self.tree_path(sha).resolve() == target:
                return sha
        return None

    def _print_trees(
        self, shas: list[str], *, numbered: bool, current: str | None,
        sizes: bool,
    ) -> None:
        rows: list[dict[str, Any]] = []
        for sha in shas:
            dest = self.tree_path(sha)
            short, version = self._rev_parts(dest, sha)
            self.unregister_stale_links(sha)
            entries = self._link_entries(sha)
            rows.append({
                "sha": sha,
                "short": short,
                "version": version,
                "date": self._commit_date(dest),
                "size": _human_size(_du_bytes(dest)) if sizes else "",
                "nlink": len(entries),
                "entries": entries,
            })
        if not rows:
            return
        w_short = max(len(r["short"]) for r in rows)
        w_ver = max(len(r["version"]) for r in rows)
        w_size = max((len(r["size"]) for r in rows), default=0) if sizes else 0
        w_nlink = max(len(str(r["nlink"])) for r in rows)
        idx_w = len(str(len(rows)))
        for i, row in enumerate(rows, 1):
            if numbered:
                prefix = f"  {i:>{idx_w}}) "
            else:
                prefix = "  "
            short = row["short"].ljust(w_short)
            version = row["version"]
            pad = " " * (w_ver - len(version))
            version_col = (_c("1;33", version) + pad) if version else pad
            if w_ver:
                version_col += "  "
            size_col = ""
            if sizes:
                size_col = f"{_c('33', row['size'].rjust(w_size))}  "
            nlink = row["nlink"]
            mark = ""
            if current is not None and row["sha"] == current:
                mark = " " + _c("1;33", "current")
            print(
                f"{prefix}{_c('1;97', row['sha'])}  "
                f"{_c('1;36', short)}  {version_col}"
                f"{_c('2', row['date'])}  "
                f"{size_col}"
                f"{_c('32' if nlink else '2', f'{nlink:>{w_nlink}} link(s)')}"
                f"{mark}"
            )
            for entry in row["entries"]:
                line = f"    {_c('36', entry['path'])}"
                if entry["note"]:
                    line += " " + _c("2", f"({entry['note']})")
                print(line)

    def pick_installed(self, project: Path) -> str:
        shas = self.installed_shas()
        if not shas:
            _die(f"no installed trees in {self.root}")
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            _die("ref required when not running in a terminal")
        current = self._current_sha(project)
        print(_c("1", "installed commits:"))
        self._print_trees(shas, numbered=True, current=current, sizes=False)
        while True:
            try:
                ans = input(
                    "select [number or sha prefix, q to abort]: "
                ).strip()
            except EOFError:
                _die("aborted")
            if not ans or ans.lower() == "q":
                _die("aborted")
            if ans.isdigit():
                n = int(ans)
                if 1 <= n <= len(shas):
                    return shas[n - 1]
                _warn(f"choose 1..{len(shas)}")
                continue
            hits = [s for s in shas if s.startswith(ans.lower())]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                _warn(f"ambiguous prefix {ans!r}")
            else:
                _warn(f"no installed tree matches {ans!r}")

    def _commit_date(self, repo: Path) -> str:
        proc = git(
            ["log", "-1", "--date=short", "--format=%cd"],
            cwd=repo, capture=True, check=False,
        )
        date = (proc.stdout or "").strip()
        if proc.returncode == 0 and date:
            return date
        return "-"

    def _check_submodule_status(
        self, repo: Path, *, as_error: bool
    ) -> list[str]:
        proc = git(
            ["submodule", "status", "--recursive"],
            cwd=repo, capture=True, check=False,
        )
        problems: list[str] = []
        if proc.returncode != 0:
            problems.append(
                f"git submodule status failed in {repo}: "
                f"{(proc.stderr or proc.stdout or '').strip()}"
            )
        for line in (proc.stdout or "").splitlines():
            if not line:
                continue
            mark = line[0]
            if mark in "-+U":
                problems.append(line)
        if problems and as_error:
            _die(
                "submodule tree is not clean:\n  "
                + "\n  ".join(problems)
            )
        return problems

    # -- index links --

    def _tree_meta(self, sha: str) -> dict[str, Any]:
        trees = self.load_index().setdefault("trees", {})
        entry = trees.get(sha)
        if entry is None:
            entry = {"describe": "", "note": "", "links": []}
            trees[sha] = entry
            return entry
        entry.setdefault("describe", "")
        entry.setdefault("note", "")
        entry.setdefault("links", [])
        return entry

    def _entries_from_raw(self, raw: Any) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw or []:
            if isinstance(item, str):
                path, note = item, ""
            elif isinstance(item, dict) and item.get("path"):
                path = str(item["path"])
                note = str(item.get("note") or "").strip()
            else:
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append({"path": path, "note": note})
        return out

    def _link_entries(self, sha: str) -> list[dict[str, str]]:
        """Normalize links to [{path, note}, ...] and migrate a tree-level note."""
        meta = self._tree_meta(sha)
        entries = self._entries_from_raw(meta.get("links"))
        raw = meta.get("links") or []
        tree_note = (meta.get("note") or "").strip()
        changed = any(not isinstance(x, dict) for x in raw) or len(raw) != len(entries)
        if tree_note:
            if len(entries) == 1 and not entries[0]["note"]:
                entries[0]["note"] = tree_note
            meta["note"] = ""
            changed = True
        if changed:
            meta["links"] = entries
            self.save_index()
        return entries

    def _link_list(self, sha: str) -> list[str]:
        return [e["path"] for e in self._link_entries(sha)]

    def set_note(self, sha: str, note: str, *, link: Path) -> None:
        key = str(link)
        entries = self._link_entries(sha)
        text = note.strip()
        for entry in entries:
            if entry["path"] == key:
                entry["note"] = text
                break
        else:
            entries.append({"path": key, "note": text})
        meta = self._tree_meta(sha)
        meta["links"] = entries
        meta["note"] = ""
        self.save_index()

    def register_link(self, sha: str, link: Path) -> None:
        key = str(link)
        idx = self.load_index()
        trees = idx.setdefault("trees", {})
        for other, meta in trees.items():
            if other == sha:
                continue
            entries = self._entries_from_raw(meta.get("links"))
            kept = [e for e in entries if e["path"] != key]
            if len(kept) != len(entries):
                meta["links"] = kept
        entries = self._link_entries(sha)
        if not any(e["path"] == key for e in entries):
            entries.append({"path": key, "note": ""})
            self._tree_meta(sha)["links"] = entries
        dest = self.tree_path(sha)
        if dest.is_dir():
            self._remember_short_describe(sha, dest)
        self.save_index()

    def unregister_stale_links(self, sha: str) -> None:
        entries = self._link_entries(sha)
        tree = self.tree_path(sha).resolve()
        keep: list[dict[str, str]] = []
        for entry in entries:
            p = Path(entry["path"])
            if not p.is_symlink():
                continue
            try:
                if p.resolve() == tree:
                    keep.append(entry)
            except OSError:
                continue
        self._tree_meta(sha)["links"] = keep
        self.save_index()

    # -- link ./esp-idf --

    def assert_esp_idf_slot(self, project_dir: Path) -> Path:
        """Refuse before any fetch if cwd/esp-idf is a real directory."""
        project_dir = project_dir.expanduser()
        if not project_dir.is_dir():
            _die(f"not a directory: {project_dir}")
        dest = project_dir / LINK_NAME
        if dest.is_symlink():
            return dest
        if _lexists(dest):
            _die(
                f"{dest} exists and is not a symlink. "
                f"Move it aside manually, then re-run get "
                f"(existing clones are never replaced)."
            )
        return dest

    def link_project(self, sha: str, project_dir: Path, *, clean: bool,
                     jobs: int) -> Path:
        dest = self.assert_esp_idf_slot(project_dir)
        tree = self.ensure_tree(sha, clean=clean, jobs=jobs).resolve()

        if dest.is_symlink():
            try:
                current = dest.resolve()
            except OSError:
                current = None
            if current == tree and not clean:
                _info(f"already linked {dest} -> {tree}")
                self.register_link(sha, dest)
                return dest

        tmp = dest.with_name(dest.name + ".idf-store-new")
        if _lexists(tmp):
            tmp.unlink()
        os.symlink(str(tree), str(tmp), target_is_directory=True)
        os.replace(str(tmp), str(dest))
        self.register_link(sha, dest)
        _info(f"linked {dest} -> {tree}")
        return dest

    # -- drop / gc --

    def drop(self, ref: str) -> None:
        key = ref.strip().lower()
        sha = self._match_installed(key) if HEX.match(key) else None
        if sha is None:
            _die(f"no installed tree matches {ref!r}")
        self.unregister_stale_links(sha)
        live = self._link_list(sha)
        if live:
            _die(
                f"refusing to drop {sha}: {len(live)} symlink(s) still point here:\n  "
                + "\n  ".join(live)
            )
        dest = self.tree_path(sha)
        _info(f"removing {dest}")
        shutil.rmtree(dest)
        trees = self.load_index().setdefault("trees", {})
        trees.pop(sha, None)
        self.save_index()

    def gc(self) -> None:
        bares = [self.objects]
        if self.modules.is_dir():
            bares.extend(
                p for p in self.modules.iterdir()
                if p.is_dir() and (p / "HEAD").exists()
            )
        for bare in bares:
            _info(f"git gc {bare}")
            git(["gc"], cwd=bare)

    # -- doctor / status --

    def _iter_gitdirs(self, tree: Path) -> Iterator[Path]:
        top = tree / ".git"
        if top.is_dir():
            yield top
            modules = top / "modules"
            if modules.is_dir():
                for root, dirs, files in os.walk(modules, followlinks=False):
                    if "HEAD" in files:
                        yield Path(root)
                        # still walk nested modules
        elif top.is_file():
            text = top.read_text().strip()
            if text.startswith("gitdir:"):
                gd = Path(text.split(":", 1)[1].strip())
                if not gd.is_absolute():
                    gd = (tree / gd).resolve()
                yield gd

    def doctor(self) -> int:
        errors = 0
        warnings = 0
        if not self.initialized():
            _die(f"store not initialized: {self.root} (run idf-store.py init)")

        self._migrate_tree_names()
        root = self.root.resolve()
        for sha in self.installed_shas():
            dest = self.tree_path(sha)
            head = git_out(["rev-parse", "HEAD"], cwd=dest).lower()
            if head != sha:
                _error(f"{dest}: HEAD is {head}, expected {sha}")
                errors += 1
            if not head.startswith(dest.name.lower()):
                _error(
                    f"{dest}: directory name is not a prefix of HEAD {head}"
                )
                errors += 1
            for gd in self._iter_gitdirs(dest):
                alt = gd / "objects" / "info" / "alternates"
                if not alt.is_file():
                    continue
                for line in alt.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    target = Path(line)
                    if not target.is_absolute():
                        target = (gd / "objects" / "info" / line)
                    try:
                        resolved = target.resolve()
                    except OSError:
                        _error(f"{gd}: broken alternate {line}")
                        errors += 1
                        continue
                    if not str(resolved).startswith(str(root) + os.sep):
                        _error(f"{gd}: alternate outside store: {resolved}")
                        errors += 1
                    elif not resolved.exists():
                        _error(f"{gd}: alternate missing: {resolved}")
                        errors += 1
            for line in self._check_submodule_status(dest, as_error=False):
                if line.startswith("-"):
                    _error(f"{dest}: uninitialized submodule: {line}")
                    errors += 1
                else:
                    _warn(f"{dest}: {line}")
                    warnings += 1

        idx = self.load_index()
        for sha, meta in (idx.get("trees") or {}).items():
            dest = self.tree_path(sha)
            for entry in self._entries_from_raw(meta.get("links")):
                p = Path(entry["path"])
                if not p.is_symlink():
                    if _lexists(p):
                        _error(f"recorded path is not a symlink: {p}")
                        errors += 1
                    else:
                        _warn(f"stale link record: {p}")
                        warnings += 1
                    continue
                try:
                    target = p.resolve()
                except OSError:
                    _error(f"broken symlink: {p}")
                    errors += 1
                    continue
                if dest.is_dir() and target != dest.resolve():
                    _error(f"{p} -> {target}, expected {dest}")
                    errors += 1

        if errors:
            _error(f"doctor: {errors} error(s), {warnings} warning(s)")
            return 1
        if warnings:
            _warn(f"doctor: 0 errors, {warnings} warning(s)")
        else:
            _info("doctor: ok")
        return 0

    def status(self) -> None:
        if not self.initialized():
            _die(f"store not initialized: {self.root} (run idf-store.py init)")
        self._migrate_tree_names()
        print(f"{_c('2', 'store:')} {self.root}")
        print(
            f"{_c('2', 'objects.git:')} "
            f"{_c('33', _human_size(_du_bytes(self.objects)))}"
        )
        print(
            f"{_c('2', 'modules:')} "
            f"{_c('33', _human_size(_du_bytes(self.modules)))}"
        )
        shas = self.installed_shas()
        if not shas:
            print(f"{_c('2', 'trees:')} {_c('2', '(none)')}")
            return
        print(_c("1", "trees:"))
        current = self._current_sha(Path.cwd())
        self._print_trees(shas, numbered=False, current=current, sizes=True)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(store: Store, _args: argparse.Namespace) -> int:
    store.root.mkdir(parents=True, exist_ok=True)
    store.modules.mkdir(exist_ok=True)
    store.trees.mkdir(exist_ok=True)
    created = not store.config_path.is_file()
    if created:
        store._cfg = {
            "version": STORE_VERSION,
            "remotes": list(DEFAULT_REMOTES),
        }
        store.save_config()
    else:
        store.load_config()
    if not store.index_path.is_file():
        store._idx = {"version": STORE_VERSION, "trees": {}}
        store.save_index()
    else:
        store.load_index()
    store.ensure_objects()
    if created:
        _info(f"initialized {store.root}")
    else:
        _info(f"already initialized {store.root}")
    return 0


def cmd_get(store: Store, args: argparse.Namespace) -> int:
    if not store.initialized():
        _die(f"store not initialized: {store.root} (run idf-store.py init)")
    project = Path(args.dir).expanduser().resolve() if args.dir else Path.cwd()
    store.assert_esp_idf_slot(project)
    ref = (args.ref or "").strip()
    if ref:
        sha = store.resolve_super_ref(ref)
        _info(f"resolved {ref} -> {sha}")
    else:
        sha = store.pick_installed(project)
        _info(f"selected {sha}")
    dest = store.link_project(sha, project, clean=args.clean, jobs=args.jobs)
    store.ensure_version(sha)
    tree = store.tree_path(sha)
    short, version = store._rev_parts(tree, sha)
    label = _c("1;36", short)
    if version:
        label += " " + _c("1;33", version)
    _info(label)
    if args.note is not None:
        store.set_note(sha, args.note, link=dest)
        text = (args.note or "").strip()
        if text:
            _info(f"note: {text}")
        else:
            _info("note cleared")
    return 0


def cmd_clean(store: Store, args: argparse.Namespace) -> int:
    if not store.initialized():
        _die(f"store not initialized: {store.root} (run idf-store.py init)")
    ref = (args.ref or "").strip()
    if ref:
        sha, dest = store.resolve_installed_ref(ref)
    else:
        link = Path.cwd() / LINK_NAME
        if not link.exists():
            _die(f"no {LINK_NAME} directory: {link}")
        if not link.is_dir():
            _die(f"not a directory: {link}")
        try:
            repo = link.resolve()
        except OSError:
            _die(f"no {LINK_NAME} directory: {link}")
        found = store._pinned_sha_at(repo)
        if found is None:
            _die(f"{link} is not an idf-store tree")
        sha, dest = found, repo
    _warn(
        "clean will discard local changes (including "
        f"submodules) in shared tree {dest}"
    )
    store._clean_tree(dest, sha, jobs=args.jobs)
    _info(f"cleaned {dest}")
    return 0


def cmd_status(store: Store, _args: argparse.Namespace) -> int:
    store.status()
    return 0


def cmd_drop(store: Store, args: argparse.Namespace) -> int:
    if not store.initialized():
        _die(f"store not initialized: {store.root} (run idf-store.py init)")
    store.drop(args.ref)
    return 0


def cmd_gc(store: Store, _args: argparse.Namespace) -> int:
    if not store.initialized():
        _die(f"store not initialized: {store.root} (run idf-store.py init)")
    store.gc()
    return 0


def cmd_doctor(store: Store, _args: argparse.Namespace) -> int:
    return store.doctor()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="idf-store.py",
        description="Shallow shared ESP-IDF store: one working tree per "
                    "commit, project ./esp-idf is a symlink.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
store layout (default ~/esp/idf, override with --store or IDF_STORE):
  objects.git          shared superproject objects (shallow, by SHA)
  modules/<name>.git   shared submodule objects (shallow, by SHA)
  trees/<8-char-sha>   working tree (prefix lengthens on collision)

examples:
  %(prog)s init
  %(prog)s get    // pick an installed tree
  %(prog)s get 296b6eab
  %(prog)s get v5.4.4 --note custom
  %(prog)s get v5.4.4 --clean
  %(prog)s clean  // clean ./esp-idf
  %(prog)s clean v5.4.4
  %(prog)s clean 296b6eab
  %(prog)s status
  %(prog)s doctor
  %(prog)s drop 296b6eab
""",
    )
    p.add_argument(
        "--store",
        default=os.environ.get("IDF_STORE", str(DEFAULT_STORE)),
        metavar="PATH",
        help="store root (default: env IDF_STORE or ~/esp/idf)",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p_init = sub.add_parser("init", help="Create an empty store")
    p_init.set_defaults(func=cmd_init)

    p_get = sub.add_parser(
        "get",
        help="Install trees/<8-char-sha> if needed and symlink ./esp-idf",
    )
    p_get.add_argument(
        "ref",
        nargs="?",
        default=None,
        help="Commit SHA (full or unique prefix), tag, or branch. "
             "Omit to pick an installed tree interactively",
    )
    p_get.add_argument(
        "--clean",
        action="store_true",
        help="Discard local changes in the shared tree, including submodules",
    )
    p_get.add_argument(
        "--dir",
        default=None,
        metavar="PATH",
        help="Project directory to place esp-idf in (default: cwd)",
    )
    p_get.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        metavar="N",
        help=f"parallel submodule clones (default: {DEFAULT_JOBS})",
    )
    p_get.add_argument(
        "--note",
        default=None,
        metavar="TEXT",
        help="Per-project label shown next to this ./esp-idf link in status; "
             "omit to leave unchanged, pass empty string to clear",
    )
    p_get.set_defaults(func=cmd_get)

    p_clean = sub.add_parser(
        "clean",
        help="Reset an installed esp-idf tree to its commit, including "
             "submodules",
    )
    p_clean.add_argument(
        "ref",
        nargs="?",
        default=None,
        help="Commit SHA (full or unique prefix), tag, or branch already "
             "installed in the store. Omit to clean ./esp-idf in the "
             "current directory",
    )
    p_clean.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        metavar="N",
        help=f"parallel submodule resets (default: {DEFAULT_JOBS})",
    )
    p_clean.set_defaults(func=cmd_clean)

    p_status = sub.add_parser(
        "status",
        help="Show installed commits, versions, sizes, and symlinks",
    )
    p_status.set_defaults(func=cmd_status)
    p_list = sub.add_parser("list", help="Alias for status")
    p_list.set_defaults(func=cmd_status)

    p_drop = sub.add_parser(
        "drop", help="Delete a tree that has no remaining symlinks"
    )
    p_drop.add_argument("ref", help="Installed SHA (unique prefix OK)")
    p_drop.set_defaults(func=cmd_drop)

    p_gc = sub.add_parser("gc", help="git gc on objects.git and module caches")
    p_gc.set_defaults(func=cmd_gc)

    p_doc = sub.add_parser(
        "doctor",
        help="Check HEAD pins, symlinks, alternates, submodule init",
    )
    p_doc.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(Path(args.store))
    try:
        with store:
            return int(args.func(store, args))
    except RuntimeError as exc:
        _die(str(exc))
    except KeyboardInterrupt:
        _error("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
