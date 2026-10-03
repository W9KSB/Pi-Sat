# SSTV Decoder

Pi-Sat's optional SSTV module consumes a bounded copy of the native IC-9700
audio stream. It never opens a radio socket, starts a second Icom session, or
sends a CI-V command. With stereo native audio, the tap selects the right-hand
SUB/RX channel; mono PCM is passed through unchanged. The browser Radio audio
subscriber remains independent.

The IC-9700 stereo stream is two receiver channels, not a stereo SSTV image.
`slowrx` receives one signed 16-bit mono stream. Pi-Sat therefore maps MAIN to
the left native channel and SUB/RX to the right native channel, then extracts
SUB/RX before decoding. No stereo down-mix is used, so audio from the other
receiver cannot contaminate the SSTV signal.

The module is disabled whenever the Pi-Sat backend starts. Enabling it starts a
backend consumer. Once native audio is available, that consumer starts one
persistent `pi-sat-sstv-decoder` process and writes raw signed 16-bit
little-endian mono PCM to its standard input. The bundled worker links to the
`slowrx.rs` Rust library directly; it is not a per-file or per-transmission CLI.
Disabling the module removes only this audio subscriber and stops the worker.

Connect the native radio session before enabling the decoder. Use normal AF
receive audio with **External DATA Decode disabled**. Built-in SSTV is a passive
AF consumer; External DATA Decode switches the shared LAN source to IF for
wider-bandwidth software demodulation. **Do not use both modules at the same
time.** See [External DATA Decode and Windows Connector](rx-audio-stream.md)
for external decoder operation.

The built-in worker requires a supported VIS header at the start of a
transmission. Enable it before that header; it does not automatically acquire
an image from a mid-transmission fragment. Browser PCM/WebRTC playback selection,
browser volume, and browser mute do not affect its native audio input. No
Windows connector or virtual audio device is required for built-in decoding.

Decoder events travel to `/api/sstv/events` over a dedicated WebSocket. VIS,
status, decoded-line, and completion events update the Modules > SSTV Decoder
page. The backend continues consuming and saving images without any browser
connected.

## Decode a recording

The SSTV Decoder page accepts an MP3 or WAV recording. The browser streams the
file bytes directly to Pi-Sat; the filename is sent separately so the endpoint
does not require a multipart parser. Pi-Sat writes the upload to a temporary
file, uses the installed `ffmpeg` executable to normalize it to 16 kHz signed
16-bit mono PCM, and feeds that PCM to the same bundled `slowrx.rs` worker used
for live radio audio. The temporary upload is removed after processing whether
decoding succeeds or fails. A successful decode is saved in `data/sstv/` and
appears in the gallery with `source: "uploaded"`.

The upload worker is independent of the persistent live-radio worker, so a
recording decode cannot replace or reset the live receiver's VIS search state.
Only one file decode is accepted at a time.

The live canvas is updated from `line_decoded` events as slowrx emits them.
The engine buffers approximately a full image-duration before synchronization
and pixel decoding, so lines normally arrive in a burst rather than appearing
continuously during reception. **SSTV detected** means that a VIS header was
recognized and audio is being collected; **Decoding** means that pixel lines
are being returned. A zero-line display during collection is not by itself a
worker failure. Falling audio level alone does not end or reset the decode.

Pi-Sat checks the sequence number in each native audio envelope. A short loss
is never replaced with fabricated samples: the missing span is skipped, the
current image is retained, and the image is labeled `degraded` with an
interference count. A larger loss ends the current decode at the last valid
boundary, saves a degraded partial image when pixels exist, and restarts slowrx
so the next image begins with a fresh VIS search. The UI reports both cases.
Buffered audio that has not yet produced pixels cannot be saved as a partial
image by the manager. A local queue overflow can also force a restart after all
rows have arrived but before the completion event is handled. Such a save can
be labeled degraded even with every row present. The label reports loss or
partial completion, not a visual quality score.

Sequence-gap counters describe the local audio path; they do not account for
every possible radio-to-Pi network loss.

## Decoder console and input recording

The scrolling **Decoder Console** below the images shows startup, detection,
line delivery, saves, errors, and periodic input statistics. **Follow** follows
new entries; **Clear** clears only the displayed entries. The console contains
live events from the current page session, not historical service logs. Use
`journalctl -u pi-sat -n 100 --no-pager` for recent service logs.

Input statistics distinguish received packet/sample counts, trimmed decoder
peak level, latest-packet RMS level, local sequence gaps, and queue drops. The
RMS value is for the latest packet, not an interval-wide average. Noise or
non-SSTV audio can produce continuous packets without a valid VIS detection.

**Download decoder audio** returns up to the last three minutes of audio sent
to the worker, after SUB extraction and decoder gain. The WAV contains mono
signed 16-bit PCM at the input sample rate; it is independent of browser audio.
The memory buffer replaces its oldest audio when full. Disabling retains it;
re-enabling or restarting Pi-Sat clears it. Download promptly after a failed
transmission to retain its beginning, and review the audio before sharing it.

## Decoder input level

The decoder input slider in the module toolbar trims the level of this module's
own copy of the receive audio between -30 and +12 dB, and persists it as
`[sstv] rx_gain_db`. The trim is confined to the stream handed to slowrx: the
radio, the browser playback level, and every other audio consumer, including the
APRS decoder, keep their own levels. The `Decoder level` readout shows the
trimmed level the decoder actually receives.

## Persistence

Completed PNGs and JSON metadata sidecars are stored under `data/sstv/`. The
metadata records UTC time, mode, decode status, quality, interference count,
current SUB frequency, and the selected tracking satellite when those values
are available. The gallery is
loaded from disk after application restarts. After each save, Pi-Sat removes
the oldest image and sidecar pairs beyond the newest 25.

Each saved image also contains a small `diagnostic` object. Pi-Sat compares the
line-by-line progressive RGB buffer with slowrx's final RGB buffer and records
the number of changed pixels/channels, missing progressive lines, and changed
right-edge pixels. These measurements help diagnose rendering differences
without altering the final decoded image.

## Decoder timing

`slowrx` 0.5.3 performs VIS detection, synchronization, and slant correction.
Pi-Sat renders each `line_decoded` event as it arrives. Audio collection and the
engine's whole-image processing must finish before that line batch is available.

## Decoder executable

The repository includes one stripped, statically linked AArch64 worker at
`bin/pi-sat-sstv-decoder`. The installer does not build or download a second
decoder. Set `PI_SAT_SSTV_DECODER` only when intentionally using an alternate
compatible worker.
