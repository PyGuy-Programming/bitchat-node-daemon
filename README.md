# BitChat Node Daemon

A **decentralized, encrypted, peer-to-peer** mesh messaging daemon.

This is a **TCP-based port** of the original [BitChat BLE client](https://github.com/ShilohEye/bitchat-terminal). It replaces Bluetooth Low Energy with TCP networking while keeping the same packet protocol, Noise encryption, and mesh relay logic – laying the foundation for a scalable mesh network.

## Features

- **P2P Mesh** – nodes discover each other and relay messages with TTL-based flooding
- **P2P Mesh** – nodes discover each other and relay messages with TTL-based flooding
- **End-to-End Encryption** – Noise Protocol (XX handshake + ChaCha20-Poly1305) for private messages
- **Message Relaying** – every node acts as a relay, spreading encrypted messages across the network
- **Fragment Reassembly** – large messages are fragmented and reassembled automatically
- **REST API** – operational control only (`/status`, `/peers`, `/connect`, …) – **no message content exposed**
- **WebSocket Stream** – real-time operational events (`peer_joined`, `peer_left`, `ack`) – **no message content**
- **mDNS Discovery** – automatically finds peers on the local network
- **Persistence** – state and peer data survive restarts
- **systemd / Docker** – install as a background service or run in a container

## Quick Start

### Install (one-liner)

```bash
curl -sSfL https://raw.githubusercontent.com/PyGuy-Programming/bitchat-node-daemon/main/install.sh | sh
```

This clones the repo to `/opt/bitchat-node`, installs dependencies, creates a `bitchat` system user, sets up a systemd service, and starts it.

### Uninstall

```bash
curl -sSfL https://raw.githubusercontent.com/PyGuy-Programming/bitchat-node-daemon/main/install.sh | sh -s uninstall
```

Stoppt und entfernt den Service, löscht Config, Daten und den System-User.

### Run manually

```bash
# Install dependencies
pip install .

# Start the daemon
python -m daemon --port 8765 --nickname my-node

# With debug logging
python -m daemon --port 8765 --nickname my-node --debug
```

### CLI options

| Flag | Description |
|---|---|
| `--port`, `-p` | TCP port for peer connections |
| `--api-port` | REST + WebSocket API port |
| `--nickname`, `-n` | Node display name |
| `--config`, `-c` | Path to config YAML |
| `--debug`, `-d` | Enable debug logging |

## API

The daemon exposes a REST API on `http://127.0.0.1:8080` by default.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/status` | Node status, peers, sessions |
| `GET` | `/peers` | List connected peers |
| `POST` | `/connect` | Connect to a peer `{"host": "...", "port": 8765}` |
| `POST` | `/disconnect` | Disconnect from a peer `{"address": "host:port"}` |
| `PUT` | `/name` | Change nickname `{"nickname": "alice"}` |

> **Note:** The daemon is a headless relay and intentionally exposes **no message content** via the API. Endpoints for sending messages, listing channels, or joining/leaving channels are omitted.

### WebSocket

Connect to `ws://127.0.0.1:8080/ws` to receive real-time operational events:

```json
{"event": "peer_joined", "data": {"peer_id": "...", "nickname": "bob"}}
{"event": "peer_left", "data": {"peer_id": "...", "nickname": "bob"}}
{"event": "ack", "data": {"original_message_id": "...", "recipient_id": "...", "recipient_nickname": "bob"}}
```

> **No message content** is broadcast via WebSocket. Only peer lifecycle and delivery acknowledgments are forwarded.

## Configuration

The daemon loads `config.yaml` by default. Environment variables override file settings:

| Variable | Description |
|---|---|---|
| `BITCHAT_PORT` | TCP peer port |
| `BITCHAT_API_PORT` | REST/WS API port |
| `BITCHAT_API_HOST` | API bind address (default `127.0.0.1`, use `0.0.0.0` für Docker) |
| `BITCHAT_NICKNAME` | Node nickname |
| `BITCHAT_BOOTSTRAP` | Comma-separated bootstrap peers (`host:port,host:port`) |

See [`config.yaml`](./config.yaml) for the full default configuration.

## Docker

### Quick start

```bash
docker compose up -d
```

Zieht das Image von Docker Hub und startet den Daemon als Headless-Relay. Persistente Daten landen in einem named volume. Ports: `8765` (TCP peers), `8080` (REST + WebSocket – nur operational, keine Nachrichteninhalte).

Lokal bauen (z.B. nach Code-Änderungen):

```bash
docker compose build
docker compose up -d
```

### Custom config

```yaml
# docker-compose.yml
services:
  bitchat-node:
    # ...
    volumes:
      - ./my-config.yaml:/etc/bitchat-node/config.yaml:ro
```

Oder per Environment-Variablen:

```yaml
    environment:
      - BITCHAT_NICKNAME=my-node
      - BITCHAT_BOOTSTRAP=peer1.example.com:8765,peer2.example.com:8765
```

## Project Structure

```
bitchat-node-daemon/
├── bitchat/              # Protocol library (shared with BLE client)
│   ├── protocol.py       #   Packet structures, parsing, creation
│   ├── encryption.py     #   Noise Protocol (XX pattern)
│   ├── compression.py    #   LZ4 compression
│   ├── fragmentation.py  #   Message fragmenter
│   └── persistence.py    #   State file I/O
├── daemon/               # Node daemon
│   ├── transport.py      #   TCP server + client (replaces BLE)
│   ├── mesh.py           #   Mesh relay, handshake, peer table
│   ├── discovery.py      #   mDNS + bootstrap peer discovery
│   ├── api.py            #   REST API
│   ├── ws.py             #   WebSocket event stream
│   ├── config.py         #   Config loader (YAML + env)
│   └── __main__.py       #   Entry point
├── bitchat.py            # Original BLE client (still works)
├── Dockerfile            # Docker image
├── docker-compose.yml    # Docker Compose setup
├── install.sh            # One-curl installer / uninstaller
├── config.yaml           # Default configuration
└── pyproject.toml        # Python project metadata
```

## Architecture

### Transport Layer (`transport.py`)
TCP replaces BLE as the transport. Each packet is framed with a 2-byte big-endian length prefix:

```
[2 bytes: payload length][raw BitchatPacket bytes]
```

### Mesh Layer (`mesh.py`)
The mesh layer is a direct port of the BLE client's message handling, minus the display code:

- **Peer Table**: tracks `peer_id → address / connection / encryption session`
- **Relay**: same TTL-based flooding as the original – TTL is decremented and the packet is re-broadcast
- **Duplication Detection**: Bloom filter + seen-set (identical to BLE)
- **Fragment Reassembly**: `FragmentCollector` from the original code
- **Handshake**: Noise Protocol XX – tie-breaker based on peer ID ordering

### Future Mesh Routing
Currently the mesh uses **flooding** (every node re-broadcasts within TTL range). The architecture is designed to swap in smarter routing later:

- **Distance Vector** – each node learns the topology and forwards only toward relevant peers
- **Kademlia DHT** – scalable routing for thousands of nodes

The `_relay()` method in `mesh.py` is the single point to replace.

## Dependencies

| Package | Purpose |
|---|---|
| `cryptography` | X25519, ChaCha20-Poly1305, HKDF |
| `lz4` | Optional message compression |
| `pybloom_live` | Bloom filter for duplicate detection |
| `aiohttp` | REST API + WebSocket server |
| `pyyaml` | Configuration file parsing |
| `zeroconf` (optional) | mDNS peer discovery on LAN |

## License

MIT
