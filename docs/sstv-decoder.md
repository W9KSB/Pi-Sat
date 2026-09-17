# SSTV Decoder

Pi-Sat's optional SSTV module consumes a bounded copy of the native IC-9700
audio stream. It never opens a radio socket, starts a second Icom session, or
sends a CI-V command. With stereo native audio, the tap selects the right-hand
SUB/RX channel; mono PCM is passed through unchanged. The browser Radio audio
subscriber remains independent.

The module is disabled whenever the Pi-Sat backend starts. Enabling it starts a
backend consumer. Once native audio is available, that consumer starts one
persistent `pi-sat-sstv-decoder` process and writes raw signed 16-bit
little-endian mono PCM to its standard input. The worker links to the
`slowrx` Rust library directly; it is not a per-file or per-transmission CLI.
Disabling the module removes only this audio subscriber and stops the worker.

Decoder events travel to `/api/sstv/events` over a dedicated WebSocket. VIS,
status, decoded-line, and completion events update the Modules > SSTV Decoder
page. The backend continues consuming and saving images without any browser
connected.

## Decoder input level

The decoder input slider in the module toolbar trims the level of this module's
own copy of the receive audio between -30 and +12 dB, and persists it as
`[sstv] rx_gain_db`. The trim is confined to the stream handed to slowrx: the
radio, the browser playback level, and every other audio consumer, including the
APRS decoder, keep their own levels. The `Decoder level` readout shows the
trimmed level the decoder actually receives.

## Persistence

Completed PNGs and JSON metadata sidecars are stored under `data/sstv/`. The
metadata records UTC time, mode, decode status, current SUB frequency, and the
selected tracking satellite when those values are available. The gallery is
loaded from disk after application restarts. After each save, Pi-Sat removes
the oldest image and sidecar pairs beyond the newest 25.

## Decoder timing

`slowrx` 0.5.3 performs whole-image sync and slant correction before it emits
decoded image lines. VIS detection and decoder state are live, but image pixels
appear after the RF transmission completes. Pi-Sat renders each available line
immediately.

## Decoder executable

The installer builds the worker and copies it to `bin/pi-sat-sstv-decoder`.
Set `PI_SAT_SSTV_DECODER` to use an executable at another path.
