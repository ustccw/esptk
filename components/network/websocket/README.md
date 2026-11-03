# WebSocket servers

`ws-server.py` 和 `wss-server.py` 各自独立，用来给 AT 测 WebSocket 吞吐。依赖 `requirements.txt` 里的 `websockets`。库自己回 PONG，脚本不再往同一条连接上另开写线程。

`-m` / `--mode`：

| 模式 | 行为 |
| --- | --- |
| `recv` | 只收应用数据，不发。测 AT 上行。 |
| `send` | 只发脚本生成的二进制流，不统计对端上行。测 AT 下行。发完后关闭连接。 |
| `echo` | 收到一帧就原样回一帧。 |
| `both` | 收和发并行。发送的是脚本自己产生的二进制流，不是把收到的内容回回去。 |

- `-sb` / `--send-bytes`：一共发多少字节。`send` 和 `both` 必填。
- `-rb` / `--recv-bytes`：期望收多少字节。`recv`、`echo`、`both` 可用。收到这个字节数时 RX 速率定格，之后连接还在也不会把速率拉低。再收到更多字节时打一行 warning，字节和帧数继续累加。
- `-sr` / `--send-rate`：字节/秒，`0` 表示不限速。只用于 `send` 和 `both`。
- `-fs` / `--frame-size`：每帧长度，默认 `4096`。不要超过 AT 侧 `AT+WSCFG` 的 buffer（烟测用 8192）

限速按帧：下一帧时刻是第一帧发出时刻加上已发字节除以 rate。已经落后就马上发。`send` 和 `both` 在客户端连上后先等 1 秒，再发第一帧。

RX 从这一次连接收到的第一包应用数据开始计时，TX 从这一次连接发出的第一包开始计时。PING/PONG 不计入。给了 `-rb` 时，RX 速率的结束时刻是第一次达到该字节数的时刻。连接关掉后不再打吞吐，下一次连接从零重算。`-i` / `--interval` 默认 `1` 秒打一行（连接数、RX/TX 字节、帧数、KiB/s、Mbps）。没有连接时不打；这一行和上一行相同时也不再重复打。`0` 表示只在连接结束和 Ctrl-C 时打累计结果。

`-H` / `--host` 默认 `0.0.0.0`。`-p` / `--port` 明文默认 `8765`，TLS 默认 `8766`。

```bash
python ws-server.py -m recv -rb 262144
python ws-server.py -m send -sb 262144 -sr 32768 -fs 4096
python ws-server.py -m echo -rb 262144 -i 0
python ws-server.py -m both -sb 262144 -rb 262144 -sr 49152 -fs 4096

python wss-server.py -m recv -rb 262144
python wss-server.py -pki 01 -m send -sb 262144 -sr 32768
python wss-server.py -m recv --verify-client
```

不压缩（`permessage-deflate` 关闭），单帧大小不设上限。

## TLS 证书

`wss-server.py` 的服务器身份默认取脚本旁边的 `wss_00/server/`，不是 AT 分区里的客户端证书：

- 证书：`wss_00/server/server.crt`
- 私钥：`wss_00/server/server.key`
- 校验客户端时的 CA：同目录的 `server_ca.crt`

`wss_00/` 和 `wss_01/` 是从 test-kits 拷来的完整材料，client 和 server 都在。`-pki 01` 改用 `wss_01/server/` 的同名文件。`--pki` 是同一参数。`-c` / `--cert`、`-k` / `--key`、`--ca` 可以单独覆盖。`--verify-client` 才 `CERT_REQUIRED` 并加载 CA；默认不校验，AT 用 `AT+WSCFG=...,0` 就能连。

和 `esp-at/components/customized_partitions/raw_data` 里 `wss_*` 的对应关系：

- `wss_cert/wss_client_00.crt`、`wss_key/wss_client_00.key` 等于 `wss_00/client/client.crt` 和 `client.key`。`01` 同样等于 `wss_01/client/`。
- `wss_ca/wss_ca_00.crt` 等于 `wss_00/client/server_ca.crt`，不是 `wss_00/server/server_ca.crt`。`01` 同样。这块 CA 签的是 `server/server.crt`，设备用 auth mode 2 时靠它校验服务器。
- `server/server_ca.crt` 签的是 `client/client.crt`，也就是烧进设备的 `wss_client_00` / `wss_client_01`。服务器要校验设备时用这一份，不能用 `wss_ca_*.crt`。
- `server/server.crt` 和 `server/server.key` 在 `raw_data` 里没有同名副本。本目录下的 `wss_00/server/` 和 `wss_01/server/` 是从 test-kits 拷出来的那一份。
