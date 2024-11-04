# Usage
1. Install the library: [websockets](https://pypi.org/project/websockets/)
```
pip install websockets
```

2. Configurate
- `ws-server.py` (base on tcp), update the following parameters according to your case.
```
host = "192.168.200.249"
port = 8765
```

- `wss-server.py` (base on ssl), update the following parameters according to your case.
```
host = '192.168.200.249'
port = 8765
server_ca = '/your_path/server_ca.crt'
server_cert = '/your_path/server.crt'
server_key = '/your_path/server.key'
```

3. Start the service
- `ws-server.py` (base on tcp)
```
python ws-server.py
```

- `wss-server.py` (base on ssl)
```
python wss-server.py
```
