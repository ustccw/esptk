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

CTRL_R = '\x12'
CTRL_C = '\x03'
CTRL_D = '\x04'
BACKSPACE_CHARS = ('\x7f', '\x08')

REPL_PROMPT = '>>> '

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
    def resolve_ports(
        port0: Optional[str], port1: Optional[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """Resolve AT log / command ports from args or auto-detect."""
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
        except Exception:
            self._stdin_old_attrs = None

    def _disable_hotkeys(self) -> None:
        if self._stdin_old_attrs is None or termios is None:
            return
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._stdin_old_attrs)
        except Exception:
            pass
        self._stdin_old_attrs = None

    def _read_stdin_char(self) -> Optional[str]:
        if not sys.stdin.isatty():
            return None
        try:
            if msvcrt is not None and platform.system().lower() == 'windows':
                if not msvcrt.kbhit():
                    return None
                ch = msvcrt.getch()
                return ch.decode('latin1') if isinstance(ch, bytes) else ch
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                return None
            return sys.stdin.read(1)
        except Exception:
            return None

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
        if not self._line_buf:
            return
        self._line_buf.pop()
        if sys.stdout.isatty():
            sys.stdout.write('\b \b')
            sys.stdout.flush()

    def _poll_stdin(self) -> None:
        while True:
            ch = self._read_stdin_char()
            if ch is None:
                return
            if ch == CTRL_C:
                self.should_exit = True
                self.log_info('\nCtrl+C pressed, exiting...')
                return
            if ch == CTRL_D:
                if not self._line_buf:
                    self.should_exit = True
                    self.log_info('\nCtrl+D pressed, exiting...')
                    return
                continue
            if ch == CTRL_R:
                self.log_info('Ctrl+R pressed, resetting ESP chip...')
                self.reset_esp_chip()
                continue
            if ch in BACKSPACE_CHARS:
                self._handle_backspace()
                continue
            if ch in ('\r', '\n'):
                self._submit_line()
                continue
            if ord(ch) < 32:
                continue
            # Ensure >>> is visible before first typed char when idle.
            if self._should_show_repl_prompt() and not self._repl_prompt_visible:
                self._emit_repl_prompt()
            self._line_buf.append(ch)
            self._echo_char(ch)

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
        self.log_info('Hotkey: Ctrl+R resets the ESP chip; Ctrl+C exits')
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
            'Ctrl+R resets the chip (log port); Ctrl+C exits. '
            'Path-like lines (/ ./ ~/ drive:) send the file as raw bytes; '
            'all other input is sent as an AT line with \\r\\n appended.'
        ),
    )
    parser.add_argument(
        '--port0', '-p0',
        default=None,
        help='AT log port device. Default: smallest candidate port.',
    )
    parser.add_argument(
        '--port1', '-p1',
        default=None,
        help='AT command port device. Default: second-smallest candidate (or same if only one).',
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
