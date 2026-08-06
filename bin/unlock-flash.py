#!/usr/bin/env python3
"""
Query SPI flash status, or reset flash (clear status protect + erase all).

Some modules lock part of flash via status-register protect bits; stock
esptool write-flash-status may not clear them. This tool queries status (-s)
or resets the chip (-r: clear protect, then erase-flash).

Examples:
  unlock-flash.py -p 2 -s
  unlock-flash.py -p 2 -r
  unlock-flash.py -p 2 -r -b 115200

Requires: pip install esptool. Supported flash models: see --help.
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
from typing import Any, NoReturn

DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 921600

# Supported flash (extend here when adding more vendors/families).
# Fudan FM25Q*: mfg 0xA1, mem type 0x40; density in 3rd JEDEC byte
# (0x13=512KB, 0x14=1MB, 0x15=2MB, 0x16=4MB, ...).
SUPPORTED_FLASH = (
    "Fudan FM25Q series (JEDEC a1/40xx; any density, e.g. 512KB–4MB+)"
)
FM25Q_MFG = 0xA1
FM25Q_TYPE = 0x40

# Optional name hint from density code (part number suffix ≈ Mbit).
_FM25Q_NAME_BY_CAPACITY = {
    0x13: "FM25Q40",   # 4Mbit  = 512KB
    0x14: "FM25Q80",   # 8Mbit  = 1MB
    0x15: "FM25Q16",   # 16Mbit = 2MB
    0x16: "FM25Q32",   # 32Mbit = 4MB
}

# Status SR1 bits that indicate block / status-register protect
SR1_BP_MASK = 0x3C  # BP0..BP3
SR1_SRP0 = 0x80


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


def resolve_port(port: str) -> str:
    return f"/dev/ttyUSB{port}" if port.isdigit() else port


def _format_size(nbytes: int) -> str:
    if nbytes >= 1024 * 1024 and nbytes % (1024 * 1024) == 0:
        return f"{nbytes // (1024 * 1024)}MB"
    if nbytes >= 1024 and nbytes % 1024 == 0:
        return f"{nbytes // 1024}KB"
    return f"{nbytes}B"


def _capacity_bytes(capacity_code: int) -> int | None:
    # JEDEC density code: size = 2^code (common NOR range 0x13..0x19).
    if 0x10 <= capacity_code <= 0x19:
        return 1 << capacity_code
    return None


# ---------------------------------------------------------------------------
# esptool helpers
# ---------------------------------------------------------------------------

def _require_esptool() -> None:
    try:
        import esptool  # noqa: F401
    except ImportError:
        _die("esptool not found. Install with: pip install esptool")


def _find_esptool() -> list[str]:
    import shutil

    for name in ("esptool", "esptool.py"):
        path = shutil.which(name)
        if path:
            return [path]
    _die("esptool not found on PATH. Install with: pip install esptool")


def _connect(port: str, baud: int):
    from esptool.cmds import detect_chip

    # Stub upload is more reliable at 115200; raise baud afterwards if needed.
    _info(f"Connecting {port}...")
    esp = detect_chip(port=port, baud=115200)
    if hasattr(esp, "run_stub"):
        esp = esp.run_stub()
    if baud and baud != 115200:
        try:
            esp.change_baud(baud)
        except Exception as e:
            _warn(f"Could not change baud to {baud}: {e}; staying at 115200")
    return esp


def _close(esp) -> None:
    for closer in (
        lambda: esp.hard_reset(),
        lambda: esp._port.close(),
    ):
        try:
            closer()
        except Exception:
            pass


def _flash_id(esp) -> tuple[int, int, int]:
    """Return (mfg, memory_type, capacity_code) from RDID."""
    raw = esp.flash_id()
    # Packed as capacity<<16 | mem_type<<8 | mfg (SPI RDID order).
    return raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF


def _status(esp, num_bytes: int = 2) -> int:
    return esp.read_status(num_bytes)


def _fmt_status(st: int, num_bytes: int = 2) -> str:
    return f"0x{st:0{num_bytes * 2}x}"


def _is_protected(st: int) -> bool:
    return bool((st & 0xFF) & (SR1_BP_MASK | SR1_SRP0))


def _is_fm25q(mfg: int, mem_type: int, capacity_code: int = 0) -> bool:
    _ = capacity_code  # size only; unlock is family-wide
    return mfg == FM25Q_MFG and mem_type == FM25Q_TYPE


def _describe_flash(mfg: int, mem_type: int, capacity_code: int) -> str:
    size = _capacity_bytes(capacity_code)
    size_s = _format_size(size) if size is not None else "unknown size"
    jedec = f"0x{mfg:02x}{mem_type:02x}{capacity_code:02x}"
    if _is_fm25q(mfg, mem_type, capacity_code):
        name = _FM25Q_NAME_BY_CAPACITY.get(capacity_code, "FM25Q")
        return f"{name} ({jedec}, {size_s})"
    return f"unknown ({jedec}, {size_s})"


def _require_supported(mfg: int, mem_type: int, capacity_code: int) -> None:
    if _is_fm25q(mfg, mem_type, capacity_code):
        return
    _die(
        "Unsupported flash: "
        f"{_describe_flash(mfg, mem_type, capacity_code)}. "
        f"Supported: {SUPPORTED_FLASH}."
    )


def _clear_status_protect(esp, value: int = 0) -> int:
    """Clear status-register protect for the supported flash family."""
    # Fudan FM25Q: vendor CAM enter -> WREN -> WRSR -> CAM exit
    for cmd in (0x66, 0x3C, 0xC3):
        esp.run_spiflash_command(cmd)
    esp.run_spiflash_command(0x06)  # WREN
    esp.run_spiflash_command(0x01, struct.pack("<H", value & 0xFFFF))
    esp.run_spiflash_command(0xFF)
    return _status(esp, 2)


def _erase_flash(port: str, baud: int) -> None:
    cmd = _find_esptool() + [
        "--port", port,
        "--baud", str(baud),
        "erase-flash",
    ]
    _info("Running: " + " ".join(cmd))
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        _die(f"erase-flash failed (exit {rc})")


def _read_info(esp) -> tuple[int, int, int, int]:
    mfg, mem_type, capacity_code = _flash_id(esp)
    st = _status(esp, 2)
    _info(f"Flash:  {_describe_flash(mfg, mem_type, capacity_code)}")
    _info(f"Status: {_fmt_status(st)}")
    if _is_protected(st):
        _warn("Block / status-register protect bits are set.")
    else:
        _info("No BP/SRP protect bits set.")
    return mfg, mem_type, capacity_code, st


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(port: str, baud: int) -> None:
    """Query flash id and status. Exit 2 if protect bits are set."""
    _require_esptool()
    esp = _connect(port, baud)
    try:
        mfg, mem_type, capacity_code, st = _read_info(esp)
        _require_supported(mfg, mem_type, capacity_code)
        if _is_protected(st):
            raise SystemExit(2)
    finally:
        _close(esp)


def cmd_reset(port: str, baud: int) -> None:
    """Clear status protect and erase entire flash."""
    _require_esptool()
    esp = _connect(port, baud)
    try:
        mfg, mem_type, capacity_code, st = _read_info(esp)
        _require_supported(mfg, mem_type, capacity_code)

        if _is_protected(st):
            _info("Clearing flash status protect -> 0x0000...")
            st = _clear_status_protect(esp, 0)
            _info(f"Status after clear: {_fmt_status(st)}")
            if _is_protected(st):
                _die(
                    "Failed to clear flash status. "
                    "Check WP# wiring or replace the module."
                )
        else:
            _info("Flash status already clear.")
    finally:
        _close(esp)

    _info("Erasing entire flash...")
    _erase_flash(port, baud)

    esp = _connect(port, baud)
    try:
        _, _, _, st = _read_info(esp)
        if _is_protected(st):
            _die("Flash status protect reappeared after erase.")
    finally:
        _close(esp)

    _info("Flash reset done.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="unlock-flash",
        description=(
            "Query SPI flash status, or reset flash "
            "(clear status protect + erase all)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Supported flash: {SUPPORTED_FLASH}.\n"
            "\n"
            "Examples:\n"
            "  unlock-flash.py -p 2 -s\n"
            "      Query flash id/status on /dev/ttyUSB2.\n"
            "  unlock-flash.py -p 2 -r\n"
            "      Clear status protect and erase entire flash.\n"
            "  unlock-flash.py -p /dev/ttyUSB2 -r -b 115200\n"
            "      Same, at 115200 baud.\n"
        ),
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "-s", "--status", action="store_true",
        help="Query flash id and status",
    )
    mode.add_argument(
        "-r", "--reset", action="store_true",
        help="Reset flash: clear status protect, then erase all",
    )
    p.add_argument(
        "-p", "--port", default="0",
        help=f"Serial port; digit N -> /dev/ttyUSBN (default: 0 -> {DEFAULT_PORT})",
    )
    p.add_argument(
        "-b", "--baud", type=int, default=DEFAULT_BAUD,
        help=f"Serial baud rate (default: {DEFAULT_BAUD})",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    port = resolve_port(args.port)
    try:
        if args.status:
            cmd_status(port, args.baud)
        else:
            cmd_reset(port, args.baud)
    except KeyboardInterrupt:
        if sys.stderr.isatty():
            sys.stderr.write("\n")
            sys.stderr.flush()
        _die("Interrupted.", code=130)


if __name__ == "__main__":
    main()
