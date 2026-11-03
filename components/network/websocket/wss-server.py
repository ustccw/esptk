#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TLS WebSocket server for AT throughput tests.

Modes (``-m``) match ws-server.py: ``recv``, ``send``, ``echo``, ``both``.

The server certificate defaults to the copies next to this script:

* ``wss_00/server/server.crt``
* ``wss_00/server/server.key``

``-pki 01`` switches to ``wss_01/server/``. ``--verify-client`` loads
``server/server_ca.crt`` from the same directory. That CA signs the client
certificate flashed as ``wss_client_00`` / ``wss_client_01``. It is not the
CA the device uses to verify this server (``wss_ca_00`` / ``wss_ca_01``).
"""

import argparse
import asyncio
import signal
import ssl
import time
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8766
DEFAULT_FRAME = 4096
HERE = Path(__file__).resolve().parent


class Stats:
    def __init__(self):
        self.connections = 0
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.rx_frames = 0
        self.tx_frames = 0
        self.rx_t0 = None
        self.tx_t0 = None

    def add_rx(self, n):
        if n <= 0:
            return
        if self.rx_t0 is None:
            self.rx_t0 = time.monotonic()
        self.rx_bytes += n
        self.rx_frames += 1

    def add_tx(self, n):
        if n <= 0:
            return
        if self.tx_t0 is None:
            self.tx_t0 = time.monotonic()
        self.tx_bytes += n
        self.tx_frames += 1

    @staticmethod
    def _rate(nbytes, t0):
        if t0 is None or nbytes <= 0:
            return "n/a"
        dt = time.monotonic() - t0
        if dt <= 0:
            return "n/a"
        kib = nbytes / 1024 / dt
        mbps = nbytes * 8 / dt / 1_000_000
        return f"{kib:.1f} KiB/s, {mbps:.3f} Mbps"

    def idle(self):
        return self.connections == 0 and self.rx_bytes == 0 and self.tx_bytes == 0

    def clear_traffic(self):
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.rx_frames = 0
        self.tx_frames = 0
        self.rx_t0 = None
        self.tx_t0 = None

    def format(self):
        return (
            f"conn={self.connections} "
            f"rx={self.rx_bytes} B/{self.rx_frames} frames "
            f"({self._rate(self.rx_bytes, self.rx_t0)}) "
            f"tx={self.tx_bytes} B/{self.tx_frames} frames "
            f"({self._rate(self.tx_bytes, self.tx_t0)})"
        )


def payload_len(message):
    if isinstance(message, str):
        return len(message.encode("utf-8"))
    return len(message)


def chunk_bytes(start, n):
    return bytes(((start + i) * 31 + 7) & 0xFF for i in range(n))


async def recv_loop(websocket, stats, echo, count):
    try:
        async for message in websocket:
            n = payload_len(message)
            if count:
                stats.add_rx(n)
            if echo and n:
                await websocket.send(message)
                stats.add_tx(n)
    except ConnectionClosed:
        return


async def send_loop(websocket, args, stats):
    sent = 0
    t0 = None
    try:
        # Let AT+WSOPEN print OK before the first +WS_DATA.
        await asyncio.sleep(1)
        while sent < args.bytes:
            n = min(args.size, args.bytes - sent)
            if t0 is not None and args.rate > 0:
                delay = t0 + sent / args.rate - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            await websocket.send(chunk_bytes(sent, n))
            now = time.monotonic()
            if t0 is None:
                t0 = now
            if stats.tx_t0 is None:
                stats.tx_t0 = now
            stats.add_tx(n)
            sent += n
    except ConnectionClosed:
        return


def build_handler(args, stats):
    async def handler(websocket):
        if stats.connections == 0:
            stats.clear_traffic()
        stats.connections += 1
        peer = websocket.remote_address
        print(f"open {peer} mode={args.mode}", flush=True)
        tasks = []
        try:
            if args.mode in ("recv", "echo", "both"):
                tasks.append(asyncio.create_task(
                    recv_loop(websocket, stats, args.mode == "echo", True)))
            elif args.mode == "send":
                tasks.append(asyncio.create_task(
                    recv_loop(websocket, stats, False, False)))
            if args.mode in ("send", "both"):
                tasks.append(asyncio.create_task(send_loop(websocket, args, stats)))
            if args.mode == "send":
                await tasks[-1]
            else:
                await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            print(f"closed {peer} {stats.format()}", flush=True)
            stats.connections -= 1
            if stats.connections == 0:
                stats.clear_traffic()

    return handler


async def reporter(stats, interval):
    last = None
    while True:
        await asyncio.sleep(interval)
        if stats.connections == 0:
            last = None
            continue
        line = stats.format()
        if line == last:
            continue
        last = line
        print(line, flush=True)


def material_paths(args):
    base = HERE / f"wss_{args.pki}" / "server"
    cert = Path(args.cert) if args.cert else base / "server.crt"
    key = Path(args.key) if args.key else base / "server.key"
    ca = Path(args.ca) if args.ca else base / "server_ca.crt"
    return cert, key, ca


def ssl_context(args):
    cert, key, ca = material_paths(args)
    missing = [path for path in (cert, key) if not path.is_file()]
    if args.verify_client and not ca.is_file():
        missing.append(ca)
    if missing:
        raise SystemExit("missing " + ", ".join(str(path) for path in missing))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    if args.verify_client:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=str(ca))
    else:
        ctx.verify_mode = ssl.CERT_NONE
    return ctx, cert, key, ca


def parse_args():
    parser = argparse.ArgumentParser(
        description="TLS WebSocket server for AT throughput tests.")
    parser.add_argument(
        "-m", "--mode", required=True, choices=("recv", "send", "echo", "both"),
        help="recv, send, echo, or both (independent TX, not an echo)")
    parser.add_argument("-H", "--host", default=DEFAULT_HOST)
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "-i", "--interval", type=float, default=1,
        help="stats period in seconds; 0 prints only on close and Ctrl-C")
    parser.add_argument(
        "-b", "--bytes", type=int, default=None,
        help="total bytes to send (required for send and both)")
    parser.add_argument(
        "-r", "--rate", type=float, default=0,
        help="send rate in bytes/s; 0 means unlimited")
    parser.add_argument(
        "-s", "--size", type=int, default=DEFAULT_FRAME,
        help="bytes per sent frame (default 4096)")
    parser.add_argument(
        "-pki", "--pki", choices=("00", "01"), default="00",
        help="test-kits server set: wss_00 or wss_01 (default 00)")
    parser.add_argument("-c", "--cert", help="server certificate, overrides --pki")
    parser.add_argument("-k", "--key", help="server private key, overrides --pki")
    parser.add_argument("--ca", help="CA for --verify-client, overrides --pki")
    parser.add_argument(
        "--verify-client", action="store_true",
        help="require a client certificate signed by server/server_ca.crt")
    args = parser.parse_args()
    if args.mode in ("send", "both"):
        if args.bytes is None or args.bytes < 1:
            parser.error("-b/--bytes is required and must be > 0 for mode send or both")
    elif args.bytes is not None or args.rate != 0:
        parser.error("-b/--bytes and -r/--rate apply only to mode send or both")
    if args.rate < 0:
        parser.error("-r/--rate must be >= 0")
    if args.size < 1:
        parser.error("-s/--size must be > 0")
    if args.interval < 0:
        parser.error("-i/--interval must be >= 0")
    return args


async def serve_forever(args):
    stats = Stats()
    ctx, cert, key, ca = ssl_context(args)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def request_stop():
        stop.set()
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)

    loop.add_signal_handler(signal.SIGINT, request_stop)
    loop.add_signal_handler(signal.SIGTERM, request_stop)
    report = None
    if args.interval > 0:
        report = asyncio.create_task(reporter(stats, args.interval))
    try:
        async with serve(
                build_handler(args, stats), args.host, args.port,
                ssl=ctx, compression=None, max_size=None):
            print(
                f"listening wss://{args.host}:{args.port} mode={args.mode} "
                f"bytes={args.bytes} rate={args.rate} size={args.size}",
                flush=True)
            print(f"cert={cert}", flush=True)
            print(f"key={key}", flush=True)
            if args.verify_client:
                print(f"verify-client ca={ca}", flush=True)
            await stop.wait()
    finally:
        if report is not None:
            report.cancel()
        if not stats.idle():
            print(f"final {stats.format()}", flush=True)


def main():
    args = parse_args()
    try:
        asyncio.run(serve_forever(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
