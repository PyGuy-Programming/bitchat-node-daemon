#!/bin/sh
#
# BitChat Node Daemon – Installer
#
# Usage:
#   curl -sSfL https://raw.githubusercontent.com/<user>/bitchat-node-daemon/main/install.sh | sh
#
# This script:
#   1. Clones the repository
#   2. Installs Python dependencies
#   3. Creates a systemd service
#   4. Starts the daemon
#

set -e

REPO_URL="https://github.com/PyGuy-Programming/bitchat-node-daemon.git"
INSTALL_DIR="${INSTALL_DIR:-/opt/bitchat-node}"
SERVICE_USER="${SERVICE_USER:-bitchat}"
PYTHON="${PYTHON:-python3}"
CONFIG_PATH="${CONFIG_PATH:-/etc/bitchat-node/config.yaml}"
DATA_DIR="${DATA_DIR:-/var/lib/bitchat-node}"

# ------------------------------------------------------------------
# Color helpers
# ------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { printf "${CYAN}%s${NC}\n" "$*"; }
ok()    { printf "${GREEN}✓ %s${NC}\n" "$*"; }
warn()  { printf "${YELLOW}⚠ %s${NC}\n" "$*"; }
err()   { printf "${RED}✗ %s${NC}\n" "$*"; exit 1; }

# ------------------------------------------------------------------
# Pre-flight checks
# ------------------------------------------------------------------
info "=== BitChat Node Daemon Installer ==="
echo ""

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

# ------------------------------------------------------------------
# Create service user
# ------------------------------------------------------------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    info "Creating system user '$SERVICE_USER'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "User '$SERVICE_USER' created"
else
    ok "User '$SERVICE_USER' already exists"
fi

# ------------------------------------------------------------------
# Clone / update repository
# ------------------------------------------------------------------
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

# ------------------------------------------------------------------
# Install Python dependencies
# ------------------------------------------------------------------
info "Installing Python dependencies..."
cd "$INSTALL_DIR"
"$PYTHON" -m pip install --quiet --upgrade pip
"$PYTHON" -m pip install --quiet .
ok "Dependencies installed"

# ------------------------------------------------------------------
# Create data directory
# ------------------------------------------------------------------
mkdir -p "$DATA_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
ok "Data directory created at $DATA_DIR"

# ------------------------------------------------------------------
# Create config directory
# ------------------------------------------------------------------
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

# ------------------------------------------------------------------
# Create systemd service
# ------------------------------------------------------------------
SERVICE_FILE="/etc/systemd/system/bitchat-node.service"

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

# ------------------------------------------------------------------
# Enable and start service
# ------------------------------------------------------------------
info "Enabling and starting service..."
systemctl daemon-reload
systemctl enable bitchat-node.service
systemctl start bitchat-node.service

# Wait a moment and check status
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
