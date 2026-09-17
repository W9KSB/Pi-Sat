# Native IC-9700 Connectivity

Pi-Sat connects to the IC-9700 over the radio's LAN interface. Native control,
spectrum, receive audio, microphone audio, and PTT share one managed radio
session.

## Connection ownership

Enable the native IC-9700 connector only when Pi-Sat is the sole application
controlling that radio connection. While native control is enabled, other
Hamlib connectors in Pi-Sat are unavailable. Do not run another controller
against the same radio session.

The Radio page opens the connection only when the operator selects **Connect**.
Changing configuration or starting Pi-Sat does not connect automatically.

## Receive and microphone audio

Listening audio and microphone audio use separate browser connections. A
microphone interruption does not stop receive audio, and an RX connection
failure does not block the microphone path.

The browser sends microphone audio while capture is active, but Pi-Sat forwards
it to the radio only while PTT is commanded. Releasing PTT discards buffered
microphone audio so it cannot carry into the next transmission. A paced digital
transmission, such as an APRS burst, has exclusive use of the transmit buffer.

The LAN transmit stream uses 20 ms mono LPCM16 frames at the configured sample
rate. The radio's advertised audio capability and the configured rate are shown
in the log when the connection starts. Review warnings about unavailable
transmit audio before changing the rate.

## PTT safety

Pi-Sat confirms key-up with the radio and releases PTT when the microphone
session ends, the browser leaves, the radio connection closes, or Pi-Sat shuts
down. If the radio cannot confirm release, the console reports
`tx_unconfirmed`. Treat that state as a possible live carrier and check the
radio directly.

These safeguards do not replace the radio's own transmit timeout. Do not use
browser-controlled PTT for unattended transmission.

## Radio audio configuration

The IC-9700 selects its modulation source separately from PTT. For browser or
APRS transmit audio, set both `DATA OFF MOD` and `DATA MOD` to LAN and choose an
appropriate LAN modulation level on the Radio page. Pi-Sat reads these settings
before an APRS transmission and refuses to key when they would produce a silent
carrier.

Receive audio can be MAIN, SUB, or Both. Stereo native audio carries MAIN on the
left and SUB on the right. SUB and Both require a stereo stream. Listening
selection also controls which side the spectrum follows; it does not change the
radio's operating MAIN/SUB or A/B selection.

## Troubleshooting

- If control works but receive audio is silent, confirm the listening side,
  Dualwatch state, radio AF gain, and the configured sample rate.
- If PTT works but transmitted audio is silent, confirm the radio's LAN
  modulation source and LAN modulation level.
- If the console reports `tx_unconfirmed`, release PTT at the radio and restore
  the connection before transmitting again.
- If commands time out, make sure no other application owns the radio session
  and review `journalctl -u pi-sat -n 100 --no-pager`.
- If audio is intermittent while control remains connected, use the audio
  buffer counters described in [Radio Audio and Scope Performance](radio-audio-latency.md).

## Limitations

- Native IC-9700 connectivity is LAN-only.
- One Pi-Sat native session owns radio control, scope, audio, and PTT.
- Split operation and SAT mode are not enabled by the native console.
- There is no hardware watchdog independent of the radio and Pi-Sat.
