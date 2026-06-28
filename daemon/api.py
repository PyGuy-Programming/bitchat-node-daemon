"""
HTTP server for the bitchat node daemon.

Serves both REST API endpoints and WebSocket on the same port.
"""

import asyncio
import json
import logging
from typing import Optional, Set

from aiohttp import web

from bitchat.protocol import BitchatMessage, DeliveryAck

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
        self.mesh.on_message = self._on_message
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
        self._app.router.add_post("/message", self._handle_send_message)
        self._app.router.add_get("/channels", self._handle_channels)
        self._app.router.add_post("/channels/join", self._handle_join_channel)
        self._app.router.add_post("/channels/leave", self._handle_leave_channel)
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

    async def _handle_send_message(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        content = data.get("content", "").strip()
        if not content:
            return web.json_response({"error": "content required"}, status=400)

        target = data.get("target")
        is_private = data.get("private", False)
        channel = data.get("channel")

        try:
            if is_private and target:
                peer = self.mesh.peers.get(target)
                nickname = peer.nickname if peer else target
                msg_id = await self.mesh.send_private_message(content, target, nickname)
            elif channel:
                msg_id = await self.mesh.send_public_message(content, channel)
            else:
                msg_id = await self.mesh.send_public_message(content)

            return web.json_response({"status": "sent", "message_id": msg_id})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_channels(self, request):
        channels = []
        for ch in sorted(self.mesh.discovered_channels):
            channels.append({
                "name": ch,
                "protected": ch in self.mesh.password_protected_channels,
                "has_key": ch in self.mesh.channel_keys,
                "creator": self.mesh.channel_creators.get(ch, ""),
            })
        return web.json_response(channels)

    async def _handle_join_channel(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        channel = data.get("channel", "").strip()
        password = data.get("password")

        if not channel:
            return web.json_response({"error": "channel required"}, status=400)

        if channel in self.mesh.password_protected_channels:
            if not password:
                return web.json_response({"error": "password required"}, status=400)
            if channel in self.mesh.channel_key_commitments:
                from bitchat.encryption import EncryptionService
                import hashlib
                key = EncryptionService.derive_channel_key(password, channel)
                expected = self.mesh.channel_key_commitments[channel]
                if hashlib.sha256(key).hexdigest() != expected:
                    return web.json_response({"error": "wrong password"}, status=403)
                self.mesh.channel_keys[channel] = key

        self.mesh.discovered_channels.add(channel)
        return web.json_response({"status": "joined", "channel": channel})

    async def _handle_leave_channel(self, request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        channel = data.get("channel", "").strip()
        if not channel:
            return web.json_response({"error": "channel required"}, status=400)

        self.mesh.channel_keys.pop(channel, None)
        self.mesh.password_protected_channels.discard(channel)
        self.mesh.channel_creators.pop(channel, None)
        self.mesh.channel_key_commitments.pop(channel, None)
        self.mesh.discovered_channels.discard(channel)
        return web.json_response({"status": "left", "channel": channel})

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

    def _on_message(self, message: BitchatMessage, sender_id: str, is_private: bool, sender_str: str):
        peer = self.mesh.peers.get(sender_id)
        nickname = peer.nickname if peer else sender_id[:8]
        asyncio.create_task(self._broadcast({
            "event": "message",
            "data": {
                "id": message.id,
                "content": message.content,
                "channel": message.channel,
                "is_encrypted": message.is_encrypted,
                "is_private": is_private,
                "sender_id": sender_id,
                "sender_nickname": nickname,
            }
        }))

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
