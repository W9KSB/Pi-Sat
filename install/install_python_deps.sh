#!/usr/bin/env sh
set -eu

echo "Installing Python dependencies"
echo ""
echo "[1] Installing requirements.txt into the host Python"
sudo python3 -m pip install --break-system-packages -r requirements.txt
echo ""
echo "Python dependency install complete."
