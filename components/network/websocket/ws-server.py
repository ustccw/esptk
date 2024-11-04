#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import asyncio
from websockets.server import serve

host = "192.168.200.249"
port = 8765

async def echo(websocket):
    async for message in websocket:
        await websocket.send(message)

async def main():
    async with serve(echo, host, port):
        print(f"Server started at ws://{host}:{port}")
        await asyncio.get_running_loop().create_future()  # run forever

asyncio.run(main())
