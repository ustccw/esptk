#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# chenwu@espressif.com
"""
ESP-AT automation test runner (single- or multi-DUT).

Runs a pure-Python test file against one or more AT devices. Each DUT has two
UARTs: a log port and a command port. Fully automated — no REPL.

Quick start::

    # Single DUT
    at-test.py -t bin/examples/at_smoke.py -p0 /dev/ttyUSB0 -p1 /dev/ttyUSB1

    # Multi DUT
    at-test.py -t bin/examples/at_multi_dut.py \\
      --dut AT1=/dev/ttyUSB0,/dev/ttyUSB1 \\
      --dut AT2=/dev/ttyUSB2,/dev/ttyUSB3

Test file contract
------------------
- Optional ``DEVICES`` — named DUTs (ports / baud rates).
- Optional ``setup(ctx)`` / ``teardown(ctx)``.
- Required ``run(ctx)`` or ``test(ctx)`` (``run`` preferred).
- Optional ``FAIL_FAST = True`` — stop on the first real failure.

Single-DUT example::

    def run(ctx):
        ctx.at('AT', expect='OK', name='step1')
        ctx.at('AT+GMR', expect='OK', name='step2')

Multi-DUT example::

    DEVICES = {'AT1': {}, 'AT2': {}}

    def run(ctx):
        ctx['AT1'].at('AT', expect='OK', name='step1')
        ctx['AT2'].at('AT', expect='OK', name='step2')

Common ``dut.at`` / ``ctx.at`` arguments: ``cmd``, ``expect`` (default ``OK``),
``timeout``, ``name``, ``expect_fail``, ``expect_port`` (``cmd``|``log``|``any``),
``setup``, ``teardown``.

Expect specs: exact string, or ``re.compile(...)`` (use regex for substring
matches as well). For cross-DUT races use ``mark = dut.mark()`` then
``dut.expect(..., after=mark)``.

See ``bin/examples/README.md`` / ``README_CN.md`` for full usage. ``at-test.py -h``
for CLI options.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import os
import platform
import re
import select
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import ModuleType
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Pattern,
    TextIO,
    Tuple,
    Union,
)

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

# ESP-IDF style levels: I/W/E/D/V/A/F, optionally preceded by CR from terminal.
_LOG_LEVEL_RE = re.compile(r'^[\r]*([IWEADVF]) ')
_LEVEL_COLORS = {
    'I': '32',
    'W': '33',
    'E': '31',
    'D': '36',
    'V': '37',
    'A': '35',
    'F': '31',
}

# AT command reply terminators for the wait-loop state machine only
# (when to stop waiting after a write). Not used by expect matching.
_AT_END_LINES = frozenset({'OK', 'ERROR', 'FAIL'})
_AT_FAIL_LINES = frozenset({'ERROR', 'FAIL'})


@dataclass(frozen=True)
class Contains:
    """Substring expect: matches if ``text`` appears anywhere in the line."""

    text: str


def contains(text: str) -> Contains:
    """Build a substring expect spec (see module docstring)."""
    return Contains(text)


ExpectAtom = Union[str, Pattern[str], Contains]
ExpectSpec = Union[ExpectAtom, List[ExpectAtom], Tuple[ExpectAtom, ...]]
HookSpec = Union[Callable[..., Any], str, None]

# Port filter for expect matching: cmd (AT command UART), log (AT log UART), or any.
ExpectPort = str  # 'cmd' | 'log' | 'any'


def normalize_expect_port(port: Optional[str]) -> str:
    """Map user aliases to cmd|log|any."""
    if port is None:
        return 'cmd'
    p = port.strip().lower()
    aliases = {
        'cmd': 'cmd',
        'command': 'cmd',
        'at': 'cmd',
        'dut': 'cmd',
        'log': 'log',
        'logger': 'log',
        'any': 'any',
        'both': 'any',
        '*': 'any',
    }
    if p in aliases:
        return aliases[p]
    # Allow "AT1/cmd", "AT2/log" style — side only; DUT is the AtDevice itself.
    if '/' in p:
        side = p.split('/')[-1]
        if side in ('cmd', 'command', 'at'):
            return 'cmd'
        if side in ('log', 'logger'):
            return 'log'
    raise ValueError(
        f"Invalid expect_port {port!r}; use 'cmd', 'log', or 'any' "
        f"(optionally 'NAME/cmd', 'NAME/log')"
    )



# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def color_enabled(stream) -> bool:
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    try:
        return stream.isatty()
    except Exception:
        return False


def colorize(message: str, color_code: str, stream=sys.stdout) -> str:
    if color_enabled(stream):
        return f'\033[{color_code}m{message}\033[0m'
    return message


def create_directory(path: str) -> None:
    path = path.strip().rstrip('\\/')
    os.makedirs(path, exist_ok=True)


# ---------------------------------------------------------------------------
# Port discovery / validation
# ---------------------------------------------------------------------------

def is_candidate_port(device: str, system: Optional[str] = None) -> bool:
    system = (system or platform.system()).lower()
    if system == 'linux':
        return 'ttyUSB' in device or 'ttyACM' in device
    if system == 'darwin':
        return 'tty.usbserial' in device or 'tty.usbmodem' in device
    if system == 'windows':
        return device.upper().startswith('COM')
    return False


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


def find_candidate_ports() -> List[str]:
    ports = list(serial.tools.list_ports.comports())
    system = platform.system().lower()
    ports = [p for p in ports if is_candidate_port(p.device, system)]
    ports.sort(key=lambda p: p.device)
    return [p.device for p in ports]


def resolve_ports(
    port0: Optional[str], port1: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    port0 = resolve_port(port0)
    port1 = resolve_port(port1)
    if port0 is not None and port1 is not None:
        return port0, port1

    candidates = find_candidate_ports()
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


# ---------------------------------------------------------------------------
# Step results / report
# ---------------------------------------------------------------------------

class StepStatus(str, Enum):
    PASS = 'PASS'
    FAIL = 'FAIL'
    EXPECTED_FAIL = 'EXPECTED_FAIL'
    SKIPPED = 'SKIPPED'


@dataclass
class StepResult:
    dut: str
    name: str
    cmd: str
    status: StepStatus
    elapsed: float
    error: str = ''
    expect_fail: bool = False


@dataclass
class TestReport:
    results: List[StepResult] = field(default_factory=list)

    def add(self, result: StepResult) -> None:
        self.results.append(result)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == StepStatus.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == StepStatus.FAIL)

    @property
    def expected_failed(self) -> int:
        return sum(1 for r in self.results if r.status == StepStatus.EXPECTED_FAIL)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == StepStatus.SKIPPED)

    def has_real_failures(self) -> bool:
        return self.failed > 0

    def summary_lines(self) -> List[str]:
        """Compact formal verdict: RESULT PASS/FAIL, with failure details."""
        failures = [r for r in self.results if r.status == StepStatus.FAIL]
        if not failures:
            return [
                '======== RESULT: PASS ========',
            ]
        lines = [
            '======== RESULT: FAIL ========',
        ]
        for r in failures:
            reason = r.error or 'failed'
            lines.append(f'  [{r.dut}] {r.name}: {reason}')
        return lines


class FailFastAbort(Exception):
    """Raised to stop remaining steps when fail-fast is enabled."""


class StepFailed(Exception):
    """Internal: a step failed (may become EXPECTED_FAIL)."""

    def __init__(self, message: str, device_failed: bool = False):
        super().__init__(message)
        self.device_failed = device_failed


# ---------------------------------------------------------------------------
# Logger (shared by runner)
# ---------------------------------------------------------------------------

class RunnerLog:
    def __init__(self) -> None:
        self.enable_timestamp: bool = True
        self.enable_prompt: bool = False
        self.log_file_handle: Optional[TextIO] = None
        self.log_file_path: Optional[str] = None

    def open_log_file(self) -> None:
        log_dir = os.path.join(os.getcwd(), 'esp_logs')
        create_directory(log_dir)
        filename = datetime.now().strftime('%Y%m%d_%H%M%S_%f.log')
        self.log_file_path = os.path.join(log_dir, filename)
        self.log_file_handle = open(self.log_file_path, 'w', encoding='utf-8')

    def close_log_file(self) -> None:
        if not self.log_file_handle:
            return
        try:
            self.log_file_handle.flush()
            self.log_file_handle.close()
        except Exception:
            pass
        if self.log_file_path:
            msg = f'Log saved to: {self.log_file_path}'
            print(f'\n{colorize(msg, "1;32")}')
        self.log_file_handle = None

    def _line_prefix(self, tag: Optional[str] = None) -> str:
        parts: List[str] = []
        if self.enable_timestamp:
            parts.append(f'[{datetime.now()}]')
        if self.enable_prompt and tag:
            parts.append(f'({tag})')
        if not parts:
            return ''
        return ' '.join(parts) + ' '

    def _write_to_file(self, message: str, add_newline: bool = True) -> None:
        if not self.log_file_handle:
            return
        suffix = '\n' if add_newline else ''
        self.log_file_handle.write(f'{message}{suffix}')
        self.log_file_handle.flush()

    def log_info(self, message: str) -> None:
        formatted = self._line_prefix('PC') + message
        print(colorize(formatted, '32'))
        self._write_to_file(formatted)

    def log_warn(self, message: str) -> None:
        formatted = self._line_prefix('PC') + message
        print(colorize(formatted, '33'))
        self._write_to_file(formatted)

    def log_error(self, message: str) -> None:
        formatted = self._line_prefix('PC') + message
        sys.stderr.write(colorize(f'{formatted}\n', '31', sys.stderr))
        self._write_to_file(formatted)

    def emit_serial_line(self, line: str, tag: str, add_newline: bool = True) -> None:
        raw = line
        if add_newline and not raw.endswith('\n'):
            raw = raw + '\n'
        prefix = self._line_prefix(tag)
        formatted = prefix + raw
        match = _LOG_LEVEL_RE.match(raw)
        color = _LEVEL_COLORS.get(match.group(1)) if match else None
        to_print = colorize(formatted, color) if color else formatted
        print(to_print, end='')
        sys.stdout.flush()
        self._write_to_file(formatted, add_newline=False)


# ---------------------------------------------------------------------------
# AtDevice
# ---------------------------------------------------------------------------

class AtDevice:
    """One AT DUT: log port + command port."""

    def __init__(
        self,
        name: str,
        log_port: str,
        cmd_port: str,
        *,
        log_baudrate: int = 115200,
        cmd_baudrate: int = 115200,
        flow_control: bool = False,
        logger: Optional[RunnerLog] = None,
        runner: Optional['AtCmdRunner'] = None,
    ) -> None:
        self.name = name
        self.log_port = log_port
        self.cmd_port = cmd_port
        self.log_baudrate = log_baudrate
        self.cmd_baudrate = cmd_baudrate
        self.flow_control = flow_control
        self.logger = logger or RunnerLog()
        self.runner = runner

        self.log_serial: Optional[serial.Serial] = None
        self.cmd_serial: Optional[serial.Serial] = None
        self._same_port = log_port == cmd_port
        self._log_locked = False
        self._cmd_locked = False

        self._rx_bufs: Dict[str, str] = {'log': '', 'cmd': ''}
        self._rx_buf_touched: Dict[str, float] = {'log': 0.0, 'cmd': 0.0}
        self._partial_flush_s = 0.05

        self.at_ready = False
        self._line_history: List[Tuple[str, str]] = []  # (port, stripped_line)
        self._history_limit = 5000

    # -- tagging ------------------------------------------------------------

    def _tag(self, which: str) -> str:
        """Display tag for ``-p``: cmd → AT1, log → LOG1."""
        if which == 'cmd':
            return self.name
        m = re.match(r'^AT(\d+)$', self.name, re.IGNORECASE)
        if m:
            return f'LOG{m.group(1)}'
        return f'{self.name}/log'

    # -- serial open/close --------------------------------------------------

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
            raise RuntimeError(
                f'Serial port {ser.port} is locked by another process'
            ) from e

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

    def open(self) -> None:
        self._same_port = self.log_port == self.cmd_port
        try:
            if self._same_port:
                ser = serial.Serial(
                    self.log_port,
                    self.cmd_baudrate,
                    timeout=0,
                    rtscts=self.flow_control,
                )
                self._lock_port(ser, 'log')
                self.log_serial = ser
                self.cmd_serial = ser
                self.logger.log_info(
                    f'Opened {self.log_port} (log+cmd) '
                    f'@ {self.cmd_baudrate}'
                    + (' flow-control' if self.flow_control else '')
                )
            else:
                log_ser = serial.Serial(
                    self.log_port, self.log_baudrate, timeout=0, rtscts=False
                )
                self._lock_port(log_ser, 'log')
                cmd_ser = serial.Serial(
                    self.cmd_port,
                    self.cmd_baudrate,
                    timeout=0,
                    rtscts=self.flow_control,
                )
                self._lock_port(cmd_ser, 'cmd')
                self.log_serial = log_ser
                self.cmd_serial = cmd_ser
                self.logger.log_info(
                    f'Opened log {self.log_port} @ {self.log_baudrate}'
                )
                self.logger.log_info(
                    f'Opened cmd {self.cmd_port} @ {self.cmd_baudrate}'
                    + (' flow-control' if self.flow_control else '')
                )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._same_port:
            if self.log_serial:
                try:
                    if self.log_serial.in_waiting:
                        data = self.log_serial.read(
                            self.log_serial.in_waiting
                        ).decode('utf-8', 'ignore')
                        self.feed_rx(data, 'cmd')
                except Exception:
                    pass
                self._flush_complete_lines('cmd')
                self._flush_all_partials()
                self._unlock_port(self.log_serial, 'log')
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
                    self.feed_rx(data, which)
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

    # -- RX reassembly ------------------------------------------------------

    def feed_rx(self, message: str, which: str) -> None:
        if not message or which not in self._rx_bufs:
            return
        self._rx_bufs[which] += message
        self._rx_buf_touched[which] = time.monotonic()
        self._flush_complete_lines(which)

    def _flush_complete_lines(self, which: str) -> None:
        buf = self._rx_bufs.get(which, '')
        while True:
            nl = buf.find('\n')
            if nl < 0:
                self._rx_bufs[which] = buf
                return
            line = buf[: nl + 1]
            buf = buf[nl + 1 :]
            self._emit_serial_line(line, which, add_newline=False)

    def flush_idle_partials(self) -> None:
        now = time.monotonic()
        for which in ('log', 'cmd'):
            buf = self._rx_bufs.get(which, '')
            if not buf:
                continue
            touched = self._rx_buf_touched.get(which, 0.0)
            if now - touched < self._partial_flush_s:
                continue
            self._rx_bufs[which] = ''
            # Device prompts like '>' often have no trailing newline; still end the
            # log line so subsequent host (pc) messages do not glue onto it.
            self._emit_serial_line(buf, which, add_newline=True)

    def _flush_all_partials(self) -> None:
        for which in ('log', 'cmd'):
            buf = self._rx_bufs.get(which, '')
            if not buf:
                continue
            self._rx_bufs[which] = ''
            self._emit_serial_line(buf, which, add_newline=True)

    def _emit_serial_line(
        self, line: str, which: str, add_newline: bool = True
    ) -> None:
        self.logger.emit_serial_line(line, self._tag(which), add_newline=add_newline)
        stripped = line.rstrip('\r\n')
        self._handle_rx_line(stripped, which)

    def _handle_rx_line(self, line: str, which: str) -> None:
        stripped = line.strip()
        if not stripped and not line:
            return
        self._line_history.append((which, stripped))
        if len(self._line_history) > self._history_limit:
            self._line_history = self._line_history[-self._history_limit :]
        if stripped.lower() == 'ready':
            self.at_ready = True

    def clear_history(self) -> None:
        self._line_history.clear()

    def mark(self) -> int:
        """Snapshot ``_line_history`` length for later ``expect(..., after=mark)``."""
        return len(self._line_history)

    def drain_once(self) -> None:
        if self._same_port:
            self._drain_port(self.cmd_serial, 'cmd')
        else:
            self._drain_port(self.log_serial, 'log')
            self._drain_port(self.cmd_serial, 'cmd')
        self.flush_idle_partials()

    def _drain_port(self, ser: Optional[serial.Serial], which: str) -> None:
        if not ser or not ser.is_open:
            return
        try:
            waiting = ser.in_waiting
            if waiting <= 0:
                return
            raw = ser.read(waiting)
            if raw:
                self.feed_rx(raw.decode('utf-8', 'ignore'), which)
        except Exception as e:
            raise RuntimeError(
                f'Failed to read from {which} port: {e}'
            ) from e

    def filenos(self) -> List[int]:
        fds: List[int] = []
        seen = set()
        for ser in (self.log_serial, self.cmd_serial):
            if not ser or not ser.is_open:
                continue
            try:
                fd = ser.fileno()
            except Exception:
                continue
            if fd in seen:
                continue
            seen.add(fd)
            fds.append(fd)
        return fds

    # -- TX -----------------------------------------------------------------

    def write_cmd(self, data: bytes) -> None:
        if not self.cmd_serial or not self.cmd_serial.is_open:
            raise RuntimeError(f'[{self.name}] Command port is not open')
        self.cmd_serial.write(data)
        self.cmd_serial.flush()

    def reset(self) -> None:
        ser = self.log_serial
        if not ser or not ser.is_open:
            self.logger.log_warn('Cannot reset: log port not open')
            return
        self.at_ready = False
        ser.dtr = False
        ser.rts = True
        time.sleep(0.1)
        ser.rts = False
        time.sleep(0.05)
        self.logger.log_info('ESP chip reset completed')

    # -- high-level API (delegates timing/drain to runner/context) ----------

    def at(
        self,
        cmd: str,
        expect: Optional[ExpectSpec] = 'OK',
        timeout: Optional[float] = None,
        setup: HookSpec = None,
        teardown: HookSpec = None,
        name: Optional[str] = None,
        expect_fail: bool = False,
        expect_port: str = 'cmd',
    ) -> StepResult:
        """Send an AT command and wait for expect on the chosen UART.

        expect_port:
          - ``cmd`` (default): match only on this DUT's AT command port
          - ``log``: match only on this DUT's AT log port
          - ``any``: match on either port
          - ``AT1/cmd``, ``AT2/log``, ...: same as cmd/log (DUT is ``self``)
        """
        if not self.runner or not self.runner.ctx:
            raise RuntimeError('AtDevice is not attached to a running context')
        return self.runner.ctx._run_at_step(
            self,
            cmd=cmd,
            expect=expect,
            timeout=timeout,
            setup=setup,
            teardown=teardown,
            name=name,
            expect_fail=expect_fail,
            expect_port=expect_port,
        )

    def send_raw(
        self,
        data: bytes,
        expect: Optional[ExpectSpec] = None,
        timeout: Optional[float] = None,
        setup: HookSpec = None,
        teardown: HookSpec = None,
        name: Optional[str] = None,
        expect_fail: bool = False,
        wait_terminator: bool = False,
        expect_port: str = 'cmd',
    ) -> StepResult:
        if not self.runner or not self.runner.ctx:
            raise RuntimeError('AtDevice is not attached to a running context')
        return self.runner.ctx._run_raw_step(
            self,
            data=data,
            expect=expect,
            timeout=timeout,
            setup=setup,
            teardown=teardown,
            name=name,
            expect_fail=expect_fail,
            wait_terminator=wait_terminator,
            expect_port=expect_port,
        )

    def send_file(
        self,
        path: str,
        expect: Optional[ExpectSpec] = None,
        timeout: Optional[float] = None,
        setup: HookSpec = None,
        teardown: HookSpec = None,
        name: Optional[str] = None,
        expect_fail: bool = False,
        wait_terminator: bool = False,
        expect_port: str = 'cmd',
    ) -> StepResult:
        path = os.path.expanduser(path)
        with open(path, 'rb') as f:
            data = f.read()
        return self.send_raw(
            data,
            expect=expect,
            timeout=timeout,
            setup=setup,
            teardown=teardown,
            name=name or f'send_file:{os.path.basename(path)}',
            expect_fail=expect_fail,
            wait_terminator=wait_terminator,
            expect_port=expect_port,
        )

    def expect(
        self,
        pattern: ExpectSpec,
        timeout: Optional[float] = None,
        port: str = 'any',
        expect_port: Optional[str] = None,
        name: Optional[str] = None,
        expect_fail: bool = False,
        after: Optional[int] = None,
    ) -> StepResult:
        """Wait for pattern without sending. ``port`` / ``expect_port``: cmd|log|any.

        ``after``: history mark from ``dut.mark()`` — match lines received since
        that mark (needed when the peer already produced output during another
        DUT's step).
        """
        if not self.runner or not self.runner.ctx:
            raise RuntimeError('AtDevice is not attached to a running context')
        return self.runner.ctx._run_expect_step(
            self,
            pattern=pattern,
            timeout=timeout,
            port=expect_port if expect_port is not None else port,
            name=name,
            expect_fail=expect_fail,
            after=after,
        )

    def sleep(self, seconds: float) -> None:
        if self.runner and self.runner.ctx:
            self.runner.ctx.sleep(seconds)
        else:
            time.sleep(seconds)


# ---------------------------------------------------------------------------
# Expect helpers
# ---------------------------------------------------------------------------

def _normalize_expect(expect: Optional[ExpectSpec]) -> List[ExpectAtom]:
    if expect is None:
        return []
    if isinstance(expect, (list, tuple)):
        return list(expect)
    return [expect]


def _line_matches(line: str, spec: ExpectAtom) -> bool:
    """Match one expect atom against one UART line (no protocol-specific lists).

    - ``str``: exact match on ``line.strip()``
    - ``Contains``: substring anywhere in the raw line
    - ``Pattern``: ``re.search`` on the raw line
    """
    if isinstance(spec, Pattern):
        return bool(spec.search(line))
    if isinstance(spec, Contains):
        return spec.text in line
    return line.strip() == spec


def _patterns_matched(
    lines: Iterable[str], patterns: List[ExpectAtom]
) -> bool:
    if not patterns:
        return True
    remaining = list(patterns)
    for line in lines:
        still = []
        for p in remaining:
            if _line_matches(line, p):
                continue
            still.append(p)
        remaining = still
        if not remaining:
            return True
    return False



# ---------------------------------------------------------------------------
# Hook loading
# ---------------------------------------------------------------------------

def load_python_module(path: str, module_name: str = 'at_cmd_test_mod') -> ModuleType:
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise FileNotFoundError(f'Test/hook file not found: {path}')
    if not path.endswith('.py'):
        raise ValueError(f'Expected a .py file: {path}')
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f'Could not load module from {path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def call_hook(
    hook: HookSpec,
    ctx: 'AtContext',
    dut: Optional[AtDevice],
    role: str,
) -> None:
    if hook is None:
        return

    if callable(hook) and not isinstance(hook, str):
        _invoke_callable(hook, ctx, dut)
        return

    if isinstance(hook, str):
        mod = load_python_module(hook, module_name=f'at_cmd_hook_{role}')
        fn = None
        if role == 'setup' and hasattr(mod, 'setup'):
            fn = getattr(mod, 'setup')
        elif role == 'teardown' and hasattr(mod, 'teardown'):
            fn = getattr(mod, 'teardown')
        elif hasattr(mod, 'run'):
            fn = getattr(mod, 'run')
        elif hasattr(mod, role):
            fn = getattr(mod, role)
        if fn is None:
            raise RuntimeError(
                f'Hook file {hook} has no usable entry '
                f'(need {role}()/run())'
            )
        _invoke_callable(fn, ctx, dut)
        return

    raise TypeError(f'Invalid hook type: {type(hook)!r}')


def _invoke_callable(fn: Callable[..., Any], ctx: 'AtContext', dut: Optional[AtDevice]) -> None:
    try:
        sig = inspect.signature(fn)
        params = [
            p
            for p in sig.parameters.values()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        n = len(params)
    except (TypeError, ValueError):
        n = 1

    if n >= 2 and dut is not None:
        fn(ctx, dut)
    elif n >= 1:
        fn(ctx)
    else:
        fn()


# ---------------------------------------------------------------------------
# AtContext
# ---------------------------------------------------------------------------

class AtContext:
    def __init__(self, runner: 'AtCmdRunner') -> None:
        self.runner = runner
        self.logger = runner.logger
        self.report = runner.report
        self.fail_fast = runner.fail_fast
        self.default_timeout = runner.default_timeout
        self._aborted = False
        self._skip_remaining = False

    @property
    def duts(self) -> Dict[str, AtDevice]:
        return self.runner.devices

    def dut(self, name: str) -> AtDevice:
        if name not in self.runner.devices:
            raise KeyError(
                f'Unknown DUT {name!r}; available: {list(self.runner.devices)}'
            )
        return self.runner.devices[name]

    def __getitem__(self, name: str) -> AtDevice:
        return self.dut(name)

    def _default_dut(self) -> AtDevice:
        devices = self.runner.devices
        if len(devices) == 1:
            return next(iter(devices.values()))
        for key in ('AT1', 'default', 'DUT', 'dut'):
            if key in devices:
                return devices[key]
        raise RuntimeError(
            'ctx.at() requires a single default DUT; use ctx["AT1"].at(...) '
            f'instead (devices={list(devices)})'
        )

    def at(self, *args, **kwargs) -> StepResult:
        return self._default_dut().at(*args, **kwargs)

    def send_raw(self, *args, **kwargs) -> StepResult:
        return self._default_dut().send_raw(*args, **kwargs)

    def send_file(self, *args, **kwargs) -> StepResult:
        return self._default_dut().send_file(*args, **kwargs)

    def expect(self, *args, **kwargs) -> StepResult:
        return self._default_dut().expect(*args, **kwargs)

    def reset(self, name: Optional[str] = None) -> None:
        if name:
            self.dut(name).reset()
        else:
            self._default_dut().reset()

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.runner.should_exit:
                break
            self.runner.drain_all(timeout=min(0.05, end - time.monotonic()))

    def log_info(self, message: str) -> None:
        self.logger.log_info(message)

    def log_warn(self, message: str) -> None:
        self.logger.log_warn(message)

    def log_error(self, message: str) -> None:
        self.logger.log_error(message)

    # -- step runners -------------------------------------------------------

    def _begin_step(self, dut: AtDevice, name: str, cmd: str) -> Optional[StepResult]:
        if self._skip_remaining:
            result = StepResult(
                dut=dut.name,
                name=name,
                cmd=cmd,
                status=StepStatus.SKIPPED,
                elapsed=0.0,
                error='skipped due to fail-fast',
            )
            self.report.add(result)
            return result
        return None

    def _finish_step(
        self,
        dut: AtDevice,
        name: str,
        cmd: str,
        started: float,
        expect_fail: bool,
        error: Optional[str],
        device_failed: bool = False,
        matched_device_failure: bool = False,
    ) -> StepResult:
        """Classify step outcome.

        expect_fail=True only treats *device-side* failures (ERROR/FAIL or
        StepFailed(device_failed=True)) as EXPECTED_FAIL. Timeouts, hook
        errors, and other runner/infra failures remain real FAIL.
        """
        elapsed = time.monotonic() - started
        device_side = bool(device_failed or matched_device_failure)

        if expect_fail:
            if device_side:
                result = StepResult(
                    dut=dut.name,
                    name=name,
                    cmd=cmd,
                    status=StepStatus.EXPECTED_FAIL,
                    elapsed=elapsed,
                    error=error or 'device reported failure as expected',
                    expect_fail=True,
                )
            elif error is None:
                result = StepResult(
                    dut=dut.name,
                    name=name,
                    cmd=cmd,
                    status=StepStatus.FAIL,
                    elapsed=elapsed,
                    error='expected failure but step succeeded',
                    expect_fail=True,
                )
            else:
                result = StepResult(
                    dut=dut.name,
                    name=name,
                    cmd=cmd,
                    status=StepStatus.FAIL,
                    elapsed=elapsed,
                    error=error,
                    expect_fail=True,
                )
        elif error is None:
            result = StepResult(
                dut=dut.name,
                name=name,
                cmd=cmd,
                status=StepStatus.PASS,
                elapsed=elapsed,
                expect_fail=False,
            )
        else:
            result = StepResult(
                dut=dut.name,
                name=name,
                cmd=cmd,
                status=StepStatus.FAIL,
                elapsed=elapsed,
                error=error,
                expect_fail=False,
            )

        self.report.add(result)
        # Only log non-PASS outcomes during the run; final verdict is printed once.
        if result.status != StepStatus.PASS:
            label = result.status.value
            msg = f'{label} {name} ({elapsed:.3f}s)'
            if result.error:
                msg += f': {result.error}'
            if result.status == StepStatus.EXPECTED_FAIL:
                self.logger.log_warn(msg)
            else:
                self.logger.log_error(msg)

        if result.status == StepStatus.FAIL and self.fail_fast:
            self._skip_remaining = True
            raise FailFastAbort(result.error or name)
        return result

    def _run_at_step(
        self,
        dut: AtDevice,
        cmd: str,
        expect: Optional[ExpectSpec],
        timeout: Optional[float],
        setup: HookSpec,
        teardown: HookSpec,
        name: Optional[str],
        expect_fail: bool,
        expect_port: str = 'cmd',
    ) -> StepResult:
        step_name = name or cmd
        skipped = self._begin_step(dut, step_name, cmd)
        if skipped:
            return skipped

        started = time.monotonic()
        timeout = self.default_timeout if timeout is None else timeout
        patterns = _normalize_expect(expect)
        port_filter = normalize_expect_port(expect_port)
        error: Optional[str] = None
        device_failed = False
        matched_device_failure = False

        try:
            call_hook(setup, self, dut, 'setup')
            hist_start = len(dut._line_history)
            payload = (cmd if cmd.endswith('\r\n') else cmd + '\r\n').encode('utf-8')
            self.logger.log_info(f'{step_name} >>> {cmd}')
            dut.write_cmd(payload)
            term = self._wait_response(
                dut,
                patterns=patterns,
                timeout=timeout,
                hist_start=hist_start,
                require_terminator=True,
                accept_prompt=any(
                    (isinstance(p, str) and p == '>')
                    or (isinstance(p, Pattern) and p.pattern == '>')
                    for p in patterns
                ),
                port_filter=port_filter,
            )
            if term in _AT_FAIL_LINES:
                matched_device_failure = True
            elif patterns and all(
                isinstance(p, str) and p in _AT_FAIL_LINES for p in patterns
            ):
                matched_device_failure = True
        except StepFailed as e:
            error = str(e)
            device_failed = e.device_failed
        except FailFastAbort:
            raise
        except Exception as e:
            error = f'{type(e).__name__}: {e}'
        finally:
            try:
                call_hook(teardown, self, dut, 'teardown')
            except Exception as e:
                td_err = f'teardown error: {type(e).__name__}: {e}'
                error = f'{error}; {td_err}' if error else td_err

        return self._finish_step(
            dut,
            step_name,
            cmd,
            started,
            expect_fail,
            error,
            device_failed,
            matched_device_failure=matched_device_failure,
        )

    def _run_raw_step(
        self,
        dut: AtDevice,
        data: bytes,
        expect: Optional[ExpectSpec],
        timeout: Optional[float],
        setup: HookSpec,
        teardown: HookSpec,
        name: Optional[str],
        expect_fail: bool,
        wait_terminator: bool,
        expect_port: str = 'cmd',
    ) -> StepResult:
        step_name = name or f'send_raw({len(data)} bytes)'
        skipped = self._begin_step(dut, step_name, step_name)
        if skipped:
            return skipped

        started = time.monotonic()
        timeout = self.default_timeout if timeout is None else timeout
        patterns = _normalize_expect(expect)
        port_filter = normalize_expect_port(expect_port)
        error: Optional[str] = None
        device_failed = False
        matched_device_failure = False

        try:
            call_hook(setup, self, dut, 'setup')
            hist_start = len(dut._line_history)
            self.logger.log_info(f'{step_name} >>> raw {len(data)} bytes')
            dut.write_cmd(data)
            if patterns or wait_terminator:
                term = self._wait_response(
                    dut,
                    patterns=patterns,
                    timeout=timeout,
                    hist_start=hist_start,
                    require_terminator=wait_terminator or not patterns,
                    accept_prompt=False,
                    port_filter=port_filter,
                )
                if term in _AT_FAIL_LINES:
                    matched_device_failure = True
                elif patterns and all(
                    isinstance(p, str) and p in _AT_FAIL_LINES for p in patterns
                ):
                    matched_device_failure = True
        except StepFailed as e:
            error = str(e)
            device_failed = e.device_failed
        except FailFastAbort:
            raise
        except Exception as e:
            error = f'{type(e).__name__}: {e}'
        finally:
            try:
                call_hook(teardown, self, dut, 'teardown')
            except Exception as e:
                td_err = f'teardown error: {type(e).__name__}: {e}'
                error = f'{error}; {td_err}' if error else td_err

        return self._finish_step(
            dut,
            step_name,
            step_name,
            started,
            expect_fail,
            error,
            device_failed,
            matched_device_failure=matched_device_failure,
        )

    def _run_expect_step(
        self,
        dut: AtDevice,
        pattern: ExpectSpec,
        timeout: Optional[float],
        port: str,
        name: Optional[str],
        expect_fail: bool,
        after: Optional[int] = None,
    ) -> StepResult:
        patterns = _normalize_expect(pattern)
        port_filter = normalize_expect_port(port)
        step_name = name or f'expect[{dut.name}/{port_filter}]:{patterns!r}'
        skipped = self._begin_step(dut, step_name, step_name)
        if skipped:
            return skipped

        started = time.monotonic()
        timeout = self.default_timeout if timeout is None else timeout
        error: Optional[str] = None
        device_failed = False
        matched_device_failure = False
        if after is None:
            hist_start = len(dut._line_history)
        else:
            hist_start = max(0, min(after, len(dut._line_history)))

        try:
            self._wait_response(
                dut,
                patterns=patterns,
                timeout=timeout,
                hist_start=hist_start,
                require_terminator=False,
                accept_prompt=False,
                port_filter=port_filter,
            )
            if patterns and all(
                isinstance(p, str) and p in _AT_FAIL_LINES for p in patterns
            ):
                matched_device_failure = True
        except StepFailed as e:
            error = str(e)
            device_failed = e.device_failed
        except Exception as e:
            error = f'{type(e).__name__}: {e}'

        return self._finish_step(
            dut,
            step_name,
            step_name,
            started,
            expect_fail,
            error,
            device_failed,
            matched_device_failure=matched_device_failure,
        )

    def _history_lines(
        self,
        dut: AtDevice,
        hist_start: int,
        port_filter: str,
    ) -> List[str]:
        """Lines from ``hist_start`` onward, filtered by expect_port."""
        out: List[str] = []
        for which, line in dut._line_history[hist_start:]:
            if port_filter == 'cmd' and which != 'cmd':
                continue
            if port_filter == 'log' and which != 'log':
                continue
            out.append(line)
        return out

    @staticmethod
    def _is_terminator_expect(patterns: List[ExpectAtom]) -> bool:
        """True when expect is only OK/ERROR/FAIL string(s)."""
        return bool(patterns) and all(
            isinstance(p, str) and p in _AT_END_LINES for p in patterns
        )

    def _wait_response(
        self,
        dut: AtDevice,
        patterns: List[ExpectAtom],
        timeout: float,
        hist_start: int,
        require_terminator: bool,
        accept_prompt: bool,
        port_filter: str = 'any',
    ) -> Optional[str]:
        """Wait until expect patterns and/or reply terminator. Return terminator if any."""
        end = time.monotonic() + timeout
        saw_terminator: Optional[str] = None
        saw_prompt = False
        terminator_expect = self._is_terminator_expect(patterns)

        while time.monotonic() < end:
            if self.runner.should_exit:
                raise StepFailed('interrupted')
            self.runner.drain_all(timeout=0.05)

            filtered = self._history_lines(dut, hist_start, port_filter)
            saw_terminator = None
            saw_prompt = False
            for line in filtered:
                stripped = line.strip()
                if stripped in _AT_END_LINES:
                    saw_terminator = stripped
                if accept_prompt and '>' in line:
                    saw_prompt = True

            if patterns:
                if _patterns_matched(filtered, patterns):
                    return saw_terminator
                if accept_prompt and saw_prompt and all(
                    isinstance(p, str) and p == '>' for p in patterns
                ):
                    return saw_terminator

            if require_terminator and saw_terminator:
                if not patterns:
                    if saw_terminator in _AT_FAIL_LINES:
                        raise StepFailed(
                            f'device returned {saw_terminator}',
                            device_failed=True,
                        )
                    return saw_terminator
                if saw_terminator in _AT_FAIL_LINES:
                    raise StepFailed(
                        f'device returned {saw_terminator}; '
                        f'expect not matched: {patterns!r}',
                        device_failed=True,
                    )
                # expect='OK'/'ERROR'/... but got a different terminator (e.g.
                # expect ERROR, got OK) — fail immediately, do not wait out timeout.
                if terminator_expect:
                    raise StepFailed(
                        f'device returned {saw_terminator}; '
                        f'expect not matched: {patterns!r}',
                        device_failed=saw_terminator in _AT_FAIL_LINES,
                    )
                # OK arrived but non-terminator patterns still pending.
                # Only keep waiting for post-OK prompt (AT+CIPSEND → OK then '>').
                # Otherwise URC/body lines come *before* OK; waiting out timeout
                # cannot help (e.g. expect [+CIPFWVER:, OK] but device only OK).
                if accept_prompt:
                    continue
                raise StepFailed(
                    f'device returned {saw_terminator}; '
                    f'expect not matched: {patterns!r}',
                    device_failed=False,
                )

            if accept_prompt and saw_prompt and not patterns:
                return saw_terminator

        filtered = self._history_lines(dut, hist_start, port_filter)
        saw_terminator = None
        for line in filtered:
            stripped = line.strip()
            if stripped in _AT_END_LINES:
                saw_terminator = stripped
        if patterns and _patterns_matched(filtered, patterns):
            return saw_terminator
        if require_terminator and saw_terminator:
            if patterns and not _patterns_matched(filtered, patterns):
                raise StepFailed(
                    f'timeout after {timeout}s; got {saw_terminator} but '
                    f'expect not matched: {patterns!r}',
                    device_failed=saw_terminator in _AT_FAIL_LINES,
                )
            if saw_terminator in _AT_FAIL_LINES:
                raise StepFailed(
                    f'device returned {saw_terminator}',
                    device_failed=True,
                )
            return saw_terminator
        if patterns:
            raise StepFailed(
                f'timeout after {timeout}s waiting for {patterns!r}'
            )
        raise StepFailed(
            f'timeout after {timeout}s waiting for OK/ERROR/FAIL'
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class DeviceSpec:
    name: str
    log_port: Optional[str] = None
    cmd_port: Optional[str] = None
    log_baudrate: Optional[int] = None
    cmd_baudrate: Optional[int] = None


class AtCmdRunner:
    def __init__(self) -> None:
        self.logger = RunnerLog()
        self.report = TestReport()
        self.devices: Dict[str, AtDevice] = {}
        self.ctx: Optional[AtContext] = None
        self.fail_fast = False
        self.default_timeout = 5.0
        self.ready_timeout = 5.0
        self.no_reboot_chip = False
        self.flow_control = False
        self.baudrate = 115200
        self.should_exit = False

    def signal_handler(self, sig, frame) -> None:
        self.should_exit = True
        self.logger.log_info('\nCtrl+C pressed, exiting...')

    def drain_all(self, timeout: float = 0.05) -> None:
        self._wait_for_io(timeout)
        for dut in self.devices.values():
            dut.drain_once()

    def _wait_for_io(self, timeout: float = 0.05) -> None:
        if platform.system().lower() == 'windows' or not hasattr(select, 'select'):
            time.sleep(timeout)
            return
        fds: List[int] = []
        for dut in self.devices.values():
            fds.extend(dut.filenos())
        if not fds:
            time.sleep(timeout)
            return
        try:
            select.select(fds, [], [], timeout)
        except (ValueError, OSError):
            time.sleep(timeout)

    def open_all(self) -> None:
        for dut in self.devices.values():
            dut.open()

    def close_all(self) -> None:
        for dut in self.devices.values():
            try:
                dut.close()
            except Exception:
                pass

    def reset_and_wait_ready(self) -> None:
        for dut in self.devices.values():
            if self.no_reboot_chip:
                self.logger.log_info(
                    'skip reboot/ready wait (--no-reboot-chip)'
                )
                continue
            dut.reset()
            self._wait_dut_ready(dut)

    def _wait_dut_ready(self, dut: AtDevice) -> None:
        self.logger.log_info(
            f'Waiting for ready (timeout {self.ready_timeout}s)...'
        )
        end = time.monotonic() + self.ready_timeout
        while time.monotonic() < end:
            if self.should_exit:
                return
            self.drain_all(0.05)
            if dut.at_ready:
                return
        self.logger.log_warn(
            f'ready not seen within {self.ready_timeout}s; continuing'
        )

    def build_devices(self, specs: List[DeviceSpec]) -> None:
        self.devices.clear()
        for spec in specs:
            if not spec.log_port or not spec.cmd_port:
                raise ValueError(
                    f'DUT {spec.name}: both log and cmd ports are required'
                )
            validate_serial_port(spec.log_port)
            if spec.cmd_port != spec.log_port:
                validate_serial_port(spec.cmd_port)
            dut = AtDevice(
                spec.name,
                spec.log_port,
                spec.cmd_port,
                log_baudrate=spec.log_baudrate or self.baudrate,
                cmd_baudrate=spec.cmd_baudrate or self.baudrate,
                flow_control=self.flow_control,
                logger=self.logger,
                runner=self,
            )
            self.devices[spec.name] = dut

    def run_test_module(self, mod: ModuleType) -> int:
        self.ctx = AtContext(self)
        self.ctx.fail_fast = self.fail_fast

        setup_fn = getattr(mod, 'setup', None)
        teardown_fn = getattr(mod, 'teardown', None)
        run_fn = getattr(mod, 'run', None) or getattr(mod, 'test', None)
        if run_fn is None:
            raise RuntimeError('Test module must define run(ctx) or test(ctx)')

        mod_fail_fast = bool(getattr(mod, 'FAIL_FAST', False))
        if mod_fail_fast:
            self.fail_fast = True
            self.ctx.fail_fast = True

        try:
            setup_ok = True
            if setup_fn:
                self.logger.log_info('Running suite setup...')
                try:
                    call_hook(setup_fn, self.ctx, None, 'setup')
                except Exception as e:
                    setup_ok = False
                    self.logger.log_error(
                        f'Suite setup failed: {type(e).__name__}: {e}'
                    )
                    self.report.add(
                        StepResult(
                            dut='*',
                            name='suite_setup',
                            cmd='',
                            status=StepStatus.FAIL,
                            elapsed=0.0,
                            error=f'{type(e).__name__}: {e}',
                        )
                    )
            if setup_ok:
                try:
                    self.logger.log_info('Running test...')
                    call_hook(run_fn, self.ctx, None, 'run')
                except FailFastAbort as e:
                    self.logger.log_warn(f'Fail-fast abort: {e}')
                except Exception as e:
                    self.logger.log_error(
                        f'Suite run failed: {type(e).__name__}: {e}'
                    )
                    self.logger.log_error(traceback.format_exc())
                    self.report.add(
                        StepResult(
                            dut='*',
                            name='suite_run',
                            cmd='',
                            status=StepStatus.FAIL,
                            elapsed=0.0,
                            error=f'{type(e).__name__}: {e}',
                        )
                    )
        finally:
            if teardown_fn:
                try:
                    self.logger.log_info('Running suite teardown...')
                    call_hook(teardown_fn, self.ctx, None, 'teardown')
                except Exception as e:
                    self.logger.log_error(
                        f'Suite teardown failed: {type(e).__name__}: {e}'
                    )
                    self.report.add(
                        StepResult(
                            dut='*',
                            name='suite_teardown',
                            cmd='',
                            status=StepStatus.FAIL,
                            elapsed=0.0,
                            error=str(e),
                        )
                    )

        for line in self.report.summary_lines():
            if self.report.has_real_failures():
                self.logger.log_error(line)
            else:
                self.logger.log_info(line)

        return 1 if self.report.has_real_failures() else 0


# ---------------------------------------------------------------------------
# Device table merge
# ---------------------------------------------------------------------------

def parse_dut_arg(value: str) -> DeviceSpec:
    """Parse --dut NAME=log,cmd."""
    if '=' not in value:
        raise argparse.ArgumentTypeError(
            f'Invalid --dut {value!r}; expected NAME=log_port,cmd_port'
        )
    name, ports = value.split('=', 1)
    name = name.strip()
    parts = [p.strip() for p in ports.split(',')]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise argparse.ArgumentTypeError(
            f'Invalid --dut {value!r}; expected NAME=log_port,cmd_port'
        )
    return DeviceSpec(
        name=name,
        log_port=resolve_port(parts[0]),
        cmd_port=resolve_port(parts[1]),
    )


def devices_from_module(mod: ModuleType) -> Dict[str, DeviceSpec]:
    raw = getattr(mod, 'DEVICES', None)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TypeError('DEVICES must be a dict')
    out: Dict[str, DeviceSpec] = {}
    for name, cfg in raw.items():
        cfg = cfg or {}
        if not isinstance(cfg, dict):
            raise TypeError(f'DEVICES[{name!r}] must be a dict')
        out[str(name)] = DeviceSpec(
            name=str(name),
            log_port=resolve_port(cfg.get('log') or cfg.get('log_port')),
            cmd_port=resolve_port(cfg.get('cmd') or cfg.get('cmd_port')),
            log_baudrate=cfg.get('log_baudrate'),
            cmd_baudrate=cfg.get('cmd_baudrate'),
        )
    return out


def merge_device_specs(
    mod_specs: Dict[str, DeviceSpec],
    cli_duts: List[DeviceSpec],
    port0: Optional[str],
    port1: Optional[str],
) -> List[DeviceSpec]:
    merged: Dict[str, DeviceSpec] = {k: DeviceSpec(
        name=v.name,
        log_port=v.log_port,
        cmd_port=v.cmd_port,
        log_baudrate=v.log_baudrate,
        cmd_baudrate=v.cmd_baudrate,
    ) for k, v in mod_specs.items()}

    for d in cli_duts:
        if d.name in merged:
            cur = merged[d.name]
            cur.log_port = d.log_port or cur.log_port
            cur.cmd_port = d.cmd_port or cur.cmd_port
        else:
            merged[d.name] = DeviceSpec(
                name=d.name, log_port=d.log_port, cmd_port=d.cmd_port
            )

    port0 = resolve_port(port0)
    port1 = resolve_port(port1)

    # Legacy -p0/-p1 maps to AT1 when no multi-dut CLI entries used them.
    if port0 is not None or port1 is not None:
        name = 'AT1'
        if name not in merged and not merged:
            merged[name] = DeviceSpec(name=name)
        elif name not in merged and len(merged) == 1:
            name = next(iter(merged))
        elif name not in merged:
            merged[name] = DeviceSpec(name=name)
        cur = merged[name]
        if port0 is not None:
            cur.log_port = port0
        if port1 is not None:
            cur.cmd_port = port1

    if not merged:
        # Single auto-detected DUT named AT1
        log_p, cmd_p = resolve_ports(None, None)
        merged['AT1'] = DeviceSpec(name='AT1', log_port=log_p, cmd_port=cmd_p)

    # Auto-detect ports only when exactly one DUT and ports missing.
    if len(merged) == 1:
        only = next(iter(merged.values()))
        if only.log_port is None or only.cmd_port is None:
            log_p, cmd_p = resolve_ports(only.log_port, only.cmd_port)
            only.log_port = log_p
            only.cmd_port = cmd_p
    else:
        missing = [
            s.name
            for s in merged.values()
            if not s.log_port or not s.cmd_port
        ]
        if missing:
            raise ValueError(
                'Multi-DUT mode requires explicit ports for: '
                + ', '.join(missing)
                + ' (use --dut NAME=log,cmd or DEVICES in the test file)'
            )

    return list(merged.values())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='ESP-AT multi-DUT automation test runner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Test files are pure Python with run(ctx)/test(ctx). '
            'Default policy continues after failures; use --fail-fast to stop. '
            'Mark negative cases with expect_fail=True (counts as pass).\n'
            '\n'
            'Documentation and examples:\n'
            '  bin/examples/README.md\n'
            '  bin/examples/README_CN.md\n'
            '  bin/examples/at_smoke.py\n'
            '  bin/examples/at_multi_dut.py'
        ),
    )
    parser.add_argument(
        '--test', '-t',
        required=True,
        metavar='FILE',
        help='Python test file (must define run(ctx) or test(ctx)).',
    )
    parser.add_argument(
        '--dut',
        action='append',
        default=[],
        type=parse_dut_arg,
        metavar='NAME=LOG,CMD',
        help='Register/override a DUT. Repeatable. '
             'Example: AT1=/dev/ttyUSB0,/dev/ttyUSB1 or AT1=0,1',
    )
    parser.add_argument(
        '--port0', '-p0',
        default=None,
        help='Single-DUT log port (alias for --dut AT1=LOG,CMD with -p1); '
             'full path or digit N -> /dev/ttyUSBN.',
    )
    parser.add_argument(
        '--port1', '-p1',
        default=None,
        help='Single-DUT command port; full path or digit N -> /dev/ttyUSBN.',
    )
    parser.add_argument(
        '--baudrate', '-b',
        type=int,
        default=115200,
        help='Default baud rate for all ports. Default: 115200.',
    )
    parser.add_argument(
        '--port0-baudrate', '-p0b',
        type=int,
        default=None,
        help='Override log baud for single-DUT AT1 (legacy).',
    )
    parser.add_argument(
        '--port1-baudrate', '-p1b',
        type=int,
        default=None,
        help='Override cmd baud for single-DUT AT1 (legacy).',
    )
    parser.add_argument(
        '--flow-control', '-fc',
        action='store_true',
        help='Enable hardware flow control on command ports.',
    )
    parser.add_argument(
        '--save-log', '-s',
        action='store_true',
        help='Save logs under ./esp_logs/.',
    )
    parser.add_argument(
        '--prompt', '-p',
        action='store_true',
        help='Prefix lines with source tag (e.g. AT1, LOG1, PC). '
             'On by default when more than one DUT is used.',
    )
    parser.add_argument(
        '--no-timestamp', '-nt',
        action='store_true',
        help='Disable timestamps.',
    )
    parser.add_argument(
        '--no-reboot-chip', '-nr',
        action='store_true',
        help='Skip chip reset at start.',
    )
    parser.add_argument(
        '--fail-fast',
        action='store_true',
        help='Stop after the first real failure or hook error.',
    )
    parser.add_argument(
        '--default-timeout',
        type=float,
        default=5.0,
        help='Default per-step timeout in seconds. Default: 5.',
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = create_argument_parser()
    args = parser.parse_args(argv)

    runner = AtCmdRunner()
    runner.logger.enable_timestamp = not args.no_timestamp
    runner.logger.enable_prompt = args.prompt
    runner.fail_fast = args.fail_fast
    runner.default_timeout = args.default_timeout
    runner.no_reboot_chip = args.no_reboot_chip
    runner.flow_control = args.flow_control
    runner.baudrate = args.baudrate

    signal.signal(signal.SIGINT, runner.signal_handler)

    if args.save_log:
        try:
            runner.logger.open_log_file()
        except Exception as e:
            runner.logger.log_error(str(e))
            return 1

    try:
        mod = load_python_module(args.test, module_name='at_cmd_user_test')
    except Exception as e:
        runner.logger.log_error(f'Failed to load test file: {e}')
        runner.logger.close_log_file()
        return 1

    try:
        mod_specs = devices_from_module(mod)
        specs = merge_device_specs(
            mod_specs, list(args.dut), args.port0, args.port1
        )
        # Apply legacy baud overrides to AT1 if present.
        if args.port0_baudrate is not None or args.port1_baudrate is not None:
            for s in specs:
                if s.name == 'AT1' or len(specs) == 1:
                    if args.port0_baudrate is not None:
                        s.log_baudrate = args.port0_baudrate
                    if args.port1_baudrate is not None:
                        s.cmd_baudrate = args.port1_baudrate
                    break
        runner.build_devices(specs)
        # Multi-DUT: default-on source tags so AT1/LOG1/AT2/... stay distinguishable.
        if not args.prompt and len(runner.devices) > 1:
            runner.logger.enable_prompt = True
    except Exception as e:
        runner.logger.log_error(f'Device configuration error: {e}')
        runner.logger.close_log_file()
        return 1

    runner.logger.log_info(
        'AT automation runner started with devices: '
        + ', '.join(
            f'{d.name}(log={d.log_port}, cmd={d.cmd_port})'
            for d in runner.devices.values()
        )
    )

    exit_code = 1
    try:
        runner.open_all()
        runner.reset_and_wait_ready()
        if runner.should_exit:
            exit_code = 1
        else:
            exit_code = runner.run_test_module(mod)
    except Exception as e:
        runner.logger.log_error(f'Fatal error: {e}')
        runner.logger.log_error(traceback.format_exc())
        exit_code = 1
    finally:
        runner.close_all()
        runner.logger.close_log_file()

    return exit_code


if __name__ == '__main__':
    sys.exit(main())
