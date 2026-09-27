# Pi-Sat RX Audio Stream Connector

This Windows x64 connector subscribes to Pi-Sat's passive TCP audio stream and
writes its signed 16-bit PCM, mono or stereo, to a selected installed output device. It
does not control the radio, issue CI-V commands, or change Pi-Sat state.

Run `external-streamer.exe`, enter the Pi-Sat server address and port, choose a
Windows output device, then use Start and Stop. The receiver keeps up to one
second of PCM queued so short scheduling delays do not interrupt playback. The
window reports frame counts, sequence gaps, byte counts, and local buffer drops.

The Python reference source is in `src/`. To rebuild on Windows, install the
packages from `requirements.txt` and run `build.ps1` from this directory.
