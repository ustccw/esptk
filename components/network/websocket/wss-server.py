#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import asyncio
import ssl
import websockets

host = '192.168.200.249'
port = 8765
server_ca = '/your_path/server_ca.crt'
server_cert = '/your_path/server.crt'
server_key = '/your_path/server.key'

async def echo(websocket, path):
    async for message in websocket:
        await websocket.send(message)

ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ssl_context.load_cert_chain(certfile=server_cert, keyfile=server_key)
ssl_context.verify_mode = ssl.CERT_REQUIRED
ssl_context.load_verify_locations(server_ca)

start_server = websockets.serve(echo, host, port, ssl=ssl_context)

asyncio.get_event_loop().run_until_complete(start_server)
print(f"Server started at wss://{host}:{port}")

asyncio.get_event_loop().run_forever()
