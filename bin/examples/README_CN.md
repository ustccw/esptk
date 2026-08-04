# at-test.py 示例

ESP-AT 多 DUT 自动化测试工具（`bin/at-test.py`）的示例与用法说明。

English version: [README.md](README.md)。

## 快速开始

每个 DUT 需要两路 UART：**日志口**（`-p0`）和 **命令口**（`-p1`）。

```bash
# 单 DUT 冒烟（AT → STA 模式 → 入网 → 查版本）
at-test.py -t bin/examples/at_smoke.py -p0 /dev/ttyUSB0 -p1 /dev/ttyUSB1

# 双 DUT SoftAP TCP 回显
#   AT1: SoftAP + TCP server   (log=/dev/ttyUSB0, cmd=/dev/ttyUSB1)
#   AT2: Station + TCP client  (log=/dev/ttyUSB2, cmd=/dev/ttyUSB3)
at-test.py -t bin/examples/at_multi_dut.py \
  --dut AT1=/dev/ttyUSB0,/dev/ttyUSB1 \
  --dut AT2=/dev/ttyUSB2,/dev/ttyUSB3
```

常用参数：

| 参数 | 含义 |
|------|------|
| `-s` / `--save-log` | 把控制台输出保存到 `./esp_logs/` |
| `-p` / `--prompt` | 行首加上来源标签（如 `AT1`、`LOG1`、`PC`）。多 DUT 时默认开启 |
| `-nr` / `--no-reboot-chip` | 启动时不复位芯片 |
| `--fail-fast` | 遇到第一个真实失败即停止 |
| `--default-timeout SEC` | 单步默认超时（默认 5 秒） |

## 示例脚本

| 文件 | 用途 |
|------|------|
| `at_smoke.py` | 单 DUT 冒烟：`AT` → `CWMODE=1` → `CWJAP="688018",""` → `GMR` |
| `at_multi_dut.py` | 双 DUT SoftAP TCP 回显：AT1 server ↔ AT2 client，载荷 `hello, esp!` |

## 编写测试文件

约定：

- 可选 `DEVICES`：声明命名 DUT。
- 可选 `setup(ctx)` / `teardown(ctx)`。
- 必须提供 `run(ctx)` 或 `test(ctx)`（推荐 `run`）。
- 可选 `FAIL_FAST = True`。

单 DUT 最小示例：

```python
def run(ctx):
    ctx.at('AT', expect='OK')
    ctx.at('AT+INVALID', expect='ERROR', expect_fail=True)
```

多 DUT：

```python
DEVICES = {'AT1': {}, 'AT2': {}}

def run(ctx):
    ctx['AT1'].at('AT', expect='OK')
    ctx['AT2'].at('AT', expect='OK')
```

### `ctx.at` / `dut.at` 参数

单 DUT 时可用 `ctx.at(...)`；多 DUT 时用 `ctx['AT1'].at(...)` / `ctx.dut('AT1').at(...)`。

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `cmd` | *（必填）* | AT 命令字符串 |
| `expect` | `'OK'` | 匹配规格：精确字符串、`re.compile(...)`，或它们的列表 |
| `timeout` | `None` | 本步超时秒数（`None` 时用 CLI `--default-timeout`，默认 5） |
| `name` | `None` | 步骤名，出现在日志和 `RESULT: FAIL` 中（默认用命令文本） |
| `expect_fail` | `False` | 为 `True` 时，仅设备侧 `ERROR`/`FAIL` 记为通过（超时/基础设施错误仍失败） |
| `expect_port` | `'cmd'` | 在哪路 UART 上匹配：`cmd` / `log` / `any` |
| `setup` | `None` | 本步执行前 hook（callable 或 `.py` 路径） |
| `teardown` | `None` | 本步执行后 hook（callable 或 `.py` 路径） |

### Expect 匹配

通过 `expect_port` 指定在哪一路 UART 上匹配：

| 取值 | 匹配范围 |
|------|----------|
| `cmd`（默认） | AT 命令口 |
| `log` | AT 日志口 |
| `any` | 任意一路 |

Expect 规格（通用匹配，不特化 AT 关键字）：

| 规格 | 含义 |
|------|------|
| `"OK"` | 整行去空白后精确匹配 |
| `re.compile(r"...")` | 正则（子串也用这个，例如 `re.compile(r'\+IPD')`） |

反向用例：设 `expect_fail=True`，仅设备侧失败（`ERROR`/`FAIL`）记为通过；超时与 runner/hook 错误仍为真实失败。

### 其他 API

- `dut.expect(pattern, timeout=None, expect_port='any', name=..., after=None)` — 只等待、不发送。对端 DUT 步骤期间已到达的行，用 `after=dut.mark()` 纳入匹配范围。
- `dut.send_raw(data, expect=None, ...)` / `dut.send_file(path, ...)`
- `dut.mark()` — 历史快照，配合 `expect(..., after=...)`
- `ctx.sleep(seconds)` / `ctx.reset()` / `ctx.log_info(...)`

完整 CLI 选项见 `at-test.py -h`。
