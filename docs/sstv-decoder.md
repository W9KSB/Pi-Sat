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
recording test cannot replace or reset the live receiver's VIS search state.
Only one file decode is accepted at a time.

The live canvas is updated from `line_decoded` events as slowrx emits them. A
station can therefore watch a received image build line by line. The exact
latency depends on slowrx's synchronization and slant-correction stage.

Pi-Sat checks the sequence number in each native audio envelope. A short loss
is never replaced with fabricated samples: the missing span is skipped, the
current image is retained, and the image is labeled `degraded` with an
interference count. A larger loss ends the current decode at the last valid
boundary, saves a degraded partial image when pixels exist, and restarts slowrx
so the next image begins with a fresh VIS search. The UI reports both cases.

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
Pi-Sat renders each `line_decoded` event immediately; if the engine withholds
lines while it finishes synchronization, the UI waits without inventing pixels.

## Decoder executable

The repository includes one stripped, statically linked AArch64 worker at
`bin/pi-sat-sstv-decoder`. The installer does not build or download a second
decoder. Set `PI_SAT_SSTV_DECODER` only when intentionally testing an alternate
compatible worker.
