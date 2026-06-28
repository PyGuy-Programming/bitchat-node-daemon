"""
TCP Transport Layer – replaces BLE for the daemon.

Frames each BitChat packet with a 2-byte big-endian length prefix:

    [2 bytes: payload length][raw BitchatPacket bytes]

Manages a TCP server (listen) and multiple outgoing client connections.
"""

import asyncio
import logging
import struct
from typing import Callable, Dict, Optional, Set
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Maximum size for a single framed packet (10 MB)
MAX_PACKET_SIZE = 10 * 1024 * 1024


@dataclass
class PeerConnection:
    """Represents an active TCP connection to a peer daemon."""
    host: str
    port: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer_id: Optional[str] = None
    is_outgoing: bool = False

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def close(self):
        try:
            self.writer.close()
        except Exception:
            pass


class Transport:
    """
    TCP-based transport that replaces BLE.

    Provides:
      - listen on a TCP port for incoming daemon connections
      - connect to remote daemons
      - send framed BitChat packets
      - callbacks for incoming packets and connection events
    """

    def __init__(self):
        self._server: Optional[asyncio.AbstractServer] = None
        self._connections: Dict[str, PeerConnection] = {}  # address -> connection
        self._pending: Dict[str, asyncio.Future] = {}  # address -> future (for reconnect tracking)

        # Callbacks – set by the mesh layer
        self.on_packet: Optional[Callable[[PeerConnection, bytes], None]] = None
        self.on_connected: Optional[Callable[[PeerConnection], None]] = None
        self.on_disconnected: Optional[Callable[[PeerConnection], None]] = None

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------

    async def start_server(self, host: str = "0.0.0.0", port: int = 8765):
        """Start the TCP server and begin accepting incoming connections."""
        self._server = await asyncio.start_server(
            self._handle_client,
            host=host,
            port=port,
        )
        log.info("TCP server listening on %s:%d", host, port)

    async def stop_server(self):
        """Stop the TCP server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            log.info("TCP server stopped")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle an incoming daemon connection."""
        peername = writer.get_extra_info("peername")
        host, port = peername[0], peername[1]
        addr = f"{host}:{port}"

        conn = PeerConnection(
            host=host,
            port=port,
            reader=reader,
            writer=writer,
            is_outgoing=False,
        )
        self._connections[addr] = conn
        log.info("Incoming connection from %s", addr)

        try:
            if self.on_connected:
                self.on_connected(conn)
            await self._read_loop(conn)
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self._connections.pop(addr, None)
            conn.close()
            if self.on_disconnected:
                self.on_disconnected(conn)
            log.info("Connection closed: %s", addr)

    # ------------------------------------------------------------------
    # Client (outgoing connections)
    # ------------------------------------------------------------------

    async def connect(self, host: str, port: int) -> PeerConnection:
        """
        Connect to a remote daemon. Returns the PeerConnection.
        If already connected, returns the existing connection.
        """
        addr = f"{host}:{port}"
        if addr in self._connections:
            return self._connections[addr]

        log.info("Connecting to %s", addr)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=10,
            )
        except (ConnectionRefusedError, TimeoutError, OSError) as e:
            log.warning("Failed to connect to %s: %s", addr, e)
            raise

        conn = PeerConnection(
            host=host,
            port=port,
            reader=reader,
            writer=writer,
            is_outgoing=True,
        )
        self._connections[addr] = conn
        log.info("Connected to %s", addr)

        # Fire connected callback and start reading
        if self.on_connected:
            self.on_connected(conn)

        # Start reading in background
        task = asyncio.create_task(self._read_loop(conn))
        task.add_done_callback(lambda _: self._on_read_done(conn))

        return conn

    async def disconnect(self, addr: str):
        """Disconnect from a specific peer address."""
        conn = self._connections.pop(addr, None)
        if conn:
            conn.close()
            log.info("Disconnected from %s", addr)
            if self.on_disconnected:
                self.on_disconnected(conn)

    def _on_read_done(self, conn: PeerConnection):
        """Called when the read loop finishes (connection lost)."""
        if conn.address in self._connections:
            del self._connections[conn.address]
        conn.close()
        if self.on_disconnected:
            self.on_disconnected(conn)

    # ------------------------------------------------------------------
    # Send / Receive
    # ------------------------------------------------------------------

    async def send_packet(self, conn: PeerConnection, packet: bytes):
        """
        Send a raw BitChat packet over a peer connection.

        Framing: [2-byte big-endian length][packet bytes]
        """
        payload_len = len(packet)
        if payload_len > MAX_PACKET_SIZE:
            raise ValueError(f"Packet too large: {payload_len} bytes")

        header = struct.pack(">H", payload_len)
        try:
            conn.writer.write(header + packet)
            await conn.writer.drain()
        except (ConnectionError, OSError) as e:
            log.warning("Send error to %s: %s", conn.address, e)
            self._on_read_done(conn)
            raise

    async def broadcast_packet(self, packet: bytes, exclude: Optional[Set[str]] = None):
        """Send a packet to every connected peer, optionally excluding some addresses."""
        if exclude is None:
            exclude = set()
        for addr, conn in list(self._connections.items()):
            if addr in exclude:
                continue
            try:
                await self.send_packet(conn, packet)
            except (ConnectionError, OSError):
                pass  # already handled in send_packet

    async def _read_loop(self, conn: PeerConnection):
        """Read framed packets from a peer connection indefinitely."""
        while True:
            # Read 2-byte length header
            header = await conn.reader.readexactly(2)
            payload_len = struct.unpack(">H", header)[0]

            if payload_len > MAX_PACKET_SIZE:
                log.error("Invalid packet size %d from %s, disconnecting", payload_len, conn.address)
                break

            # Read the raw packet bytes
            raw = await conn.reader.readexactly(payload_len)

            if self.on_packet:
                self.on_packet(conn, raw)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def get_connections(self) -> Dict[str, PeerConnection]:
        """Return all active connections keyed by address."""
        return dict(self._connections)

    def get_connection_count(self) -> int:
        return len(self._connections)

    async def stop(self):
        """Close all connections and stop the server."""
        for addr, conn in list(self._connections.items()):
            conn.close()
        self._connections.clear()
        await self.stop_server()
        log.info("Transport stopped")
