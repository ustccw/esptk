# at-test.py examples

Examples for the ESP-AT multi-DUT automation runner (`bin/at-test.py`).

中文说明见 [README_CN.md](README_CN.md)。

## Quick start

Each DUT needs two UARTs: a **log** port (`-p0`) and a **command** port (`-p1`).

```bash
# Single DUT smoke (ping → STA mode → join AP → version)
at-test.py -t bin/examples/at_smoke.py -p0 /dev/ttyUSB0 -p1 /dev/ttyUSB1

# Two-DUT SoftAP TCP echo
#   AT1: SoftAP + TCP server   (log=/dev/ttyUSB0, cmd=/dev/ttyUSB1)
#   AT2: Station + TCP client  (log=/dev/ttyUSB2, cmd=/dev/ttyUSB3)
at-test.py -t bin/examples/at_multi_dut.py \
  --dut AT1=/dev/ttyUSB0,/dev/ttyUSB1 \
  --dut AT2=/dev/ttyUSB2,/dev/ttyUSB3
```

Useful flags:

| Flag | Meaning |
|------|---------|
| `-s` / `--save-log` | Save console output under `./esp_logs/` |
| `-p` / `--prompt` | Prefix lines with source tag (`AT1`, `LOG1`, `PC`, …). Auto-on for multi-DUT |
| `-nr` / `--no-reboot-chip` | Skip chip reset at start |
| `--fail-fast` | Stop on the first real failure |
| `--default-timeout SEC` | Default per-step timeout (default: 5) |

## Example scripts

| File | Purpose |
|------|---------|
| `at_smoke.py` | Single-DUT smoke: `AT` → `CWMODE=1` → `CWJAP="688018",""` → `GMR` |
| `at_multi_dut.py` | Two-DUT SoftAP TCP echo: AT1 server ↔ AT2 client, payload `hello, esp!` |

## Writing a test file

Contract:

- Optional `DEVICES` dict declaring named DUTs.
- Optional `setup(ctx)` / `teardown(ctx)`.
- Required `run(ctx)` or `test(ctx)` (`run` preferred).
- Optional `FAIL_FAST = True`.

Minimal single-DUT:

```python
def run(ctx):
    ctx.at('AT', expect='OK')
    ctx.at('AT+INVALID', expect='ERROR', expect_fail=True)
```

Multi-DUT:

```python
DEVICES = {'AT1': {}, 'AT2': {}}

def run(ctx):
    ctx['AT1'].at('AT', expect='OK')
    ctx['AT2'].at('AT', expect='OK')
```

### `ctx.at` / `dut.at` parameters

`ctx.at(...)` is a shortcut for the single default DUT; with multiple DUTs use
`ctx['AT1'].at(...)` / `ctx.dut('AT1').at(...)`.

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `cmd` | *(required)* | AT command string |
| `expect` | `'OK'` | Match spec: exact string, `re.compile(...)`, or a list of these |
| `timeout` | `None` | Step timeout in seconds (`None` → CLI `--default-timeout`, default 5) |
| `name` | `None` | Step label in logs / `RESULT: FAIL` (defaults to the command text) |
| `expect_fail` | `False` | If `True`, a device-side `ERROR`/`FAIL` counts as pass (not timeouts/infra errors) |
| `expect_port` | `'cmd'` | UART side to match: `cmd` / `log` / `any` |
| `setup` | `None` | Per-step pre-hook (callable or `.py` path) |
| `teardown` | `None` | Per-step post-hook (callable or `.py` path) |

### Expect matching

Matching is per-DUT UART side via `expect_port`:

| Value | Match on |
|-------|----------|
| `cmd` (default) | AT command port |
| `log` | AT log port |
| `any` | either port |

Expect specs (generic, not AT-keyword-aware):

| Spec | Meaning |
|------|---------|
| `"OK"` | exact stripped line match |
| `re.compile(r"...")` | regex (also use for substring, e.g. `re.compile(r'\+IPD')`) |

```python
import re

ctx.at('AT+GMR', expect='OK')
ctx.expect(re.compile(r'stack overflow'), expect_port='log', timeout=5)
```

Negative cases: set `expect_fail=True` so a **device-side** failure (`ERROR`/`FAIL`)
counts as pass. Timeouts and runner/hook errors still count as real FAIL.

### Other APIs

- `dut.expect(pattern, timeout=None, expect_port='any', name=..., after=None)` — wait without sending. Use `after=dut.mark()` to include lines already received while another DUT was active.
- `dut.send_raw(data, expect=None, ...)` / `dut.send_file(path, ...)`
- `dut.mark()` — history snapshot for `expect(..., after=...)`
- `ctx.sleep(seconds)` / `ctx.reset()` / `ctx.log_info(...)`

See `at-test.py -h` for CLI options.
