#!/bin/sh
#
# BitChat Node Daemon – Installer / Uninstaller
#
# Usage:
#   curl -sSfL https://raw.githubusercontent.com/PyGuy-Programming/bitchat-node-daemon/main/install.sh | sh
#   curl -sSfL https://raw.githubusercontent.com/PyGuy-Programming/bitchat-node-daemon/main/install.sh | sh -s uninstall
#

set -e

REPO_URL="https://github.com/PyGuy-Programming/bitchat-node-daemon.git"
INSTALL_DIR="${INSTALL_DIR:-/opt/bitchat-node}"
SERVICE_USER="${SERVICE_USER:-bitchat}"
PYTHON="${PYTHON:-python3}"
CONFIG_PATH="${CONFIG_PATH:-/etc/bitchat-node/config.yaml}"
DATA_DIR="${DATA_DIR:-/var/lib/bitchat-node}"
SERVICE_FILE="/etc/systemd/system/bitchat-node.service"

# ------------------------------------------------------------------
# Color helpers
# ------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { printf "${CYAN}%s${NC}\n" "$*"; }
ok()    { printf "${GREEN}✓ %s${NC}\n" "$*"; }
warn()  { printf "${YELLOW}⚠ %s${NC}\n" "$*"; }
err()   { printf "${RED}✗ %s${NC}\n" "$*"; exit 1; }

# ------------------------------------------------------------------
# Usage
# ------------------------------------------------------------------
usage() {
    echo ""
    info "Usage: $0 [install|uninstall]"
    echo ""
    info "  install   (default) Install BitChat Node Daemon as a systemd service"
    info "  uninstall           Remove BitChat Node Daemon, config, data, and user"
    echo ""
    exit 0
}

# ------------------------------------------------------------------
# Pre-flight checks (install only)
# ------------------------------------------------------------------
preflight() {
    # Check root / sudo
    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            warn "Not running as root – re-executing with sudo..."
            exec sudo sh "$0" "$@"
            exit $?
        else
            err "This script must be run as root. Use: sudo sh install.sh"
        fi
    fi

    # Check Python version
    if ! command -v "$PYTHON" >/dev/null 2>&1; then
        err "Python 3 not found. Install Python 3.10+ and try again."
    fi

    PYTHON_VERSION=$("$PYTHON" --version 2>&1 | awk '{print $2}')
    PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

    if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]; }; then
        err "Python 3.10+ required (found $PYTHON_VERSION)"
    fi
    ok "Python $PYTHON_VERSION found"

    # Check pip
    if ! command -v pip3 >/dev/null 2>&1 && ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
        err "pip3 not found. Install python3-pip and try again."
    fi
    ok "pip3 found"

    # Check git
    if ! command -v git >/dev/null 2>&1; then
        err "git not found. Install git and try again."
    fi
    ok "git found"

    echo ""
}

# ==================================================================
# INSTALL
# ==================================================================
install() {
    info "=== BitChat Node Daemon Installer ==="
    echo ""

    preflight

    # Create service user
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        info "Creating system user '$SERVICE_USER'..."
        useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
        ok "User '$SERVICE_USER' created"
    else
        ok "User '$SERVICE_USER' already exists"
    fi

    # Clone / update repository
    if [ -d "$INSTALL_DIR" ]; then
        info "Updating existing installation at $INSTALL_DIR..."
        cd "$INSTALL_DIR"
        git pull --ff-only
        ok "Repository updated"
    else
        info "Cloning repository to $INSTALL_DIR..."
        git clone "$REPO_URL" "$INSTALL_DIR"
        ok "Repository cloned"
    fi

    # Install Python dependencies
    info "Installing Python dependencies..."
    cd "$INSTALL_DIR"
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet .
    ok "Dependencies installed"

    # Create data directory
    mkdir -p "$DATA_DIR"
    chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
    ok "Data directory created at $DATA_DIR"

    # Create config directory
    CONFIG_DIR=$(dirname "$CONFIG_PATH")
    mkdir -p "$CONFIG_DIR"

    if [ ! -f "$CONFIG_PATH" ]; then
        cp "$INSTALL_DIR/config.yaml" "$CONFIG_PATH"
        # Update data_dir in config
        sed -i "s|~/.bitchatxxk|$DATA_DIR|g" "$CONFIG_PATH"
        ok "Default config created at $CONFIG_PATH"
    else
        ok "Config already exists at $CONFIG_PATH – not overwriting"
    fi

    chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"

    # Create systemd service
    info "Creating systemd service..."

    cat > "$SERVICE_FILE" <<SERVICEEOF
[Unit]
Description=BitChat Node Daemon
Documentation=${REPO_URL%.git}
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON -m daemon --config $CONFIG_PATH
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR $CONFIG_DIR
PrivateDevices=true

[Install]
WantedBy=multi-user.target
SERVICEEOF

    ok "systemd service created at $SERVICE_FILE"

    # Enable and start service
    info "Enabling and starting service..."
    systemctl daemon-reload
    systemctl enable bitchat-node.service
    systemctl start bitchat-node.service

    sleep 2
    if systemctl is-active --quiet bitchat-node.service; then
        ok "BitChat Node Daemon is running!"
    else
        warn "Service started but may not be ready. Check: journalctl -u bitchat-node -n 50 --no-pager"
    fi

    echo ""
    info "=== Installation complete ==="
    echo ""
    info "  Status:  systemctl status bitchat-node"
    info "  Logs:    journalctl -u bitchat-node -f"
    info "  Config:  $CONFIG_PATH"
    info "  API:     http://127.0.0.1:8080/status"
    echo ""
}

# ==================================================================
# UNINSTALL
# ==================================================================
uninstall() {
    info "=== BitChat Node Daemon Uninstaller ==="
    echo ""

    # Check root / sudo
    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            warn "Not running as root – re-executing with sudo..."
            exec sudo sh "$0" uninstall
            exit $?
        else
            err "This script must be run as root. Use: sudo sh install.sh uninstall"
        fi
    fi

    warn "This will remove the BitChat Node Daemon completely."
    printf "Continue? [y/N] "
    read -r CONFIRM
    case "$CONFIRM" in
        [yY]|[yY][eE][sS]) ;;
        *) err "Aborted." ;;
    esac

    # Stop and disable systemd service
    if [ -f "$SERVICE_FILE" ]; then
        info "Stopping and disabling systemd service..."
        systemctl stop bitchat-node.service 2>/dev/null || true
        systemctl disable bitchat-node.service 2>/dev/null || true
        rm -f "$SERVICE_FILE"
        systemctl daemon-reload
        ok "Systemd service removed"
    else
        ok "No systemd service found"
    fi

    # Remove installation directory
    if [ -d "$INSTALL_DIR" ]; then
        info "Removing installation directory $INSTALL_DIR..."
        rm -rf "$INSTALL_DIR"
        ok "Installation directory removed"
    else
        ok "No installation directory found"
    fi

    # Remove config
    CONFIG_DIR=$(dirname "$CONFIG_PATH")
    if [ -d "$CONFIG_DIR" ]; then
        info "Removing config directory $CONFIG_DIR..."
        rm -rf "$CONFIG_DIR"
        ok "Config directory removed"
    else
        ok "No config directory found"
    fi

    # Remove data
    if [ -d "$DATA_DIR" ]; then
        info "Removing data directory $DATA_DIR..."
        rm -rf "$DATA_DIR"
        ok "Data directory removed"
    else
        ok "No data directory found"
    fi

    # Remove system user
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        info "Removing system user '$SERVICE_USER'..."
        userdel "$SERVICE_USER" 2>/dev/null || true
        ok "User '$SERVICE_USER' removed"
    else
        ok "No system user found"
    fi

    echo ""
    info "=== Uninstall complete ==="
    echo ""
}

# ==================================================================
# Main
# ==================================================================
case "${1:-install}" in
    install|--install)
        install
        ;;
    uninstall|--uninstall)
        uninstall
        ;;
    help|--help|-h)
        usage
        ;;
    *)
        err "Unknown command: $1. Use: $0 [install|uninstall]"
        ;;
esac
