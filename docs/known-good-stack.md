# Known Good Stack

Initial target:

- Raspberry Pi 5
- Raspberry Pi OS Trixie
- Python 3.11 or newer
- Hamlib 4.6.2 or newer through `rigctld` (4.6 is the minimum for optional pushed radio-state updates)
- SDR++ rigctl-compatible control

Pi-Sat does not automatically upgrade Hamlib. Older versions and backends without generic async support continue using polling.

Pin exact OS package versions after first hardware validation on the Pi.
