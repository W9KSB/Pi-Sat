# Pi-Sat Satellite Communications Controller

<p align="center">
  Browser-based satellite tracking, radio control, SDR control, and rotator control for Raspberry Pi.
</p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Raspberry%20Pi-darkgreen">
  <img alt="Runtime" src="https://img.shields.io/badge/runtime-Python%203-blue">
  <img alt="UI" src="https://img.shields.io/badge/interface-Web%20UI-1f6feb">
  <img alt="Control" src="https://img.shields.io/badge/hamlib-RX%20%7C%20TX%20%7C%20Rotator-orange">
</p>

Pi-Sat is a local web control surface for satellite operations. It combines pass prediction, live tracking, Doppler-aware RX/TX tuning, SDR coordination, and rotator control into one Pi-hosted interface designed for actual operating use rather than just passive monitoring.

The Raspberry Pi owns the backend, device control, and tracking logic. The browser is the operator console.

Setup and operating guidance is available in the [Pi-Sat Controller Wiki](https://github.com/W9KSB/Pi-Sat/wiki).
Hardware support depends on the selected Hamlib backend and device. Report
compatibility problems or hardware-specific behavior in
[GitHub Issues](https://github.com/W9KSB/Pi-Sat/issues).

<p align="center">
  <img alt="Dashboard" src="https://www.w9ksb.com/wp-content/uploads/2026/06/Dashboard.jpg">
</p>

## Features

- Live pass tracking with map and pass arc display
- Doppler-aware RX/TX tuning
- SDR, radio, and rotator control
- Native IC-9700 CI-V control and bidirectional audio over LAN connectivity
- Multi-source TLE loading and merge handling
- Satellite profile management
- Monitor page with backend event logging
- Optional live SSTV decoding from the existing native IC-9700 SUB/RX audio stream
- Optional APRS decoding from a selectable side of the native IC-9700 RX audio
  stream, plus manual APRS transmit of a beacon, status line, or message
- Systemd-based Pi service install and update flow

## Software Requirements

- Raspberry Pi OS or another Debian-based Linux environment
- Raspberry Pi 3B or newer
- Python 3 with `venv`
- `git`
- Rust 1.85 or newer and Cargo (the installer uses the OS packages and verifies the version)
- Dire Wolf for the optional APRS receiver and `gen_packets` transmit modulator
  (installed by the installer, or built from source)
- Hamlib utilities through `libhamlib-utils`
  - `rigctl`
  - `rigctld`
  - `rotctl`
  - `rotctld`

Hamlib 4.6 or newer can reduce radio read traffic by publishing generic asynchronous state updates. This optimization is optional: Pi-Sat detects the installed version and selected backend at runtime and retains normal polling when it is unavailable. Raspberry Pi OS Trixie's Hamlib 4.6.2 package is supported. See [Radio State Updates](docs/radio-state-updates.md) for configuration and troubleshooting.

## Quick Install

Run this on the Pi as the normal user:

```sh
curl -fsSL https://raw.githubusercontent.com/W9KSB/Pi-Sat/main/install/install_pi.sh | sh
```

### What the installer does

- clones or updates the repo into `~/pi-sat`
- creates `pi-sat-controller.conf` from the example template if needed
- creates `update_pi.sh` from `updater.template` if needed
- creates `.venv`
- installs Python dependencies from `requirements.txt`
- grants the service user serial-device access for local USB/serial radios and rotators
- builds the persistent `slowrx.rs` SSTV decoder worker
- installs Dire Wolf for the optional APRS receiver
- installs the `pi-sat` systemd service
- starts the service

After install, open `https://<PI-IP>/` in a browser on your local network.
Pi-Sat defaults to HTTPS on port 443.
On first start it generates a self-signed certificate if none exists, then reuses
it on subsequent starts. Browser certificate acceptance/trust and microphone
permission are still required. See [HTTPS setup](docs/https.md) for certificates,
custom ports, and the explicit HTTP option for reverse proxies.

### Useful Service Commands

```sh
sudo systemctl status pi-sat
sudo systemctl restart pi-sat
journalctl -u pi-sat -f
```

## Manual Installation and Updates

See the [Pi-Sat Controller Wiki](https://github.com/W9KSB/Pi-Sat/wiki) for
manual installation, update, and customization guidance.


## Credits

### Software and Libraries

- [Hamlib](https://hamlib.github.io/) for radio and rotator control interfaces
- [Skyfield](https://rhodesmill.org/skyfield/) for orbital calculations and pass prediction
- [slowrx.rs](https://github.com/jasonherald/slowrx.rs), based on slowrx by Oona Räisänen (OH2EIQ), for SSTV decoding
- [Dire Wolf](https://github.com/wb2osz/direwolf) by John Langner (WB2OSZ) for APRS and AX.25 decoding

Full third-party notices for the SSTV decoder and the APRS receiver are preserved in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).


### Data Sources

- [CelesTrak](https://celestrak.org/) for TLE data used by the application

Pi-Sat depends on these projects and data sources for core functionality. Thanks to them for the outstanding work they have done for years in this space and wish them continued success.
