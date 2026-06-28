"""
Configuration loader for the bitchat node daemon.

Loads from YAML file with sane defaults.
"""

import os
import logging
from typing import Dict, Any

log = logging.getLogger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
    "node": {
        "port": 8765,
        "listen": "0.0.0.0",
        "nickname": "bitchat-node",
        "data_dir": os.path.expanduser("~/.bitchatxxk"),
    },
    "mesh": {
        "bootstrap_nodes": [],
        "max_peers": 50,
        "default_ttl": 7,
    },
    "discovery": {
        "mdns": True,
        "mdns_service": "_bitchat._tcp",
    },
    "api": {
        "rest_port": 8080,
        "ws_port": 8080,
        "host": "127.0.0.1",
    },
    "logging": {
        "level": "INFO",
        "file": None,
    },
}


def load_config(path: str = None) -> Dict[str, Any]:
    """Load configuration from a YAML file, merging with defaults."""
    config = DEFAULT_CONFIG.copy()

    if path and os.path.exists(path):
        try:
            import yaml
            with open(path, "r") as f:
                user_config = yaml.safe_load(f) or {}
            _deep_merge(config, user_config)
            log.info("Loaded config from %s", path)
        except Exception as e:
            log.warning("Failed to load config %s: %s", path, e)

    # Override from environment variables
    env_port = os.environ.get("BITCHAT_PORT")
    if env_port:
        config["node"]["port"] = int(env_port)

    env_api_port = os.environ.get("BITCHAT_API_PORT")
    if env_api_port:
        config["api"]["rest_port"] = int(env_api_port)
        config["api"]["ws_port"] = int(env_api_port)

    env_nick = os.environ.get("BITCHAT_NICKNAME")
    if env_nick:
        config["node"]["nickname"] = env_nick

    env_api_host = os.environ.get("BITCHAT_API_HOST")
    if env_api_host:
        config["api"]["host"] = env_api_host

    env_bootstrap = os.environ.get("BITCHAT_BOOTSTRAP")
    if env_bootstrap:
        config["mesh"]["bootstrap_nodes"] = [s.strip() for s in env_bootstrap.split(",")]

    return config


def _deep_merge(base: dict, overlay: dict):
    """Recursively merge overlay into base."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
