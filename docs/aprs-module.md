# APRS

Pi-Sat's optional APRS module decodes 1200 baud APRS packets from a bounded copy
of the native IC-9700 audio stream, and can transmit one operator-requested
frame at a time through the same native radio.

The module is disabled whenever the Pi-Sat backend starts. Enabling it starts a
backend consumer that subscribes to the native PCM fan-out, narrows the stream
to the MAIN channel (the left channel in stereo native audio), and writes raw
signed 16-bit little-endian mono PCM to the standard input of one persistent
`direwolf` process. Disabling the module removes only this audio subscriber and
stops that process.

The operator owns the frequency. Pi-Sat does not check that MAIN is parked on an
APRS channel, so a mis-set radio simply produces no packets while the module
keeps listening.

## Decoded traffic

Dire Wolf reports decoded AX.25 frames over its KISS TCP interface. Pi-Sat
connects to `127.0.0.1` on the configured KISS port (8001 by default), decodes
the AX.25 unnumbered-information frame, interprets the APRS information field,
and forwards one `packet` event per frame to `/api/aprs/events`.

The newest 25 packets are kept, newest first, in `data/aprs/packets.json`, so
the list survives a backend restart.

## Configuration

| Variable | Purpose |
| --- | --- |
| `PI_SAT_APRS_DIREWOLF` | Explicit path to the `direwolf` executable. |
| `PI_SAT_APRS_KISS_PORT` | KISS TCP port. Defaults to 8001. |
| `PI_SAT_APRS_MYCALL` | Callsign override used when `[aprs] mycall` is empty. |

Pi-Sat regenerates `data/aprs/direwolf.conf` at each start. The generated file
sets `ADEVICE stdin null`, naming both audio halves explicitly: the input is the
stdin PCM stream, and the output half is null because this host has no sound
card. Both halves have to be named, because a single `ADEVICE` name is copied to
the output half as well and Dire Wolf then refuses to start. The file also sets
one audio channel, the configured native sample rate, 1200 baud, `AGWPORT 0` to
disable the AGW interface, and no transmit path. Pi-Sat always connects to
`127.0.0.1`; check how the installed build binds the KISS port with
`ss -ltnp | grep <KISS port>` if you want to confirm it is not reachable from
your LAN.

The `[aprs]` section of `pi-sat-controller.conf` holds the transmit settings:

| Setting | Purpose |
| --- | --- |
| `mycall` | APRS callsign and SSID, for example `W9KSB-9`. Empty disables transmit. |
| `channel` | Which receiver the decoder listens to: `main` (left stereo side), `right`, or `both` (mono sum). |
| `destination` | APRS destination field. Defaults to `APDW16`. |
| `path` | Digipeater path. Defaults to `WIDE1-1,WIDE2-1`. |
| `symbol` | Two characters: symbol table then symbol code. Defaults to `/>`. |
| `comment` | Free text appended to a beacon. The last two characters may be a Maidenhead locator. |
| `tx_delay_ms` | Delay between key-up and the start of the burst. |
| `tx_tail_ms` | Delay between the end of the burst and key-down. |
| `amplitude` | Modulator amplitude in percent, `0`-`200`. |
| `min_interval_s` | Minimum seconds between transmissions. |

The beacon position is the `[station]` latitude and longitude. It is amended
only if you change it there; Pi-Sat never reads a position from the radio.

The stereo RX stream carries both receivers, and which side is MAIN is a radio
and transport property rather than something the Icom protocol guarantees.
`channel` therefore selects the decoder's side instead of assuming one. The
APRS page reports the peak level of each side, so the operator can see which
one carries traffic and set `channel` to match.

## Transmit

Transmit is manual and off by default. The APRS page's Transmit panel must be
armed before any button is enabled, and the backend disarms on restart so a
stopped or crashed process cannot keep transmitting.

One transmission works like this:

1. Pi-Sat builds a TNC2-format line, `MYCALL>DEST,PATH:info`, from the settings
   above. A beacon uses `[station]` for its position.
2. `gen_packets`, installed beside `direwolf`, modulates that line into one
   complete Bell 202 burst as a mono 16-bit WAV at the active transport's TX
   audio rate.
3. Pi-Sat asserts CI-V PTT, waits `tx_delay_ms`, streams the burst at real time
   through the shared native radio, waits `tx_tail_ms`, then releases PTT.

The PCM is sent over the radio's network audio channel at `[icom] sample_rate`.
The APRS module uses only the shared controller's PTT and audio calls, so the
transport stays inside the connectivity module.

Dire Wolf does not key the radio. Pi-Sat owns PTT, so Dire Wolf is generated a
configuration with no transmit path and only ever produces the audio.

The IC-9700 selects its modulation input separately from PTT: `DATA OFF MOD`
(used outside DATA mode) and `DATA MOD` choose between the microphone, the
accessory socket, USB and LAN, and there is a further LAN modulation level.
Pi-Sat's transmit audio is the LAN stream, so those settings must
name the LAN input. Before keying, Pi-Sat reads the radio back and
refuses the transmission with a specific message when the modulation input or
the LAN level would only produce a silent carrier. Set them on the Radio page
under microphone and transmit audio configuration.

Transmit and receive share one radio, so an operator should not beacon during a
tracked pass: the tracker's retune can interrupt a burst in progress.

## Diagnosing a silent decoder

The APRS page reports the whole receive chain, so a decode failure can be
localised before touching the radio:

| Readout | Meaning |
| --- | --- |
| State badge | `Waiting for radio audio` means no RX audio reached the decoder at all, which is a radio-session problem rather than a decoder one. `Decode failed` carries the reason. |
| Decoder input | The rate, channel count, selected side and sample count actually handed to Dire Wolf. A climbing sample count proves the feed is live. |
| Channel levels | Peak level of each stereo side. Traffic on one side and near-silence on the other tells you which `channel` to select. |
| Decoder level | Peak level after the decoder input trim, which is what Dire Wolf actually receives. |
| Decoder link | Whether Pi-Sat holds a KISS connection to Dire Wolf. `No connection` means Dire Wolf decoded nothing Pi-Sat can read, whatever the audio. |
| Decoder log | Dire Wolf's own output: startup banner, audio device, KISS listener, and every frame it decodes. |

The decoder log is the fastest discriminator. If it lists decoded frames, the
fault is on the Pi-Sat side of the KISS link. If it lists none, the audio
reaching Dire Wolf does not match what Dire Wolf expects.

Dire Wolf reports `Audio input level is too high` when its input is well above
half scale. The decoder input slider in the module toolbar trims that level
between -30 and +12 dB and persists it as `[aprs] rx_gain_db`. The trim is
applied only to this module's own copy of the receive audio: the radio, the
browser playback level, and every other consumer are untouched, and the
`Channel levels` readout still shows the untrimmed receiver level.

## Licensing

Dire Wolf is GPL-2.0-or-later. Pi-Sat runs it as a separate program and does not
link or copy its source, so Pi-Sat's own license is unaffected. Distributing a
Dire Wolf binary carries Dire Wolf's own GPL obligations. See
`THIRD_PARTY_LICENSES.md`.

## Known limitations

- Only the selected `[aprs] channel` is decoded. A satellite whose APRS
  downlink is on another receiver side will not be heard.
- Compressed APRS positions are reported as `Compressed position` without
  decoding the base-91 latitude and longitude.
- Transmit sends only what the operator asks for: a position beacon, a status
  line, or a one-line message. There is no automatic beacon interval, no
  digipeating, and no APRS-IS connection.
- Transmit timing depends on the radio's key-up behaviour. The default
  `tx_delay_ms` and `tx_tail_ms` are starting values, not measured ones.
