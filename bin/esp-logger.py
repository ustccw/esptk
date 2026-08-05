#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
ESP Serial Port Logger

A simple serial port logger for ESP chips with logging, reset and reconnection capabilities.
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
from typing import Optional, TextIO

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

# Interactive hotkey: same reset sequence as the initial auto-reboot.
CTRL_R = '\x12'


class SerialPortLogger:
    """A serial port logger for ESP chips."""

    def __init__(self):
        self.log_file_handle: Optional[TextIO] = None
        self.enable_timestamp: bool = True
        self.serial_handle: Optional[serial.Serial] = None
        self.should_exit: bool = False
        self.log_file_path: Optional[str] = None
        self._port_locked: bool = False
        self._stdin_old_attrs = None

    @staticmethod
    def color_enabled(stream) -> bool:
        """Whether ANSI colors should be used for the given stream.

        Honors NO_COLOR / FORCE_COLOR (https://no-color.org/) and TTY detection.
        """
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
        """Wrap message with ANSI color if enabled for stream."""
        if SerialPortLogger.color_enabled(stream):
            return f'\033[{color_code}m{message}\033[0m'
        return message

    @staticmethod
    def colorize_esp_log_line(message: str, formatted_msg: str, stream=sys.stdout) -> str:
        """Color a line based on ESP-IDF log level prefix in the raw serial text."""
        match = _LOG_LEVEL_RE.match(message)
        if not match:
            return formatted_msg
        color_code = _LEVEL_COLORS.get(match.group(1))
        if not color_code:
            return formatted_msg
        return SerialPortLogger.colorize(formatted_msg, color_code, stream)

    def log_info(self, message: str) -> None:
        """Log an info message in green color."""
        formatted_msg = self._format_message(message)
        print(self.colorize(formatted_msg, '32'))
        self._write_to_file(formatted_msg)

    def log_error(self, message: str) -> None:
        """Log an error message in red color."""
        formatted_msg = self._format_message(message)
        sys.stderr.write(self.colorize(f'{formatted_msg}\n', '31', sys.stderr))
        self._write_to_file(formatted_msg)

    def log_warn(self, message: str) -> None:
        """Log a warning message in yellow color."""
        formatted_msg = self._format_message(message)
        print(self.colorize(formatted_msg, '33'))
        self._write_to_file(formatted_msg)

    def log_raw(self, message: str) -> None:
        """Log a raw message without newline."""
        formatted_msg = self._format_message(message)
        print(self.colorize_esp_log_line(message, formatted_msg), end='')
        self._write_to_file(formatted_msg, add_newline=False)

    def _format_message(self, message: str) -> str:
        """Format message with timestamp if enabled."""
        if self.enable_timestamp:
            return f'[{datetime.now()}] {message}'
        return message

    def _write_to_file(self, message: str, add_newline: bool = True) -> None:
        """Write message to log file if logging is enabled."""
        if self.log_file_handle:
            suffix = '\n' if add_newline else ''
            self.log_file_handle.write(f'{message}{suffix}')
            self.log_file_handle.flush()

    @staticmethod
    def is_candidate_port(device: str, system: Optional[str] = None) -> bool:
        """Return True if device name looks like an ESP-usable serial port on this OS."""
        system = (system or platform.system()).lower()
        if system == 'linux':
            return 'ttyUSB' in device or 'ttyACM' in device
        if system == 'darwin':
            return 'tty.usbserial' in device or 'tty.usbmodem' in device
        if system == 'windows':
            # pyserial reports COMx; accept COM / com
            return device.upper().startswith('COM')
        return False

    @staticmethod
    def find_first_available_port() -> Optional[str]:
        """Find the first available serial port based on the operating system."""
        ports = list(serial.tools.list_ports.comports())
        if not ports:
            return None
        system = platform.system().lower()
        ports = [p for p in ports if SerialPortLogger.is_candidate_port(p.device, system)]
        ports.sort(key=lambda p: p.device)
        return ports[0].device if ports else None

    @staticmethod
    def create_directory(path: str) -> None:
        """Create directory if it doesn't exist."""
        path = path.strip().rstrip('\\/')
        os.makedirs(path, exist_ok=True)

    def create_log_file(self) -> TextIO:
        """Create and open a log file for writing."""
        try:
            log_dir = os.path.join(os.getcwd(), 'esp_logs')
            self.create_directory(log_dir)
            filename = datetime.now().strftime('%Y%m%d_%H%M%S_%f.log')
            self.log_file_path = os.path.join(log_dir, filename)
            return open(self.log_file_path, 'w', encoding='utf-8')
        except Exception as e:
            raise RuntimeError(f'Failed to create log file: {e}') from e

    def _lock_serial_port(self) -> None:
        """Try to take an exclusive advisory lock (Unix flock). No-op on Windows."""
        if fcntl is None or self.serial_handle is None:
            return
        try:
            fcntl.flock(self.serial_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._port_locked = True
        except (BlockingIOError, OSError) as e:
            raise RuntimeError('Serial port is locked by another process') from e

    def _unlock_serial_port(self) -> None:
        """Release advisory lock if held."""
        if not self._port_locked or fcntl is None or self.serial_handle is None:
            self._port_locked = False
            return
        try:
            fcntl.flock(self.serial_handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        self._port_locked = False

    def _enable_hotkeys(self) -> None:
        """Put stdin into cbreak mode so Ctrl+R is readable without Enter (Unix TTY)."""
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
        """Restore stdin terminal attributes if we changed them."""
        if self._stdin_old_attrs is None or termios is None:
            return
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._stdin_old_attrs)
        except Exception:
            pass
        self._stdin_old_attrs = None

    def _read_hotkey_char(self) -> Optional[str]:
        """Non-blocking read of one stdin character, or None if nothing pending."""
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

    def _poll_hotkeys(self) -> None:
        """Handle interactive hotkeys (Ctrl+R -> reset chip)."""
        ch = self._read_hotkey_char()
        if ch == CTRL_R:
            self.log_info('Ctrl+R pressed, resetting ESP chip...')
            self.reset_esp_chip()

    def cleanup_and_exit(self) -> None:
        """Clean up resources and exit gracefully."""
        self._disable_hotkeys()
        if self.serial_handle:
            try:
                # Read out remaining data before closing
                if self.serial_handle.in_waiting:
                    data = self.serial_handle.read(self.serial_handle.in_waiting).decode('utf-8', 'ignore')
                    self.log_raw(data)
            except Exception:
                pass
            self._unlock_serial_port()
            try:
                self.serial_handle.close()
            except Exception:
                pass
            self.serial_handle = None

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

    def signal_handler(self, sig, frame) -> None:
        """Handle interrupt signals (Ctrl+C)."""
        self.should_exit = True
        self.log_info('\nCtrl+C pressed, exiting...')

    @staticmethod
    def validate_serial_port(port: Optional[str]) -> str:
        """Validate that the serial port exists (non-Windows) and can be opened.

        On Windows, COM ports are not filesystem paths, so os.path.exists is skipped.
        """
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

    def reset_esp_chip(self) -> None:
        """Reset the ESP chip using DTR and RTS signals."""
        if not self.serial_handle or not self.serial_handle.is_open:
            self.log_warn('Cannot reset: serial port is not open')
            return
        self.serial_handle.dtr = False
        self.serial_handle.rts = True
        time.sleep(0.1)
        self.serial_handle.rts = False
        time.sleep(0.05)
        self.log_info('ESP chip reset completed')

    def run(self, args) -> None:
        """Main execution loop for the serial port logger."""
        self.enable_timestamp = not args.no_timestamp
        if args.save_log:
            self.log_file_handle = self.create_log_file()

        # Set up signal handler
        signal.signal(signal.SIGINT, self.signal_handler)
        self._enable_hotkeys()
        self.log_info('Hotkey: Ctrl+R resets the ESP chip')

        has_reset = False
        first_reconnect = True
        reconnect_delay = 0.5
        while not self.should_exit:
            try:
                self.serial_handle = serial.Serial(
                    args.port, args.baudrate, timeout=0.2, rtscts=args.flow_control
                )
                self._lock_serial_port()
            except Exception as e:
                if first_reconnect:
                    self.log_warn(f'Failed to open {args.port}: {e}. Reconnecting...')
                first_reconnect = False
                if self.serial_handle:
                    try:
                        self.serial_handle.close()
                    except Exception:
                        pass
                    self.serial_handle = None
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 2.0)
                continue
            first_reconnect = True
            reconnect_delay = 0.5
            self.log_info(f'Opened {args.port} with baudrate {args.baudrate}')

            # Reset ESP chip if requested at the start
            if not args.no_reboot_chip and not has_reset:
                self.reset_esp_chip()
                has_reset = True

            # Block on readline so each line keeps color heuristics intact.
            # timeout=0.2 lets Ctrl+C / Ctrl+R / should_exit be checked without busy-waiting.
            while not self.should_exit:
                try:
                    raw = self.serial_handle.readline()
                    if raw:
                        self.log_raw(raw.decode('utf-8', 'ignore'))
                    self._poll_hotkeys()
                except Exception as e:
                    self.log_error(f'Failed to read data from {args.port}: {e}')
                    self._unlock_serial_port()
                    try:
                        self.serial_handle.close()
                    except Exception:
                        pass
                    self.serial_handle = None
                    break
        self.cleanup_and_exit()


def create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the command line argument parser."""
    parser = argparse.ArgumentParser(
        description='ESP Serial Port Logger',
        epilog='While running, press Ctrl+R to reset the ESP chip (same as initial reboot).',
    )
    parser.add_argument(
        '--port', '-p',
        default=None,
        help='Serial port device. Default: the first available port.',
    )
    parser.add_argument(
        '--baudrate', '-b',
        type=int,
        default=115200,
        help='Serial port baud rate. Default: 115200.',
    )
    parser.add_argument(
        '--flow-control', '-fc',
        action='store_true',
        help='Enable hardware flow control. Default: False.',
    )
    parser.add_argument(
        '--save-log', '-s',
        action='store_true',
        help='Save logs to local files. Default: False.',
    )
    parser.add_argument(
        '--no-timestamp', '-nt',
        action='store_true',
        help='Disable timestamp in log output. Default: False.',
    )
    parser.add_argument(
        '--no-reboot-chip', '-nr',
        action='store_true',
        help='Skip ESP chip reboot before logging. Default: False.',
    )
    # TODO: log rotation options (--save-log-rotate-interval / --save-log-max-size)
    return parser


def main():
    parser = create_argument_parser()
    args = parser.parse_args()

    logger = SerialPortLogger()
    port = args.port or SerialPortLogger.find_first_available_port()
    try:
        args.port = SerialPortLogger.validate_serial_port(port)
    except ValueError as e:
        logger.log_error(str(e))
        sys.exit(1)

    try:
        logger.run(args)
    except Exception as e:
        logger.log_error(f'A fatal error occurred: {e}')
        logger.cleanup_and_exit()
        sys.exit(1)


if __name__ == '__main__':
    main()
