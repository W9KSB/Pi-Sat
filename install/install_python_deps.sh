#!/usr/bin/env sh
set -eu

echo "Installing Python dependencies"
echo ""
echo "[1] Creating virtual environment"
python3 -m venv .venv
. .venv/bin/activate
echo "[2] Upgrading pip"
python -m pip install --upgrade pip
echo "[3] Installing requirements.txt"
python -m pip install -r requirements.txt
echo ""
echo "Python dependency install complete."
