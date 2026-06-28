"""
bitchat – BitChat protocol library.

Shared protocol & crypto modules used by both the legacy BLE client (bitchat.py)
and the new TCP node daemon (daemon/).
"""

from .encryption import EncryptionService, NoiseError, NoiseSession, NoiseHandshakeState, NoiseCipherState, NoiseRole
from .compression import compress_if_beneficial, decompress, COMPRESSION_THRESHOLD
from .fragmentation import Fragment, FragmentType, fragment_payload, MAX_FRAGMENT_SIZE
from .persistence import AppState, load_state, save_state, encrypt_password, decrypt_password, get_state_file_path
from .terminal_ux import ChatContext, ChatMode, Public, Channel, PrivateDM, format_message_display, print_help, clear_screen
from .protocol import (
    VERSION,
    COVER_TRAFFIC_PREFIX,
    FLAG_HAS_RECIPIENT, FLAG_HAS_SIGNATURE, FLAG_IS_COMPRESSED,
    MSG_FLAG_IS_RELAY, MSG_FLAG_IS_PRIVATE, MSG_FLAG_HAS_ORIGINAL_SENDER,
    MSG_FLAG_HAS_RECIPIENT_NICKNAME, MSG_FLAG_HAS_SENDER_PEER_ID,
    MSG_FLAG_HAS_MENTIONS, MSG_FLAG_HAS_CHANNEL, MSG_FLAG_IS_ENCRYPTED,
    SIGNATURE_SIZE, BROADCAST_RECIPIENT,
    DebugLevel, MessageType,
    Peer, BitchatPacket, BitchatMessage, DeliveryAck,
    DeliveryTracker, FragmentCollector,
    set_debug_level, debug_println, debug_full_println,
    parse_bitchat_packet, parse_bitchat_message_payload,
    create_bitchat_packet, create_bitchat_packet_with_signature,
    create_bitchat_packet_with_recipient_and_signature,
    create_bitchat_packet_with_recipient,
    create_bitchat_message_payload_full,
    create_encrypted_channel_message_payload,
    parse_noise_identity_announcement_binary,
    encode_noise_identity_announcement_binary,
    unpad_packet, unpad_message,
    should_fragment, should_send_ack,
)

__all__ = [
    'EncryptionService', 'NoiseError', 'NoiseSession', 'NoiseHandshakeState',
    'NoiseCipherState', 'NoiseRole',
    'compress_if_beneficial', 'decompress', 'COMPRESSION_THRESHOLD',
    'Fragment', 'FragmentType', 'fragment_payload', 'MAX_FRAGMENT_SIZE',
    'AppState', 'load_state', 'save_state', 'encrypt_password', 'decrypt_password',
    'get_state_file_path',
    'ChatContext', 'ChatMode', 'Public', 'Channel', 'PrivateDM',
    'format_message_display', 'print_help', 'clear_screen',
    'VERSION', 'COVER_TRAFFIC_PREFIX',
    'FLAG_HAS_RECIPIENT', 'FLAG_HAS_SIGNATURE', 'FLAG_IS_COMPRESSED',
    'MSG_FLAG_IS_RELAY', 'MSG_FLAG_IS_PRIVATE', 'MSG_FLAG_HAS_ORIGINAL_SENDER',
    'MSG_FLAG_HAS_RECIPIENT_NICKNAME', 'MSG_FLAG_HAS_SENDER_PEER_ID',
    'MSG_FLAG_HAS_MENTIONS', 'MSG_FLAG_HAS_CHANNEL', 'MSG_FLAG_IS_ENCRYPTED',
    'SIGNATURE_SIZE', 'BROADCAST_RECIPIENT',
    'DebugLevel', 'MessageType', 'Peer', 'BitchatPacket', 'BitchatMessage',
    'DeliveryAck', 'DeliveryTracker', 'FragmentCollector',
    'set_debug_level', 'debug_println', 'debug_full_println',
    'parse_bitchat_packet', 'parse_bitchat_message_payload',
    'create_bitchat_packet', 'create_bitchat_packet_with_signature',
    'create_bitchat_packet_with_recipient_and_signature',
    'create_bitchat_packet_with_recipient',
    'create_bitchat_message_payload_full',
    'create_encrypted_channel_message_payload',
    'parse_noise_identity_announcement_binary',
    'encode_noise_identity_announcement_binary',
    'unpad_packet', 'unpad_message',
    'should_fragment', 'should_send_ack',
]
