#!/usr/bin/env python3
"""
BitChat Node Daemon – main entry point.

Usage:
    python -m daemon [--config config.yaml]
    python -m daemon --help
"""

import argparse
import asyncio
import logging
import os
import signal
import sys

from .config import load_config
from .transport import Transport
from .mesh import MeshNode
from .discovery import Discovery
from .api import RestApi
from .ws import WsServer

log = logging.getLogger("daemon")


def setup_logging(config: dict):
    level = getattr(logging, config.get("logging", {}).get("level", "INFO").upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]

    log_file = config.get("logging", {}).get("file")
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(level=level, format=fmt, handlers=handlers)


class Daemon:
    """Orchestrates all daemon components."""

    def __init__(self, config: dict):
        self.config = config
        self.transport = Transport()
        self.mesh = MeshNode(self.transport, config)
        self.discovery = Discovery(config)
        self.api = RestApi(self.mesh, config)
        self.ws = WsServer(self.mesh, config)
        self._running = False

    async def start(self):
        """Start all daemon components."""
        self._running = True

        node_config = self.config["node"]
        port = node_config["port"]
        listen = node_config["listen"]

        # 1. Start TCP transport
        await self.transport.start_server(listen, port)
        log.info("TCP transport listening on %s:%d", listen, port)

        # 2. Wire discovery → transport
        self.discovery.on_peer_found = self._on_peer_discovered

        # 3. Start discovery
        await self.discovery.start()

        # 4. Send initial identity on new transport connections
        original_on_connected = self.transport.on_connected
        async def on_connected_wrapper(conn):
            if original_on_connected:
                original_on_connected(conn)
            await self.mesh.on_connect_transport(conn.host, conn.port)
        self.transport.on_connected = on_connected_wrapper

        # 5. Start API
        await self.api.start()

        # 6. Start WebSocket (shares port with API)
        await self.ws.start()

        log.info("Daemon started (peer_id=%s, nickname=%s)",
                 self.mesh.my_peer_id[:8], self.mesh.nickname)

    async def stop(self):
        """Gracefully stop all components."""
        log.info("Shutting down...")

        await self.mesh.send_leave()

        await self.ws.stop()
        await self.api.stop()
        await self.discovery.stop()
        await self.mesh.save_state()
        await self.transport.stop()

        self._running = False
        log.info("Daemon stopped")

    def _on_peer_discovered(self, host: str, port: int):
        """Called by Discovery when a new peer is found."""
        asyncio.create_task(self._connect_to_peer(host, port))

    async def _connect_to_peer(self, host: str, port: int):
        """Connect to a discovered peer and send identity."""
        try:
            conn = await self.transport.connect(host, port)
            log.info("Connected to discovered peer %s:%d", host, port)
        except (ConnectionRefusedError, OSError, TimeoutError) as e:
            log.debug("Could not connect to %s:%d: %s", host, port, e)

    async def run_forever(self):
        """Run until a shutdown signal is received."""
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self._shutdown(stop_event)))
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        await self.start()
        await stop_event.wait()

    async def _shutdown(self, stop_event: asyncio.Event):
        await self.stop()
        stop_event.set()


def main():
    parser = argparse.ArgumentParser(description="BitChat Node Daemon")
    parser.add_argument("--config", "-c", default=None, help="Path to config YAML file")
    parser.add_argument("--port", "-p", type=int, default=None, help="TCP port to listen on")
    parser.add_argument("--api-port", type=int, default=None, help="API port")
    parser.add_argument("--nickname", "-n", default=None, help="Node nickname")
    parser.add_argument("--debug", "-d", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.port:
        config["node"]["port"] = args.port
    if args.api_port:
        config["api"]["rest_port"] = args.api_port
        config["api"]["ws_port"] = args.api_port
    if args.nickname:
        config["node"]["nickname"] = args.nickname
    if args.debug:
        config["logging"]["level"] = "DEBUG"

    setup_logging(config)
    daemon = Daemon(config)

    try:
        asyncio.run(daemon.run_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
