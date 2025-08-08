#!/usr/bin/env python
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
ESP Serial Port Logger

A simple serial port logger for ESP chips with logging, reset and reconnection capabilities.
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

class SerialPortLogger:
    """A serial port logger for ESP chips."""

    def __init__(self):
        self.log_file_handle: Optional[TextIO] = None
        self.enable_timestamp: bool = True
        self.serial_handle: Optional[serial.Serial] = None
        self.should_exit: bool = False
        self.log_file_path: Optional[str] = None

    def log_info(self, message: str) -> None:
        """Log an info message in green color."""
        formatted_msg = self._format_message(message)
        print(f'\033[32m{formatted_msg}\033[0m')
        self._write_to_file(formatted_msg)

    def log_error(self, message: str) -> None:
        """Log an error message in red color."""
        formatted_msg = self._format_message(message)
        sys.stderr.write(f'\033[31m{formatted_msg}\n\033[0m')
        self._write_to_file(formatted_msg)

    def log_warn(self, message: str) -> None:
        """Log an warning message in yellow color."""
        formatted_msg = self._format_message(message)
        print(f'\033[33m{formatted_msg}\033[0m')
        self._write_to_file(formatted_msg)

    def log_raw(self, message: str) -> None:
        """Log a raw message without newline."""
        formatted_msg = self._format_message(message)
        print(formatted_msg, end='')
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

    @staticmethod
    def find_first_available_port() -> Optional[str]:
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
        return ports[0].device if ports else None

    @staticmethod
    def create_directory(path: str) -> None:
        """Create directory if it doesn't exist."""
        path = path.strip().rstrip('\\')
        if not os.path.exists(path):
            os.makedirs(path)

    def create_log_file(self) -> TextIO:
        """Create and open a log file for writing."""
        try:
            log_dir = os.path.join(os.getcwd(), 'esp_log')
            self.create_directory(log_dir)
            filename = datetime.now().strftime('%Y%m%d_%H%M%S.log')
            self.log_file_path = os.path.join(log_dir, filename)
            return open(self.log_file_path, 'w', encoding='utf-8')
        except Exception as e:
            raise RuntimeError(f'Failed to create log file: {e}') from e

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

        if self.log_file_handle:
            try:
                self.log_file_handle.flush()
                self.log_file_handle.close()
            except Exception:
                pass
            if self.log_file_path:
                print(f'\n\033[1;32mLog saved to: {self.log_file_path}\033[0m')
            self.log_file_handle = None

    def signal_handler(self, sig, frame) -> None:
        """Handle interrupt signals (Ctrl+C)."""
        self.should_exit = True
        self.log_info('\nCtrl+C pressed, exiting...')

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

    def reset_esp_chip(self) -> None:
        """Reset the ESP chip using DTR and RTS signals."""
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
                        data = self.serial_handle.readline().decode('utf-8', 'ignore')
                        self.log_raw(data)
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
    parser = argparse.ArgumentParser(description='ESP Serial Port Logger')
    logger = SerialPortLogger()
    parser.add_argument(
        '--port', '-p',
        type=logger.validate_serial_port,
        default=logger.find_first_available_port(),
        help='Serial port device. Default: the first available port.'
    )
    parser.add_argument(
        '--baudrate', '-b',
        type=int,
        default=115200,
        help='Serial port baud rate. Default: 115200.'
    )
    parser.add_argument(
        '--flow-control', '-fc',
        action='store_true',
        help='Enable hardware flow control. Default: False.'
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
    # TODO: Implement log rotation options
    # parser.add_argument(
    #     '--save-log-rotate-interval',
    #     type=int,
    #     default=None,
    #     help='Rotate log file every N minutes. Default: None (no rotation).'
    # )
    # parser.add_argument(
    #     '--save-log-max-size',
    #     type=int,
    #     default=None,
    #     help='Rotate log file when size exceeds N MB. Default: None (no size limit).'
    # )
    return parser

def main():
    parser = create_argument_parser()
    args = parser.parse_args()

    # Validate the serial port
    logger = SerialPortLogger()
    try:
        logger.validate_serial_port(args.port)
    except argparse.ArgumentTypeError as e:
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
