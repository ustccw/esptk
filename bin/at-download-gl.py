#!/usr/bin/env python3
"""
Download esp-at firmware from GitLab CI (pipeline/job artifacts) and optionally
flash a device.

Standalone (Python 3 stdlib only). Flashing needs esptool / esptool.py on PATH.

Prerequisites:
  1. GitLab PAT (scopes: api, read_user, read_api). File:
     ~/.esptk/gitlab_oauth_token (or ~/.gitlab_oauth_token), or env GITLAB_TOKEN.
  2. For flashing: pip install esptool (or esptool.py on PATH).

Examples:
  at-download-gl.py
      Interactive: pick a job from the latest master pipeline, save firmware.
  at-download-gl.py -p /dev/ttyUSB0
      Download from master, then flash to the given serial port.
  at-download-gl.py -p 0 -m
      Same as above (port /dev/ttyUSB0), but keep zip/bin in memory only.
  at-download-gl.py -B release/v2.3.0.0_esp8266
      Use the latest successful pipeline on another branch instead of master.
  at-download-gl.py -u https://gitlab.espressif.cn:6688/application/esp-at/-/pipelines/352799
      Download from a specific pipeline URL (interactive job pick).
  at-download-gl.py -u https://gitlab.espressif.cn:6688/application/esp-at/-/jobs/14643605
      Download artifacts from a specific job URL (skip job pick).
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

GITLAB_BASE = "https://gitlab.espressif.cn:6688"
PROJECT_ID = 234
PROJECT_PATH = "application/esp-at"
API_BASE = f"{GITLAB_BASE}/api/v4/projects/{PROJECT_ID}"
JOB_WEB_PREFIX = f"{GITLAB_BASE}/{PROJECT_PATH}/-/jobs"
PIPELINE_WEB_PREFIX = f"{GITLAB_BASE}/{PROJECT_PATH}/-/pipelines"
DEFAULT_BRANCH = "master"
DEFAULT_BAUD = 921600
DEFAULT_TOKEN_FILES = (
    "~/.esptk/gitlab_oauth_token",
    "~/.gitlab_oauth_token",
)
USER_AGENT = "at-download-gl"
CHUNK_SIZE = 1024 * 1024
PROGRESS_WIDTH = 30

# Internal GitLab often uses a cert curl treated with --insecure.
_SSL_CTX = ssl._create_unverified_context()

PIPELINE_URL_RE = re.compile(
    r"^https?://[^/]+/.+/-/pipelines/(\d+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)
JOB_URL_RE = re.compile(
    r"^https?://[^/]+/.+/-/jobs/(\d+)/?(?:[?#].*)?$",
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

    _die(
        "No GitLab token found. Set GITLAB_TOKEN, or create "
        f"{DEFAULT_TOKEN_FILES[0]} (PAT with api + read_api scopes). See --help."
    )


def format_gitlab_api_error(status: int, body: str, url: str) -> str:
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
        return (
            "GitLab authentication failed (HTTP 401).\n"
            "  Token is missing, expired, revoked, or malformed.\n"
            f"  Create a PAT: {GITLAB_BASE}/-/user_settings/personal_access_tokens\n"
            "  Scopes: api, read_user, read_api.\n"
            f"  Detail: {detail}"
        )
    if status == 403:
        return f"GitLab API forbidden (HTTP 403) for {url}\n  Detail: {detail}"
    if status == 404:
        return f"GitLab resource not found (HTTP 404) for {url}\n  Detail: {detail}"
    return f"GitLab API HTTP {status} for {url}: {message or text[:500]}"


def resolve_port(port: str) -> str:
    return f"/dev/ttyUSB{port}" if port.isdigit() else port


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _auth_headers(token: str) -> dict[str, str]:
    return {
        "PRIVATE-TOKEN": token,
        "User-Agent": USER_AGENT,
    }


def _curl_header_args(token: str) -> list[str]:
    h = _auth_headers(token)
    return [
        "-H", f"PRIVATE-TOKEN: {h['PRIVATE-TOKEN']}",
        "-H", f"User-Agent: {h['User-Agent']}",
    ]


def gitlab_request(
    url: str,
    token: str,
    *,
    die_on_error: bool = True,
) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, method="GET", headers=_auth_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.getcode() or 200, body, headers
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        if die_on_error:
            _die(format_gitlab_api_error(e.code, err, url))
        return e.code, err.encode("utf-8"), {}
    except urllib.error.URLError as e:
        if die_on_error:
            _die(f"GitLab API request failed: {e}")
        return 0, str(e).encode("utf-8"), {}


def gitlab_json(url: str, token: str) -> Any:
    _, body, _ = gitlab_request(url, token)
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        _die(f"Invalid JSON from {url}: {e}")


def check_token(token: str, source: str) -> None:
    _info(f"Token source: {source}")
    code, body, _ = gitlab_request(
        f"{GITLAB_BASE}/api/v4/user", token, die_on_error=False
    )
    if code != 200:
        _die(format_gitlab_api_error(
            code, body.decode("utf-8", errors="replace"), f"{GITLAB_BASE}/api/v4/user"
        ))
    try:
        user = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        _die("Token check returned invalid JSON from /user")
    _info(f"Token OK — authenticated as '{user.get('username') or user.get('name') or '?'}'")


# ---------------------------------------------------------------------------
# Pipeline / jobs
# ---------------------------------------------------------------------------

def parse_url(url_or_id: str) -> tuple[str, int]:
    """
    Return ('pipeline'|'job', id).
    Bare digits are treated as a pipeline id; use a jobs URL for a job id.
    """
    s = url_or_id.strip()
    m = JOB_URL_RE.match(s)
    if m:
        return "job", int(m.group(1))
    m = PIPELINE_URL_RE.match(s)
    if m:
        return "pipeline", int(m.group(1))
    if "/jobs/" in s.lower() and s.rstrip("/").split("/")[-1].isdigit():
        return "job", int(s.rstrip("/").split("/")[-1])
    if "/pipelines/" in s.lower() and s.rstrip("/").split("/")[-1].isdigit():
        return "pipeline", int(s.rstrip("/").split("/")[-1])
    if s.isdigit():
        return "pipeline", int(s)
    _die(
        f"Invalid --url value: {url_or_id!r}\n"
        f"  Expected a pipeline URL ({PIPELINE_WEB_PREFIX}/<id>),\n"
        f"  a job URL ({JOB_WEB_PREFIX}/<id>), or a numeric pipeline id."
    )


def find_latest_successful_pipeline(token: str, branch: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"status": "success", "ref": branch, "per_page": "1", "page": "1"}
    )
    data = gitlab_json(f"{API_BASE}/pipelines?{query}", token)
    if not isinstance(data, list) or not data:
        _die(
            f"No successful GitLab pipeline on branch '{branch}'. "
            "Push a build or choose another --branch."
        )
    return data[0]


def get_pipeline(token: str, pipeline_id: int) -> dict[str, Any]:
    return gitlab_json(f"{API_BASE}/pipelines/{pipeline_id}", token)


def get_job(token: str, job_id: int) -> dict[str, Any]:
    return gitlab_json(f"{API_BASE}/jobs/{job_id}", token)


def list_jobs_for_pipeline(token: str, pipeline_id: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    page = 1
    while page <= 20:
        query = urllib.parse.urlencode({"per_page": "100", "page": str(page)})
        data = gitlab_json(
            f"{API_BASE}/pipelines/{pipeline_id}/jobs?{query}", token
        )
        if not isinstance(data, list) or not data:
            break
        jobs.extend(data)
        if len(data) < 100:
            break
        page += 1
    return jobs


def filter_firmware_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Firmware CI jobs are named with an 'esp' prefix (e.g. esp32c6_4mb_at)."""
    return [
        j for j in jobs
        if (j.get("name") or "").lower().startswith("esp")
    ]


def _job_short_commit(job: dict[str, Any]) -> str:
    commit = job.get("commit") or {}
    if isinstance(commit, dict):
        return str(commit.get("short_id") or "?")
    return "?"


def job_artifacts_size(job: dict[str, Any]) -> int | None:
    af = job.get("artifacts_file")
    if isinstance(af, dict) and af.get("size") is not None:
        try:
            size = int(af["size"])
        except (TypeError, ValueError):
            return None
        return size if size > 0 else None
    return None


def select_job(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    jobs = filter_firmware_jobs(jobs)
    if not jobs:
        _die(
            "No firmware jobs found in this pipeline "
            "(expected job names starting with 'esp')."
        )

    print()
    print(f"Found {len(jobs)} firmware job(s) (name starts with 'esp').\n")
    sep = "-" * 81
    print(sep)
    print(
        f"| {'idx':>3} | {'job name':<39} | {'state':^7} | "
        f"{'commit':^7} | {'job id':>8} |"
    )
    print(sep)
    for i, job in enumerate(jobs):
        name = (job.get("name") or "?")[:39]
        state = (job.get("status") or "?")[:7]
        commit = _job_short_commit(job)[:7]
        jid = str(job.get("id") or "?")
        print(f"| {i:3d} | {name:<39} | {state:^7} | {commit:^7} | {jid:>8} |")
    print(sep)
    print()

    try:
        raw = input(
            f"Select job by index "
            f"(0-{len(jobs) - 1}, default 0): "
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
    if not 0 <= idx < len(jobs):
        _die(f"Index out of range: {idx}")

    job = jobs[idx]
    status = job.get("status") or ""
    if status != "success":
        _die(
            f"Expect job state <success>, but "
            f"<{job.get('name')}> has state <{status}>"
        )
    return job


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
    if len(body) < 50:
        _die(f"ESP-AT firmware download failed (tiny response):\n{preview}")
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
        curl, "-fsSL", "-k",
        "--connect-timeout", "30",
        "--max-time", "600",
        *_curl_header_args(token),
        archive_url,
    ]
    _info("Downloading job artifacts zip (curl)...")
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
    _info("Downloading job artifacts zip (urllib)...")
    req = urllib.request.Request(
        archive_url, method="GET", headers=_auth_headers(token)
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=300, context=_SSL_CTX) as resp:
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
        _die(format_gitlab_api_error(e.code, err, archive_url))
    except urllib.error.URLError as e:
        _die(f"Artifact download failed: {e}")
    except KeyboardInterrupt:
        _interrupted()

    _end_progress_line()
    _validate_zip_bytes(body, headers.get("content-type", ""))
    _log_download_done(len(body), started)
    return body


def download_job_artifacts(
    token: str, job_id: int, *, size_hint: int | None = None
) -> bytes:
    url = f"{API_BASE}/jobs/{job_id}/artifacts"
    total = size_hint if size_hint and size_hint > 0 else None
    return _download_with_curl(url, token, total) or _download_with_urllib(
        url, token, total
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

def _safe_job_name(name: str) -> str:
    """Keep filesystem-friendly characters; collapse other runs to '_'."""
    cleaned = re.sub(r"[^\w.+-]+", "_", name.strip(), flags=re.ASCII)
    cleaned = cleaned.strip("._-") or "job"
    return cleaned


def _safe_keyword(keyword: str) -> str:
    """Keep filesystem-friendly characters; collapse other runs to '_'."""
    cleaned = re.sub(r"[^\w.+-]+", "_", keyword.strip(), flags=re.ASCII)
    cleaned = cleaned.strip("._-")
    if not cleaned:
        _die(f"Invalid --keyword value: {keyword!r} (empty after sanitizing)")
    return cleaned


def save_to_disk(
    zip_bytes: bytes,
    member: str,
    job_name: str,
    keyword: str | None = None,
) -> Path:
    # Original: {job_name}-{date}; with -k: insert keyword before the date.
    safe_name = _safe_job_name(job_name)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if keyword:
        base = f"{safe_name}_{_safe_keyword(keyword)}-{stamp}"
    else:
        base = f"{safe_name}-{stamp}"
    dst_dir = Path(base)
    dst_zip = Path(f"{base}.zip")
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
        # Some archives nest under a top-level dir; search like the old shell.
        matches = [
            p for p in dst_dir.rglob("factory*.bin")
            if p.is_file() and "param" not in p.name.lower()
        ]
        if not matches:
            _die(f"Expected firmware missing after extract: {out_bin}")
        out_bin = matches[0].resolve()
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


def _flash_via_esptool_api(
    port: str, baud: int, bin_data: bytes, hint_name: str
) -> bool:
    """esptool 4.x: detect_chip + esp.run_stub(); write_flash takes Namespace."""
    if not _try_import_esptool():
        return False
    try:
        from argparse import Namespace
        from esptool.cmds import detect_chip, write_flash

        _info(
            f"Flashing {_format_bytes(len(bin_data))} from memory via esptool API "
            f"(port={port}, baud={baud})..."
        )
        bio = io.BytesIO(bin_data)
        bio.name = hint_name or "firmware.bin"
        args = Namespace(
            addr_filename=[(0x0, bio)],
            compress=None,
            no_compress=False,
            no_stub=False,
            force=False,
            encrypt=False,
            encrypt_files=None,
            erase_all=False,
            flash_mode="keep",
            flash_freq="keep",
            flash_size="keep",
            spi_connection=None,
            no_progress=False,
            verify=False,
            ignore_flash_encryption_efuse_setting=False,
        )
        esp = detect_chip(port=port, baud=115200)
        try:
            if hasattr(esp, "run_stub"):
                esp = esp.run_stub()
            if baud and baud != 115200:
                try:
                    esp.change_baud(baud)
                except Exception:
                    pass
            write_flash(esp, args)
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


def _run_esptool_flash(port: str, baud: int, bin_path: Path | str) -> int:
    """Run esptool write_flash; return exit code (does not exit the process)."""
    cmd = find_esptool_cmd() + [
        "--port", port,
        "--baud", str(baud),
        "write_flash",
        "0x0",
        str(bin_path),
    ]
    _info("Running: " + " ".join(cmd))
    try:
        return subprocess.run(cmd).returncode
    except OSError as e:
        _warn(f"Failed to start esptool: {e}")
        return 127


def _flash_via_memfd(port: str, baud: int, bin_data: bytes) -> bool:
    """
    Pass firmware to esptool via memfd. Use /proc/<parent_pid>/fd/N so the
    child process can open the parent's memfd (/proc/self/fd/N is wrong there).
    """
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
        path = f"/proc/{os.getpid()}/fd/{fd}"
        _info(
            f"Flashing via in-memory fd "
            f"({_format_bytes(len(bin_data))}, no disk)..."
        )
        rc = _run_esptool_flash(port, baud, path)
        if rc != 0:
            _warn(f"memfd flash failed (esptool exit {rc})")
            return False
        _info("Firmware successfully flashed to chip.")
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
    if _flash_via_esptool_api(port, baud, bin_data, hint_name):
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
        prog="at-download-gl",
        description=(
            "Download the latest esp-at firmware from GitLab CI "
            f"(default branch: {DEFAULT_BRANCH}), optionally flash a device."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Token:\n"
            f"  {GITLAB_BASE}/-/user_settings/personal_access_tokens\n"
            "  Scopes: api, read_user, read_api.\n"
            f"  Save to {DEFAULT_TOKEN_FILES[0]} or set GITLAB_TOKEN.\n"
            "\n"
            "Examples:\n"
            "  at-download-gl.py\n"
            "      Interactive: pick a job from the latest master pipeline,\n"
            "      save firmware zip/bin to the current directory.\n"
            "\n"
            "  at-download-gl.py -p /dev/ttyUSB0\n"
            "      Download from master, then flash to that serial port.\n"
            "\n"
            "  at-download-gl.py -p 0 -m\n"
            "      Flash /dev/ttyUSB0; -p 0 is short for /dev/ttyUSB0.\n"
            "      -m keeps zip/bin in memory only (no files left on disk).\n"
            "\n"
            "  at-download-gl.py -B release/v2.3.0.0_esp8266\n"
            "      Use the latest successful pipeline on another branch\n"
            "      instead of the default master.\n"
            "\n"
            f"  at-download-gl.py -u {PIPELINE_WEB_PREFIX}/352799\n"
            "      Download from a specific pipeline (interactive job pick).\n"
            "      A bare numeric id is treated as a pipeline id.\n"
            "\n"
            f"  at-download-gl.py -u {JOB_WEB_PREFIX}/14643605\n"
            "      Download artifacts from a specific job URL.\n"
        ),
    )
    p.add_argument(
        "-B", "--branch", default=DEFAULT_BRANCH,
        help=f"Branch for latest successful pipeline (default: {DEFAULT_BRANCH})",
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
        help=(
            "Pipeline/job URL or numeric pipeline id, e.g. "
            f"{PIPELINE_WEB_PREFIX}/<id> or {JOB_WEB_PREFIX}/<id>"
        ),
    )
    p.add_argument(
        "-k", "--keyword", metavar="KEYWORD",
        help=(
            "Optional tag inserted into the saved zip name "
            "({job}_{keyword}-{date}.zip)"
        ),
    )
    p.add_argument(
        "-t", "--token-file", metavar="PATH",
        help="GitLab token file (overrides default search paths)",
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

    job: dict[str, Any]
    if args.url:
        kind, entity_id = parse_url(args.url)
        if kind == "job":
            _info(f"Using job from --url (id={entity_id})")
            job = get_job(token, entity_id)
            status = job.get("status") or ""
            if status and status != "success":
                _warn(f"Job status is '{status}' (expected success); continuing.")
        else:
            _info(f"Using pipeline from --url (id={entity_id})")
            pipeline = get_pipeline(token, entity_id)
            _info(
                f"Pipeline: id={pipeline.get('id')} ref={pipeline.get('ref')} "
                f"status={pipeline.get('status')} "
                f"sha={(pipeline.get('sha') or '')[:8]}"
            )
            _info(f"Created: {pipeline.get('created_at', '?')}")
            web = pipeline.get("web_url") or f"{PIPELINE_WEB_PREFIX}/{entity_id}"
            _info(f"GitLab pipeline: {web}")
            jobs = list_jobs_for_pipeline(token, entity_id)
            job = select_job(jobs)
    else:
        _info(f"Using branch: {args.branch}")
        _info("Looking up latest successful GitLab pipeline...")
        pipeline = find_latest_successful_pipeline(token, args.branch)
        pipeline_id = int(pipeline["id"])
        _info(
            f"Pipeline: id={pipeline_id} ref={pipeline.get('ref')} "
            f"status={pipeline.get('status')} "
            f"sha={(pipeline.get('sha') or '')[:8]}"
        )
        _info(f"Created: {pipeline.get('created_at', '?')}")
        web = pipeline.get("web_url") or f"{PIPELINE_WEB_PREFIX}/{pipeline_id}"
        _info(f"GitLab pipeline: {web}")
        jobs = list_jobs_for_pipeline(token, pipeline_id)
        job = select_job(jobs)

    job_id = int(job["id"])
    job_name = job.get("name") or "?"
    _info(f"Selected job: {job_name} (id={job_id})")
    _info(f"GitLab job: {JOB_WEB_PREFIX}/{job_id}")

    zip_bytes = download_job_artifacts(
        token, job_id, size_hint=job_artifacts_size(job)
    )
    member, bin_data = extract_factory_bin(zip_bytes)

    if args.memory and port:
        flash_from_memory(port, args.baud, bin_data, member)
        return 0

    bin_path = save_to_disk(zip_bytes, member, job_name, keyword=args.keyword)
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
