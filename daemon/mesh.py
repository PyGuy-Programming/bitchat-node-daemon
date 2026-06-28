"""
Mesh Layer – overlay network logic built on top of Transport.

Handles:
  - Peer discovery & table management
  - Identity announcements & Noise handshakes
  - Message relay with TTL
  - Fragment reassembly
  - Duplicate detection (Bloom filter)
  - Channel management
  - Encryption sessions
"""

import asyncio
import json
import logging
import os
import random
import time
import uuid
import hashlib
from typing import Callable, Dict, List, Optional, Set, Tuple

from pybloom_live import BloomFilter

from bitchat.protocol import (
    MessageType, BitchatPacket, BitchatMessage, Peer, DeliveryAck,
    DeliveryTracker, FragmentCollector,
    parse_bitchat_packet, parse_bitchat_message_payload,
    create_bitchat_packet, create_bitchat_packet_with_recipient,
    create_bitchat_packet_with_signature,
    create_bitchat_message_payload_full,
    create_encrypted_channel_message_payload,
    parse_noise_identity_announcement_binary,
    encode_noise_identity_announcement_binary,
    unpad_message, should_send_ack,
    COVER_TRAFFIC_PREFIX, BROADCAST_RECIPIENT,
    debug_println, debug_full_println,
)
from bitchat.encryption import EncryptionService, NoiseError
from bitchat.persistence import AppState, load_state, save_state, encrypt_password, decrypt_password

from .transport import Transport, PeerConnection

log = logging.getLogger(__name__)


class MeshNode:
    """
    Core mesh node logic.

    Connects to Transport for packet I/O and provides callbacks for the
    API layer to consume messages and events.
    """

    def __init__(self, transport: Transport, config: dict):
        self.transport = transport

        # Identity & state
        self.my_peer_id = os.urandom(8).hex()
        self.nickname = config.get("nickname", "anon-daemon")
        self.app_state = load_state()
        if self.app_state.nickname:
            self.nickname = self.app_state.nickname

        # Encryption
        self.encryption_service = EncryptionService()
        self.encryption_service.on_peer_authenticated = self._on_peer_authenticated
        self.encryption_service.on_handshake_required = self._on_handshake_required

        # Peer table (peer_id -> Peer + associated transport address)
        self.peers: Dict[str, Peer] = {}
        self.peer_addresses: Dict[str, str] = {}  # peer_id -> transport address

        # Bloom filter + set for duplicate detection
        self.bloom = BloomFilter(capacity=5000, error_rate=0.01)
        self.processed_messages: Set[str] = set()

        # Fragment reassembly
        self.fragment_collector = FragmentCollector()

        # Delivery tracking
        self.delivery_tracker = DeliveryTracker()

        # Channel state
        self.channel_keys: Dict[str, bytes] = {}
        self.channel_creators: Dict[str, str] = {}
        self.password_protected_channels: Set[str] = set()
        self.channel_key_commitments: Dict[str, str] = {}
        self.discovered_channels: Set[str] = set()

        # Blocked peers (by fingerprint)
        self.blocked_peers: Set[str] = set()

        # Handshake tracking
        self.handshake_attempt_times: Dict[str, float] = {}
        self.handshake_timeout = 5.0

        # Pending private messages waiting for handshake
        self.pending_private_messages: Dict[str, List[Tuple[str, str, str]]] = {}

        # Restore state from disk
        self.blocked_peers = self.app_state.blocked_peers
        self.channel_creators = self.app_state.channel_creators
        self.password_protected_channels = set(self.app_state.password_protected_channels)
        self.channel_key_commitments = self.app_state.channel_key_commitments
        self._restore_channel_keys()

        # Callbacks for the API layer
        self.on_message: Optional[Callable[[BitchatMessage, str, bool, Optional[str]], None]] = None
        self.on_peer_joined: Optional[Callable[[str, str], None]] = None
        self.on_peer_left: Optional[Callable[[str, str], None]] = None
        self.on_delivery_ack: Optional[Callable[[DeliveryAck], None]] = None
        self.on_session_established: Optional[Callable[[str, str], None]] = None

        # Wire up transport callbacks
        self.transport.on_packet = self._on_packet_received
        self.transport.on_connected = self._on_peer_connected
        self.transport.on_disconnected = self._on_peer_disconnected

    # ------------------------------------------------------------------
    # State restoration
    # ------------------------------------------------------------------

    def _restore_channel_keys(self):
        if self.app_state.identity_key:
            for channel, encrypted_password in self.app_state.encrypted_channel_passwords.items():
                try:
                    password = decrypt_password(encrypted_password, self.app_state.identity_key)
                    key = EncryptionService.derive_channel_key(password, channel)
                    self.channel_keys[channel] = key
                except Exception:
                    pass

    async def save_state(self):
        self.app_state.nickname = self.nickname
        self.app_state.blocked_peers = self.blocked_peers
        self.app_state.channel_creators = self.channel_creators
        self.app_state.password_protected_channels = self.password_protected_channels
        self.app_state.channel_key_commitments = self.channel_key_commitments
        try:
            save_state(self.app_state)
        except Exception as e:
            log.error("Failed to save state: %s", e)

    # ------------------------------------------------------------------
    # Connection events from Transport
    # ------------------------------------------------------------------

    def _on_peer_connected(self, conn: PeerConnection):
        """Called by Transport when a new TCP connection is established."""
        log.info("Peer connected via transport: %s", conn.address)
        # We'll send identity announce after a short delay to let the connection settle

    def _on_peer_disconnected(self, conn: PeerConnection):
        """Called by Transport when a TCP connection is lost."""
        log.info("Peer disconnected: %s", conn.address)
        # Find which peer_id was using this address
        peer_id = None
        for pid, addr in list(self.peer_addresses.items()):
            if addr == conn.address:
                peer_id = pid
                break
        if peer_id:
            self._remove_peer(peer_id)

    def _on_peer_authenticated(self, peer_id: str, fingerprint: str):
        """Callback from EncryptionService when Noise handshake completes."""
        log.info("Peer authenticated: %s (fingerprint: %s...)", peer_id, fingerprint[:16])
        asyncio.create_task(self._send_pending_private_messages(peer_id))

    def _on_handshake_required(self, peer_id: str):
        """Callback from EncryptionService when handshake is needed."""
        log.info("Handshake required for %s", peer_id)

    # ------------------------------------------------------------------
    # Incoming packet handling
    # ------------------------------------------------------------------

    def _on_packet_received(self, conn: PeerConnection, raw: bytes):
        """Called by Transport when a framed packet arrives from TCP."""
        try:
            packet = parse_bitchat_packet(raw)
        except Exception as e:
            log.warning("Failed to parse packet from %s: %s", conn.address, e)
            return

        # Ignore our own messages
        if packet.sender_id_str == self.my_peer_id:
            return

        # Track which transport address this peer is at
        self.peer_addresses[packet.sender_id_str] = conn.address

        # Handle by type
        handler = {
            MessageType.ANNOUNCE: self._handle_announce,
            MessageType.MESSAGE: self._handle_message,
            MessageType.FRAGMENT_START: self._handle_fragment,
            MessageType.FRAGMENT_CONTINUE: self._handle_fragment,
            MessageType.FRAGMENT_END: self._handle_fragment,
            MessageType.KEY_EXCHANGE: self._handle_key_exchange,
            MessageType.NOISE_HANDSHAKE_INIT: self._handle_noise_handshake_init,
            MessageType.NOISE_HANDSHAKE_RESP: self._handle_noise_handshake_resp,
            MessageType.NOISE_ENCRYPTED: self._handle_noise_encrypted,
            MessageType.LEAVE: self._handle_leave,
            MessageType.CHANNEL_ANNOUNCE: self._handle_channel_announce,
            MessageType.NOISE_IDENTITY_ANNOUNCE: self._handle_noise_identity_announce,
            MessageType.DELIVERY_ACK: self._handle_delivery_ack,
        }.get(packet.msg_type)

        if handler:
            asyncio.create_task(handler(packet, conn, raw))
        else:
            debug_full_println(f"[MESH] Unhandled packet type: {packet.msg_type}")

    # ------------------------------------------------------------------
    # Send helpers
    # ------------------------------------------------------------------

    async def _raw_send(self, packet: bytes, exclude_addr: Optional[str] = None):
        """Send a raw packet to all peers, with optional exclusion."""
        exclude = {exclude_addr} if exclude_addr else None
        await self.transport.broadcast_packet(packet, exclude=exclude)

    async def _send_to_peer(self, peer_id: str, packet: bytes):
        """Send a packet to a specific peer by peer_id."""
        addr = self.peer_addresses.get(peer_id)
        if not addr:
            log.warning("Cannot send to %s: unknown address", peer_id)
            return
        conn = self.transport.get_connections().get(addr)
        if not conn:
            log.warning("Cannot send to %s: no connection for %s", peer_id, addr)
            return
        await self.transport.send_packet(conn, packet)

    def _get_conn_for_peer(self, peer_id: str) -> Optional[PeerConnection]:
        """Get the transport connection for a peer."""
        addr = self.peer_addresses.get(peer_id)
        if not addr:
            return None
        return self.transport.get_connections().get(addr)

    # ------------------------------------------------------------------
    # Identity & Handshake
    # ------------------------------------------------------------------

    async def send_identity_announce(self):
        """Send our identity announcement to all connected peers."""
        timestamp_ms = int(time.time() * 1000)
        pub_key = self.encryption_service.get_public_key()
        signing_pub_key = self.encryption_service.get_signing_public_key_bytes()

        binding_data = self.my_peer_id.encode() + pub_key + str(timestamp_ms).encode()
        signature = self.encryption_service.sign_data(binding_data)

        payload = encode_noise_identity_announcement_binary(
            self.my_peer_id, pub_key, signing_pub_key,
            self.nickname, timestamp_ms, signature
        )
        packet = create_bitchat_packet_with_signature(
            self.my_peer_id, MessageType.NOISE_IDENTITY_ANNOUNCE, payload, signature
        )
        await self._raw_send(packet)
        log.info("Sent identity announce")

    async def send_announce(self):
        """Send a basic announce (nickname)."""
        packet = create_bitchat_packet(
            self.my_peer_id, MessageType.ANNOUNCE, self.nickname.encode()
        )
        await self._raw_send(packet)

    async def _handle_announce(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        """Handle a basic announce packet."""
        nickname = packet.payload.decode('utf-8', errors='ignore').strip()
        is_new = packet.sender_id_str not in self.peers

        if packet.sender_id_str not in self.peers:
            self.peers[packet.sender_id_str] = Peer()
        self.peers[packet.sender_id_str].nickname = nickname

        if is_new:
            log.info("Peer announced: %s (%s)", nickname, packet.sender_id_str[:8])
            if self.on_peer_joined:
                self.on_peer_joined(packet.sender_id_str, nickname)

    async def _handle_noise_identity_announce(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        """Handle a Noise identity announcement."""
        sender_id = packet.sender_id_str
        if sender_id == self.my_peer_id:
            return

        announcement = None
        try:
            announcement = parse_noise_identity_announcement_binary(packet.payload)
        except Exception:
            try:
                data = json.loads(packet.payload.decode('utf-8'))
                announcement = {
                    'peerID': data.get('peerID', sender_id),
                    'nickname': data.get('nickname', 'Unknown'),
                    'publicKey': data.get('publicKey', ''),
                    'signingPublicKey': data.get('signingPublicKey', ''),
                    'timestamp': data.get('timestamp', 0),
                    'signature': data.get('signature', ''),
                }
            except Exception:
                return

        if not announcement:
            return

        peer_id = announcement['peerID']
        nickname = announcement['nickname']
        is_new = peer_id not in self.peers

        if peer_id not in self.peers:
            self.peers[peer_id] = Peer()
        self.peers[peer_id].nickname = nickname

        if is_new:
            log.info("Identity announce: %s (%s)", nickname, peer_id[:8])
            if self.on_peer_joined:
                self.on_peer_joined(peer_id, nickname)

        # Tie-breaker: lower peer ID initiates handshake
        if self.my_peer_id < peer_id:
            if not self.encryption_service.is_session_established(peer_id):
                await self._initiate_handshake(peer_id)
        else:
            debug_println(f"[MESH] Waiting for {peer_id} to initiate handshake")

    async def _initiate_handshake(self, peer_id: str):
        """Initiate a Noise handshake with a peer."""
        try:
            msg = self.encryption_service.initiate_handshake(peer_id)
            packet = create_bitchat_packet_with_recipient(
                self.my_peer_id, peer_id, MessageType.NOISE_HANDSHAKE_INIT, msg, None
            )
            packet_data = bytearray(packet)
            packet_data[2] = 3  # TTL=3 for handshake
            await self._send_to_peer(peer_id, bytes(packet_data))
            log.info("Initiated handshake with %s", peer_id[:8])
        except Exception as e:
            log.warning("Failed to initiate handshake with %s: %s", peer_id[:8], e)

    async def _handle_noise_handshake_init(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        if packet.recipient_id_str and packet.recipient_id_str != self.my_peer_id:
            return

        try:
            payload_bytes = bytes(packet.payload) if isinstance(packet.payload, bytearray) else packet.payload
            response = self.encryption_service.process_handshake_message(packet.sender_id_str, payload_bytes)

            if response:
                resp_packet = create_bitchat_packet_with_recipient(
                    self.my_peer_id, packet.sender_id_str, MessageType.NOISE_HANDSHAKE_RESP, response, None
                )
                resp_data = bytearray(resp_packet)
                resp_data[2] = 3
                await self._send_to_peer(packet.sender_id_str, bytes(resp_data))

            if self.encryption_service.is_session_established(packet.sender_id_str):
                self.handshake_attempt_times.pop(packet.sender_id_str, None)
                log.info("Handshake completed with %s", packet.sender_id_str[:8])
                await self._send_pending_private_messages(packet.sender_id_str)
        except Exception as e:
            log.warning("Handshake init failed with %s: %s", packet.sender_id_str[:8], e)
            self.encryption_service.clear_handshake_state(packet.sender_id_str)

    async def _handle_noise_handshake_resp(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        if packet.recipient_id_str and packet.recipient_id_str != self.my_peer_id:
            return

        try:
            payload_bytes = bytes(packet.payload) if isinstance(packet.payload, bytearray) else packet.payload
            response = self.encryption_service.process_handshake_message(packet.sender_id_str, payload_bytes)

            if response:
                final_packet = create_bitchat_packet_with_recipient(
                    self.my_peer_id, packet.sender_id_str, MessageType.NOISE_HANDSHAKE_INIT, response, None
                )
                final_data = bytearray(final_packet)
                final_data[2] = 3
                await self._send_to_peer(packet.sender_id_str, bytes(final_data))

            if self.encryption_service.is_session_established(packet.sender_id_str):
                self.handshake_attempt_times.pop(packet.sender_id_str, None)
                log.info("Handshake completed with %s", packet.sender_id_str[:8])
                await self._send_pending_private_messages(packet.sender_id_str)
        except Exception as e:
            log.warning("Handshake response failed with %s: %s", packet.sender_id_str[:8], e)
            self.encryption_service.clear_handshake_state(packet.sender_id_str)

    # ------------------------------------------------------------------
    # Key exchange (legacy fallback)
    # ------------------------------------------------------------------

    async def _handle_key_exchange(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        try:
            payload_bytes = bytes(packet.payload) if isinstance(packet.payload, bytearray) else packet.payload
            response = self.encryption_service.process_handshake_message(packet.sender_id_str, payload_bytes)
            if response:
                resp_packet = create_bitchat_packet(
                    self.my_peer_id, MessageType.KEY_EXCHANGE, response
                )
                await self._send_to_peer(packet.sender_id_str, resp_packet)
        except Exception as e:
            debug_println(f"[MESH] Key exchange failed: {e}")

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def _handle_message(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        """Handle a chat message."""
        # Blocked peer check
        fingerprint = self.encryption_service.get_peer_fingerprint(packet.sender_id_str)
        if fingerprint and fingerprint in self.blocked_peers:
            return

        # Check if message is for us
        is_broadcast = packet.recipient_id == BROADCAST_RECIPIENT if packet.recipient_id else True
        is_for_us = is_broadcast or (packet.recipient_id_str == self.my_peer_id)

        if not is_for_us:
            if packet.ttl > 1:
                await self._relay(raw, packet.ttl)
            return

        is_private = not is_broadcast and is_for_us
        decrypted_payload = None
        if is_private:
            try:
                decrypted_payload = self.encryption_service.decrypt_from_peer(
                    packet.sender_id_str,
                    bytes(packet.payload) if isinstance(packet.payload, bytearray) else packet.payload
                )
            except NoiseError:
                return

        try:
            if is_private and decrypted_payload:
                unpadded = unpad_message(decrypted_payload)
                message = parse_bitchat_message_payload(unpadded)
            else:
                message = parse_bitchat_message_payload(packet.payload)

            if message.id not in self.processed_messages:
                self.bloom.add(message.id)
                self.processed_messages.add(message.id)

                # Deliver via callback (to API layer)
                if self.on_message:
                    self.on_message(message, packet.sender_id_str, is_private, packet.sender_id_str)

                # ACK
                if should_send_ack(is_private, message.channel, None, self.nickname, len(self.peers)):
                    await self._send_delivery_ack(message.id, packet.sender_id_str, is_private)

                # Relay
                if packet.ttl > 1:
                    await self._relay(raw, packet.ttl)
        except Exception:
            pass

    async def _handle_noise_encrypted(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        """Handle an encrypted Noise message."""
        fingerprint = self.encryption_service.get_peer_fingerprint(packet.sender_id_str)
        if fingerprint and fingerprint in self.blocked_peers:
            return

        try:
            payload_bytes = bytes(packet.payload) if isinstance(packet.payload, bytearray) else packet.payload
            decrypted = self.encryption_service.decrypt_from_peer(packet.sender_id_str, payload_bytes)

            if len(decrypted) > 0 and decrypted[0] == 1:
                inner_packet = parse_bitchat_packet(decrypted)
                if inner_packet and inner_packet.msg_type == MessageType.MESSAGE:
                    message = parse_bitchat_message_payload(inner_packet.payload)
                    if message.id not in self.processed_messages:
                        self.bloom.add(message.id)
                        self.processed_messages.add(message.id)
                        if self.on_message:
                            self.on_message(message, packet.sender_id_str, True, packet.sender_id_str)
                        await self._send_delivery_ack(message.id, packet.sender_id_str, True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Fragments
    # ------------------------------------------------------------------

    async def _handle_fragment(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        if len(packet.payload) >= 13:
            fragment_id = packet.payload[0:8]
            index = struct.unpack('>H', packet.payload[8:10])[0]
            total = struct.unpack('>H', packet.payload[10:12])[0]
            original_type = packet.payload[12]
            fragment_data = packet.payload[13:]

            result = self.fragment_collector.add_fragment(
                fragment_id, index, total, original_type, fragment_data, packet.sender_id_str
            )
            if result:
                complete_data, _ = result
                reassembled = parse_bitchat_packet(complete_data)
                conn = self._get_conn_for_peer(packet.sender_id_str)
                if conn:
                    await self._handle_packet_internal(reassembled, conn, complete_data)

        if packet.ttl > 1:
            await self._relay(raw, packet.ttl)

    async def _handle_packet_internal(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        """Route a (possibly reassembled) packet to the right handler."""
        handler = {
            MessageType.ANNOUNCE: self._handle_announce,
            MessageType.MESSAGE: self._handle_message,
            MessageType.NOISE_IDENTITY_ANNOUNCE: self._handle_noise_identity_announce,
            MessageType.NOISE_HANDSHAKE_INIT: self._handle_noise_handshake_init,
            MessageType.NOISE_HANDSHAKE_RESP: self._handle_noise_handshake_resp,
            MessageType.NOISE_ENCRYPTED: self._handle_noise_encrypted,
            MessageType.LEAVE: self._handle_leave,
            MessageType.CHANNEL_ANNOUNCE: self._handle_channel_announce,
            MessageType.KEY_EXCHANGE: self._handle_key_exchange,
            MessageType.DELIVERY_ACK: self._handle_delivery_ack,
        }.get(packet.msg_type)
        if handler:
            await handler(packet, conn, raw)

    # ------------------------------------------------------------------
    # Relay
    # ------------------------------------------------------------------

    async def _relay(self, raw: bytes, ttl: int):
        """Relay a raw packet with decremented TTL."""
        if ttl <= 1:
            return
        await asyncio.sleep(random.uniform(0.01, 0.05))
        relay_data = bytearray(raw)
        relay_data[2] = ttl - 1
        await self._raw_send(bytes(relay_data))

    # ------------------------------------------------------------------
    # Leave / Disconnect
    # ------------------------------------------------------------------

    async def _handle_leave(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        payload = packet.payload.decode('utf-8', errors='ignore').strip()
        if payload.startswith('#'):
            # Channel leave
            pass
        else:
            # Peer disconnect
            self._remove_peer(packet.sender_id_str)

        if packet.ttl > 1:
            await self._relay(raw, packet.ttl)

    def _remove_peer(self, peer_id: str):
        peer = self.peers.pop(peer_id, None)
        self.peer_addresses.pop(peer_id, None)
        self.pending_private_messages.pop(peer_id, None)
        self.encryption_service.remove_session(peer_id)
        if peer and peer.nickname:
            log.info("Peer left: %s (%s)", peer.nickname, peer_id[:8])
            if self.on_peer_left:
                self.on_peer_left(peer_id, peer.nickname)

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------

    async def _handle_channel_announce(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        payload = packet.payload.decode('utf-8', errors='ignore')
        parts = payload.split('|')
        if len(parts) >= 3:
            channel = parts[0]
            is_protected = parts[1] == '1'
            creator_id = parts[2]
            key_commitment = parts[3] if len(parts) > 3 else ""

            if creator_id:
                self.channel_creators[channel] = creator_id

            if is_protected:
                self.password_protected_channels.add(channel)
                if key_commitment:
                    self.channel_key_commitments[channel] = key_commitment
            else:
                self.password_protected_channels.discard(channel)
                self.channel_keys.pop(channel, None)
                self.channel_key_commitments.pop(channel, None)

            self.discovered_channels.add(channel)
            await self.save_state()

    async def send_channel_announce(self, channel: str, is_protected: bool, key_commitment: Optional[str]):
        payload = f"{channel}|{'1' if is_protected else '0'}|{self.my_peer_id}|{key_commitment or ''}"
        packet = create_bitchat_packet(self.my_peer_id, MessageType.CHANNEL_ANNOUNCE, payload.encode())
        packet_data = bytearray(packet)
        packet_data[2] = 5
        await self._raw_send(bytes(packet_data))

    # ------------------------------------------------------------------
    # Delivery ACKs
    # ------------------------------------------------------------------

    async def _handle_delivery_ack(self, packet: BitchatPacket, conn: PeerConnection, raw: bytes):
        is_for_us = packet.recipient_id_str == self.my_peer_id if packet.recipient_id_str else False
        if is_for_us:
            ack_payload = packet.payload
            if packet.ttl == 3 and self.encryption_service.is_session_established(packet.sender_id_str):
                try:
                    ack_payload = self.encryption_service.decrypt_from_peer(
                        packet.sender_id_str,
                        bytes(packet.payload) if isinstance(packet.payload, bytearray) else packet.payload
                    )
                except Exception:
                    pass

            try:
                ack_data = json.loads(ack_payload)
                ack = DeliveryAck(
                    ack_data['originalMessageID'],
                    ack_data['ackID'],
                    ack_data['recipientID'],
                    ack_data['recipientNickname'],
                    ack_data['timestamp'],
                    ack_data['hopCount'],
                )
                if self.delivery_tracker.mark_delivered(ack.original_message_id):
                    if self.on_delivery_ack:
                        self.on_delivery_ack(ack)
            except Exception as e:
                debug_println(f"[ACK] Failed to parse: {e}")
        elif packet.ttl > 1:
            await self._relay(raw, packet.ttl)

    async def _send_delivery_ack(self, message_id: str, sender_id: str, is_private: bool):
        ack_id = f"{message_id}-{self.my_peer_id}"
        if not self.delivery_tracker.should_send_ack(ack_id):
            return

        ack_payload = json.dumps({
            'originalMessageID': message_id,
            'ackID': str(uuid.uuid4()),
            'recipientID': self.my_peer_id,
            'recipientNickname': self.nickname,
            'timestamp': int(time.time() * 1000),
            'hopCount': 1,
        }).encode()

        if is_private:
            try:
                ack_payload = self.encryption_service.encrypt(ack_payload, sender_id)
            except Exception:
                pass

        ack_packet = create_bitchat_packet_with_recipient(
            self.my_peer_id, sender_id, MessageType.DELIVERY_ACK, ack_payload, None
        )
        ack_data = bytearray(ack_packet)
        ack_data[2] = 3
        await self._send_to_peer(sender_id, bytes(ack_data))

    # ------------------------------------------------------------------
    # Pending private messages
    # ------------------------------------------------------------------

    async def _send_pending_private_messages(self, peer_id: str):
        if peer_id not in self.pending_private_messages:
            return
        messages = self.pending_private_messages.pop(peer_id, [])
        for content, nickname, msg_id in messages:
            try:
                await asyncio.sleep(0.3)
                await self.send_private_message(content, peer_id, nickname, msg_id)
            except Exception as e:
                log.warning("Failed to send pending message to %s: %s", peer_id[:8], e)

    # ------------------------------------------------------------------
    # Public API for sending messages (used by API layer)
    # ------------------------------------------------------------------

    async def send_public_message(self, content: str, channel: Optional[str] = None):
        """Send a public or channel message to all peers."""
        if channel and channel in self.channel_keys:
            creator_fp = self.channel_creators.get(channel, '')
            encrypted = self.encryption_service.encrypt_for_channel(
                content, channel, self.channel_keys[channel], creator_fp
            )
            payload, msg_id = create_bitchat_message_payload_full(
                self.nickname, content, channel, False, self.my_peer_id, True, encrypted
            )
        else:
            payload, msg_id = create_bitchat_message_payload_full(
                self.nickname, content, channel, False, self.my_peer_id, False, None
            )

        self.delivery_tracker.track_message(msg_id, content, False)
        packet = create_bitchat_packet(self.my_peer_id, MessageType.MESSAGE, payload)
        await self._raw_send(packet)
        return msg_id

    async def send_private_message(
        self, content: str, target_peer_id: str, target_nickname: str,
        message_id: Optional[str] = None
    ):
        """Send a private encrypted message to a specific peer."""
        if not self.encryption_service.is_session_established(target_peer_id):
            msg_id = message_id or str(uuid.uuid4())
            self.pending_private_messages.setdefault(target_peer_id, [])
            self.pending_private_messages[target_peer_id].append((content, target_nickname, msg_id))

            current_time = time.time()
            if target_peer_id in self.handshake_attempt_times:
                last = self.handshake_attempt_times[target_peer_id]
                if current_time - last < self.handshake_timeout:
                    return msg_id

            self.handshake_attempt_times[target_peer_id] = current_time
            await self._initiate_handshake(target_peer_id)
            return msg_id

        payload, msg_id = create_bitchat_message_payload_full(
            self.nickname, content, None, True, self.my_peer_id, False, None
        )
        self.delivery_tracker.track_message(msg_id, content, True)

        inner_packet = create_bitchat_packet_with_recipient(
            self.my_peer_id, target_peer_id, MessageType.MESSAGE, payload, None
        )
        inner_data = bytearray(inner_packet)
        inner_data[2] = 7
        inner_packet = bytes(inner_data)

        encrypted = self.encryption_service.encrypt_for_peer(target_peer_id, inner_packet)
        outer_packet = create_bitchat_packet_with_recipient(
            self.my_peer_id, target_peer_id, MessageType.NOISE_ENCRYPTED, encrypted, None
        )
        await self._send_to_peer(target_peer_id, outer_packet)
        return msg_id

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def on_connect_transport(self, host: str, port: int):
        """Called when Transport connects to a new peer. Send identity."""
        try:
            await asyncio.sleep(0.2)
            await self.send_identity_announce()
            await asyncio.sleep(0.3)
            await self.send_announce()
        except Exception as e:
            log.warning("Failed to send initial announce to %s:%d: %s", host, port, e)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        return {
            'peer_id': self.my_peer_id,
            'nickname': self.nickname,
            'peers_count': len(self.peers),
            'connections_count': self.transport.get_connection_count(),
            'session_count': self.encryption_service.get_session_count(),
            'peers': [
                {
                    'peer_id': pid,
                    'nickname': p.nickname,
                    'address': self.peer_addresses.get(pid, ''),
                }
                for pid, p in self.peers.items()
            ],
            'channels': list(self.discovered_channels),
        }

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def send_leave(self):
        """Send a leave notification to all peers."""
        packet = create_bitchat_packet(
            self.my_peer_id, MessageType.LEAVE, self.nickname.encode()
        )
        await self._raw_send(packet)
