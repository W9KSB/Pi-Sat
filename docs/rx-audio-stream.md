# RX Audio Stream

Pi-Sat can expose the receive audio it already has in memory to an external
decoder or audio adapter. The stream is deliberately passive:

- It carries mono signed 16-bit PCM from the existing SUB/RX receive fan-out.
- It never opens a second radio connection.
- It never sends CI-V commands, tunes, keys PTT, changes tracking, or changes
  the browser's audio.
- The listener is enabled only by the `[audio_stream]` section in the Pi-Sat
  configuration file and remains enabled across reboots.
- The listener is considered active when one or more external clients are
  connected; no client means no audio work beyond the idle subscriber loop.

Example configuration:

```ini
[audio_stream]
enabled = true
bind_host = 0.0.0.0
port = 8765
channel = right
```

The module page reports the bind address, port, connected clients, frame and
byte counters, refused connections, queue overflow drops, and any startup or
runtime error. The Windows connector is in `external-streamer/`; it provides a
server field, output-device selector, Start and Stop controls, a one-second
playback buffer, and sequence-gap statistics.
