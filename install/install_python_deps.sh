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
else
  PYTHON_BIN="$(command -v python3)"
fi

echo "Installing Python dependencies"
echo ""
pi_sat_install_dependencies "${PYTHON_BIN}"
echo ""
echo "Python dependency install complete."
