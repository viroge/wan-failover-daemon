#!/bin/bash
set -euo pipefail

# WAN Failover Daemon - Installer
# Run as root: sudo bash install.sh

INSTALL_DIR="/opt/wan-failover"
CONFIG_DIR="/etc/wan-failover"
STATE_DIR="/var/lib/wan-failover"
SERVICE_FILE="/etc/systemd/system/wan-failover.service"
DHCLIENT_HOOK="/etc/dhcp/dhclient-exit-hooks.d/wan-failover"

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo bash install.sh)"
    exit 1
fi

echo "=== WAN Failover Daemon Installer ==="

# Install Python dependency
echo "[1/6] Installing Python dependencies..."
pip3 install -r requirements.txt

# Copy daemon
echo "[2/6] Installing daemon to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
cp wan_failover.py "${INSTALL_DIR}/"
chmod +x "${INSTALL_DIR}/wan_failover.py"

# Config
echo "[3/6] Setting up configuration..."
mkdir -p "${CONFIG_DIR}"
mkdir -p "${STATE_DIR}"
if [ ! -f "${CONFIG_DIR}/config.yaml" ]; then
    cp config.example.yaml "${CONFIG_DIR}/config.yaml"
    echo "  -> Created ${CONFIG_DIR}/config.yaml from example"
    echo "  -> IMPORTANT: Edit ${CONFIG_DIR}/config.yaml to match your setup!"
else
    echo "  -> ${CONFIG_DIR}/config.yaml already exists, skipping"
fi

# dhclient exit hook
echo "[4/6] Installing dhclient exit hook..."
if [ -d /etc/dhcp ]; then
    mkdir -p "$(dirname "${DHCLIENT_HOOK}")"
    cp dhclient-exit-hook "${DHCLIENT_HOOK}"
    chmod +x "${DHCLIENT_HOOK}"
    echo "  -> Installed ${DHCLIENT_HOOK}"
else
    echo "  -> /etc/dhcp not found, skipping dhclient hook"
    echo "  -> Gateway detection will fall back to routing table parsing"
fi

# Systemd service
echo "[5/6] Installing systemd service..."
cp wan_failover.service "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable wan-failover.service

echo "[6/6] Done!"
echo ""
echo "Next steps:"
echo "  1. Edit your config:  sudo nano ${CONFIG_DIR}/config.yaml"
echo "  2. Start the service: sudo systemctl start wan-failover"
echo "  3. Check status:      sudo systemctl status wan-failover"
echo "  4. View logs:         sudo journalctl -u wan-failover -f"
