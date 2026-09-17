# Supported Platform

Pi-Sat is intended for the following environment:

- Raspberry Pi 5
- Raspberry Pi OS Trixie
- Python 3.11 or newer
- Hamlib 4.6.2 or newer through `rigctld`
- SDR++ rigctl-compatible control

Hamlib 4.6 is the minimum version for optional pushed radio-state updates.
Pi-Sat does not upgrade Hamlib automatically. Radios or Hamlib backends without
generic asynchronous state support use normal polling.
