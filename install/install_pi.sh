#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SERVICE_NAME="pi-sat"
REPO_URL="${REPO_URL:-https://github.com/W9KSB/Pi-Sat.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/pi-sat}"
SYSTEMD_UNIT_DIR="/etc/systemd/system"
RUN_USER="$(id -un)"
STEP=0
TOTAL_STEPS=8
WSJTX_INSTALL_RESULT="not_requested"
INSTALLER_SERVICE_STOPPED=0
INSTALLER_SERVICE_EXISTED=0
INSTALLER_SERVICE_WAS_ACTIVE=0
INSTALLER_CONFIG_CHANGED=0
SERVICE_FILE_EXISTED=0
OVERRIDE_FILE_EXISTED=0
TMP_SERVICE=""
TMP_OVERRIDE=""
SERVICE_BACKUP=""
OVERRIDE_BACKUP=""
UNINSTALL=0

usage() {
  echo "Usage: sh install/install_pi.sh [--uninstall | --help]"
  echo "  No option: install or update Pi-Sat."
  echo "  --uninstall: stop and remove the Pi-Sat service; preserve application files and station data."
  echo "  --help: show this help."
}

uninstall_pi_sat() {
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl is required to uninstall the Pi-Sat service." >&2
    return 1
  fi

  uninstall_unit="${SERVICE_NAME}.service"
  if uninstall_load_state="$(sudo systemctl show --property=LoadState --value "${uninstall_unit}")"; then
    :
  elif [ "${uninstall_load_state}" != "not-found" ]; then
    echo "Cannot inspect ${uninstall_unit}; no service files were removed." >&2
    return 1
  fi
  if [ -z "${uninstall_load_state}" ]; then
    echo "Cannot determine the state of ${uninstall_unit}; no service files were removed." >&2
    return 1
  fi

  if [ "${uninstall_load_state}" != "not-found" ]; then
    if ! sudo systemctl disable --now "${uninstall_unit}"; then
      echo "Cannot stop and disable ${uninstall_unit}; no service files were removed." >&2
      return 1
    fi
  fi

  if ! sudo rm -f -- \
      "${SYSTEMD_UNIT_DIR}/${uninstall_unit}" \
      "${SYSTEMD_UNIT_DIR}/${uninstall_unit}.d/90-pi-sat-python.conf"; then
    echo "Could not remove the Pi-Sat service files; uninstallation is incomplete." >&2
    return 1
  fi
  if ! sudo systemctl daemon-reload; then
    echo "Could not reload systemd; uninstallation is incomplete." >&2
    return 1
  fi

  echo "Pi-Sat service uninstalled."
  echo "Application files, configuration, recordings, images, and QSO logs were preserved."
  echo "Shared packages, audio services, and user permissions were left unchanged."
}

case "$#" in
  0) ;;
  1)
    case "$1" in
      --uninstall) UNINSTALL=1 ;;
      --help|-h) usage; exit 0 ;;
      *) usage >&2; exit 2 ;;
    esac
    ;;
  *) usage >&2; exit 2 ;;
esac

log_step() {
  STEP=$((STEP + 1))
  echo ""
  echo "Step ${STEP} of ${TOTAL_STEPS}: $1"
}

log_info() {
  echo "    $1"
}

install_optional_wsjtx() {
  log_info "WSJT-X is optional and enables FT4/FT8 with the native IC-9700."
  # Quick install pipes the script into sh, so answers must come from the terminal.
  if ! ( : < /dev/tty ) 2>/dev/null; then
    log_info "No controlling terminal; skipping optional WSJT-X installation."
    return 0
  fi

  exec 3</dev/tty
  while :; do
    printf "Install WSJT-X for FT4/FT8? [y/N] "
    if ! IFS= read -r wsjtx_answer <&3; then
      echo ""
      log_info "Skipping optional WSJT-X installation."
      WSJTX_INSTALL_RESULT="declined"
      exec 3<&-
      return 0
    fi
    case "${wsjtx_answer}" in
      y|Y|yes|YES|Yes)
        exec 3<&-
        log_info "Installing WSJT-X and its runtime dependencies"
        if sh "${INSTALL_DIR}/install/install_wsjtx.sh" </dev/null; then
          WSJTX_INSTALL_RESULT="installed"
        else
          WSJTX_INSTALL_RESULT="failed"
          echo "Warning: optional WSJT-X installation failed; continuing with the Pi-Sat core installation." >&2
          echo "Retry with: sh \"${INSTALL_DIR}/install/install_wsjtx.sh\"" >&2
        fi
        return 0
        ;;
      ""|n|N|no|NO|No)
        log_info "Skipping optional WSJT-X installation."
        WSJTX_INSTALL_RESULT="declined"
        exec 3<&-
        return 0
        ;;
      *)
        echo "Please answer y or n."
        ;;
    esac
  done
}

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this script as the normal Pi user, not with sudo."
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required."
  exit 1
fi

# Uninstallation must not enter the installer or its service-restoration trap.
if [ "${UNINSTALL}" -eq 1 ]; then
  uninstall_pi_sat
  exit 0
fi

cleanup_temp_files() {
  if [ -n "${TMP_SERVICE}" ]; then
    rm -f "${TMP_SERVICE}"
  fi
  if [ -n "${TMP_OVERRIDE}" ]; then
    rm -f "${TMP_OVERRIDE}"
  fi
  if [ -n "${SERVICE_BACKUP}" ]; then
    rm -f "${SERVICE_BACKUP}"
  fi
  if [ -n "${OVERRIDE_BACKUP}" ]; then
    rm -f "${OVERRIDE_BACKUP}"
  fi
}

restore_service_on_failure() {
  status=$?
  trap - EXIT
  restore_failed=0
  if [ "${INSTALLER_CONFIG_CHANGED}" -eq 1 ]; then
    echo "Restoring the previous ${SERVICE_NAME} unit configuration."
    if [ "${SERVICE_FILE_EXISTED}" -eq 1 ]; then
      sudo install -m 0644 "${SERVICE_BACKUP}" "${SERVICE_FILE}" || restore_failed=1
    else
      sudo rm -f "${SERVICE_FILE}" || restore_failed=1
    fi
    if [ "${OVERRIDE_FILE_EXISTED}" -eq 1 ]; then
      sudo install -m 0644 "${OVERRIDE_BACKUP}" "${PYTHON_OVERRIDE}" || restore_failed=1
    else
      sudo rm -f "${PYTHON_OVERRIDE}" || restore_failed=1
    fi
    sudo systemctl daemon-reload || restore_failed=1
  fi
  if [ "${INSTALLER_SERVICE_STOPPED}" -eq 1 ] \
      || { [ "${INSTALLER_CONFIG_CHANGED}" -eq 1 ] \
        && [ "${INSTALLER_SERVICE_EXISTED}" -eq 1 ] \
        && [ "${INSTALLER_SERVICE_WAS_ACTIVE}" -eq 1 ]; }; then
    echo "Installation failed; restarting ${SERVICE_NAME} with its previous configuration."
    sudo systemctl restart "${SERVICE_NAME}" || restore_failed=1
  fi
  if [ "${restore_failed}" -ne 0 ]; then
    echo "The previous ${SERVICE_NAME} unit or process may need manual review." >&2
  fi
  cleanup_temp_files
  exit "${status}"
}
trap restore_service_on_failure EXIT

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
sudo apt install -y python3 python3-venv git ffmpeg libhamlib-utils direwolf openssl
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

log_step "Installing optional WSJT-X"
install_optional_wsjtx

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

log_step "Preparing the isolated Python environment"
. "${INSTALL_DIR}/install/python_environment.sh"
PYTHON_BIN="$(pi_sat_find_compatible_python)"
log_info "Using ${PYTHON_BIN} to bootstrap Python"
VENV_DIR="${INSTALL_DIR}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python"
if command -v systemctl >/dev/null 2>&1 \
    && [ "$(systemctl show --property=LoadState --value "${SERVICE_NAME}")" != "not-found" ]; then
  INSTALLER_SERVICE_EXISTED=1
  configured_python="$(pi_sat_service_python "${SERVICE_NAME}")"
  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    INSTALLER_SERVICE_WAS_ACTIVE=1
  fi
  if [ "${configured_python}" = "${VENV_PYTHON}" ] \
      && systemctl is-active --quiet "${SERVICE_NAME}"; then
    log_info "Stopping ${SERVICE_NAME} while its active virtual environment is updated"
    sudo systemctl stop "${SERVICE_NAME}"
    INSTALLER_SERVICE_STOPPED=1
  fi
fi
pi_sat_prepare_virtual_environment "${PYTHON_BIN}" "${VENV_DIR}"
PYTHON_BIN="${VENV_PYTHON}"
log_info "Pi-Sat dependencies are installed in ${VENV_DIR}"

log_step "Checking Dire Wolf for the APRS module"
if command -v direwolf >/dev/null 2>&1; then
  log_info "Dire Wolf is available at $(command -v direwolf)"
else
  log_info "Dire Wolf is unavailable; the APRS module will report that when enabled."
fi

SERVICE_FILE="${SYSTEMD_UNIT_DIR}/${SERVICE_NAME}.service"
PYTHON_OVERRIDE_DIR="${SYSTEMD_UNIT_DIR}/${SERVICE_NAME}.service.d"
PYTHON_OVERRIDE="${PYTHON_OVERRIDE_DIR}/90-pi-sat-python.conf"
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

SERVICE_BACKUP="$(mktemp)"
OVERRIDE_BACKUP="$(mktemp)"
if sudo test -f "${SERVICE_FILE}"; then
  sudo cp "${SERVICE_FILE}" "${SERVICE_BACKUP}"
  SERVICE_FILE_EXISTED=1
fi
if sudo test -f "${PYTHON_OVERRIDE}"; then
  sudo cp "${PYTHON_OVERRIDE}" "${OVERRIDE_BACKUP}"
  OVERRIDE_FILE_EXISTED=1
fi

INSTALLER_CONFIG_CHANGED=1
sudo install -m 0644 "${TMP_SERVICE}" "${SERVICE_FILE}"
rm -f "${TMP_SERVICE}"

if sudo systemctl list-unit-files | grep -q '^sat-controller\.service'; then
  log_info "Removing older sat-controller service name"
  sudo systemctl disable --now sat-controller >/dev/null 2>&1 || true
  sudo rm -f "${SYSTEMD_UNIT_DIR}/sat-controller.service"
fi

log_step "Enabling and starting Pi-Sat"
log_info "Reloading systemd"
sudo systemctl daemon-reload

if sudo test -f "${PYTHON_OVERRIDE}"; then
  log_info "Refreshing the managed Python interpreter override"
  current_python="$(pi_sat_service_python "${SERVICE_NAME}")"
  replacement_execstart="$(pi_sat_replacement_execstart "${current_python}" "${PYTHON_BIN}" "${SERVICE_NAME}")"
  TMP_OVERRIDE="$(mktemp)"
  {
    printf '[Service]\n'
    printf 'ExecStart=\n'
    printf '%s\n' "${replacement_execstart}"
  } > "${TMP_OVERRIDE}"
  sudo install -m 0644 "${TMP_OVERRIDE}" "${PYTHON_OVERRIDE}"
  rm -f "${TMP_OVERRIDE}"
  sudo systemctl daemon-reload
fi

selected_python="$(pi_sat_service_python "${SERVICE_NAME}")"
if [ "${selected_python}" != "${PYTHON_BIN}" ]; then
  echo "The Pi-Sat service still selects ${selected_python} instead of ${PYTHON_BIN}." >&2
  echo "Check service drop-ins for a higher-priority ExecStart override." >&2
  exit 1
fi

# Pi-Sat no longer runs a local STUN service. Retire the unit an earlier
# install added, and the coturn package it existed for, so the station stops
# listening on UDP 3478 and nothing reintroduces it.
if sudo systemctl list-unit-files | grep -q '^pi-sat-stun\.service'; then
  log_info "Removing the retired local STUN service"
  sudo systemctl disable --now pi-sat-stun >/dev/null 2>&1 || true
  sudo rm -f "${SYSTEMD_UNIT_DIR}/pi-sat-stun.service"
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
if ! pi_sat_wait_service_active "${SERVICE_NAME}"; then
  echo "${SERVICE_NAME} did not remain active after installation." >&2
  exit 1
fi
INSTALLER_SERVICE_STOPPED=0
cleanup_temp_files
trap - EXIT

echo ""
echo "Install complete."
echo "Service: sudo systemctl status ${SERVICE_NAME}"
echo "Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "Default URL: https://$(hostname -I | awk '{print $1}')"
echo "Custom ports / HTTP opt-out: see [server] in pi-sat-controller.conf."
echo "First HTTPS start generates a self-signed certificate using OpenSSL if none exists."
if [ "${WSJTX_INSTALL_RESULT}" = "failed" ]; then
  echo "WSJT-X: optional installation did not complete; retry after reviewing the warning above."
fi
