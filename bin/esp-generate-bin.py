#!/usr/bin/env python
# -*- coding: utf-8 -*-
# chenwu@espressif.com

"""Generate a binary blob with a predictable content pattern.

This utility is useful for quickly generating target files used in tests,
flash/OTA experiments, or transport verification.

Content modes:
  0: fill with 0x00
  1: fill with 0xFF (default)
  2: position-aware string pattern (read any bytes -> infer their file offsets)
  3: random bytes
"""

import argparse
import os
import secrets
import sys
from typing import Optional


def log_info(msg: str) -> None:
    print(f"\033[32m{msg}\033[0m")


def log_warn(msg: str) -> None:
    print(f"\033[33m{msg}\033[0m")


def log_error(msg: str) -> None:
    print(f"\033[31m{msg}\033[0m")


def parse_size(size_str: str) -> int:
    """Parse human friendly sizes: 4096, 4k, 4KiB, 1m, 1MiB, etc."""
    s = size_str.strip()
    if not s:
        raise ValueError("Empty size string")

    s_lower = s.lower()

    # Accept suffixes: k, kb, kib, m, mb, mib
    mult = 1
    if s_lower.endswith("kib"):
        mult = 1024
        num = s_lower[:-3]
    elif s_lower.endswith("kb"):
        mult = 1000
        num = s_lower[:-2]
    elif s_lower.endswith("k"):
        mult = 1024
        num = s_lower[:-1]
    elif s_lower.endswith("mib"):
        mult = 1024 * 1024
        num = s_lower[:-3]
    elif s_lower.endswith("mb"):
        mult = 1000 * 1000
        num = s_lower[:-2]
    elif s_lower.endswith("m"):
        mult = 1024 * 1024
        num = s_lower[:-1]
    else:
        num = s_lower

    try:
        value = int(num, 0)
    except ValueError as e:
        raise ValueError(f"Invalid size: {size_str}") from e

    if value < 0:
        raise ValueError("Size must be non-negative")

    return value * mult


def format_size_for_name(size_str: str) -> str:
    """Normalize the user provided size string for filenames.

    Requirement:
      - Use lowercase letters.
      - Preserve the user's input semantics (do not re-quantize).

    Examples:
      "4KiB" -> "4kib"
      "64k"  -> "64k"
      "0x1000" -> "0x1000"
      "4096" -> "4096"
    """

    return size_str.strip().lower().replace(" ", "")


def default_output_name(mode: int, size_str: str) -> str:
    """Build default output filename based on mode and user size string."""

    prefix_map = {
        0: "zero-",
        1: "blank-",
        2: "string-",
        3: "random-",
    }
    prefix = prefix_map.get(mode, f"mode{mode}-")
    return f"{prefix}{format_size_for_name(size_str)}.bin"


def gen_fill_bytes(size_bytes: int, fill: int) -> bytes:
    return bytes([fill]) * size_bytes


def gen_random_bytes(size_bytes: int) -> bytes:
    # secrets is suitable and available in stdlib.
    return secrets.token_bytes(size_bytes)


def gen_position_aware_bytes(size_bytes: int) -> bytes:
    """Generate a position-aware ASCII byte stream.

    Test-oriented pattern:
      - The file is emitted as fixed 10-byte groups.
      - The first 9 bytes of each group are ASCII and encode the group offset
        (in bytes) in uppercase hex. The last byte is '\n'.

    Group format (10 bytes):
      OOOOOOOOO\n
    Where:
      - OOOOOOOOO is the absolute group offset in 9-digit uppercase hex.
      - '\n' is a delimiter.

    Example:
      group 0 (offset 0x000000000): "000000000\n"
      group 1 (offset 0x00000000A): "00000000A\n"
      group 2 (offset 0x000000014): "000000014\n"

    Notes:
      - This mode is intended for small/medium test artifacts.
      - The output is ASCII, not arbitrary binary.
    """

    group_len = 10
    payload_len = 9

    out = bytearray(size_bytes)
    num_groups = (size_bytes + group_len - 1) // group_len

    for group_idx in range(num_groups):
        group_off = group_idx * group_len
        # Encode in 9 hex chars so the payload length is fixed.
        payload = f"{group_off:0{payload_len}X}".encode("ascii")
        chunk = payload + b"\n"  # exactly 10 bytes

        start = group_off
        end = min(group_off + group_len, size_bytes)
        out[start:end] = chunk[: end - start]

    return bytes(out)


def build_blob(size_bytes: int, mode: int) -> bytes:
    if mode == 0:
        return gen_fill_bytes(size_bytes, 0x00)
    if mode == 1:
        return gen_fill_bytes(size_bytes, 0xFF)
    if mode == 2:
        return gen_position_aware_bytes(size_bytes)
    if mode == 3:
        return gen_random_bytes(size_bytes)

    raise ValueError(f"Unsupported mode: {mode}")


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="esp-generate-bin.py",
        description="Generate a binary file with a specific content pattern.",
    )

    parser.add_argument(
        "-s",
        "--size",
        default="4KiB",
        help="Output file size (default: 4KiB). Examples: 4096, 0x1000, 4KiB, 64k, 1MiB",
    )

    parser.add_argument(
        "-m",
        "--mode",
        type=int,
        choices=[0, 1, 2, 3],
        default=1,
        help="Content mode: 0=0x00, 1=0xFF, 2=position-aware string, 3=random (default: 1)",
    )

    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output filename (default: <prefix><size>.bin; prefixes: zero/blank/string/random)",
    )

    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    try:
        size_bytes = parse_size(args.size)
    except Exception as e:
        log_error(f"Failed to parse size '{args.size}': {e}")
        return 2

    if size_bytes == 0:
        log_warn("Requested size is 0 bytes; creating an empty file.")

    output_path = args.output or default_output_name(args.mode, args.size)

    try:
        blob = build_blob(size_bytes, args.mode)
        ensure_parent_dir(output_path)
        with open(output_path, "wb") as f:
            f.write(blob)
    except Exception as e:
        log_error(f"Failed to generate file: {e}")
        return 1

    log_info(f"Generated: {os.path.abspath(output_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
