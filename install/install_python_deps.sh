#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"
. "${SCRIPT_DIR}/python_environment.sh"
if [ "$#" -gt 0 ]; then
  PYTHON_BIN="$1"
elif command -v systemctl >/dev/null 2>&1 \
    && [ "$(systemctl show --property=LoadState --value pi-sat)" != "not-found" ]; then
  PYTHON_BIN="$(pi_sat_service_python pi-sat)"
elif [ -x "${REPO_ROOT}/.venv/bin/python" ]; then
  PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
else
  echo "No Pi-Sat virtual environment is available." >&2
  echo "Run install/install_pi.sh or update_pi.sh to prepare it." >&2
  exit 1
fi

echo "Installing Python dependencies"
echo ""
pi_sat_install_dependencies "${PYTHON_BIN}"
echo ""
echo "Python dependency install complete."
