#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SERVICE_NAME="pi-sat"
REPO_URL="${REPO_URL:-https://github.com/W9KSB/Pi-Sat.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/pi-sat}"
RUN_USER="$(id -un)"
STEP=0
TOTAL_STEPS=7

log_step() {
  STEP=$((STEP + 1))
  echo ""
  echo "Step ${STEP} of ${TOTAL_STEPS}: $1"
}

log_info() {
  echo "    $1"
}

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this installer as the normal Pi user, not with sudo."
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required."
  exit 1
fi

echo "Installing Pi-Sat Controller from: ${INSTALL_DIR}"
echo "Service will run as user: ${RUN_USER}"
echo "Repository: ${REPO_URL}"
echo ""
echo "You may be prompted for your sudo password during package install and service setup."
echo "That is normal."

log_step "Installing system packages"
log_info "Running apt update"
sudo apt update
log_info "Installing host-level Python, Git, Hamlib, ffmpeg, and Dire Wolf"
sudo apt install -y python3 python3-pip git ffmpeg libhamlib-utils direwolf
log_info "Granting the service user serial-device access"
sudo usermod -aG dialout "${RUN_USER}"

if [ -d "${INSTALL_DIR}/.git" ]; then
  log_step "Updating existing Pi-Sat checkout"
  log_info "Existing git checkout found in ${INSTALL_DIR}"
  log_info "Fetching latest repository changes"
  git -C "${INSTALL_DIR}" fetch --all --prune
  log_info "Fast-forwarding local checkout"
  git -C "${INSTALL_DIR}" pull --ff-only
else
  log_step "Cloning Pi-Sat repository"
  if [ -e "${INSTALL_DIR}" ] && [ ! -d "${INSTALL_DIR}" ]; then
    echo "INSTALL_DIR exists and is not a directory: ${INSTALL_DIR}"
    exit 1
  fi
  if [ -d "${INSTALL_DIR}" ] && [ -n "$(ls -A "${INSTALL_DIR}" 2>/dev/null)" ]; then
    echo "INSTALL_DIR exists and is not an existing git checkout: ${INSTALL_DIR}"
    echo "Use an empty directory or remove it first."
    exit 1
  fi
  mkdir -p "$(dirname "${INSTALL_DIR}")"
  log_info "Cloning ${REPO_URL} into ${INSTALL_DIR}"
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"

if [ ! -f bin/pi-sat-sstv-decoder ]; then
  echo "Missing bundled SSTV decoder: ${INSTALL_DIR}/bin/pi-sat-sstv-decoder"
  exit 1
fi
chmod +x bin/pi-sat-sstv-decoder
if command -v file >/dev/null 2>&1; then
  file_output="$(file -b bin/pi-sat-sstv-decoder)"
  case "${file_output}" in
    *"ELF 64-bit"*"ARM aarch64"*) : ;;
    *) echo "Bundled SSTV decoder is not a 64-bit AArch64 ELF: ${file_output}"; exit 1 ;;
  esac
fi

log_step "Ensuring local runtime files"
created_runtime_file=0
if [ ! -f pi-sat-controller.conf ]; then
  if [ -f pi-sat-controller.conf.example ]; then
    cp pi-sat-controller.conf.example pi-sat-controller.conf
    log_info "Created local pi-sat-controller.conf from example template"
    created_runtime_file=1
  else
    echo "Missing pi-sat-controller.conf.example in ${INSTALL_DIR}."
    exit 1
  fi
fi

if [ ! -f update_pi.sh ]; then
  if [ -f updater.template ]; then
    cp updater.template update_pi.sh
    chmod +x update_pi.sh
    log_info "Created local update_pi.sh from updater.template"
    created_runtime_file=1
  else
    echo "Missing updater.template in ${INSTALL_DIR}."
    exit 1
  fi
fi

if [ "${created_runtime_file}" -eq 0 ]; then
  log_info "Local runtime files already exist"
fi

log_step "Installing host-level Python dependencies"
PYTHON_BIN="$(command -v python3)"
log_info "Using ${PYTHON_BIN}"
log_info "Installing Python dependencies from requirements.txt"
sudo "${PYTHON_BIN}" -m pip install --break-system-packages -r requirements.txt

log_step "Checking Dire Wolf for the APRS module"
if command -v direwolf >/dev/null 2>&1; then
  log_info "Dire Wolf is available at $(command -v direwolf)"
else
  log_info "Dire Wolf is unavailable; the APRS module will report that when enabled."
fi

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TMP_SERVICE="$(mktemp)"

log_step "Installing systemd service"
log_info "Writing service file to ${SERVICE_FILE}"
cat > "${TMP_SERVICE}" <<EOF
[Unit]
Description=Pi-Sat Controller
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON_BIN} -m pi_sat_controller.backend.run_server
Restart=on-failure
RestartSec=5
User=${RUN_USER}
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

sudo install -m 0644 "${TMP_SERVICE}" "${SERVICE_FILE}"
rm -f "${TMP_SERVICE}"

if sudo systemctl list-unit-files | grep -q '^sat-controller\.service'; then
  log_info "Removing older sat-controller service name"
  sudo systemctl disable --now sat-controller >/dev/null 2>&1 || true
  sudo rm -f /etc/systemd/system/sat-controller.service
fi

log_step "Enabling and starting Pi-Sat"
log_info "Reloading systemd"
sudo systemctl daemon-reload
# Pi-Sat no longer runs a local STUN service. Retire the unit an earlier
# install added, and the coturn package it existed for, so the station stops
# listening on UDP 3478 and nothing reintroduces it.
if sudo systemctl list-unit-files | grep -q '^pi-sat-stun\.service'; then
  log_info "Removing the retired local STUN service"
  sudo systemctl disable --now pi-sat-stun >/dev/null 2>&1 || true
  sudo rm -f /etc/systemd/system/pi-sat-stun.service
  sudo systemctl daemon-reload
fi
if dpkg -s coturn >/dev/null 2>&1; then
  log_info "Removing coturn, which Pi-Sat installed only for that STUN service"
  sudo apt-get purge -y coturn >/dev/null 2>&1 || log_info "coturn could not be removed automatically"
fi
log_info "Enabling ${SERVICE_NAME}"
sudo systemctl enable "${SERVICE_NAME}"
log_info "Starting or restarting ${SERVICE_NAME} with the installed code"
sudo systemctl restart "${SERVICE_NAME}"

echo ""
echo "Install complete."
echo "Service: sudo systemctl status ${SERVICE_NAME}"
echo "Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "Default URL: https://$(hostname -I | awk '{print $1}')"
echo "Custom ports / HTTP opt-out: see [server] in pi-sat-controller.conf."
echo "First HTTPS start generates a self-signed certificate using OpenSSL if none exists."
