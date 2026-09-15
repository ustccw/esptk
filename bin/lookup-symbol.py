#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
Find a symbol in static libraries (*.a) under the current directory.

Walks the current working directory and subdirectories, following
directory symlinks (for example a project's esp-idf link). Directories
named build/ are skipped. Uses `nm` from PATH.

Default match is substring and case-sensitive. Default nm types are
defined functions and variables (not undefined references).

Examples:
  lookup-symbol.py esp_wifi_connect
  lookup-symbol.py esp_at_*_cmd_regist
  lookup-symbol.py -c {esp32c3,esp32c5} at_handle
  lookup-symbol.py -t a g_esp_netif
  lookup-symbol.py -w mqtt
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, NoReturn


ARCHIVE_SUFFIX = ".a"
IDENT_CHAR = r"0-9A-Za-z_"
GLOB_METACHARS = "*?["
_GLOB_FILE_EXTS = (
    ".c", ".cc", ".cpp", ".h", ".hpp", ".S", ".s", ".o", ".obj", ".a",
)
BUILD_DIR = "build"
SKIP_DIR_NAMES = frozenset({BUILD_DIR, ".git"})
DEFAULT_TYPES = "T,t,W,D,d,B,b,R,r,C,V"

# Longest-first: esp32c61 before esp32c6 before esp32.
KNOWN_CHIPS = (
    "esp32c61",
    "esp32c6",
    "esp32c5",
    "esp32c3",
    "esp32c2",
    "esp32s3",
    "esp32s2",
    "esp32p4",
    "esp32h2",
    "esp32",
    "esp8266",
)

# GNU nm / nm -S: [addr] [size] type name (undefined has neither addr nor size).
_SYM_LINE = re.compile(
    r"^\s*"
    r"(?:([0-9A-Fa-f]+)\s+)?"
    r"(?:([0-9A-Fa-f]+)\s+)?"
    r"([A-Za-z?\-])\s+"
    r"(\S+)\s*$"
)

TYPE_DESC = {
    "A": "absolute",
    "B": "global BSS",
    "b": "local BSS",
    "C": "common",
    "D": "global data",
    "d": "local data",
    "N": "debug",
    "R": "global rodata",
    "r": "local rodata",
    "T": "global function",
    "t": "local function",
    "U": "undefined",
    "V": "weak object",
    "v": "weak object (undefined)",
    "W": "weak function",
    "w": "weak function (undefined)",
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


def _dim(text: str, *, stream: Any = sys.stdout) -> str:
    return _c("2", text, stream=stream)


def _info(msg: str) -> None:
    print(f"{_c('1;32', '[INFO]')} {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"{_c('1;33', '[WARN]', stream=sys.stderr)} {msg}",
          file=sys.stderr, flush=True)


def _error(msg: str) -> None:
    print(f"{_c('1;31', '[ERROR]', stream=sys.stderr)} {msg}",
          file=sys.stderr, flush=True)


def _hint(msg: str) -> None:
    print(f"{_c('36', '[HINT]', stream=sys.stderr)} {msg}",
          file=sys.stderr, flush=True)


def _die(msg: str, code: int = 1, *, hints: list[str] | None = None) -> NoReturn:
    _error(msg)
    for line in hints or ():
        _hint(line)
    raise SystemExit(code)


def hint_no_match(
    symbol: str,
    *,
    scanned: int,
    word: bool,
    is_glob: bool,
    types: set[str] | None,
    chips: set[str] | None,
) -> None:
    quoted = shlex.quote(symbol)
    if scanned == 0:
        _hint("no *.a here (build/ is skipped); cd to the project root")
    if not word:
        _hint(f"whole-word: lookup-symbol.py -w {quoted}")
    else:
        _hint(f"substring is default; omit -w: lookup-symbol.py {quoted}")
    if types is not None:
        _hint(f"all types:  lookup-symbol.py -t a {quoted}")
    if not is_glob:
        _hint("glob:       lookup-symbol.py esp_at_*_cmd_regist")
    if chips:
        _hint("all chips:  omit -c")
    _hint("help:       lookup-symbol.py -h")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def parse_types(value: str) -> set[str] | None:
    if value.strip().lower() in ("a", "all"):
        return None
    out: set[str] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        out.update(part)
    if not out:
        _die(
            "-t/--type is empty",
            hints=["example: lookup-symbol.py -t T,t,W esp_wifi_connect",
                   "example: lookup-symbol.py -t a g_esp_netif"],
        )
    return out


def normalize_chip_token(s: str) -> str:
    return s.strip().lower().replace("_", "").replace("-", "")


def looks_like_chip_arg(s: str) -> bool:
    """True if s is a chip, comma list, or {a,b} group (no leading dash)."""
    if not s or s.startswith("-"):
        return False
    t = s.strip()
    if t.startswith("{") and t.endswith("}"):
        t = t[1:-1]
    parts = [p.strip() for p in t.split(",") if p.strip()]
    if not parts:
        return False
    known = KNOWN_CHIPS
    return all(normalize_chip_token(p) in known for p in parts)


def fold_chip_argv(argv: list[str]) -> list[str]:
    """Join chip tokens after -c/--chip (bash expands {esp32c2,esp32c3})."""
    out: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        if arg in ("-c", "--chip"):
            out.append(arg)
            i += 1
            chips: list[str] = []
            while i < n and looks_like_chip_arg(argv[i]):
                chips.append(argv[i])
                i += 1
            if chips:
                out.append(",".join(chips))
            continue
        out.append(arg)
        i += 1
    return out


def parse_chip_args(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    known = {chip: chip for chip in KNOWN_CHIPS}
    out: set[str] = set()
    for value in values:
        s = value.strip()
        if s.startswith("{") and s.endswith("}"):
            s = s[1:-1]
        for tok in s.split(","):
            tok = tok.strip()
            if not tok:
                continue
            key = normalize_chip_token(tok)
            if key not in known:
                _die(
                    f"unknown chip {tok!r}; "
                    f"known: {', '.join(KNOWN_CHIPS)}",
                    hints=[
                        "example: lookup-symbol.py -c esp32c3 esp_wifi_connect",
                        "example: lookup-symbol.py -c {esp32c2,esp32c3} esp_wifi_set_ps",
                    ],
                )
            out.add(known[key])
    if not out:
        _die(
            "-c/--chip is empty",
            hints=["example: lookup-symbol.py -c esp32c3 esp_wifi_connect"],
        )
    return out


def chips_in_path(path: str) -> set[str]:
    """Chip ids mentioned in a path/filename; longest match wins."""
    s = path.replace("\\", "/").lower()
    n = len(s)
    found: set[str] = set()
    for chip in KNOWN_CHIPS:
        start = 0
        clen = len(chip)
        while True:
            i = s.find(chip, start)
            if i < 0:
                break
            j = i + clen
            if j < n and s[j].isalnum():
                start = i + 1
                continue
            found.add(chip)
            start = j
    return found


def archive_matches_chips(path: Path, allowed: set[str] | None) -> bool:
    if not allowed:
        return True
    found = chips_in_path(str(path))
    if not found:
        return True
    return bool(found & allowed)


def symbol_is_glob(symbol: str) -> bool:
    return any(ch in symbol for ch in GLOB_METACHARS)


def _looks_like_expanded_file(token: str) -> bool:
    """True if token is a file in the current directory (not a subdirectory)."""
    if symbol_is_glob(token):
        return False
    if os.path.sep in token or os.path.dirname(token) not in ("", "."):
        return False
    if not os.path.isfile(token):
        return False
    return any(os.path.basename(token).endswith(ext) for ext in _GLOB_FILE_EXTS)


def symbol_is_too_broad(symbol: str) -> bool:
    return not any(c.isalnum() for c in symbol)


def coalesce_symbol_tokens(tokens: list[str]) -> str:
    """Rebuild a glob if the shell expanded * into several filenames."""
    if len(tokens) == 1:
        name = os.path.basename(tokens[0])
        if not _looks_like_expanded_file(tokens[0]):
            return tokens[0]
        for ext in _GLOB_FILE_EXTS:
            if name.endswith(ext):
                return name[:-len(ext)]
        return name
    if not all(_looks_like_expanded_file(t) for t in tokens):
        extra = " ".join(tokens[1:])
        _die(
            f"unexpected extra arguments: {extra}",
            hints=[
                "one symbol only; quote the glob if * matches files here",
                "example: lookup-symbol.py 'esp_at_*_cmd_regist'",
            ],
        )
    bases = [os.path.basename(t) for t in tokens]
    prefix = os.path.commonprefix(bases)
    suffix = os.path.commonprefix([b[::-1] for b in bases])[::-1]
    if len(set(bases)) == 1:
        name = bases[0]
    else:
        name = f"{prefix}*{suffix}"
    for ext in _GLOB_FILE_EXTS:
        if name.endswith(ext) and all(b.endswith(ext) for b in bases):
            name = name[:-len(ext)]
            break
    return name


def warn_if_shell_globbed(tokens: list[str], rebuilt: str) -> None:
    hits = [t for t in tokens if _looks_like_expanded_file(t)]
    if not hits:
        return
    preview = hits[:8]
    more = f" ... ({len(hits) - 8} more)" if len(hits) > 8 else ""
    names = ", ".join(os.path.basename(t) for t in preview) + more
    _warn(
        f"the shell expanded '*' to {len(hits)} file(s) in the current "
        f"directory: {names}"
    )
    _warn(f"quote the glob so it is not expanded, e.g. '{rebuilt}'")


def compile_name_pattern(
    symbol: str,
    *,
    ignore_case: bool,
    word: bool,
    is_glob: bool,
) -> re.Pattern[str]:
    if is_glob:
        glob_pat = symbol if word else (
            ("" if symbol.startswith("*") else "*")
            + symbol
            + ("" if symbol.endswith("*") else "*")
        )
        body = fnmatch.translate(glob_pat)
    else:
        body = re.escape(symbol)
        if word:
            body = rf"(?<![{IDENT_CHAR}])(?:{body})(?![{IDENT_CHAR}])"
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(body, flags)
    except re.error as exc:
        _die(
            f"invalid pattern: {exc}",
            hints=["example: lookup-symbol.py esp_at_*_cmd_regist"],
        )


def symbol_basename(name: str) -> str:
    return name.split("@@", 1)[0]


def name_matches(
    nm_name: str,
    pattern: re.Pattern[str],
    *,
    whole: bool,
) -> bool:
    base = symbol_basename(nm_name)
    op = pattern.fullmatch if whole else pattern.search
    if op(base):
        return True
    return base != nm_name and op(nm_name) is not None


def iter_archives(root: Path) -> Iterator[Path]:
    """Yield unique real paths of *.a files, following directory symlinks.

    Directories named build/ are not entered (by walk name or realpath).
    """
    seen_dirs: set[str] = set()
    seen_files: set[str] = set()

    def _onerror(_err: OSError) -> None:
        return

    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=True, onerror=_onerror,
    ):
        dir_real = os.path.realpath(dirpath)
        if dir_real in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(dir_real)
        if os.path.basename(dir_real) in SKIP_DIR_NAMES:
            dirnames[:] = []
            continue
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIR_NAMES
            and os.path.realpath(os.path.join(dirpath, name)) not in seen_dirs
        ]
        for filename in filenames:
            if not filename.endswith(ARCHIVE_SUFFIX):
                continue
            file_real = os.path.realpath(os.path.join(dirpath, filename))
            if file_real in seen_files or not os.path.isfile(file_real):
                continue
            seen_files.add(file_real)
            yield Path(file_real)


@dataclass(frozen=True)
class Hit:
    archive: Path
    member: str | None
    addr: str | None
    size: str | None
    kind: str
    name: str


def parse_nm_hits(
    output: str,
    archive: Path,
    pattern: re.Pattern[str],
    types: set[str] | None,
    *,
    whole: bool,
) -> list[Hit]:
    hits: list[Hit] = []
    member: str | None = None
    archive_name = archive.name
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        matched = _SYM_LINE.match(line)
        if matched is None:
            if line.endswith(":"):
                header = line[:-1]
                paren = re.fullmatch(r"(.*)\((.*)\)", header)
                if paren:
                    member = os.path.basename(paren.group(2))
                else:
                    base = os.path.basename(header)
                    member = None if base == archive_name else base
            continue
        addr, size, kind, name = matched.groups()
        if types is not None and kind not in types:
            continue
        if not name_matches(name, pattern, whole=whole):
            continue
        hits.append(Hit(archive, member, addr, size, kind, name))
    return hits


def nm_archive(path: Path) -> str | None:
    last_err: OSError | None = None
    for cmd in (["nm", "-S", str(path)], ["nm", str(path)]):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            last_err = exc
            continue
        if result.returncode != 0 and not result.stdout.strip():
            continue
        return result.stdout
    if last_err is not None:
        _warn(f"nm failed on {path}: {last_err}")
    return None


def fmt_hex(value: str | None) -> str:
    if not value:
        return "-"
    return hex(int(value, 16))


def type_color(kind: str) -> str:
    if kind in "Tt":
        return "1;32"
    if kind in "Ww":
        return "1;33"
    if kind in "Uu":
        return "1;31"
    if kind in "DdBbRrVv":
        return "1;36"
    return "1;37"


def rel_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def print_search_banner(
    symbol: str,
    *,
    root: Path,
    ignore_case: bool,
    word: bool,
    is_glob: bool,
    types: set[str] | None,
    chips: set[str] | None,
) -> None:
    _info(f"lookup  {_c('1;37', symbol)}")
    bits = [
        "glob" if is_glob else "literal",
        "whole-word" if word else "substring",
        "case-insensitive" if ignore_case else "case-sensitive",
    ]
    if types is None:
        bits.append("all types")
    else:
        bits.append(
            "types "
            + ",".join(sorted(types, key=lambda c: (c.lower(), c)))
        )
    if chips:
        bits.append("chips " + ",".join(sorted(chips)))
    print(f"       {_dim(' · '.join(bits))}", flush=True)
    print(f"       {_dim(f'{root}  (skip {BUILD_DIR}/)')}", flush=True)


def print_report(
    hits: list[Hit],
    *,
    root: Path,
    scanned: int,
    skipped: int,
) -> None:
    by_arch: dict[Path, list[Hit]] = {}
    order: list[Path] = []
    for hit in hits:
        if hit.archive not in by_arch:
            order.append(hit.archive)
            by_arch[hit.archive] = []
        by_arch[hit.archive].append(hit)

    n_arch = len(order)
    _info(
        f"{_c('1;32', str(len(hits)))} hit(s) in "
        f"{_c('1;32', str(n_arch))} archive(s)  "
        f"{_dim(f'({scanned} scanned, {skipped} unread)')}"
    )
    print(flush=True)

    for i, archive in enumerate(order, 1):
        arch_hits = by_arch[archive]
        found_chips = chips_in_path(str(archive))
        chip_txt = (
            "  " + _c("1;33", "[" + ", ".join(sorted(found_chips)) + "]")
            if found_chips else ""
        )
        print(
            f"  {_c('1;36', f'{i}.')} "
            f"{_c('1;37', archive.name)}"
            f"{chip_txt}",
            flush=True,
        )
        print(f"     {_dim('path    ')}{rel_path(archive, root)}", flush=True)

        members: dict[str | None, list[Hit]] = {}
        mem_order: list[str | None] = []
        for hit in arch_hits:
            if hit.member not in members:
                mem_order.append(hit.member)
                members[hit.member] = []
            members[hit.member].append(hit)

        for member in mem_order:
            obj = member if member else "(archive)"
            print(f"     {_dim('object  ')}{_c('36', obj)}", flush=True)
            for hit in members[member]:
                meaning = TYPE_DESC.get(hit.kind, "unknown")
                kind = _c(type_color(hit.kind), f"{hit.kind:>1}")
                addr = f"{fmt_hex(hit.addr):<10}"
                size = f"{fmt_hex(hit.size):<8}"
                name = _c("1;37", hit.name)
                print(
                    f"       {kind}  {_dim(addr)} {_dim(size)} "
                    f"{name}  {_dim(meaning)}",
                    flush=True,
                )
        print(flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lookup-symbol.py",
        description=(
            "Find a symbol in static libraries (*.a) under the current "
            "directory and its subdirectories (directory symlinks followed; "
            "build/ skipped)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
nm types (default: defined functions and variables, not U/w/v):
  T / t   global / local code (function implementation)
  W / w   weak defined / weak undefined
  D / d   initialized data
  B / b   BSS (zeroed data)
  R / r   read-only data
  C / V   common / weak object
  U       undefined (referenced, not defined here)
  -t T,t,W     functions only
  -t a / -t all    do not filter by type

glob: * / ? in SYMBOL match any / one character.
  %(prog)s esp_at_*_cmd_regist
  Quote the glob if * matches files in the current directory.
  A pattern with no letters/digits (e.g. *) is rejected.

chips:
  -c esp32c3
  -c esp32c3,esp32c5
  -c {esp32c2,esp32c3}            (bash expands; no quotes needed)

  Archives whose path/name contains another chip (libesp32s2*.a,
  **/esp32s2/*.a, ...) are dropped. Archives with no chip id are kept.
  Known: """ + ", ".join(KNOWN_CHIPS) + """

examples:
  %(prog)s esp_wifi_connect
  %(prog)s esp_at_*_cmd_regist
  %(prog)s -w mqtt
  %(prog)s -c {esp32c2,esp32c3} esp_wifi_set_ps
  %(prog)s -t a g_esp_netif
""",
    )
    p.add_argument(
        "symbol",
        nargs="+",
        help="symbol name; * / ? are globs (any / one character)",
    )
    p.add_argument(
        "-i", "--ignore-case",
        action="store_true",
        help="case-insensitive match",
    )
    p.add_argument(
        "-w", "--word",
        action="store_true",
        help="match a whole symbol name (default: substring)",
    )
    p.add_argument(
        "-t", "--type",
        metavar="TYPES",
        dest="types",
        default=DEFAULT_TYPES,
        help=(
            "nm types to keep, comma-separated, or a/all "
            f"(default: defined funcs+vars: {DEFAULT_TYPES})"
        ),
    )
    p.add_argument(
        "-c", "--chip",
        action="append",
        metavar="CHIP",
        dest="chips",
        help=(
            "keep archives for this chip (and archives that name no chip); "
            "repeatable, comma list, or {a,b} (quotes optional)"
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(fold_chip_argv(raw))
    if shutil.which("nm") is None:
        _die(
            "nm not found in PATH",
            hints=["install binutils, or use the Espressif toolchain nm"],
        )

    types = parse_types(args.types)
    chips = parse_chip_args(args.chips)
    symbol = coalesce_symbol_tokens(args.symbol)
    warn_if_shell_globbed(args.symbol, symbol)
    if symbol_is_too_broad(symbol):
        _die(
            f"unreasonable pattern {symbol!r}; "
            "include part of the name, e.g. esp_at_*",
            hints=[
                "example: lookup-symbol.py esp_wifi_connect",
                "example: lookup-symbol.py esp_at_*_cmd_regist",
            ],
        )
    word = args.word
    is_glob = symbol_is_glob(symbol)
    pattern = compile_name_pattern(
        symbol,
        ignore_case=args.ignore_case,
        word=word,
        is_glob=is_glob,
    )
    root = Path.cwd().resolve()
    print_search_banner(
        symbol,
        root=root,
        ignore_case=args.ignore_case,
        word=word,
        is_glob=is_glob,
        types=types,
        chips=chips,
    )

    hits: list[Hit] = []
    skipped = 0
    scanned = 0
    for path in iter_archives(root):
        if not archive_matches_chips(path, chips):
            continue
        output = nm_archive(path)
        if output is None:
            skipped += 1
            continue
        scanned += 1
        hits.extend(parse_nm_hits(
            output, path, pattern, types,
            whole=(word or is_glob),
        ))

    if skipped:
        _warn(f"{skipped} archive(s) could not be read by nm")

    if not hits:
        _error(
            f"no match for {_c('1;37', symbol, stream=sys.stderr)}  "
            f"{_dim(f'({scanned} scanned, {skipped} unread)', stream=sys.stderr)}"
        )
        hint_no_match(
            symbol,
            scanned=scanned,
            word=word,
            is_glob=is_glob,
            types=types,
            chips=chips,
        )
        return 1

    print_report(hits, root=root, scanned=scanned, skipped=skipped)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        if sys.stderr.isatty():
            sys.stderr.write("\n")
            sys.stderr.flush()
        _die("Interrupted.", code=130)
