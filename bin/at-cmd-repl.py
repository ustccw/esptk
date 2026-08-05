#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
ESP-AT Command REPL

Interactive dual-port serial tool: AT log port + AT command port.
Type AT commands (or file paths) on stdin; watch replies from both ports.
"""

import argparse
import os
import platform
import re
import select
import signal
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, TextIO, Tuple

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.stderr.write(
        "[ERROR] pyserial not found. Install with "
        "'pip install pyserial' (see requirements.txt).\n"
    )
    sys.exit(1)

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import termios
    import tty
except ImportError:  # Windows
    termios = None
    tty = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

# ESP-IDF style levels: I/W/E/D/V/A/F, optionally preceded by CR from terminal.
_LOG_LEVEL_RE = re.compile(r'^[\r]*([IWEADVF]) ')
_LEVEL_COLORS = {
    'I': '32',  # green
    'W': '33',  # yellow
    'E': '31',  # red
    'D': '36',  # cyan
    'V': '37',  # white/default bright
    'A': '35',  # magenta
    'F': '31',  # red (fatal)
}

CTRL_A = '\x01'
CTRL_C = '\x03'
CTRL_D = '\x04'
CTRL_E = '\x05'
CTRL_K = '\x0b'
CTRL_R = '\x12'
CTRL_U = '\x15'
CTRL_W = '\x17'
BACKSPACE_CHARS = ('\x7f', '\x08')

REPL_PROMPT = '>>> '

# Word chars for Ctrl+Left/Right / Ctrl+W (shell-like).
_WORD_CHAR_RE = re.compile(r'[A-Za-z0-9_]')

# Windows msvcrt extended keys (prefix \xe0 / \x00) -> CSI sequences.
_WIN_EXT_KEYS = {
    'K': '\x1b[D',      # Left
    'M': '\x1b[C',      # Right
    'G': '\x1b[H',      # Home
    'O': '\x1b[F',      # End
    'S': '\x1b[3~',     # Delete
    's': '\x1b[1;5D',   # Ctrl+Left
    't': '\x1b[1;5C',   # Ctrl+Right
}

# Path prefixes that trigger file-send mode.
_WIN_DRIVE_RE = re.compile(r'^[A-Za-z]:[\\/]')

# AT response terminators (exact line match after strip).
_AT_END_LINES = frozenset({'OK', 'ERROR', 'FAIL'})


class AtCmdRepl:
    """Dual-port AT command REPL for ESP-AT devices."""

    def __init__(self):
        self.log_file_handle: Optional[TextIO] = None
        self.log_file_path: Optional[str] = None
        self.enable_timestamp: bool = True
        self.enable_prompt: bool = False
        self.should_exit: bool = False

        self.log_serial: Optional[serial.Serial] = None
        self.cmd_serial: Optional[serial.Serial] = None
        self._same_port: bool = False
        self._log_locked: bool = False
        self._cmd_locked: bool = False

        self._stdin_old_attrs = None
        self._line_buf: List[str] = []
        self._line_cursor: int = 0  # index into _line_buf (0 == after >>>)
        # Leftover stdin bytes/chars when a UTF-8 or CSI sequence is split
        # across reads (select + TextIO.read(1) is unsafe; we use os.read).
        self._stdin_byte_buf = bytearray()
        self._stdin_esc_buf: str = ''

        # Per-port RX reassembly: only emit complete lines (or idle-flushed partials).
        self._rx_bufs: Dict[str, str] = {'log': '', 'cmd': ''}
        self._rx_buf_touched: Dict[str, float] = {'log': 0.0, 'cmd': 0.0}
        # Idle flush for prompts without newline (e.g. '>').
        self._partial_flush_s: float = 0.05

        # REPL >>> prompt state
        self._at_ready: bool = False
        self._awaiting_response: bool = False
        self._repl_prompt_visible: bool = False

        self.port0: Optional[str] = None
        self.port1: Optional[str] = None
        self.port0_baudrate: int = 115200
        self.port1_baudrate: int = 115200
        self.flow_control: bool = False
        self.no_reboot_chip: bool = False

    # ------------------------------------------------------------------
    # Color / formatting
    # ------------------------------------------------------------------

    @staticmethod
    def color_enabled(stream) -> bool:
        if os.environ.get('NO_COLOR'):
            return False
        if os.environ.get('FORCE_COLOR'):
            return True
        try:
            return stream.isatty()
        except Exception:
            return False

    @staticmethod
    def colorize(message: str, color_code: str, stream=sys.stdout) -> str:
        if AtCmdRepl.color_enabled(stream):
            return f'\033[{color_code}m{message}\033[0m'
        return message

    @staticmethod
    def colorize_esp_log_line(message: str, formatted_msg: str, stream=sys.stdout) -> str:
        match = _LOG_LEVEL_RE.match(message)
        if not match:
            return formatted_msg
        color_code = _LEVEL_COLORS.get(match.group(1))
        if not color_code:
            return formatted_msg
        return AtCmdRepl.colorize(formatted_msg, color_code, stream)

    def _line_prefix(self, tag: Optional[str] = None) -> str:
        """Build timestamp / source-tag prefix for the start of a line."""
        parts: List[str] = []
        if self.enable_timestamp:
            parts.append(f'[{datetime.now()}]')
        if self.enable_prompt and tag:
            parts.append(f'({tag})')
        if not parts:
            return ''
        return ' '.join(parts) + ' '

    def _write_to_file(self, message: str, add_newline: bool = True) -> None:
        if self.log_file_handle:
            suffix = '\n' if add_newline else ''
            self.log_file_handle.write(f'{message}{suffix}')
            self.log_file_handle.flush()

    def _clear_input_line_for_output(self) -> None:
        """Erase the current in-progress input / >>> so serial output is clean."""
        if not sys.stdout.isatty():
            return
        if not self._line_buf and not self._repl_prompt_visible:
            return
        sys.stdout.write('\r\033[K')
        sys.stdout.flush()
        self._repl_prompt_visible = False

    def _redraw_input_line(self) -> None:
        """Redraw >>> (if idle) and the in-progress input after serial output."""
        if not sys.stdout.isatty():
            return
        if self._should_show_repl_prompt():
            sys.stdout.write(REPL_PROMPT)
            self._repl_prompt_visible = True
        line = ''.join(self._line_buf)
        if line:
            sys.stdout.write(line)
        # Place cursor at _line_cursor (may be mid-line while editing).
        behind = len(self._line_buf) - self._line_cursor
        if behind > 0:
            sys.stdout.write(f'\033[{behind}D')
        sys.stdout.flush()

    def _should_show_repl_prompt(self) -> bool:
        return self._at_ready and not self._awaiting_response and not self.should_exit

    def _emit_repl_prompt(self) -> None:
        """Print >>> when AT is ready and we are idle."""
        if not self._should_show_repl_prompt():
            return
        if not sys.stdout.isatty():
            # Still mark visible logic for non-TTY tests; print for piping clarity.
            sys.stdout.write(REPL_PROMPT)
            sys.stdout.flush()
            self._repl_prompt_visible = True
            return
        if self._repl_prompt_visible and not self._line_buf:
            return
        self._clear_input_line_for_output()
        sys.stdout.write(REPL_PROMPT)
        sys.stdout.flush()
        self._repl_prompt_visible = True

    def log_info(self, message: str) -> None:
        formatted_msg = self._line_prefix('pc') + message
        self._clear_input_line_for_output()
        print(self.colorize(formatted_msg, '32'))
        self._write_to_file(formatted_msg)
        self._redraw_input_line()

    def log_error(self, message: str) -> None:
        formatted_msg = self._line_prefix('pc') + message
        self._clear_input_line_for_output()
        sys.stderr.write(self.colorize(f'{formatted_msg}\n', '31', sys.stderr))
        self._write_to_file(formatted_msg)
        self._redraw_input_line()

    def log_warn(self, message: str) -> None:
        formatted_msg = self._line_prefix('pc') + message
        self._clear_input_line_for_output()
        print(self.colorize(formatted_msg, '33'))
        self._write_to_file(formatted_msg)
        self._redraw_input_line()

    def _emit_serial_line(self, line: str, tag: str, add_newline: bool = True) -> None:
        """Atomically print one reassembled serial line with prefix."""
        # Preserve original newline in file/stdout when present on `line`.
        raw = line
        if add_newline and not raw.endswith('\n'):
            raw = raw + '\n'
        prefix = self._line_prefix(tag)
        formatted = prefix + raw
        match = _LOG_LEVEL_RE.match(raw)
        color = _LEVEL_COLORS.get(match.group(1)) if match else None
        to_print = self.colorize(formatted, color) if color else formatted

        self._clear_input_line_for_output()
        print(to_print, end='')
        sys.stdout.flush()
        self._write_to_file(formatted, add_newline=False)

        # ready / OK detection (strip CR/LF)
        self._handle_rx_line(raw.rstrip('\r\n'), tag)
        self._redraw_input_line()

    def log_raw(self, message: str, tag: str) -> None:
        """Reassemble per-port lines, then emit complete lines without interleaving."""
        if not message or tag not in self._rx_bufs:
            return

        self._rx_bufs[tag] += message
        self._rx_buf_touched[tag] = time.monotonic()
        self._flush_complete_lines(tag)

    def _flush_complete_lines(self, tag: str) -> None:
        buf = self._rx_bufs.get(tag, '')
        while True:
            nl = buf.find('\n')
            if nl < 0:
                self._rx_bufs[tag] = buf
                return
            line = buf[: nl + 1]  # include '\n'
            buf = buf[nl + 1 :]
            self._emit_serial_line(line, tag, add_newline=False)

    def _flush_idle_partials(self) -> None:
        """Flush partial lines that sat idle (e.g. AT '>' prompt without newline)."""
        now = time.monotonic()
        for tag in ('log', 'cmd'):
            buf = self._rx_bufs.get(tag, '')
            if not buf:
                continue
            touched = self._rx_buf_touched.get(tag, 0.0)
            if now - touched < self._partial_flush_s:
                continue
            self._rx_bufs[tag] = ''
            self._emit_serial_line(buf, tag, add_newline=False)

    def _handle_rx_line(self, line: str, tag: str) -> None:
        stripped = line.strip()
        if stripped.lower() == 'ready':
            self._at_ready = True
            self._awaiting_response = False
            return
        if stripped in _AT_END_LINES and self._at_ready:
            self._awaiting_response = False

    # ------------------------------------------------------------------
    # Port discovery / validation
    # ------------------------------------------------------------------

    @staticmethod
    def is_candidate_port(device: str, system: Optional[str] = None) -> bool:
        system = (system or platform.system()).lower()
        if system == 'linux':
            return 'ttyUSB' in device or 'ttyACM' in device
        if system == 'darwin':
            return 'tty.usbserial' in device or 'tty.usbmodem' in device
        if system == 'windows':
            return device.upper().startswith('COM')
        return False

    @staticmethod
    def find_candidate_ports() -> List[str]:
        ports = list(serial.tools.list_ports.comports())
        system = platform.system().lower()
        ports = [p for p in ports if AtCmdRepl.is_candidate_port(p.device, system)]
        ports.sort(key=lambda p: p.device)
        return [p.device for p in ports]

    @staticmethod
    def resolve_port(port: Optional[str]) -> Optional[str]:
        """Normalize a port argument.

        Full device paths are unchanged. A bare digit ``N`` is accepted as a
        shortcut for ``/dev/ttyUSBN`` (``COMn`` on Windows). ``None`` stays
        ``None`` for later auto-detect.
        """
        if port is None:
            return None
        port = str(port).strip()
        if not port:
            return port
        if port.isdigit():
            system = platform.system().lower()
            if system == 'windows':
                return f'COM{port}'
            return f'/dev/ttyUSB{port}'
        return port

    @staticmethod
    def resolve_ports(
        port0: Optional[str], port1: Optional[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """Resolve AT log / command ports from args or auto-detect."""
        port0 = AtCmdRepl.resolve_port(port0)
        port1 = AtCmdRepl.resolve_port(port1)
        if port0 is not None and port1 is not None:
            return port0, port1

        candidates = AtCmdRepl.find_candidate_ports()
        if not candidates:
            return port0, port1

        if port0 is None and port1 is None:
            if len(candidates) == 1:
                return candidates[0], candidates[0]
            return candidates[0], candidates[1]

        if port0 is None:
            for c in candidates:
                if c != port1:
                    return c, port1
            return port1, port1

        for c in candidates:
            if c != port0:
                return port0, c
        return port0, port0

    @staticmethod
    def validate_serial_port(port: Optional[str]) -> str:
        if not port:
            raise ValueError('No available serial port found')
        if platform.system().lower() != 'windows' and not os.path.exists(port):
            raise ValueError(f"Serial port '{port}' does not exist")
        try:
            with serial.Serial(port) as _:
                pass
        except Exception as e:
            raise ValueError(f"Cannot access serial port '{port}': {e}") from e
        return port

    @staticmethod
    def create_directory(path: str) -> None:
        path = path.strip().rstrip('\\/')
        os.makedirs(path, exist_ok=True)

    def create_log_file(self) -> TextIO:
        try:
            log_dir = os.path.join(os.getcwd(), 'esp_logs')
            self.create_directory(log_dir)
            filename = datetime.now().strftime('%Y%m%d_%H%M%S_%f.log')
            self.log_file_path = os.path.join(log_dir, filename)
            return open(self.log_file_path, 'w', encoding='utf-8')
        except Exception as e:
            raise RuntimeError(f'Failed to create log file: {e}') from e

    # ------------------------------------------------------------------
    # Serial lock / open / close
    # ------------------------------------------------------------------

    def _lock_port(self, ser: serial.Serial, which: str) -> None:
        if fcntl is None:
            return
        try:
            fcntl.flock(ser.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if which == 'log':
                self._log_locked = True
            else:
                self._cmd_locked = True
        except (BlockingIOError, OSError) as e:
            raise RuntimeError(f'Serial port {ser.port} is locked by another process') from e

    def _unlock_port(self, ser: Optional[serial.Serial], which: str) -> None:
        locked = self._log_locked if which == 'log' else self._cmd_locked
        if not locked or fcntl is None or ser is None:
            if which == 'log':
                self._log_locked = False
            else:
                self._cmd_locked = False
            return
        try:
            fcntl.flock(ser.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        if which == 'log':
            self._log_locked = False
        else:
            self._cmd_locked = False

    def _flush_all_partials(self) -> None:
        """Force-flush any remaining partial RX buffers (e.g. on close)."""
        for tag in ('log', 'cmd'):
            buf = self._rx_bufs.get(tag, '')
            if not buf:
                continue
            self._rx_bufs[tag] = ''
            self._emit_serial_line(buf, tag, add_newline=False)

    def _close_serials(self) -> None:
        if self._same_port:
            if self.log_serial:
                self._unlock_port(self.log_serial, 'log')
                try:
                    if self.log_serial.in_waiting:
                        data = self.log_serial.read(self.log_serial.in_waiting).decode(
                            'utf-8', 'ignore'
                        )
                        self.log_raw(data, 'cmd')
                except Exception:
                    pass
                self._flush_complete_lines('cmd')
                self._flush_all_partials()
                try:
                    self.log_serial.close()
                except Exception:
                    pass
            self.log_serial = None
            self.cmd_serial = None
            return

        for which, ser in (('log', self.log_serial), ('cmd', self.cmd_serial)):
            if not ser:
                continue
            try:
                if ser.in_waiting:
                    data = ser.read(ser.in_waiting).decode('utf-8', 'ignore')
                    self.log_raw(data, which)
            except Exception:
                pass
            self._unlock_port(ser, which)
            try:
                ser.close()
            except Exception:
                pass
        self._flush_complete_lines('log')
        self._flush_complete_lines('cmd')
        self._flush_all_partials()
        self.log_serial = None
        self.cmd_serial = None

    def _open_serials(self) -> bool:
        """Open log/cmd serial ports. Returns True on success."""
        self._same_port = self.port0 == self.port1
        try:
            if self._same_port:
                ser = serial.Serial(
                    self.port0,
                    self.port1_baudrate,
                    timeout=0,
                    rtscts=self.flow_control,
                )
                self._lock_port(ser, 'log')
                self.log_serial = ser
                self.cmd_serial = ser
                self.log_info(
                    f'Opened {self.port0} (log+cmd) with baudrate {self.port1_baudrate}'
                    + (' flow-control' if self.flow_control else '')
                )
            else:
                log_ser = serial.Serial(
                    self.port0, self.port0_baudrate, timeout=0, rtscts=False
                )
                self._lock_port(log_ser, 'log')
                cmd_ser = serial.Serial(
                    self.port1,
                    self.port1_baudrate,
                    timeout=0,
                    rtscts=self.flow_control,
                )
                self._lock_port(cmd_ser, 'cmd')
                self.log_serial = log_ser
                self.cmd_serial = cmd_ser
                self.log_info(f'Opened log port {self.port0} @ {self.port0_baudrate}')
                self.log_info(
                    f'Opened cmd port {self.port1} @ {self.port1_baudrate}'
                    + (' flow-control' if self.flow_control else '')
                )
            return True
        except Exception as e:
            self._close_serials()
            raise e

    # ------------------------------------------------------------------
    # Reset / stdin hotkeys / line editing
    # ------------------------------------------------------------------

    def reset_esp_chip(self) -> None:
        """Reset the ESP chip via DTR/RTS on the AT log port."""
        ser = self.log_serial
        if not ser or not ser.is_open:
            self.log_warn('Cannot reset: log serial port is not open')
            return
        self._at_ready = False
        self._awaiting_response = False
        self._repl_prompt_visible = False
        ser.dtr = False
        ser.rts = True
        time.sleep(0.1)
        ser.rts = False
        time.sleep(0.05)
        self.log_info('ESP chip reset completed')

    def _enable_hotkeys(self) -> None:
        self._stdin_old_attrs = None
        if not sys.stdin.isatty() or termios is None or tty is None:
            return
        try:
            fd = sys.stdin.fileno()
            self._stdin_old_attrs = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            # Bracketed paste: terminal wraps paste in ESC[200~ ... ESC[201~.
            sys.stdout.write('\033[?2004h')
            sys.stdout.flush()
        except Exception:
            self._stdin_old_attrs = None

    def _disable_hotkeys(self) -> None:
        if self._stdin_old_attrs is None or termios is None:
            return
        try:
            if sys.stdout.isatty():
                sys.stdout.write('\033[?2004l')
                sys.stdout.flush()
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._stdin_old_attrs)
        except Exception:
            pass
        self._stdin_old_attrs = None

    def _read_stdin_chunk(self) -> str:
        """Read all currently available stdin input.

        Must use os.read on the raw fd. Mixing select() on the fd with
        buffered sys.stdin.read(1) drops paste: the first read() slurps the
        whole paste into TextIO, select() then sees an empty fd, and only the
        first character (e.g. ``A`` of ``AT+...``) is processed.
        """
        if not sys.stdin.isatty():
            return ''
        try:
            if msvcrt is not None and platform.system().lower() == 'windows':
                chars: List[str] = []
                while msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if isinstance(ch, bytes):
                        # Extended keys: \xe0 / \x00 + code
                        if ch in (b'\xe0', b'\x00') and msvcrt.kbhit():
                            code = msvcrt.getch()
                            key = code.decode('latin1') if isinstance(code, bytes) else code
                            chars.append(_WIN_EXT_KEYS.get(key, ''))
                            continue
                        chars.append(ch.decode('latin1'))
                    else:
                        chars.append(ch)
                return ''.join(chars)

            fd = sys.stdin.fileno()
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                return ''
            data = os.read(fd, 4096)
            if not data:
                return ''
            self._stdin_byte_buf.extend(data)
            try:
                text = self._stdin_byte_buf.decode('utf-8')
                self._stdin_byte_buf.clear()
                return text
            except UnicodeDecodeError as e:
                # Keep a trailing incomplete UTF-8 sequence for the next read.
                if e.start > 0:
                    text = bytes(self._stdin_byte_buf[: e.start]).decode('utf-8')
                    del self._stdin_byte_buf[: e.start]
                    return text
                # Invalid leading byte — drop it and retry next time.
                del self._stdin_byte_buf[0]
                return ''
        except Exception:
            return ''

    @staticmethod
    def _csi_final_index(buf: str, start: int) -> int:
        """Return index of CSI final byte, or -1 if the sequence is incomplete."""
        # CSI: ESC [ params... final(0x40-0x7E)
        i = start
        while i < len(buf):
            o = ord(buf[i])
            if 0x40 <= o <= 0x7E:
                return i
            i += 1
        return -1

    def _parse_stdin_escape(self, buf: str) -> Tuple[Optional[str], str, str]:
        """Parse a leading ANSI escape into an editing key name.

        Returns ``(key, remainder, incomplete)``. ``key`` is one of
        left/right/home/end/delete/ctrl-left/ctrl-right, or None to ignore.
        ``incomplete`` is non-empty when the escape was split across reads.
        """
        if not buf or buf[0] != '\x1b':
            return None, buf, ''
        if len(buf) == 1:
            return None, '', buf

        # SS3: ESC O A/B/C/D/H/F (application cursor keys)
        if buf[1] == 'O':
            if len(buf) < 3:
                return None, '', buf
            key = {
                'D': 'left',
                'C': 'right',
                'H': 'home',
                'F': 'end',
            }.get(buf[2])
            return key, buf[3:], ''

        # CSI: ESC [ ...
        if buf[1] != '[':
            # ESC + single char (Alt-key); ignore
            return None, buf[2:], ''

        final = self._csi_final_index(buf, 2)
        if final < 0:
            return None, '', buf

        params = buf[2:final]
        cmd = buf[final]
        rest = buf[final + 1 :]

        # Modifier form: ESC [ 1 ; 5 D  (5 = Ctrl) — also ESC [ 5 D on some terms
        mod = 1
        body = params
        if ';' in params:
            parts = params.split(';')
            body = parts[0] if parts[0] else '1'
            try:
                mod = int(parts[1]) if len(parts) > 1 and parts[1] else 1
            except ValueError:
                mod = 1
        elif params.isdigit() and cmd in 'ABCDHF' and params in ('5', '3', '2', '4'):
            # Rare short form ESC [ 5 D
            body = '1'
            mod = int(params)

        ctrl = bool((mod - 1) & 4)  # xterm: bit 4 of (mod-1) = Control

        if cmd == 'D':
            return ('ctrl-left' if ctrl else 'left'), rest, ''
        if cmd == 'C':
            return ('ctrl-right' if ctrl else 'right'), rest, ''
        if cmd == 'H':
            return 'home', rest, ''
        if cmd == 'F':
            return 'end', rest, ''
        if cmd == '~':
            # VT sequences: 1~/7~ home, 4~/8~ end, 3~ delete
            code = body.split(';')[0] if body else ''
            return {
                '1': 'home',
                '7': 'home',
                '4': 'end',
                '8': 'end',
                '3': 'delete',
            }.get(code), rest, ''

        # Bracketed paste markers 200~/201~ and other CSI — ignore
        return None, rest, ''

    @staticmethod
    def _is_word_char(ch: str) -> bool:
        return bool(_WORD_CHAR_RE.match(ch))

    def _clamp_cursor(self) -> None:
        if self._line_cursor < 0:
            self._line_cursor = 0
        elif self._line_cursor > len(self._line_buf):
            self._line_cursor = len(self._line_buf)

    def _move_cursor(self, pos: int) -> None:
        self._line_cursor = max(0, min(pos, len(self._line_buf)))
        self._clear_input_line_for_output()
        self._redraw_input_line()

    def _cursor_left(self) -> None:
        if self._line_cursor > 0:
            self._move_cursor(self._line_cursor - 1)

    def _cursor_right(self) -> None:
        if self._line_cursor < len(self._line_buf):
            self._move_cursor(self._line_cursor + 1)

    def _cursor_home(self) -> None:
        self._move_cursor(0)

    def _cursor_end(self) -> None:
        self._move_cursor(len(self._line_buf))

    def _cursor_word_left(self) -> None:
        i = self._line_cursor
        while i > 0 and not self._is_word_char(self._line_buf[i - 1]):
            i -= 1
        while i > 0 and self._is_word_char(self._line_buf[i - 1]):
            i -= 1
        self._move_cursor(i)

    def _cursor_word_right(self) -> None:
        i = self._line_cursor
        n = len(self._line_buf)
        while i < n and not self._is_word_char(self._line_buf[i]):
            i += 1
        while i < n and self._is_word_char(self._line_buf[i]):
            i += 1
        self._move_cursor(i)

    def _handle_editing_key(self, key: str) -> None:
        handlers = {
            'left': self._cursor_left,
            'right': self._cursor_right,
            'home': self._cursor_home,
            'end': self._cursor_end,
            'delete': self._handle_delete,
            'ctrl-left': self._cursor_word_left,
            'ctrl-right': self._cursor_word_right,
        }
        handler = handlers.get(key)
        if handler:
            handler()

    @staticmethod
    def looks_like_path(text: str) -> bool:
        if not text:
            return False
        if text.startswith('/') or text.startswith('./') or text.startswith('~/'):
            return True
        if _WIN_DRIVE_RE.match(text):
            return True
        return False

    @staticmethod
    def expand_path(text: str) -> str:
        return os.path.expanduser(text)

    def _prepare_payload(self, line: str) -> Optional[bytes]:
        """Convert a submitted REPL line into bytes to send, or None to skip."""
        if not line:
            return None

        if self.looks_like_path(line):
            path = self.expand_path(line)
            if not os.path.isfile(path):
                self.log_error(f'File not found: {path}')
                return None
            try:
                with open(path, 'rb') as f:
                    data = f.read()
            except Exception as e:
                self.log_error(f'Failed to read file {path}: {e}')
                return None
            self.log_info(f'Sending file {path} ({len(data)} bytes)')
            return data

        # All other keyboard input is treated as an AT command line.
        return (line + '\r\n').encode('utf-8')

    def _send_to_cmd(self, data: bytes) -> None:
        if not self.cmd_serial or not self.cmd_serial.is_open:
            self.log_warn('Cannot send: command port is not open')
            return
        try:
            self.cmd_serial.write(data)
            self.cmd_serial.flush()
        except Exception as e:
            self.log_error(f'Failed to write to command port: {e}')

    def _submit_line(self) -> None:
        line = ''.join(self._line_buf)
        self._line_buf = []
        self._line_cursor = 0
        self._repl_prompt_visible = False
        if sys.stdout.isatty():
            sys.stdout.write('\r\n')
            sys.stdout.flush()
        payload = self._prepare_payload(line)
        if payload is None:
            self._emit_repl_prompt()
            return
        # File payloads: do not wait for OK unless device replies; still mark busy
        # for AT-style text commands so >>> returns after OK/ERROR/FAIL.
        if not self.looks_like_path(line):
            self._awaiting_response = True
        self._send_to_cmd(payload)

    def _echo_char(self, ch: str) -> None:
        if sys.stdout.isatty():
            sys.stdout.write(ch)
            sys.stdout.flush()

    def _handle_backspace(self) -> None:
        if self._line_cursor <= 0:
            return
        self._line_cursor -= 1
        del self._line_buf[self._line_cursor]
        if (
            sys.stdout.isatty()
            and self._line_cursor == len(self._line_buf)
            and self._repl_prompt_visible
        ):
            # Fast path: deleting at end of line.
            sys.stdout.write('\b \b')
            sys.stdout.flush()
            return
        self._clear_input_line_for_output()
        self._redraw_input_line()

    def _handle_delete(self) -> None:
        """Forward-delete character under the cursor (Delete key)."""
        if self._line_cursor >= len(self._line_buf):
            return
        del self._line_buf[self._line_cursor]
        self._clear_input_line_for_output()
        self._redraw_input_line()

    def _kill_to_end(self) -> None:
        if self._line_cursor >= len(self._line_buf):
            return
        del self._line_buf[self._line_cursor :]
        self._clear_input_line_for_output()
        self._redraw_input_line()

    def _kill_line(self) -> None:
        if not self._line_buf:
            return
        self._line_buf = []
        self._line_cursor = 0
        self._clear_input_line_for_output()
        self._redraw_input_line()

    def _kill_word_backward(self) -> None:
        if self._line_cursor <= 0:
            return
        end = self._line_cursor
        i = end
        while i > 0 and not self._is_word_char(self._line_buf[i - 1]):
            i -= 1
        while i > 0 and self._is_word_char(self._line_buf[i - 1]):
            i -= 1
        del self._line_buf[i:end]
        self._line_cursor = i
        self._clear_input_line_for_output()
        self._redraw_input_line()

    def _insert_printable(self, text: str) -> None:
        """Insert printable text at the cursor and refresh the display."""
        if not text:
            return
        if self._should_show_repl_prompt() and not self._repl_prompt_visible:
            self._emit_repl_prompt()
        at_end = self._line_cursor == len(self._line_buf)
        for i, ch in enumerate(text):
            self._line_buf.insert(self._line_cursor + i, ch)
        self._line_cursor += len(text)
        # Fast path: single char typed at end — just echo.
        if len(text) == 1 and at_end and self._repl_prompt_visible:
            self._echo_char(text)
            return
        self._clear_input_line_for_output()
        self._redraw_input_line()

    def _poll_stdin(self) -> None:
        chunk = self._stdin_esc_buf + self._read_stdin_chunk()
        self._stdin_esc_buf = ''
        if not chunk:
            return

        pending_printable: List[str] = []

        def flush_printable() -> None:
            if pending_printable:
                self._insert_printable(''.join(pending_printable))
                pending_printable.clear()

        while chunk:
            ch = chunk[0]
            if ch == '\x1b':
                flush_printable()
                key, chunk, incomplete = self._parse_stdin_escape(chunk)
                if incomplete:
                    self._stdin_esc_buf = incomplete
                    return
                if key:
                    self._handle_editing_key(key)
                continue
            chunk = chunk[1:]
            if ch == CTRL_C:
                flush_printable()
                self.should_exit = True
                self.log_info('\nCtrl+C pressed, exiting...')
                return
            if ch == CTRL_D:
                flush_printable()
                if not self._line_buf:
                    self.should_exit = True
                    self.log_info('\nCtrl+D pressed, exiting...')
                    return
                # Shell-like: Ctrl+D with text = forward-delete
                self._handle_delete()
                continue
            if ch == CTRL_R:
                flush_printable()
                self.log_info('Ctrl+R pressed, resetting ESP chip...')
                self.reset_esp_chip()
                continue
            if ch == CTRL_A:
                flush_printable()
                self._cursor_home()
                continue
            if ch == CTRL_E:
                flush_printable()
                self._cursor_end()
                continue
            if ch == CTRL_K:
                flush_printable()
                self._kill_to_end()
                continue
            if ch == CTRL_U:
                flush_printable()
                self._kill_line()
                continue
            if ch == CTRL_W:
                flush_printable()
                self._kill_word_backward()
                continue
            if ch in BACKSPACE_CHARS:
                flush_printable()
                self._handle_backspace()
                continue
            if ch in ('\r', '\n'):
                flush_printable()
                self._submit_line()
                continue
            if ord(ch) < 32:
                continue
            pending_printable.append(ch)

        flush_printable()

    # ------------------------------------------------------------------
    # Serial read helpers
    # ------------------------------------------------------------------

    def _drain_port(self, ser: Optional[serial.Serial], tag: str) -> None:
        if not ser or not ser.is_open:
            return
        try:
            waiting = ser.in_waiting
            if waiting <= 0:
                return
            raw = ser.read(waiting)
            if raw:
                self.log_raw(raw.decode('utf-8', 'ignore'), tag)
        except Exception as e:
            raise RuntimeError(f'Failed to read from {tag} port: {e}') from e

    def _wait_for_io(self, timeout: float = 0.05) -> None:
        """Block briefly until serial or stdin has data (Unix select; else sleep)."""
        if platform.system().lower() == 'windows' or not hasattr(select, 'select'):
            time.sleep(timeout)
            return
        fds = []
        if self.log_serial and self.log_serial.is_open:
            try:
                fds.append(self.log_serial.fileno())
            except Exception:
                pass
        if (
            not self._same_port
            and self.cmd_serial
            and self.cmd_serial.is_open
        ):
            try:
                fds.append(self.cmd_serial.fileno())
            except Exception:
                pass
        if sys.stdin.isatty():
            try:
                fds.append(sys.stdin.fileno())
            except Exception:
                pass
        if not fds:
            time.sleep(timeout)
            return
        try:
            select.select(fds, [], [], timeout)
        except (ValueError, OSError):
            time.sleep(timeout)

    def signal_handler(self, sig, frame) -> None:
        self.should_exit = True
        self.log_info('\nCtrl+C pressed, exiting...')

    def cleanup_and_exit(self) -> None:
        self._disable_hotkeys()
        self._close_serials()
        if self.log_file_handle:
            try:
                self.log_file_handle.flush()
                self.log_file_handle.close()
            except Exception:
                pass
            if self.log_file_path:
                saved = f'Log saved to: {self.log_file_path}'
                print(f'\n{self.colorize(saved, "1;32")}')
            self.log_file_handle = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, args) -> None:
        self.enable_timestamp = not args.no_timestamp
        self.enable_prompt = args.prompt
        self.port0 = args.port0
        self.port1 = args.port1
        self.port0_baudrate = args.port0_baudrate
        self.port1_baudrate = args.port1_baudrate
        self.flow_control = args.flow_control
        self.no_reboot_chip = args.no_reboot_chip

        if args.save_log:
            self.log_file_handle = self.create_log_file()

        signal.signal(signal.SIGINT, self.signal_handler)
        self._enable_hotkeys()
        self.log_info('AT Command REPL started')
        self.log_info(
            'Hotkeys: arrows/Home/End edit the line; Ctrl+Left/Right jump words; '
            'Ctrl+A/E home/end; Ctrl+K/U/W kill; Ctrl+R reset chip; Ctrl+C exit'
        )
        if self.port0 == self.port1:
            self.log_info(f'Using shared port for log+cmd: {self.port0}')
            if self.port0_baudrate != self.port1_baudrate:
                self.log_warn(
                    f'Same port: using cmd baudrate {self.port1_baudrate} '
                    f'(ignoring log baudrate {self.port0_baudrate})'
                )
        else:
            self.log_info(f'Log port: {self.port0}  Cmd port: {self.port1}')

        has_reset = False
        first_reconnect = True
        reconnect_delay = 0.5

        while not self.should_exit:
            try:
                self._open_serials()
            except Exception as e:
                if first_reconnect:
                    self.log_warn(f'Failed to open serial port(s): {e}. Reconnecting...')
                first_reconnect = False
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 2.0)
                continue

            first_reconnect = True
            reconnect_delay = 0.5

            if not self.no_reboot_chip and not has_reset:
                self.reset_esp_chip()
                has_reset = True

            while not self.should_exit:
                try:
                    self._wait_for_io(0.05)
                    if self._same_port:
                        self._drain_port(self.cmd_serial, 'cmd')
                    else:
                        self._drain_port(self.log_serial, 'log')
                        self._drain_port(self.cmd_serial, 'cmd')
                    self._flush_idle_partials()
                    self._poll_stdin()
                except Exception as e:
                    self.log_error(str(e))
                    self._close_serials()
                    break

            self._close_serials()

        self.cleanup_and_exit()


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='ESP-AT Command REPL (dual-port: AT log + AT command)',
        epilog=(
            'While running: after the module prints ready, a >>> prompt appears. '
            'Type a command and press Enter; after OK/ERROR/FAIL, >>> returns. '
            'Line editing: Left/Right, Home/End, Delete, Backspace; '
            'Ctrl+Left/Right word jump; Ctrl+A/E home/end; '
            'Ctrl+K kill-to-end; Ctrl+U kill-line; Ctrl+W kill-word. '
            'Ctrl+R resets the chip (log port); Ctrl+C exits. '
            'Path-like lines (/ ./ ~/ drive:) send the file as raw bytes; '
            'all other input is sent as an AT line with \\r\\n appended.'
        ),
    )
    parser.add_argument(
        '--port0', '-p0',
        default=None,
        help='AT log port; full path or digit N -> /dev/ttyUSBN. '
             'Default: smallest candidate port.',
    )
    parser.add_argument(
        '--port1', '-p1',
        default=None,
        help='AT command port; full path or digit N -> /dev/ttyUSBN. '
             'Default: second-smallest candidate (or same if only one).',
    )
    parser.add_argument(
        '--port0-baudrate', '-p0b',
        type=int,
        default=115200,
        help='Baud rate for AT log port. Default: 115200.',
    )
    parser.add_argument(
        '--port1-baudrate', '-p1b',
        type=int,
        default=115200,
        help='Baud rate for AT command port. Default: 115200.',
    )
    parser.add_argument(
        '--flow-control', '-fc',
        action='store_true',
        help='Enable hardware flow control on AT command port. Default: False.',
    )
    parser.add_argument(
        '--save-log', '-s',
        action='store_true',
        help='Save logs to local files under ./esp_logs/. Default: False.',
    )
    parser.add_argument(
        '--prompt', '-p',
        action='store_true',
        help='Prefix each line with source tag (log|cmd|pc). Default: False.',
    )
    parser.add_argument(
        '--no-timestamp', '-nt',
        action='store_true',
        help='Disable timestamp in log output. Default: False.',
    )
    parser.add_argument(
        '--no-reboot-chip', '-nr',
        action='store_true',
        help='Skip ESP chip reboot on the log port at start. Default: False.',
    )
    return parser


def main():
    parser = create_argument_parser()
    args = parser.parse_args()

    repl = AtCmdRepl()
    port0, port1 = AtCmdRepl.resolve_ports(args.port0, args.port1)
    try:
        args.port0 = AtCmdRepl.validate_serial_port(port0)
        args.port1 = AtCmdRepl.validate_serial_port(port1)
    except ValueError as e:
        repl.log_error(str(e))
        sys.exit(1)

    try:
        repl.run(args)
    except Exception as e:
        repl.log_error(f'A fatal error occurred: {e}')
        repl.cleanup_and_exit()
        sys.exit(1)


if __name__ == '__main__':
    main()
