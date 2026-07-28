#!/usr/bin/env python3
"""
Download esp-at firmware from GitHub Actions and optionally flash a device.

Standalone (Python 3 stdlib only). Flashing needs esptool / esptool.py on PATH.

Prerequisites:
  1. GitHub PAT (repo + workflow). File: ~/.esptk/github_oauth_token
     (or ~/.github_oauth_token), or env GITHUB_TOKEN / GH_TOKEN.
     Fine-grained tokens: lifetime <= 366 days (espressif org policy).
  2. For flashing: pip install esptool (or esptool.py on PATH).

Examples:
  at-download-gh.py
      Interactive: pick a module from the latest master build, save firmware to disk.
  at-download-gh.py -p /dev/ttyUSB0
      Download from master, then flash to the given serial port.
  at-download-gh.py -p 0 -m
      Same as above (port /dev/ttyUSB0), but keep zip/bin in memory only.
  at-download-gh.py -B release/v2.3.0.0_esp8266
      Use the latest successful build on another branch instead of master.
  at-download-gh.py -u https://github.com/espressif/esp-at/actions/runs/28586915579
      Download from a specific GitHub Actions run URL (skip branch lookup).
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, NoReturn

REPO = "espressif/esp-at"
API_BASE = f"https://api.github.com/repos/{REPO}"
DEFAULT_BRANCH = "master"
DEFAULT_BAUD = 921600
DEFAULT_TOKEN_FILES = (
    "~/.esptk/github_oauth_token",
    "~/.github_oauth_token",
)
GITHUB_ACCEPT = "application/vnd.github+json"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "at-download-gh"
AT_BUILD_WORKFLOW = "Build ESP-AT Project"
CHUNK_SIZE = 1024 * 1024
PROGRESS_WIDTH = 30

MERGE_BRANCH_RE = re.compile(r"^Merge branch .+ into .+", re.IGNORECASE)
RUN_URL_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/actions/runs/(\d+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)


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
# Token / port
# ---------------------------------------------------------------------------

def load_token(token_file: str | None) -> tuple[str, str]:
    """Return (token, source_description)."""
    for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
        env = os.environ.get(env_name, "").strip()
        if env:
            return env, f"env:{env_name}"

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

    _die(
        "No GitHub token found. Set GITHUB_TOKEN / GH_TOKEN, or create "
        f"{DEFAULT_TOKEN_FILES[0]} (PAT with repo + workflow scopes). See --help."
    )


def describe_token_shape(token: str) -> str:
    if token.startswith("github_pat_"):
        return "fine-grained PAT"
    if token.startswith("ghp_"):
        return "classic PAT"
    if token.startswith(("gho_", "ghu_", "ghs_")):
        return "GitHub OAuth/App token"
    if len(token) == 40 and all(c in "0123456789abcdefABCDEF" for c in token):
        return "legacy classic PAT (40-hex)"
    return "unknown token format"


def format_github_api_error(status: int, body: str, url: str) -> str:
    text = body.strip()
    lower = text.lower()
    message = ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            message = str(data.get("message") or "")
    except json.JSONDecodeError:
        message = text[:500]
    detail = message or text[:300]

    if status == 401:
        return (
            "GitHub authentication failed (HTTP 401).\n"
            "  Token is missing, expired, revoked, or malformed.\n"
            "  Create a PAT: https://github.com/settings/tokens\n"
            "  Classic: scopes 'repo' + 'workflow'. "
            "Fine-grained: lifetime <= 366 days for espressif.\n"
            f"  Detail: {detail}"
        )
    if status == 403:
        if "fine-grained" in lower and ("366" in lower or "lifetime" in lower):
            hint = ""
            for part in message.replace(",", " ").split():
                if part.startswith("https://github.com/settings/personal-access-tokens/"):
                    hint = part.rstrip(".'\"")
                    break
            lines = [
                "GitHub rejected this fine-grained PAT (HTTP 403):",
                "  espressif forbids fine-grained tokens with lifetime > 366 days.",
                "  Fix: Expiration <= 366 days, or use a classic PAT.",
                f"  Token settings: {hint or 'https://github.com/settings/personal-access-tokens'}",
            ]
            if message:
                lines.append(f"  Detail: {message}")
            return "\n".join(lines)
        if "sso" in lower or "saml" in lower:
            return (
                "GitHub SSO authorization required (HTTP 403).\n"
                "  Authorize this PAT for the espressif organization, then retry.\n"
                f"  Detail: {detail}"
            )
        if "rate limit" in lower:
            return f"GitHub API rate limit exceeded (HTTP 403).\n  Detail: {detail}"
        return f"GitHub API forbidden (HTTP 403) for {url}\n  Detail: {detail}"
    return f"GitHub API HTTP {status} for {url}: {message or text[:500]}"


def resolve_port(port: str) -> str:
    return f"/dev/ttyUSB{port}" if port.isdigit() else port


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Azure blob rejects GitHub Authorization headers forwarded on 302."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        if (urllib.parse.urlparse(req.full_url).netloc
                != urllib.parse.urlparse(newurl).netloc):
            for h in ("Authorization", "authorization"):
                if new_req.has_header(h):
                    new_req.remove_header(h)
        return new_req


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Accept": GITHUB_ACCEPT,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": USER_AGENT,
    }


def _curl_header_args(token: str) -> list[str]:
    h = _auth_headers(token)
    return [
        "-H", f"Authorization: {h['Authorization']}",
        "-H", f"Accept: {h['Accept']}",
        "-H", f"X-GitHub-Api-Version: {h['X-GitHub-Api-Version']}",
        "-H", f"User-Agent: {h['User-Agent']}",
    ]


def github_request(
    url: str,
    token: str,
    *,
    die_on_error: bool = True,
) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, method="GET", headers=_auth_headers(token))
    opener = urllib.request.build_opener(_StripAuthOnRedirect)
    try:
        with opener.open(req, timeout=120) as resp:
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.getcode() or 200, body, headers
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        if die_on_error:
            _die(format_github_api_error(e.code, err, url))
        return e.code, err.encode("utf-8"), {}
    except urllib.error.URLError as e:
        if die_on_error:
            _die(f"GitHub API request failed: {e}")
        return 0, str(e).encode("utf-8"), {}


def github_json(url: str, token: str) -> Any:
    _, body, _ = github_request(url, token)
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON from {url}: {e}")


def check_token(token: str, source: str) -> None:
    shape = describe_token_shape(token)
    _info(f"Token source: {source} ({shape})")
    if token.startswith("github_pat_"):
        _info("Note: espressif requires fine-grained PAT lifetime <= 366 days.")
    elif token.startswith("ghp_") and len(token) < 50:
        _warn("Classic PAT looks unusually short; regenerate if auth fails.")

    code, body, headers = github_request(
        "https://api.github.com/user", token, die_on_error=False
    )
    if code != 200:
        _die(format_github_api_error(
            code, body.decode("utf-8", errors="replace"), "https://api.github.com/user"
        ))
    try:
        user = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        _die("Token check returned invalid JSON from /user")
    _info(f"Token OK — authenticated as '{user.get('login') or '?'}'")

    scopes = headers.get("x-oauth-scopes", "")
    if scopes:
        have = {s.strip() for s in scopes.split(",") if s.strip()}
        missing = {"repo", "workflow"} - have
        if missing and not token.startswith("github_pat_"):
            _warn(f"Token may lack scopes {sorted(missing)}.")


# ---------------------------------------------------------------------------
# Workflow run / artifacts
# ---------------------------------------------------------------------------

def parse_run_url(url_or_id: str) -> int:
    s = url_or_id.strip()
    m = RUN_URL_RE.match(s)
    if m:
        owner, repo, run_id = m.group(1), m.group(2), int(m.group(3))
        full = f"{owner}/{repo}"
        if full.lower() != REPO.lower():
            _warn(f"URL points to {full}, script targets {REPO}; continuing.")
        return run_id
    if s.isdigit():
        return int(s)
    _die(
        f"Invalid --url value: {url_or_id!r}\n"
        f"  Expected like https://github.com/{REPO}/actions/runs/28586915579"
    )


def get_workflow_run(token: str, run_id: int) -> dict[str, Any]:
    return github_json(f"{API_BASE}/actions/runs/{run_id}", token)


def is_at_firmware_run(run: dict[str, Any], branch: str) -> bool:
    """
    - branch contains 'esp8266': Build ESP-AT Project (name/title) or Merge branch...
    - otherwise: only Merge branch ... into ... (usual Build ESP-AT Project titles)
    """
    name = (run.get("name") or "").strip()
    title = (run.get("display_title") or "").strip()
    is_merge = bool(MERGE_BRANCH_RE.match(name) or MERGE_BRANCH_RE.match(title))
    is_build = name == AT_BUILD_WORKFLOW or title == AT_BUILD_WORKFLOW
    if "esp8266" in branch.lower():
        return is_merge or is_build
    return is_merge


def find_latest_successful_run(token: str, branch: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"branch": branch, "status": "success", "per_page": 10}
    )
    data = github_json(f"{API_BASE}/actions/runs?{query}", token)
    for run in data.get("workflow_runs") or []:
        if is_at_firmware_run(run, branch):
            return run
    want = (
        f"'{AT_BUILD_WORKFLOW}' or 'Merge branch ... into ...'"
        if "esp8266" in branch.lower()
        else "'Merge branch ... into ...'"
    )
    _die(
        f"No matching GitHub Actions run on branch '{branch}' (want {want}). "
        "Push a build or choose another --branch."
    )


def list_artifacts_for_run(token: str, run_id: int) -> dict[str, dict[str, Any]]:
    """artifact_name -> newest non-expired artifact object."""
    by_name: dict[str, dict[str, Any]] = {}
    page = 1
    while page <= 5:
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        data = github_json(
            f"{API_BASE}/actions/runs/{run_id}/artifacts?{query}", token
        )
        arts = data.get("artifacts") or []
        if not arts:
            break
        for art in arts:
            if art.get("expired"):
                continue
            name = art.get("name") or ""
            if not name:
                continue
            prev = by_name.get(name)
            if prev is None or int(art.get("id", 0)) > int(prev.get("id", 0)):
                by_name[name] = art
        if len(arts) < 100:
            break
        page += 1
    return by_name


def select_artifact(
    artifact_names: list[str],
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not artifact_names:
        _die("No artifacts found for this workflow run.")

    print("Available artifacts:")
    for i, n in enumerate(artifact_names):
        print(f"  {i}: {n}")
    try:
        raw = input(
            f"Select artifact by index "
            f"(0-{len(artifact_names) - 1}, default 0): "
        ).strip()
    except EOFError:
        raw = ""
    except KeyboardInterrupt:
        _interrupted()
    if not raw:
        idx = 0
    else:
        try:
            idx = int(raw)
        except ValueError:
            _die(f"Invalid index: {raw!r}")
    if not 0 <= idx < len(artifact_names):
        _die(f"Index out of range: {idx}")
    return artifacts[artifact_names[idx]]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _render_progress(downloaded: int, total: int | None, started_at: float) -> None:
    if not sys.stderr.isatty():
        return
    elapsed = max(time.monotonic() - started_at, 1e-6)
    speed_s = f"  {_format_bytes(downloaded / elapsed)}/s"
    if total and total > 0:
        ratio = min(downloaded / total, 1.0)
        filled = int(PROGRESS_WIDTH * ratio)
        bar = "#" * filled + "-" * (PROGRESS_WIDTH - filled)
        line = (
            f"\r  [{bar}] {ratio * 100:5.1f}%  "
            f"{_format_bytes(downloaded)}/{_format_bytes(total)}{speed_s}"
        )
    else:
        line = f"\r  downloaded {_format_bytes(downloaded)}{speed_s}"
    sys.stderr.write(line + "\033[K")
    sys.stderr.flush()


def _end_progress_line() -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\n")
        sys.stderr.flush()


def _validate_zip_bytes(body: bytes, ctype: str = "") -> None:
    if body[:2] == b"PK" or "zip" in ctype or "octet-stream" in ctype:
        return
    preview = body[:300].decode("utf-8", errors="replace")
    if "InvalidAuthenticationInfo" in preview or "AuthenticationErrorDetail" in preview:
        _die(
            "Artifact download auth failed (GitHub token forwarded to storage). "
            f"Preview: {preview[:200]}"
        )
    _die(f"Download did not return a zip file. Preview: {preview}")


def _log_download_done(size: int, started_at: float) -> None:
    elapsed = max(time.monotonic() - started_at, 1e-6)
    _info(
        f"Downloaded {_format_bytes(size)} "
        f"in {elapsed:.1f}s ({_format_bytes(size / elapsed)}/s)."
    )


def _download_with_curl(
    archive_url: str, token: str, total: int | None
) -> bytes | None:
    curl = shutil.which("curl")
    if not curl:
        return None

    cmd = [
        curl, "-fsSL",
        "--connect-timeout", "30",
        "--max-time", "600",
        *_curl_header_args(token),
        archive_url,
    ]
    _info("Downloading artifact zip (curl)...")
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as e:
        _warn(f"curl failed to start ({e}); falling back to urllib.")
        return None

    assert proc.stdout is not None
    chunks: list[bytes] = []
    downloaded = 0
    try:
        while True:
            chunk = proc.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
            downloaded += len(chunk)
            _render_progress(downloaded, total, started)
        rc = proc.wait()
        err = proc.stderr.read() if proc.stderr else b""
    except KeyboardInterrupt:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        _interrupted()

    _end_progress_line()
    if rc != 0:
        err_s = err.decode("utf-8", errors="replace").strip()
        _warn(
            f"curl exited {rc}"
            + (f": {err_s}" if err_s else "")
            + "; falling back to urllib."
        )
        return None

    body = b"".join(chunks)
    if not body:
        _warn("curl returned empty body; falling back to urllib.")
        return None
    _validate_zip_bytes(body)
    _log_download_done(len(body), started)
    return body


def _download_with_urllib(
    archive_url: str, token: str, total: int | None
) -> bytes:
    _info("Downloading artifact zip (urllib)...")
    req = urllib.request.Request(
        archive_url, method="GET", headers=_auth_headers(token)
    )
    opener = urllib.request.build_opener(_StripAuthOnRedirect)
    started = time.monotonic()
    try:
        with opener.open(req, timeout=300) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            cl = headers.get("content-length")
            if cl and cl.isdigit():
                total = int(cl)
            chunks: list[bytes] = []
            downloaded = 0
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
                downloaded += len(chunk)
                _render_progress(downloaded, total, started)
            body = b"".join(chunks)
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        _die(format_github_api_error(e.code, err, archive_url))
    except urllib.error.URLError as e:
        _die(f"Artifact download failed: {e}")
    except KeyboardInterrupt:
        _interrupted()

    _end_progress_line()
    _validate_zip_bytes(body, headers.get("content-type", ""))
    _log_download_done(len(body), started)
    return body


def download_artifact_zip(
    token: str, archive_url: str, size_hint: int | None = None
) -> bytes:
    """size_hint: artifact size_in_bytes from GitHub API (for progress %)."""
    total = size_hint if size_hint and size_hint > 0 else None
    return _download_with_curl(archive_url, token, total) or _download_with_urllib(
        archive_url, token, total
    )


# ---------------------------------------------------------------------------
# Factory bin
# ---------------------------------------------------------------------------

def _is_param_path(name: str) -> bool:
    return "param" in Path(name.replace("\\", "/")).name.lower()


def _under_build_factory(path: str) -> bool:
    p = path.replace("\\", "/")
    return p.startswith("build/factory/") or "/build/factory/" in p


def pick_factory_member(names: list[str]) -> str:
    candidates = [
        n.replace("\\", "/")
        for n in names
        if not n.endswith("/") and not _is_param_path(n)
    ]

    def match(pred) -> list[str]:
        return sorted(n for n in candidates if pred(n))

    unfilled = match(
        lambda n: _under_build_factory(n)
        and fnmatch.fnmatch(Path(n).name, "factory_*_unfilled.bin")
    )
    if unfilled:
        return unfilled[0]

    factory = match(
        lambda n: _under_build_factory(n)
        and fnmatch.fnmatch(Path(n).name, "factory_*.bin")
        and "unfilled" not in Path(n).name.lower()
    )
    if factory:
        return factory[0]

    fallback = match(lambda n: fnmatch.fnmatch(Path(n).name, "factory*.bin"))
    if fallback:
        return fallback[0]

    _die(
        "No factory firmware found in artifact. Expected "
        "build/factory/factory_*_unfilled.bin or build/factory/factory_*.bin"
    )


def extract_factory_bin(zip_bytes: bytes) -> tuple[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        member = pick_factory_member(zf.namelist())
        _info(f"Using firmware member: {member}")
        return member, zf.read(member)


# ---------------------------------------------------------------------------
# Save / flash
# ---------------------------------------------------------------------------

def save_to_disk(
    zip_bytes: bytes,
    member: str,
    artifact_name: str,
    artifact_id: int,
) -> Path:
    dst_dir = Path(f"{artifact_name}_{artifact_id}")
    dst_zip = Path(f"{artifact_name}_{artifact_id}.zip")
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    if dst_zip.exists():
        dst_zip.unlink()

    dst_zip.write_bytes(zip_bytes)
    _info(f"Saved zip: {dst_zip.resolve()}")
    with zipfile.ZipFile(dst_zip) as zf:
        zf.extractall(dst_dir)

    out_bin = (dst_dir / member).resolve()
    if not out_bin.is_file():
        _die(f"Expected firmware missing after extract: {out_bin}")
    _info(f"Firmware saved: {out_bin}")
    return out_bin


def _try_import_esptool() -> bool:
    try:
        import esptool  # noqa: F401
        return True
    except ImportError:
        pass
    for name in ("esptool", "esptool.py"):
        which = shutil.which(name)
        if not which:
            continue
        root = str(Path(which).resolve().parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            import esptool  # noqa: F401
            return True
        except ImportError:
            continue
    return False


def find_esptool_cmd() -> list[str]:
    for name in ("esptool", "esptool.py"):
        path = shutil.which(name)
        if path:
            return [path]
    if _try_import_esptool():
        return [sys.executable, "-m", "esptool"]
    _die(
        "esptool not found. Install with 'pip install esptool' or put "
        "esptool / esptool.py on PATH."
    )


def flash_from_path(port: str, baud: int, bin_path: Path | str) -> None:
    cmd = find_esptool_cmd() + [
        "--port", port,
        "--baud", str(baud),
        "write_flash",
        "0x0",
        str(bin_path),
    ]
    _info("Running: " + " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        _die(f"Flash failed (exit {e.returncode})")
    _info("Firmware successfully flashed to chip.")


def _flash_via_esptool_api(port: str, baud: int, bin_data: bytes) -> bool:
    if not _try_import_esptool():
        return False
    try:
        from esptool.cmds import detect_chip, run_stub, write_flash

        _info(
            f"Flashing {_format_bytes(len(bin_data))} from memory via esptool API "
            f"(port={port}, baud={baud})..."
        )
        esp = detect_chip(port=port, baud=115200)
        try:
            esp = run_stub(esp)
            if baud and baud != 115200:
                try:
                    esp.change_baud(baud)
                except Exception:
                    pass
            write_flash(esp, [(0x0, bin_data)])
        finally:
            for closer in (
                lambda: esp.hard_reset(),
                lambda: esp._port.close(),
            ):
                try:
                    closer()
                except Exception:
                    pass
        _info("Firmware successfully flashed to chip (memory mode).")
        return True
    except Exception as e:
        _warn(f"esptool API flash failed ({e}); trying in-memory fd / CLI.")
        return False


def _flash_via_memfd(port: str, baud: int, bin_data: bytes) -> bool:
    if not hasattr(os, "memfd_create"):
        return False
    fd: int | None = None
    try:
        fd = os.memfd_create("at-fw.bin", 0)
        view = memoryview(bin_data)
        off = 0
        while off < len(view):
            off += os.write(fd, view[off:])
        os.lseek(fd, 0, os.SEEK_SET)
        _info(
            f"Flashing via in-memory fd "
            f"({_format_bytes(len(bin_data))}, no disk)..."
        )
        flash_from_path(port, baud, f"/proc/self/fd/{fd}")
        return True
    except Exception as e:
        _warn(f"memfd flash failed ({e})")
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def flash_from_memory(port: str, baud: int, bin_data: bytes, hint_name: str) -> None:
    """Prefer API bytes, then Linux memfd; temp file only as last resort."""
    if _flash_via_esptool_api(port, baud, bin_data):
        return
    if _flash_via_memfd(port, baud, bin_data):
        return

    _warn("--memory: short-lived temp file (no esptool API / memfd).")
    suffix = Path(hint_name).suffix or ".bin"
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="at-fw-", suffix=suffix, delete=False
        ) as tf:
            tf.write(bin_data)
            tmp_path = tf.name
        flash_from_path(port, baud, Path(tmp_path))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                _warn(f"Could not remove temp file: {tmp_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="at-download-gh",
        description=(
            "Download the latest esp-at firmware from GitHub Actions "
            f"(default branch: {DEFAULT_BRANCH}), optionally flash a device."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Token:\n"
            "  https://github.com/settings/tokens  (scopes: repo, workflow)\n"
            f"  Save to {DEFAULT_TOKEN_FILES[0]} or set GITHUB_TOKEN / GH_TOKEN.\n"
            "  Fine-grained PAT lifetime must be <= 366 days for espressif.\n"
            "\n"
            "Examples:\n"
            "  at-download-gh.py\n"
            "      Interactive: pick a module from the latest master build,\n"
            "      save firmware zip/bin to the current directory.\n"
            "\n"
            "  at-download-gh.py -p /dev/ttyUSB0\n"
            "      Download from master, then flash to that serial port.\n"
            "\n"
            "  at-download-gh.py -p 0 -m\n"
            "      Flash /dev/ttyUSB0; -p 0 is short for /dev/ttyUSB0.\n"
            "      -m keeps zip/bin in memory only (no files left on disk).\n"
            "\n"
            "  at-download-gh.py -B release/v2.3.0.0_esp8266\n"
            "      Use the latest successful build on another branch\n"
            "      instead of the default master.\n"
            "\n"
            f"  at-download-gh.py -u https://github.com/{REPO}/actions/runs/28586915579\n"
            "      Download from a specific GitHub Actions run URL\n"
            "      (skip branch lookup). You can also pass a numeric run id.\n"
        ),
    )
    p.add_argument(
        "-B", "--branch", default=DEFAULT_BRANCH,
        help=f"Branch for Actions artifacts (default: {DEFAULT_BRANCH})",
    )
    p.add_argument(
        "-p", "--port",
        help="Serial port to flash; full path or legacy digit N -> /dev/ttyUSBN",
    )
    p.add_argument(
        "-b", "--baud", type=int, default=DEFAULT_BAUD,
        help=f"Flash baud rate (default: {DEFAULT_BAUD})",
    )
    p.add_argument(
        "-m", "--memory", action="store_true",
        help="With --port: keep zip/bin in memory only; flash then discard",
    )
    p.add_argument(
        "-u", "--url", metavar="URL",
        help=f"Actions run URL or id, e.g. https://github.com/{REPO}/actions/runs/<id>",
    )
    p.add_argument(
        "-t", "--token-file", metavar="PATH",
        help="GitHub token file (overrides default search paths)",
    )
    p.add_argument("legacy_port", nargs="?", help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.memory and not (args.port or args.legacy_port):
        _die("--memory/-m requires --port/-p (or legacy USB index).")

    port = None
    if args.port:
        port = resolve_port(args.port)
    elif args.legacy_port is not None:
        port = resolve_port(args.legacy_port)

    token, token_source = load_token(args.token_file)
    check_token(token, token_source)

    if args.url:
        run_id = parse_run_url(args.url)
        _info(f"Using workflow run from --url (id={run_id})")
        run = get_workflow_run(token, run_id)
    else:
        _info(f"Using branch: {args.branch}")
        _info("Looking up latest successful GitHub Actions run...")
        run = find_latest_successful_run(token, args.branch)
        run_id = int(run["id"])

    run_url = f"https://github.com/{REPO}/actions/runs/{run_id}"
    title = run.get("display_title") or run.get("name") or "?"
    _info(f"Workflow: {run.get('name', '?')} — {title}")
    _info(f"GitHub Actions: {run_url}")
    _info(f"Created: {run.get('created_at', '?')}")

    _info("Fetching artifacts for this run...")
    artifacts = list_artifacts_for_run(token, run_id)
    artifact_names = sorted(artifacts.keys())
    _info(f"Found {len(artifact_names)} artifact(s).")

    art = select_artifact(artifact_names, artifacts)
    name = art["name"]
    art_id = int(art["id"])
    _info(f"Selected artifact: {name}")

    size_hint = art.get("size_in_bytes")
    try:
        size_hint_i = int(size_hint) if size_hint is not None else None
    except (TypeError, ValueError):
        size_hint_i = None
    zip_bytes = download_artifact_zip(
        token, art["archive_download_url"], size_hint=size_hint_i
    )
    member, bin_data = extract_factory_bin(zip_bytes)

    if args.memory and port:
        flash_from_memory(port, args.baud, bin_data, member)
        return 0

    bin_path = save_to_disk(zip_bytes, member, name, art_id)
    if port:
        flash_from_path(port, args.baud, bin_path)
    else:
        print()
        print("All done! To flash manually, run:")
        print(
            f"  esptool.py --port [PORT] --baud {args.baud} "
            f"write_flash 0x0 {bin_path}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(file=sys.stderr)
        _error("Interrupted.")
        raise SystemExit(130)
