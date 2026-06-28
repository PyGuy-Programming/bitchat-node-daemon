"""
Peer Discovery – finds other bitchat nodes on the network.

Supports:
  - mDNS/Zeroconf for LAN discovery
  - Bootstrap node list (static peers)
  - Manual peer addition via API
"""

import asyncio
import logging
from typing import Callable, List, Optional, Set

log = logging.getLogger(__name__)


class Discovery:
    """
    Discovers other bitchat daemon peers.

    Calls `on_peer_found(host, port)` for each discovered peer so the
    Transport layer can initiate a connection.
    """

    def __init__(self, config: dict):
        self.config = config
        self._mdns_enabled = config.get("discovery", {}).get("mdns", True)
        self._mdns_service = config.get("discovery", {}).get("mdns_service", "_bitchat._tcp")
        self._bootstrap_nodes: List[str] = config.get("mesh", {}).get("bootstrap_nodes", [])

        self._known: Set[str] = set()  # "host:port" already discovered
        self._zeroconf = None
        self._running = False

        # Callback – set by the daemon main
        self.on_peer_found: Optional[Callable[[str, int], None]] = None

    async def start(self):
        """Start the discovery service."""
        self._running = True

        # Connect to bootstrap nodes
        for entry in self._bootstrap_nodes:
            self._connect_bootstrap(entry)

        # Start mDNS discovery
        if self._mdns_enabled:
            try:
                await self._start_mdns()
            except ImportError:
                log.warning("zeroconf not installed – mDNS discovery disabled")
            except Exception as e:
                log.warning("mDNS discovery failed: %s", e)

        log.info("Discovery started (mdns=%s, bootstrap=%d)",
                 self._mdns_enabled, len(self._bootstrap_nodes))

    async def stop(self):
        """Stop the discovery service."""
        self._running = False
        if self._zeroconf:
            try:
                self._zeroconf.close()
            except Exception:
                pass
        log.info("Discovery stopped")

    def _connect_bootstrap(self, entry: str):
        """Parse a 'host:port' bootstrap entry and notify."""
        try:
            host, port_str = entry.rsplit(":", 1)
            port = int(port_str)
            addr = f"{host}:{port}"
            if addr not in self._known:
                self._known.add(addr)
                if self.on_peer_found:
                    self.on_peer_found(host, port)
                log.info("Discovered bootstrap peer: %s", addr)
        except Exception as e:
            log.warning("Invalid bootstrap entry '%s': %s", entry, e)

    async def _start_mdns(self):
        """Start mDNS listener using zeroconf."""
        from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange

        self._zeroconf = Zeroconf()

        def on_change(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Added:
                info = zeroconf.get_service_info(service_type, name)
                if info:
                    host = info.server.strip(".")
                    port = info.port
                    addr = f"{host}:{port}"
                    if addr not in self._known:
                        self._known.add(addr)
                        if self.on_peer_found:
                            self.on_peer_found(host, port)
                        log.info("Discovered mDNS peer: %s", addr)

        ServiceBrowser(self._zeroconf, self._mdns_service, handlers=[on_change])

        # Keep alive while running
        while self._running:
            await asyncio.sleep(1)

    def add_manual(self, host: str, port: int):
        """Manually register a peer to connect to."""
        addr = f"{host}:{port}"
        if addr not in self._known:
            self._known.add(addr)
            if self.on_peer_found:
                self.on_peer_found(host, port)
            log.info("Manual peer added: %s", addr)
