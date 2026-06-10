#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SERVICE_NAME="pi-sat"
REPO_URL="${REPO_URL:-https://github.com/W9KSB/Pi-Sat.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/pi-sat}"
RUN_USER="$(id -un)"
STEP=0
TOTAL_STEPS=6

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
log_info "Installing python3-venv, python3-pip, git, and libhamlib-utils"
sudo apt install -y python3-venv python3-pip git libhamlib-utils

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

log_step "Creating Python environment"
log_info "Creating virtual environment in ${INSTALL_DIR}/.venv"
python3 -m venv .venv
. .venv/bin/activate
log_info "Upgrading pip"
python -m pip install --upgrade pip
log_info "Installing Python dependencies from requirements.txt"
python -m pip install -r requirements.txt

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
ExecStart=${INSTALL_DIR}/.venv/bin/python -m pi_sat_controller.backend.run_server
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
log_info "Enabling and starting ${SERVICE_NAME}"
sudo systemctl enable --now "${SERVICE_NAME}"

echo ""
echo "Install complete."
echo "Service: sudo systemctl status ${SERVICE_NAME}"
echo "Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "URL:     http://$(hostname -I | awk '{print $1}')"
