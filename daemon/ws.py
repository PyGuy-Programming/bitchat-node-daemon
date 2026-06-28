"""
WebSocket endpoint – pushes real-time events to connected clients.

Events:
  - message       : new chat message
  - peer_joined   : a peer appeared on the network
  - peer_left     : a peer disconnected
  - ack           : delivery acknowledgment
  - session       : secure session established
"""

import asyncio
import json
import logging
from typing import Optional, Set

import aiohttp
from aiohttp import web

from bitchat.protocol import BitchatMessage, DeliveryAck

from .mesh import MeshNode

log = logging.getLogger(__name__)


class WsServer:
    """WebSocket server for real-time events."""

    def __init__(self, mesh: MeshNode, config: dict):
        self.mesh = mesh
        self._host = config.get("api", {}).get("host", "127.0.0.1")
        self._port = config.get("api", {}).get("ws_port", 8080)
        self._app = web.Application()
        self._runner: Optional[web.AppRunner] = None
        self._clients: Set[web.WebSocketResponse] = set()
        self._setup()

    def _setup(self):
        self._app.router.add_get("/ws", self._handle_ws)

        # Wire mesh callbacks → broadcast to WebSocket clients
        self.mesh.on_message = self._on_message
        self.mesh.on_peer_joined = self._on_peer_joined
        self.mesh.on_peer_left = self._on_peer_left
        self.mesh.on_delivery_ack = self._on_ack

    async def start(self):
        if self._runner is None:
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, self._host, self._port)
            await site.start()
            log.info("WebSocket server listening on %s:%d/ws", self._host, self._port)

    async def stop(self):
        for ws in self._clients:
            if not ws.closed:
                await ws.close()
        self._clients.clear()
        if self._runner:
            await self._runner.cleanup()
            log.info("WebSocket server stopped")

    async def _handle_ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("WebSocket client connected (%d total)", len(self._clients))

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    # Client can send commands via WS too (optional)
                    try:
                        data = json.loads(msg.data)
                        await self._handle_ws_command(ws, data)
                    except json.JSONDecodeError:
                        pass
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.warning("WebSocket error: %s", ws.exception())
        except asyncio.CancelledError:
            pass
        finally:
            self._clients.discard(ws)
            log.info("WebSocket client disconnected (%d remaining)", len(self._clients))

        return ws

    async def _handle_ws_command(self, ws, data: dict):
        """Handle a command received over WebSocket."""
        action = data.get("action")
        if action == "ping":
            await ws.send_json({"event": "pong"})

    async def _broadcast(self, event: dict):
        """Send an event to all connected WebSocket clients."""
        payload = json.dumps(event)
        for ws in list(self._clients):
            try:
                if not ws.closed:
                    await ws.send_str(payload)
            except (ConnectionError, asyncio.CancelledError):
                self._clients.discard(ws)

    # ------------------------------------------------------------------
    # Mesh callbacks
    # ------------------------------------------------------------------

    def _on_message(self, message: BitchatMessage, sender_id: str, is_private: bool, sender_str: str):
        """Called by MeshNode when a message is received."""
        asyncio.create_task(self._broadcast({
            "event": "message",
            "data": {
                "id": message.id,
                "content": message.content,
                "channel": message.channel,
                "is_encrypted": message.is_encrypted,
                "is_private": is_private,
                "sender_id": sender_id,
                "sender_nickname": self.mesh.peers.get(sender_id, type('', (), {'nickname': sender_id})()).nickname if self.mesh.peers.get(sender_id) else sender_id[:8],
            }
        }))

    def _on_peer_joined(self, peer_id: str, nickname: str):
        """Called by MeshNode when a new peer appears."""
        asyncio.create_task(self._broadcast({
            "event": "peer_joined",
            "data": {"peer_id": peer_id, "nickname": nickname},
        }))

    def _on_peer_left(self, peer_id: str, nickname: str):
        """Called by MeshNode when a peer disconnects."""
        asyncio.create_task(self._broadcast({
            "event": "peer_left",
            "data": {"peer_id": peer_id, "nickname": nickname},
        }))

    def _on_ack(self, ack: DeliveryAck):
        """Called by MeshNode when a delivery ACK arrives."""
        asyncio.create_task(self._broadcast({
            "event": "ack",
            "data": {
                "original_message_id": ack.original_message_id,
                "recipient_id": ack.recipient_id,
                "recipient_nickname": ack.recipient_nickname,
            }
        }))



