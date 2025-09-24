#!/usr/bin/env python
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
# TODO: Add some description
"""

import argparse
import os
import platform
import signal
import sys
import time
from datetime import datetime
from typing import Optional, TextIO

import serial
import serial.tools.list_ports
import threading
import importlib

# ========================================
# Logger
# ========================================
class ESPLogger:
    """A logger for ESP chips."""

    def __init__(
        self,
        save_log: bool = False,
        enable_log_tag: bool = False,
        enable_log_timestamp: bool = True,
    ):
        self.save_log = save_log
        self.enable_timestamp = enable_log_timestamp
        self.enable_log_tag = enable_log_tag
        self.log_file_handle: Optional[TextIO] = None
        self.log_file_path: Optional[str] = None
        self._lock = threading.Lock()

    def open(self):
        if self.save_log:
            self.log_file_handle = self.create_log_file()

    def close(self):
        """Close the log file if it is open."""
        if self.log_file_handle:
            with self._lock:
                try:
                    self.log_file_handle.flush()
                    self.log_file_handle.close()
                except Exception:
                    pass
                print(f'\n\033[1;32mLog saved to: {self.log_file_path}\033[0m')
                self.log_file_handle = None

    def log_info(self, message: str, tag: Optional[str] = None) -> None:
        """Log an info message in green color."""
        formatted_msg = self._format_message(message, tag)
        print(f'\033[32m{formatted_msg}\033[0m')
        self._write_to_file(formatted_msg)

    def log_error(self, message: str, tag: Optional[str] = None) -> None:
        """Log an error message in red color."""
        formatted_msg = self._format_message(message, tag)
        sys.stderr.write(f'\033[31m{formatted_msg}\n\033[0m')
        self._write_to_file(formatted_msg)

    def log_warn(self, message: str, tag: Optional[str] = None) -> None:
        """Log an warning message in yellow color."""
        formatted_msg = self._format_message(message, tag)
        print(f'\033[33m{formatted_msg}\033[0m')
        self._write_to_file(formatted_msg)

    def log_raw(self, message: str, tag: Optional[str] = None) -> None:
        """Log a raw message without newline."""
        formatted_msg = self._format_message(message, tag)
        if message.startswith('I '):
            formatted_msg_with_color = f'\033[32m{formatted_msg}\033[0m'
        elif message.startswith('W '):
            formatted_msg_with_color = f'\033[33m{formatted_msg}\033[0m'
        elif message.startswith('E '):
            formatted_msg_with_color = f'\033[31m{formatted_msg}\033[0m'
        else:
            formatted_msg_with_color = formatted_msg
        print(formatted_msg_with_color, end='')
        self._write_to_file(formatted_msg, add_newline=False)

    def _format_message(self, message: str, tag: Optional[str] = None) -> str:
        """Format message with timestamp if enabled."""
        if self.enable_log_tag and tag:
            message = f'({tag}) {message}'
        if self.enable_timestamp:
            return f'[{datetime.now()}] {message}'
        return message

    def _write_to_file(self, message: str, add_newline: bool = True) -> None:
        """Write message to log file if logging is enabled."""
        if self.log_file_handle:
            with self._lock:
                suffix = '\n' if add_newline else ''
                self.log_file_handle.write(f'{message}{suffix}')
                self.log_file_handle.flush()

    @staticmethod
    def create_directory(path: str) -> None:
        """Create directory if it doesn't exist."""
        path = path.strip().rstrip('\\')
        if not os.path.exists(path):
            os.makedirs(path)

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

# ========================================
# Serial Port Base
# ========================================
class ESPSerialPortBase:
    def __init__(self, port: str, baudrate: int = 115200, logger: Optional[ESPLogger] = None, tag: Optional[str] = None):
        self.port_name = port
        self.baudrate = baudrate
        self._opened = False
        self._buffer: bytes = b""
        self.logger = logger
        self.tag = tag
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def open(self):
        self._opened = True
        print(f"[Serial] Open {self.port_name} @ {self.baudrate}")

    def close(self):
        self._opened = False
        print(f"[Serial] Close {self.port_name}")

    def write(self, data: bytes):
        if not self._opened:
            raise RuntimeError("Serial port not opened")
        self._buffer += data
        if self.logger and self.tag:
            self.logger.log_raw("TX", self.port_name, data, tag=self.tag)

    def readline(self) -> bytes:
        if not self._opened:
            raise RuntimeError("Serial port not opened")
        if b"\n" in self._buffer:
            idx = self._buffer.index(b"\n") + 1
            line = self._buffer[:idx]
            self._buffer = self._buffer[idx:]
            if line and self.logger and self.tag:
                self.logger.log_raw("RX", self.port_name, line, tag=self.tag)
            return line
        return b""

    def read(self, size=1) -> bytes:
        if not self._opened:
            raise RuntimeError("Serial port not opened")
        data = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return data

    def start(self):
        """Start the background thread for reading data."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background thread."""
        self._running = False
        if self._thread:
            self._thread.join()

    def _loop(self):
        """Default loop for background reading. Can be overridden in subclasses."""
        while self._running:
            line = self.readline()
            if line and self.logger and self.tag:
                self.logger.log_raw("RX", self.port_name, line, tag=self.tag)
            time.sleep(0.01)

    @staticmethod
    def find_available_port(i) -> Optional[str]:
        """Find the first available serial port based on the operating system."""
        ports = list(serial.tools.list_ports.comports())
        if not ports:
            return None
        system = platform.system().lower()
        if system == 'linux':
            ports = [p for p in ports if 'ttyUSB' in p.device]
        elif system == 'darwin':
            ports = [p for p in ports if 'tty.usbserial' in p.device]
        elif system == 'windows':
            ports = [p for p in ports if 'COM' in p.device]
        ports.sort(key=lambda p: p.device)
        return ports[min(i, len(ports) - 1)].device if ports else None

    def reset_esp_chip(self) -> None:
        """Reset the ESP chip using DTR and RTS signals."""
        self.serial_handle.dtr = False
        self.serial_handle.rts = True
        time.sleep(0.1)
        self.serial_handle.rts = False
        time.sleep(0.05)
        self.log_info('ESP chip reset completed')

    @staticmethod
    def validate_serial_port(port: str) -> str:
        """Validate if the serial port is available and accessible."""
        if port is None:
            raise argparse.ArgumentTypeError('No available serial port found')
        if not os.path.exists(port):
            raise argparse.ArgumentTypeError(f"Serial port '{port}' does not exist")
        try:
            with serial.Serial(port) as _:
                pass
        except Exception as e:
            raise argparse.ArgumentTypeError(f"Cannot access serial port '{port}': {e}")
        return port

# ========================================
# AT LOG Port
# ========================================
class ATLogPort(ESPSerialPortBase):
    def __init__(self, port: str, baudrate: int, no_reboot_chip: bool, logger: ESPLogger):
        self.reboot_chip = not no_reboot_chip
        super().__init__(port, baudrate, logger, tag="LOG")

# ========================================
# ATDUT
# ========================================
class ATDUT:
    def __init__(self, cmd_port: ATCMDPort, log_port: ATLogPort, logger: ESPLogger):
        self.cmd_port = cmd_port
        self.log_port = log_port
        self.logger = logger

    def run_repl(self):
        self.logger.log_info("Starting REPL mode...")
        self.cmd_port.start()
        self.log_port.start()
        try:
            while True:
                cmd = input("esp-at > ")
                if cmd.lower() in ["exit", "quit"]:
                    break
                self.cmd_port.send_cmd(cmd)
        except KeyboardInterrupt:
            self.logger.log_info("Ctrl+C received, stopping threads...")
        finally:
            self.cmd_port.stop()
            self.log_port.stop()

    def run_test_file(self, test_file: str):
        self.logger.log_info(f"Running test file: {test_file}")
        self.cmd_port.start()
        self.log_port.start()
        try:
            mod = importlib.import_module(test_file)
            if hasattr(mod, "setup"):
                mod.setup(self)
            if hasattr(mod, "loop"):
                mod.loop(self)
            if hasattr(mod, "teardown"):
                mod.teardown(self)
        except KeyboardInterrupt:
            self.logger.log_info("Ctrl+C received, stopping threads...")
        finally:
            self.cmd_port.stop()
            self.log_port.stop()

class ESPSerialPort:

    def cleanup_and_exit(self) -> None:
        """Clean up resources and exit gracefully."""
        if self.serial_handle:
            try:
                # Read out remaining data before closing
                if self.serial_handle.in_waiting:
                    data = self.serial_handle.read(self.serial_handle.in_waiting).decode('utf-8', 'ignore')
                    self.log_raw(data)
            except Exception:
                pass
            try:
                self.serial_handle.close()
            except Exception:
                pass
            self.serial_handle = None


    def run(self, args) -> None:

        has_reset = False
        first_reconnect = True
        while not self.should_exit:
            try:
                # TODO: Implement serial port locking mechanism to avoid conflicts
                self.serial_handle = serial.Serial(args.port, args.baudrate, timeout=1, rtscts=args.flow_control)
            except Exception as e:
                if first_reconnect:
                    self.log_warn(f'Failed to open {args.port}. Reconnecting...')
                first_reconnect = False
                time.sleep(0.001)
                continue
            first_reconnect = True
            self.log_info(f'Opened {args.port} with baudrate {args.baudrate}')

            # Reset ESP chip if requested at the start
            if not args.no_reboot_chip and not has_reset:
                self.reset_esp_chip()
                has_reset = True

            # Main data reading loop
            while not self.should_exit:
                try:
                    if self.serial_handle and self.serial_handle.in_waiting > 0:
                        # TODO: Implement a more robust reading mechanism
                        # There is a potential risk of blocking here if the serial port data does not contain a newline but is continuous (e.g., wrong baud rate)
                        data = self.serial_handle.readline().decode('utf-8', 'ignore')
                        # data = self.serial_handle.read(self.serial_handle.in_waiting).decode('utf-8', 'ignore')
                        self.log_raw(data)
                    else:
                        time.sleep(0.001)
                except Exception as e:
                    self.log_error(f'Failed to read data from {args.port}: {e}')
                    try:
                        self.serial_handle.close()
                    except Exception:
                        pass
                    self.serial_handle = None
                    break
        self.cleanup_and_exit()


def create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the command line argument parser."""
    parser = argparse.ArgumentParser(description='ESP Serial Port Logger and AT Command Sender')

    parser.add_argument(
        '--port0', '-p0',
        type=ESPSerialPortBase.validate_serial_port,
        default=ESPSerialPortBase.find_available_port(0),
        help='AT log port device. Default: the first available port.'
    )
    parser.add_argument(
        '--port1', '-p1',
        type=ESPSerialPortBase.validate_serial_port,
        default=ESPSerialPortBase.find_available_port(1),
        help='AT command port device. Default: the second available port.'
    )
    parser.add_argument(
        '--port0-baudrate', '-p0b',
        type=int,
        default=115200,
        help='Baud rate for AT log port. Default: 115200.'
    )
    parser.add_argument(
        '--port1-baudrate', '-p1b',
        type=int,
        default=115200,
        help='Baud rate for AT command port. Default: 115200.'
    )
    parser.add_argument(
        '--flow-control', '-fc',
        action='store_true',
        help='Enable hardware flow control for AT command port. Default: False.'
    )

    parser.add_argument(
        '--save-log', '-s',
        action='store_true',
        help='Save logs to local files. Default: False.'
    )

    parser.add_argument(
        '--no-timestamp', '-nt',
        action='store_true',
        help='Disable timestamp in log output. Default: False.'
    )
    parser.add_argument(
        '--no-reboot-chip', '-nr',
        action='store_true',
        help='Skip ESP chip reboot before logging. Default: False.'
    )

    return parser

def ESP_LOGE(x):
    sys.stderr.write(f'\033[31m{x}\n\033[0m')

# check args
def check_args(args: argparse.Namespace) -> None:
    if args.port0 is None and args.port1 is None:
        ESP_LOGE("No available serial port found")
        return False
    return True


def main():
    parser = create_argument_parser()
    args = parser.parse_args()
    print(args)

    # check args
    if not check_args(args):
        parser.print_help()
        sys.exit(1)

    # -s, -nt, -lp
    logger = ESPLogger(args.save_log, args.log_tag, not args.no_timestamp)

    logger.open()

    logger.log_info("Starting AT CMD tool...")
    logger.log_info("Starting AT CMD tool...", "PC")
    logger.log_raw("Starting AT CMD tool...\n")
    logger.log_raw("I Starting AT CMD tool...\n", "PC")
    logger.log_raw("W Starting AT CMD tool...\n", "PC")
    logger.log_raw("E Starting AT CMD tool...\n", "ATDUT")

    # -p0, -p0b, -nr
    log_port = ATLogPort(args.port0, args.port0_baudrate, args.no_reboot_chip, logger)

    # -p1, -p1b, -fc
    cmd_port = ATCMDPort(args.port1, args.port1_baudrate, args.flow_control, logger)

    # -t, lt
    dut = ATDUT(cmd_port, log_port, logger)
    if args.test_file:
        dut.run_test_file(args.test_file)
    else:
        dut.run_repl()

if __name__ == '__main__':
    main()
