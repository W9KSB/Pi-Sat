# Radio Audio and Scope Performance

Pi-Sat keeps receive audio close to live operation by bounding queues in the
backend and browser. CAT commands, scope rendering, and microphone traffic use
separate paths so routine control activity does not intentionally delay
listening audio.

## Receive audio buffering

Browsers with AudioWorklet support use a persistent audio renderer for PCM
decoding, sample-rate conversion, buffering, and MAIN/SUB/Both routing. The
renderer targets about 45 ms of buffered audio and trims a large burst back to
that target when the backlog would exceed 110 ms. Backend listener queues retain
at most about 120 ms of PCM and reject stale packets.

The Radio page reports:

| Readout | Meaning |
| --- | --- |
| Buffer | Audio currently queued in the browser. |
| Gaps | Starvation events that forced playback to rebuffer. |
| Trimmed | Buffered audio discarded to restore bounded latency. |
| PCM level | Peak level in the selected receive stream. |

These values describe browser buffering and PCM activity. They are not an
end-to-end latency measurement and do not indicate RF signal strength.

Browsers without AudioWorklet use a playback fallback and do not provide the
same low-latency buffer counters. Worklet loading errors appear in the console
status. Reload the page after a Pi-Sat update so the browser loads the current
JavaScript and worklet files.

## Spectrum and waterfall cadence

The IC-9700's CI-V scope output determines the available waterfall cadence. A
complete 475-bin sweep arrives only as often as the radio and configured CI-V
bandwidth provide it. Pi-Sat draws each complete sweep but cannot create
additional radio data.

The spectrum toolbar's **Smooth** option changes display motion only:

- Waterfall rows move by a fractional offset between incoming sweeps, using the
  measured sweep interval.
- The spectrum trace uses a three-point average to soften bin-to-bin noise.

Smooth does not invent sweeps, alter amplitudes, or send radio commands. Turning
it off restores whole-row waterfall steps. Display level, fill, grid, and max
hold are also browser-only controls.

## Microphone traffic

Microphone capture streams while the radio session is connected. PTT still
controls whether audio reaches the transmitter, so an idle session does not
transmit. At a 16 kHz radio rate, microphone traffic is approximately
256 kbit/s; stereo receive audio is approximately 512 kbit/s.

The microphone level meter shows captured browser audio. If the uplink is
interrupted, Pi-Sat reconnects it while capture is active and records the event
in Monitor. A key held during the interruption is released. Receive audio uses
a separate connection and should remain available.

## Troubleshooting dropouts or delay

1. Connect to the radio and note the Buffer, Gaps, Trimmed, and PCM level
   readouts while listening normally.
2. If **Gaps** rises, check Wi-Fi quality, browser CPU load, radio connectivity,
   and whether the selected MAIN/SUB channel contains audio.
3. If **Trimmed** rises, the browser received a burst after falling behind.
   Hidden or heavily throttled tabs can contribute to this.
4. If Buffer remains high after activity settles, reload the page and confirm
   AudioWorklet is active.
5. Compare MAIN, SUB, and Both. SUB and Both require stereo audio and an active
   SUB receiver.
6. Review `journalctl -u pi-sat -n 100 --no-pager` for radio, audio, or socket
   errors.

When reporting a problem, include the selected listening side, configured sample
rate, connection type, browser, and the four audio readouts.

## Limitations

Pi-Sat cannot remove buffering inside the radio, network loss, TCP
head-of-line blocking, browser scheduling delays, or the output device's own
buffer. Stale audio is discarded after a stall to return playback to live
operation. Smoothing changes presentation only and cannot increase the radio's
scope information rate.
