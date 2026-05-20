#!/bin/bash
# ============================================================
# FANUC IoT Edge — Linux Installer
# Tested on: Ubuntu, Raspberry Pi OS (armv7)
# Run as: sudo bash install.sh
# ============================================================

set -e

INSTALL_DIR="/opt/fanuc_iot_edge"
DATA_DIR="/etc/fanuc_iot_edge"
SERVICE_NAME="fanuciotedge"

echo "======================================"
echo "  FANUC IoT Edge - Linux Installer"
echo "======================================"

# 1. Install system deps
echo "[1/5] Installing system dependencies..."
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv

# 2. Copy project files
echo "[2/5] Copying project files..."
mkdir -p $INSTALL_DIR
mkdir -p $DATA_DIR/machines
mkdir -p $DATA_DIR/logs
cp -r src/     $INSTALL_DIR/
cp -r lib/     $INSTALL_DIR/
cp -r machines/ $DATA_DIR/

# 3. Create virtualenv and install deps
echo "[3/5] Installing Python dependencies..."
python3 -m venv $INSTALL_DIR/venv
$INSTALL_DIR/venv/bin/pip install -q -r requirements.txt

# 4. Install systemd service
echo "[4/5] Installing systemd service..."
cp installer/fanuciotedge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable $SERVICE_NAME

# 5. Start service
echo "[5/5] Starting service..."
systemctl start $SERVICE_NAME
systemctl status $SERVICE_NAME --no-pager

echo ""
echo "======================================"
echo "  Install complete!"
echo "  GUI: http://$(hostname -I | awk '{print $1}'):8765"
echo "  Logs: journalctl -u fanuciotedge -f"
echo "======================================"
