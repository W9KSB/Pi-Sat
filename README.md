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

I had a few use cases for myself that existing software was not doing for me, so I decided to make this for my own use and share it freely. I have zero plans to monitize any of this. If you want to try it, feel free to do so. Report issues, make suggestions for features, and submit code improvements as well if you'd like!

Want more infomation about how you can get started? Be sure to check out the wiki: [Pi-sat Controller Wiki](https://github.com/W9KSB/Pi-Sat/wiki) 

<p align="center">
  <img alt="Dashboard" src="https://www.w9ksb.com/wp-content/uploads/2026/06/Dashboard.jpg">
</p>

## Features

- Live pass tracking with map and pass arc display
- Doppler-aware RX/TX tuning
- SDR, radio, and rotator control
- Multi-source TLE loading and merge handling
- Satellite profile management
- Monitor page with backend event logging
- Systemd-based Pi service install and update flow

## Software Requirements

- Raspberry Pi OS or another Debian-based Linux environment
- Raspberry Pi 3B and above tested
- Python 3 with `venv`
- `git`
- Hamlib utilities through `libhamlib-utils`
  - `rigctl`
  - `rigctld`
  - `rotctl`
  - `rotctld`

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
- installs the `pi-sat` systemd service
- starts the service

After install, open the Pi in a browser on your local network.

### Useful Service Commands

```sh
sudo systemctl status pi-sat
sudo systemctl restart pi-sat
journalctl -u pi-sat -f
```

## Manual Control
Be sure to check out the wiki if you want more manual control in terms of installing or updating. The whole project is open source as well if you need to change anything and personalize your setup.

## AI Usage
This always comes up so I want to be upfront. I've been coding with python for 8-10 years or so now and have had many fun projects as part of my hobbies. That being said, I do use AI to assist with tasks and productivity. A couple of examples are documentation and the gui interface. I'll admit it, I do not have an artistic bone in my body, so helping with visuals is a great use for me. That being said, I am transparent with this code - it's all open source. You're free to evaluate any functionality as you wish and customize as you please.


## Credits

### Software and Libraries

- [Hamlib](https://hamlib.github.io/) for radio and rotator control interfaces
- [Skyfield](https://rhodesmill.org/skyfield/) for orbital calculations and pass prediction


### Data Sources

- [CelesTrak](https://celestrak.org/) for TLE data used by the application

Pi-Sat depends on these projects and data sources for core functionality. Thanks to them for the outstanding work they have done for years in this space and wish them continued success.
