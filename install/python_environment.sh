#!/usr/bin/env sh

pi_sat_service_execstart() {
  service_exec="$(systemctl show --property=ExecStart --value "${1:-pi-sat}")" || return 1
  service_argv="${service_exec#*" ; argv[]="}"
  if [ "${service_argv}" = "${service_exec}" ]; then
    echo "Cannot determine the command configured for the Pi-Sat service." >&2
    return 1
  fi
  service_command="${service_argv%%" ; ignore_errors="*}"
  if [ -z "${service_command}" ] || [ "${service_command}" = "${service_argv}" ]; then
    echo "Cannot determine the command configured for the Pi-Sat service." >&2
    return 1
  fi
  printf '%s\n' "${service_command}"
}

pi_sat_service_python() {
  service_exec="$(systemctl show --property=ExecStart --value "${1:-pi-sat}")" || return 1
  service_command="${service_exec#"{ path="}"
  if [ "${service_command}" = "${service_exec}" ]; then
    echo "Cannot determine the Python executable configured for the Pi-Sat service." >&2
    return 1
  fi
  service_python="${service_command%%" ; argv[]="*}"
  if [ "${service_python}" = "${service_command}" ]; then
    echo "Cannot determine the Python executable configured for the Pi-Sat service." >&2
    return 1
  fi
  if [ ! -x "${service_python}" ]; then
    echo "The Pi-Sat service Python executable is unavailable: ${service_python}" >&2
    return 1
  fi
  printf '%s\n' "${service_python}"
}

pi_sat_check_migration_command() {
  old_python="$1"
  new_python="$2"
  service_name="${3:-pi-sat}"
  expected_command="${old_python} -m pi_sat_controller.backend.run_server"
  service_command="$(pi_sat_service_execstart "${service_name}")" || return 1

  case "${old_python}${new_python}" in
    *"
"*|*"	"*|*" "*|*"\\"*|*\"*|*\'*|*'$'*|*"%"*)
      echo "Automatic Python migration cannot safely represent the configured service paths." >&2
      echo "Move Pi-Sat to a path without spaces or systemd escape characters and update the service manually." >&2
      return 1
      ;;
  esac

  if [ "${service_command}" != "${expected_command}" ]; then
    echo "The Pi-Sat service command is not the standard run_server command:" >&2
    echo "  ${service_command}" >&2
    echo "Keep the custom command and migrate its Python environment manually." >&2
    return 1
  fi
}

pi_sat_replacement_execstart() {
  old_python="$1"
  new_python="$2"
  service_name="${3:-pi-sat}"
  pi_sat_check_migration_command "${old_python}" "${new_python}" "${service_name}" || return 1
  printf 'ExecStart=%s -m pi_sat_controller.backend.run_server\n' "${new_python}"
}

pi_sat_check_python() {
  if ! "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Pi-Sat requires Python 3.11 or newer for SSTVFlow." >&2
    return 1
  fi
}

pi_sat_is_virtual_environment() {
  "$1" -c '
import pathlib
import sys

if sys.prefix == sys.base_prefix:
    raise SystemExit(1)
config = pathlib.Path(sys.prefix) / "pyvenv.cfg"
if not config.is_file():
    raise SystemExit(2)
for line in config.read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if separator and key.strip() == "include-system-site-packages":
        raise SystemExit(0 if value.strip().lower() == "false" else 3)
raise SystemExit(4)
'
}

pi_sat_find_compatible_python() {
  python_path="$(command -v python3 2>/dev/null || true)"
  if [ -z "${python_path}" ]; then
    echo "Python 3 was not found on PATH." >&2
    return 1
  fi
  pi_sat_check_python "${python_path}" || return 1
  printf '%s\n' "${python_path}"
}

pi_sat_verify_environment() {
  dependency_python="$1"
  pi_sat_check_python "${dependency_python}" || return 1
  if ! pi_sat_is_virtual_environment "${dependency_python}"; then
    echo "Pi-Sat dependencies must be installed in a dedicated virtual environment." >&2
    return 1
  fi

  "${dependency_python}" -m pip check || return 1
  "${dependency_python}" - <<'PY'
from PIL import Image
from cryptography import x509
from fastapi import FastAPI
from scipy import signal
from skyfield.api import load
from sstv_decoder.decoder import Decoder
import aiortc
import numpy
import requests
import timezonefinderL
import uvicorn
import pi_sat_controller.backend.app
import pi_sat_controller.backend.run_server
PY
}

pi_sat_install_dependencies() {
  dependency_python="$1"
  pi_sat_check_python "${dependency_python}" || return 1
  if ! pi_sat_is_virtual_environment "${dependency_python}"; then
    echo "Refusing to install Pi-Sat dependencies into system Python." >&2
    echo "Run the installer or updater to prepare the Pi-Sat virtual environment." >&2
    return 1
  fi

  echo "Installing dependencies using ${dependency_python}"
  "${dependency_python}" -m pip install -r requirements.txt || return 1
  pi_sat_verify_environment "${dependency_python}"
}

pi_sat_prepare_virtual_environment() {
  base_python="$1"
  environment_dir="$2"
  environment_python="${environment_dir}/bin/python"

  pi_sat_check_python "${base_python}" || return 1
  if [ -e "${environment_dir}" ] && [ ! -d "${environment_dir}" ]; then
    echo "Pi-Sat virtual environment path is not a directory: ${environment_dir}" >&2
    return 1
  fi

  if [ -x "${environment_python}" ]; then
    if ! pi_sat_is_virtual_environment "${environment_python}"; then
      echo "Refusing to reuse an environment that is not isolated from system packages: ${environment_python}" >&2
      return 1
    fi
  else
    echo "Creating Pi-Sat virtual environment at ${environment_dir}"
    "${base_python}" -m venv "${environment_dir}" || return 1
  fi

  pi_sat_check_python "${environment_python}" || return 1
  if ! pi_sat_is_virtual_environment "${environment_python}"; then
    echo "Virtual environment creation did not produce an isolated Python: ${environment_python}" >&2
    return 1
  fi
  if ! "${environment_python}" -m pip --version >/dev/null 2>&1; then
    echo "Bootstrapping pip in ${environment_dir}"
    "${environment_python}" -m ensurepip --upgrade || return 1
  fi
  pi_sat_install_dependencies "${environment_python}"
}

pi_sat_wait_service_active() {
  service_name="${1:-pi-sat}"
  attempt=0
  while [ "${attempt}" -lt 4 ]; do
    sleep 1
    if ! sudo systemctl is-active --quiet "${service_name}"; then
      return 1
    fi
    attempt=$((attempt + 1))
  done
  return 0
}
