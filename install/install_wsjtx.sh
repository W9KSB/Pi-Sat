#!/usr/bin/env sh
set -eu

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this installer as the Pi-Sat service user, without sudo."
  exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo "Install stock WSJT-X, pulseaudio-utils, Xvfb, x11-utils, libx11-6, libxtst6, at-spi2-core, and dbus with your distribution's package manager."
  exit 1
fi

sudo apt-get update
sudo apt-get install -y --no-remove wsjtx pulseaudio-utils xvfb x11-utils libx11-6 libxtst6 at-spi2-core dbus

if pactl info >/dev/null 2>&1; then
  echo "WSJT-X dependencies are installed and this user can reach the audio server."
  exit 0
fi

if ! command -v systemctl >/dev/null 2>&1 || ! command -v loginctl >/dev/null 2>&1; then
  echo "Automatic headless audio setup requires systemd and loginctl."
  echo "Configure a PulseAudio-compatible server for the Pi-Sat service user, then rerun this installer."
  exit 1
fi
if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "Cannot reach this user's systemd manager. Log in directly as the Pi-Sat service user and rerun."
  exit 1
fi

pipewire_state=$(systemctl --user show pipewire-pulse.service --property=LoadState --value 2>/dev/null || true)
if [ -n "$pipewire_state" ] && [ "$pipewire_state" != "not-found" ]; then
  echo "Starting the existing PipeWire audio services."
  sudo loginctl enable-linger "$(id -un)"
  systemctl --user daemon-reload
  systemctl --user enable --now pipewire.socket pipewire-pulse.socket
else
  echo "Installing and starting the headless PulseAudio user service."
  sudo apt-get install -y --no-remove pulseaudio
  sudo loginctl enable-linger "$(id -un)"
  systemctl --user daemon-reload
  systemctl --user enable --now pulseaudio.socket pulseaudio.service
fi

if ! pactl info >/dev/null 2>&1; then
  echo "Audio service setup completed, but this user still cannot reach the audio server."
  echo "Check: systemctl --user status pulseaudio.service pipewire-pulse.service --no-pager"
  echo "Check: journalctl --user -u pulseaudio.service -u pipewire-pulse.service -n 40 --no-pager"
  exit 1
fi
echo "WSJT-X dependencies and the headless audio server are ready."
