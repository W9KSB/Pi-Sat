#!/usr/bin/env sh
set -eu

echo "Installing Hamlib utilities"
echo "You may be prompted for your sudo password. That is normal."
echo ""
echo "[1] Running apt update"
sudo apt update
echo "[2] Installing libhamlib-utils"
sudo apt install -y libhamlib-utils
echo ""
echo "Hamlib install complete."
