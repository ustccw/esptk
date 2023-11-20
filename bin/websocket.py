#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import asyncio
import websockets

# Dictionary to store active WebSocket clients
connected_clients = set()

async def echo_server(websocket, path):
    # Add the client to the connected clients set
    connected_clients.add(websocket)
    try:
        async for message in websocket:
            # Echo the received message back to the client
            await websocket.send(message)
    except websockets.exceptions.ConnectionClosedError:
        pass
    finally:
        # Remove the client from the connected clients set
        connected_clients.remove(websocket)

# Start the WebSocket server
async def main():
    server = await websockets.serve(echo_server, "localhost", 8765)
    print("WebSocket server started.")
    await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())
