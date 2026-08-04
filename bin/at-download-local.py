#!/usr/bin/env python3
"""
Find local esp-at firmware and optionally flash a device.

Standalone (Python 3 stdlib only). Flashing needs esptool / esptool.py on PATH.

Searches recursively for complete flashable images (bootloader + partition
table) under the current directory, ~/Downloads, and ~/$USER/share — or under
a path you pass. Chip type is read from the binary image header.

Examples:
  at-download-local.py
      Interactive: pick a local firmware, flash to /dev/ttyUSB0.
  at-download-local.py -p 1
      Same, but flash /dev/ttyUSB1.
  at-download-local.py -n ~/Downloads
      Scan ~/Downloads only; select firmware but do not flash.
  at-download-local.py -p 0 -m /path/to/artifact.zip
      Flash from zip bytes in memory when possible.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

DEFAULT_BAUD = 921600
DEFAULT_PORT = "/dev/ttyUSB0"
# Local firmware search roots (in order). ~/$USER/share is the shared drop dir.
DEFAULT_SEARCH_ROOTS = (
    ".",
    "~/Downloads",
    f"~/{os.environ.get('USER', 'user')}/share",
)

ESP_IMAGE_MAGIC = 0xE9
PART_MAGIC = 0x50AA
PART_MD5_MAGIC = 0xEBEB
APP_PARTITION_TYPE = 0x00

# esptool IMAGE_CHIP_ID -> chip name (scan does not import esptool)
IMAGE_CHIP_IDS: dict[int, str] = {
    0: "esp32",
    2: "esp32s2",
    5: "esp32c3",
    9: "esp32s3",
    12: "esp32c2",
    13: "esp32c6",
    16: "esp32h2",
    18: "esp32p4",
    20: "esp32c61",
    23: "esp32c5",
    25: "esp32h21",
    28: "esp32h4",
    31: "esp32e22",
    32: "esp32s31",
}


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


def resolve_port(port: str) -> str:
    return f"/dev/ttyUSB{port}" if port.isdigit() else port


def _format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


# ---------------------------------------------------------------------------
# Binary inspection (complete flash image + chip)
# ---------------------------------------------------------------------------

def find_bootloader_offset(data: bytes) -> int | None:
    for off in (0, 0x1000):
        if off < len(data) and data[off] == ESP_IMAGE_MAGIC:
            return off
    return None


def parse_partition_table(data: bytes, off: int) -> list[tuple[int, int, int, int, str]] | None:
    """Return partition entries if a valid ESP-IDF table starts at off."""
    if off + 32 > len(data):
        return None
    if struct.unpack_from("<H", data, off)[0] != PART_MAGIC:
        return None

    entries: list[tuple[int, int, int, int, str]] = []
    o = off
    for _ in range(100):
        if o + 32 > len(data):
            return None
        magic = struct.unpack_from("<H", data, o)[0]
        if magic == PART_MD5_MAGIC:
            break
        if magic != PART_MAGIC:
            if all(b == 0xFF for b in data[o:o + 32]):
                break
            return None

        typ, subtype = data[o + 2], data[o + 3]
        part_off, part_size = struct.unpack_from("<II", data, o + 4)
        label_raw = data[o + 12:o + 28].split(b"\x00", 1)[0]

        if part_size == 0:
            return None
        if part_off % 0x1000 != 0 or part_size % 0x1000 != 0:
            return None
        if not label_raw or any(c < 32 or c > 126 for c in label_raw):
            return None

        entries.append(
            (typ, subtype, part_off, part_size, label_raw.decode("ascii"))
        )
        o += 32

    if not entries:
        return None
    if not any(t == APP_PARTITION_TYPE for t, *_ in entries):
        return None
    return entries


def find_partition_table_offset(data: bytes) -> int | None:
    limit = len(data)
    for off in range(0, limit, 0x1000):
        if parse_partition_table(data, off) is not None:
            return off
    return None


def is_complete_flash_image(data: bytes) -> bool:
    """True if image looks flashable from 0x0 (bootloader + partition table)."""
    if find_bootloader_offset(data) is None:
        return False
    return find_partition_table_offset(data) is not None


def detect_chip_from_image(data: bytes) -> str:
    boot = find_bootloader_offset(data)
    if boot is None or boot + 14 > len(data):
        return "unknown"
    chip_id = struct.unpack_from("<H", data, boot + 12)[0]
    return IMAGE_CHIP_IDS.get(chip_id, "unknown")


def inspect_bin(data: bytes) -> tuple[bool, str]:
    """Return (is_complete, chip_name)."""
    if not is_complete_flash_image(data):
        return False, "unknown"
    return True, detect_chip_from_image(data)


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------

@dataclass
class FirmwareCandidate:
    """display_path is what the user sees; bin_path/member identify the bytes."""

    display_path: Path
    chip: str
    kind: str  # "zip" | "dir" | "bin"
    bin_path: Path | None = None
    zip_member: str | None = None


def _bin_rank(name: str) -> tuple[int, str]:
    """Lower is better: prefer factory_*_unfilled.bin."""
    base = Path(name.replace("\\", "/")).name.lower()
    if re.match(r"factory_.*_unfilled\.bin$", base):
        return (0, base)
    if re.match(r"factory_.*\.bin$", base) and "unfilled" not in base:
        return (1, base)
    if base.startswith("factory") and base.endswith(".bin"):
        return (2, base)
    return (3, base)


def _pick_best_bin_name(names: list[str]) -> str | None:
    if not names:
        return None
    return sorted(names, key=_bin_rank)[0]


def _read_file_bytes(path: Path, max_bytes: int | None = None) -> bytes | None:
    try:
        if max_bytes is None:
            return path.read_bytes()
        with path.open("rb") as f:
            return f.read(max_bytes)
    except OSError as e:
        _warn(f"Cannot read {path}: {e}")
        return None


def _candidate_from_bin_file(path: Path) -> FirmwareCandidate | None:
    data = _read_file_bytes(path)
    if data is None:
        return None
    ok, chip = inspect_bin(data)
    if not ok:
        return None
    return FirmwareCandidate(
        display_path=path.resolve(),
        chip=chip,
        kind="bin",
        bin_path=path.resolve(),
    )


def _best_complete_from_zip(zf: zipfile.ZipFile) -> tuple[str, str] | None:
    """Return (member_name, chip) for the best complete image in the zip."""
    complete: list[tuple[str, str]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if not name.lower().endswith(".bin"):
            continue
        if "param" in Path(name).name.lower():
            continue
        try:
            data = zf.read(info)
        except Exception as e:
            _warn(f"Cannot read zip member {name}: {e}")
            continue
        ok, chip = inspect_bin(data)
        if ok:
            complete.append((name, chip))
    if not complete:
        return None
    best = _pick_best_bin_name([n for n, _ in complete])
    assert best is not None
    chip = next(c for n, c in complete if n == best)
    return best, chip


def _candidate_from_zip(path: Path) -> FirmwareCandidate | None:
    try:
        with zipfile.ZipFile(path) as zf:
            picked = _best_complete_from_zip(zf)
    except zipfile.BadZipFile:
        _warn(f"Skipping invalid zip: {path}")
        return None
    except OSError as e:
        _warn(f"Cannot open zip {path}: {e}")
        return None
    if not picked:
        return None
    member, chip = picked
    return FirmwareCandidate(
        display_path=path.resolve(),
        chip=chip,
        kind="zip",
        zip_member=member,
    )


def _artifact_root_for_bin(bin_path: Path, search_root: Path) -> Path:
    """
    Prefer a directory that looks like an extracted artifact (has build/),
    else the bin's parent, clipped to search_root.
    """
    resolved_root = search_root.resolve()
    cur = bin_path.resolve().parent
    best = cur
    while True:
        if (cur / "build").is_dir():
            best = cur
        if cur == resolved_root or cur.parent == cur:
            break
        try:
            cur.relative_to(resolved_root)
        except ValueError:
            break
        cur = cur.parent
    return best


def _scan_directory(root: Path) -> list[FirmwareCandidate]:
    """Find complete images under root; group by artifact dir / zip / lone bin."""
    root = root.resolve()
    if not root.is_dir():
        return []

    by_display: dict[Path, FirmwareCandidate] = {}
    seen_bins: set[Path] = set()

    # Zips first (display = zip path)
    for zip_path in sorted(root.rglob("*.zip")):
        if not zip_path.is_file():
            continue
        cand = _candidate_from_zip(zip_path)
        if cand is None:
            continue
        key = cand.display_path
        if key not in by_display:
            by_display[key] = cand

    # Loose / extracted bins
    bin_hits: list[tuple[Path, str]] = []
    for bin_path in sorted(root.rglob("*.bin")):
        if not bin_path.is_file():
            continue
        if "param" in bin_path.name.lower():
            continue
        resolved = bin_path.resolve()
        if resolved in seen_bins:
            continue
        data = _read_file_bytes(bin_path)
        if data is None:
            continue
        ok, chip = inspect_bin(data)
        if not ok:
            continue
        seen_bins.add(resolved)
        bin_hits.append((resolved, chip))

    # Group bins under artifact roots; keep best per root
    groups: dict[Path, list[tuple[Path, str]]] = {}
    for bin_path, chip in bin_hits:
        art = _artifact_root_for_bin(bin_path, root)
        groups.setdefault(art, []).append((bin_path, chip))

    for art, items in groups.items():
        # Prefer unfilled factory within the group
        best_path = _pick_best_bin_name([str(p) for p, _ in items])
        assert best_path is not None
        best = Path(best_path)
        chip = next(c for p, c in items if p == best)

        # If this bin sits alone (artifact root == bin parent == file-ish),
        # and there is no sibling build/, show the bin path itself when the
        # artifact root only contains this one complete image and no build/.
        if len(items) == 1 and not (art / "build").is_dir() and art == best.parent:
            display = best
            kind = "bin"
        else:
            display = art
            kind = "dir"

        # Skip dir candidate if a zip with the same stem already covers it
        zip_sibling = Path(str(display) + ".zip")
        if zip_sibling.resolve() in by_display:
            continue
        if display.resolve() in by_display:
            continue

        by_display[display.resolve()] = FirmwareCandidate(
            display_path=display.resolve(),
            chip=chip,
            kind=kind,
            bin_path=best,
        )

    return list(by_display.values())


def collect_candidates(path_arg: str | None) -> list[FirmwareCandidate]:
    found: list[FirmwareCandidate] = []
    seen: set[Path] = set()

    def add_all(cands: list[FirmwareCandidate]) -> None:
        for c in cands:
            key = c.display_path.resolve()
            if key in seen:
                continue
            seen.add(key)
            found.append(c)

    if path_arg is None:
        for raw in DEFAULT_SEARCH_ROOTS:
            root = Path(os.path.expanduser(raw)).resolve()
            if not root.exists():
                _warn(f"Search root missing, skip: {root}")
                continue
            if root.is_file():
                add_all(_candidates_from_file(root))
            else:
                _info(f"Scanning for firmware under: {root}")
                add_all(_scan_directory(root))
        return found

    target = Path(os.path.expanduser(path_arg)).resolve()
    if not target.exists():
        _die(f"Path not found: {target}")
    if target.is_file():
        add_all(_candidates_from_file(target))
    else:
        _info(f"Scanning for firmware under: {target}")
        add_all(_scan_directory(target))
    return found


def _candidates_from_file(path: Path) -> list[FirmwareCandidate]:
    suffix = path.suffix.lower()
    if suffix == ".zip":
        cand = _candidate_from_zip(path)
        return [cand] if cand else []
    if suffix == ".bin":
        cand = _candidate_from_bin_file(path)
        return [cand] if cand else []
    _die(f"Unsupported file type (want .zip or .bin): {path}")


# ---------------------------------------------------------------------------
# Select / prepare / flash
# ---------------------------------------------------------------------------

def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _path_mtime_str(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except OSError:
        return "unknown"


def select_candidate(cands: list[FirmwareCandidate]) -> FirmwareCandidate:
    if not cands:
        _die("No complete flashable AT firmware found.")

    # Newest first
    cands = sorted(cands, key=lambda c: _path_mtime(c.display_path), reverse=True)
    print("Available firmware:")
    for i, c in enumerate(cands):
        idx = _c("1;36", str(i))
        date = _c("33", f"[{_path_mtime_str(c.display_path)}]")
        path = _c("1;32", str(c.display_path))
        print(f"  {idx}: {date} {path} ({c.chip})")
    try:
        raw = input(
            f"Select firmware by index "
            f"(0-{len(cands) - 1}, default 0): "
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
    if not 0 <= idx < len(cands):
        _die(f"Index out of range: {idx}")
    return cands[idx]


def load_candidate_bytes(cand: FirmwareCandidate) -> tuple[str, bytes]:
    if cand.kind == "zip":
        assert cand.zip_member is not None
        with zipfile.ZipFile(cand.display_path) as zf:
            data = zf.read(cand.zip_member)
        return cand.zip_member, data
    assert cand.bin_path is not None
    data = cand.bin_path.read_bytes()
    return str(cand.bin_path), data


def extract_candidate_to_disk(cand: FirmwareCandidate) -> Path:
    """Return a filesystem path to the complete bin for CLI flash."""
    if cand.kind in ("bin", "dir"):
        assert cand.bin_path is not None
        if not cand.bin_path.is_file():
            _die(f"Firmware bin missing: {cand.bin_path}")
        return cand.bin_path.resolve()

    assert cand.kind == "zip" and cand.zip_member is not None
    member = cand.zip_member
    dst_dir = Path(cand.display_path.stem)
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    with zipfile.ZipFile(cand.display_path) as zf:
        zf.extract(member, dst_dir)
    out = (dst_dir / member).resolve()
    if not out.is_file():
        # zip may use nested paths; extractall member only keeps relative path
        out = (dst_dir / Path(member)).resolve()
    if not out.is_file():
        _die(f"Expected firmware missing after extract: {out}")
    _info(f"Extracted firmware: {out}")
    return out


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
    rc = _run_esptool_flash(port, baud, bin_path)
    if rc != 0:
        _die(f"Flash failed (exit {rc})")
    _info("Firmware successfully flashed to chip.")


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


def _flash_via_esptool_api(
    port: str, baud: int, bin_data: bytes, hint_name: str
) -> bool:
    """esptool 4.x+: detect_chip + esp.run_stub(); write_flash takes Namespace."""
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
        prog="at-download-local",
        description=(
            "Find local esp-at firmware (complete flash images only) and "
            f"flash a device (default port: {DEFAULT_PORT})."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Search roots (when PATH omitted):\n"
            "  current directory, ~/Downloads, ~/$USER/share\n"
            "\n"
            "Only binaries that contain a bootloader and a valid partition\n"
            "table are listed (flashable from address 0x0).\n"
            "\n"
            "Examples:\n"
            "  at-download-local.py\n"
            "      Scan default roots, pick firmware, flash /dev/ttyUSB0.\n"
            "\n"
            "  at-download-local.py -p 1\n"
            "      Flash /dev/ttyUSB1 (-p 1 is short for /dev/ttyUSB1).\n"
            "\n"
            "  at-download-local.py -n ~/Downloads\n"
            "      Scan ~/Downloads only; select but do not flash.\n"
            "\n"
            "  at-download-local.py -p 0 -m artifact.zip\n"
            "      Flash from the zip; keep bin in memory when possible.\n"
        ),
    )
    p.add_argument(
        "-p", "--port", default="0",
        help=f"Serial port; digit N -> /dev/ttyUSBN (default: 0 -> {DEFAULT_PORT})",
    )
    p.add_argument(
        "-b", "--baud", type=int, default=DEFAULT_BAUD,
        help=f"Flash baud rate (default: {DEFAULT_BAUD})",
    )
    p.add_argument(
        "-m", "--memory", action="store_true",
        help="Keep firmware in memory when flashing (no extract for zip)",
    )
    p.add_argument(
        "-n", "--no-flash", action="store_true",
        help="Select firmware only; do not flash",
    )
    p.add_argument(
        "path", nargs="?",
        help="Optional file (.zip/.bin) or directory to search",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    port = resolve_port(args.port)

    if args.memory and args.no_flash:
        _die("--memory/-m cannot be combined with --no-flash/-n.")

    cands = collect_candidates(args.path)
    _info(f"Found {len(cands)} complete firmware candidate(s).")
    cand = select_candidate(cands)
    _info(f"Selected: {cand.display_path} ({cand.chip})")

    if args.no_flash:
        if cand.kind == "zip":
            _info(f"Zip member: {cand.zip_member}")
            print()
            print("All done! To flash manually, extract then run:")
            print(
                f"  esptool.py --port {port} --baud {args.baud} "
                f"write_flash 0x0 <extracted_factory.bin>"
            )
        else:
            assert cand.bin_path is not None
            _info(f"Firmware bin: {cand.bin_path}")
            print()
            print("All done! To flash manually, run:")
            print(
                f"  esptool.py --port {port} --baud {args.baud} "
                f"write_flash 0x0 {cand.bin_path}"
            )
        return 0

    if not Path(port).exists():
        _die(f"No {port}")

    if args.memory:
        hint, data = load_candidate_bytes(cand)
        flash_from_memory(port, args.baud, data, hint)
        return 0

    bin_path = extract_candidate_to_disk(cand)
    _info(f"Firmware bin: {bin_path}")
    flash_from_path(port, args.baud, bin_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(file=sys.stderr)
        _error("Interrupted.")
        raise SystemExit(130)
