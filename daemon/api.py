"""
HTTP server for the bitchat node daemon.

Serves both REST API endpoints and WebSocket on the same port.
"""

import asyncio
import json
import logging
from typing import Optional, Set

from aiohttp import web

from bitchat.protocol import DeliveryAck

from .mesh import MeshNode

log = logging.getLogger(__name__)


class HttpServer:
    """
    Single aiohttp server that handles:
      - REST API endpoints (status, peers, message, etc.)
      - WebSocket endpoint for real-time events (/ws)
    """

    def __init__(self, mesh: MeshNode, config: dict):
        self.mesh = mesh
        self.config = config
        self._host = config.get("api", {}).get("host", "127.0.0.1")
        self._port = config.get("api", {}).get("rest_port", 8080)
        self._app = web.Application()
        self._runner: Optional[web.AppRunner] = None
        self._ws_clients: Set[web.WebSocketResponse] = set()

        # Wire mesh callbacks -> WebSocket broadcast
        # NOTE: on_message is intentionally NOT wired – the daemon is a headless relay
        # and must not expose message contents via API.
        self.mesh.on_peer_joined = self._on_peer_joined
        self.mesh.on_peer_left = self._on_peer_left
        self.mesh.on_delivery_ack = self._on_ack
        if hasattr(self.mesh, 'on_session_established'):
            self.mesh.on_session_established = self._on_session

        self._setup_routes()

    def _setup_routes(self):
        # REST
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_get("/peers", self._handle_peers)
        self._app.router.add_post("/connect", self._handle_connect)
        self._app.router.add_post("/disconnect", self._handle_disconnect)
        # NOTE: /message, /channels are intentionally omitted – headless relay
        self._app.router.add_put("/name", self._handle_set_name)

        # WebSocket
        self._app.router.add_get("/ws", self._handle_ws)

    async def start(self):
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        log.info("HTTP server listening on %s:%d (REST + WebSocket)", self._host, self._port)

    async def stop(self):
        for ws in self._ws_clients:
            if not ws.closed:
                await ws.close()
        self._ws_clients.clear()
        if self._runner:
            await self._runner.cleanup()
            log.info("HTTP server stopped")

    # ------------------------------------------------------------------
    # REST handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request):
        return web.json_response({"status": "ok"})

    async def _handle_status(self, request):
        return web.json_response(self.mesh.get_status())

    async def _handle_peers(self, request):
        peers = [
            {
                "peer_id": pid,
                "nickname": p.nickname,
                "address": self.mesh.peer_addresses.get(pid, ""),
                "session_active": self.mesh.encryption_service.is_session_established(pid),
            }
            for pid, p in self.mesh.peers.items()
        ]
        return web.json_response(peers)

    async def _handle_connect(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        host = data.get("host")
        port = data.get("port")
        if not host or not port:
            return web.json_response({"error": "host and port required"}, status=400)

        try:
            await self.mesh.transport.connect(host, int(port))
            # on_connect_transport is fired automatically by the transport's on_connected callback
            return web.json_response({"status": "connecting", "host": host, "port": port})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_disconnect(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        addr = data.get("address")
        if not addr:
            return web.json_response({"error": "address required"}, status=400)

        await self.mesh.transport.disconnect(addr)
        return web.json_response({"status": "disconnected"})

    async def _handle_set_name(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        nickname = data.get("nickname", "").strip()
        if not nickname or len(nickname) > 20:
            return web.json_response({"error": "invalid nickname"}, status=400)

        self.mesh.nickname = nickname
        await self.mesh.save_state()
        await self.mesh.send_announce()
        return web.json_response({"status": "ok", "nickname": nickname})

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def _handle_ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        log.info("WebSocket client connected (%d total)", len(self._ws_clients))

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("action") == "ping":
                            await ws.send_json({"event": "pong"})
                    except json.JSONDecodeError:
                        pass
                elif msg.type == web.WSMsgType.ERROR:
                    log.warning("WebSocket error: %s", ws.exception())
        except asyncio.CancelledError:
            pass
        finally:
            self._ws_clients.discard(ws)
            log.info("WebSocket client disconnected (%d remaining)", len(self._ws_clients))

        return ws

    async def _broadcast(self, event: dict):
        """Send an event to all connected WebSocket clients."""
        payload = json.dumps(event)
        for ws in list(self._ws_clients):
            try:
                if not ws.closed:
                    await ws.send_str(payload)
            except (ConnectionError, asyncio.CancelledError):
                self._ws_clients.discard(ws)

    # ------------------------------------------------------------------
    # Mesh callbacks -> WS broadcast
    # ------------------------------------------------------------------

    def _on_peer_joined(self, peer_id: str, nickname: str):
        asyncio.create_task(self._broadcast({
            "event": "peer_joined",
            "data": {"peer_id": peer_id, "nickname": nickname},
        }))

    def _on_peer_left(self, peer_id: str, nickname: str):
        asyncio.create_task(self._broadcast({
            "event": "peer_left",
            "data": {"peer_id": peer_id, "nickname": nickname},
        }))

    def _on_ack(self, ack: DeliveryAck):
        asyncio.create_task(self._broadcast({
            "event": "ack",
            "data": {
                "original_message_id": ack.original_message_id,
                "recipient_id": ack.recipient_id,
                "recipient_nickname": ack.recipient_nickname,
            }
        }))

    def _on_session(self, peer_id: str, nickname: str):
        asyncio.create_task(self._broadcast({
            "event": "session_established",
            "data": {"peer_id": peer_id, "nickname": nickname},
        }))
