"""
WebSocket – real-time event stream.

WebSocket handling is integrated into the HttpServer (api.py) via the /ws endpoint.
This module is kept for documentation and imports; the actual implementation
lives in HttpServer in api.py.

Events pushed to WebSocket clients:
  - message        : new chat message
  - peer_joined    : a peer appeared on the network
  - peer_left      : a peer disconnected
  - ack            : delivery acknowledgment
  - session_established : Noise handshake completed
"""

# The WebSocket endpoint is served as part of the main HTTP server.
# See api.py's HttpServer class, specifically _handle_ws and _broadcast.
