#!/usr/bin/env sh

pi_sat_service_python() {
  service_exec="$(systemctl show --property=ExecStart --value "${1:-pi-sat}")" || return 1
  service_command="${service_exec#"{ path="}"
  if [ "${service_command}" = "${service_exec}" ]; then
    echo "Cannot determine the Python executable configured for the Pi-Sat service." >&2
    return 1
  fi
  service_python="${service_command%%" ; argv[]="*}"
  if [ "${service_python}" = "${service_command}" ] || [ ! -x "${service_python}" ]; then
    echo "The Pi-Sat service Python executable is unavailable: ${service_python}" >&2
    return 1
  fi
  printf '%s\n' "${service_python}"
}

pi_sat_check_python() {
  if ! "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Pi-Sat requires Python 3.11 or newer for SSTVFlow." >&2
    return 1
  fi
}

pi_sat_install_dependencies() {
  dependency_python="$1"
  pi_sat_check_python "${dependency_python}" || return 1
  echo "Installing dependencies using ${dependency_python}"
  if "${dependency_python}" -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)'; then
    "${dependency_python}" -m pip install -r requirements.txt || return 1
  else
    sudo "${dependency_python}" -m pip install --break-system-packages -r requirements.txt || return 1
  fi
  "${dependency_python}" -c 'from PIL import Image; from sstv_decoder.decoder import Decoder' || return 1
}
