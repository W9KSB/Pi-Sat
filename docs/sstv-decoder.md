# SSTV Decoder

Pi-Sat's optional SSTV module uses SSTVFlow 0.1.1 to decode **Robot24**,
**Robot36**, **Robot72**, **Martin M1/M2**, and **PD120/PD180** from a dedicated
copy of the native IC-9700 audio stream. The module never opens a radio socket,
starts a second Icom session, or sends a CI-V command. The browser Radio audio subscriber
remains independent.

The IC-9700 stereo stream contains two receiver channels. Pi-Sat maps MAIN to
the left native channel and SUB/RX to the right channel, then extracts SUB/RX
before decoding. No stereo down-mix is used, so MAIN audio cannot contaminate
the SSTV signal. Mono PCM is passed through unchanged.

## Live reception

The module starts disabled whenever the Pi-Sat backend starts. Connect the
native radio session, open **Modules > SSTV Decoder**, and select **Enable**.
Once native audio is available, the backend starts a persistent Python decoder
worker and supplies signed 16-bit little-endian mono PCM at the native sample
rate. Disabling stops new reception, drains accepted audio, and lets the worker
finish its end-of-input result. If audio is queued, this can take time.

Use normal AF receive audio with **External DATA Decode disabled**. Built-in
SSTV is a passive AF consumer; External DATA Decode switches the shared LAN
source to IF for wider-bandwidth software demodulation. **Do not use both
modules at the same time.** See
[External DATA Decode and Windows Connector](rx-audio-stream.md) for external
decoder operation.

SSTVFlow can recognize a VIS header or acquire a supported transmission from
its repeated line structure after reception has already begun. Header-free
acquisition produces a partial image whose original vertical position is
unknown. A partial image can also result from interrupted reception after a
known beginning; that does not make its vertical position unknown.

When an image starts with the wider pixel filter, received sync references can
select narrower filtering during interference and restore the wider path after
sustained clean reception. Filter selection preserves sample positions and
the line clock. This happens automatically within the decoder; it does not
change the radio's AF stream or require another control.

Adaptive pixel estimation and automatic noise reduction are enabled by default.
Clean channels bypass automatic noise reduction; noisy channels receive
signal-dependent treatment within the decoder.

Browser PCM/WebRTC playback selection, browser volume, and browser mute do not
affect native decoder input. No Windows connector or virtual audio device is
required. Once enabled, the backend continues receiving and saving images
without a browser connected.

## Progressive images and audio loss

Decoder events travel to `/api/sstv/events` over a dedicated WebSocket.
Acquisition, status, decoded rows, revisions, and image saves update the live
canvas and console. Rows appear progressively during reception. Some rows are
provisional until more color information arrives; the decoder can replace an
earlier row with a revised version. A revision repaints that row without
counting it again or reducing reception progress.

SSTV queues every received packet in order without discarding old audio when
decoding falls behind. This input queue grows in RAM when processing stalls.
Diagnostic recordings are written to disk by an independent writer.
**Queued audio** reports the backlog in seconds; sustained growth means
the decoder is not keeping up with reception. Storage errors appear under
**Audio capture** instead of silently discarding audio.

Pi-Sat checks sequence numbers in native audio envelopes. Missing audio is
never replaced with fabricated samples. A reported missing span closes the
current decoder segment, saves a degraded partial image
when recoverable pixels exist, and reacquires from subsequent audio. Gap
notifications remain ordered with the audio supplied to the decoder.
Sequence-gap counters describe the local audio path; they do not account for
every possible radio-to-Pi network loss. Buffering cannot recover UDP packets
lost before Pi-Sat receives them.

Protocol timing follows received sample positions. A pause in packet delivery
does not itself represent RF silence or end a transmission. Signal loss in
the received audio, a new transmission, a discontinuity, or end of input can
finish a recoverable partial image. Disabling allows the worker to process its
remaining input and return an end-of-input result before shutdown.

## Decode a recording

The page accepts an MP3 or WAV recording. Pi-Sat writes the uploaded bytes to
a temporary file, uses `ffmpeg` to normalize the recording to 16 kHz signed
16-bit mono PCM, and supplies it to a separate SSTVFlow worker. The temporary
upload is removed whether decoding succeeds or fails. Saved images appear in
the gallery with `source: "uploaded"`.

The upload worker is independent of the persistent live-radio worker; decoding
a recording cannot replace or reset live reception. Only one file decode is
accepted at a time. Recordings without a VIS header can produce partial images
when enough supported line structure is present.

## Decoder console and input recording

The scrolling **Decoder Console** shows startup, acquisition, row delivery,
revisions, saves, errors, and periodic input statistics. **Follow** follows new
entries; **Clear** clears only displayed entries. The console contains live
events from the current page session, not historical service logs. Use
`journalctl -u pi-sat -n 100 --no-pager` for recent service logs.

Input statistics distinguish received packet/sample counts, trimmed decoder
peak level, latest-packet RMS level, local sequence gaps, and queued audio. RMS
is for the latest packet, not an interval-wide average. Noise or non-SSTV audio
can produce continuous packets without a valid acquisition.

Audio recordings cover the session since **Enable** and are stored on disk.
They remain available after **Disable** until the next **Enable** or backend
restart. Downloads are snapshots taken when requested; download after disabling
and draining reception to capture the entire session.

* **Download SUB audio** exports mono signed 16-bit PCM after SUB extraction,
  before the decoder gain adjustment.
* **Download decoder audio** exports the exact post-gain PCM supplied to the
  decoder. While audio is queued, this recording can end earlier than the SUB
  source recording.
* **Download audio diagnostics** exports JSONL metadata for the capture,
  including packet, gain, and discontinuity information. Keep it with both WAVs
  and the decoded PNG to compare a transmission through different audio paths.

Both WAVs use the native input sample rate and are independent of browser
playback. The AF path has no resampling, compression, AGC, or filtering before
SSTVFlow; only SUB extraction and the decoder gain adjustment apply. A session
requires a stable sample rate: a rate change reports a capture error rather
than creating a WAV with inconsistent timing. Review recordings before sharing
them.

Recordings grow while SSTV is enabled and are replaced at the next Enable.
Disable after a diagnostic session to stop recording and finish its queued
audio. Radio sequence diagnostics separately report forward gaps, duplicates,
and out-of-order arrival. A forward gap is an observed skipped sequence span;
a late packet can still arrive afterward, so it is not proof of permanent loss.

## Decoder input level

The toolbar slider trims this module's copy of receive audio between -30 and
+12 dB, and persists the value as `[sstv] rx_gain_db`. The radio, browser
playback, and other consumers including APRS keep their own levels. The
**Decoder level** readout shows the trimmed level SSTVFlow receives.
**Clipped samples** counts samples saturated by the gain adjustment during the
session; increasing gain scales signal and noise together.

## Persistence

Completed and recoverable partial PNGs and JSON metadata sidecars are stored
under `data/sstv/`. Metadata records UTC time, mode, decode status, quality,
interference count, acquisition details, reception diagnostics, current SUB
frequency, and selected tracking satellite when available. The gallery reloads
from disk after restarts. After each save, Pi-Sat removes the oldest image and
sidecar pairs beyond the newest 25. Existing image downloads and configured
Discord image notifications also apply to the replacement decoder.

The decoder's final PNG is authoritative and includes the newest row revisions.
Pi-Sat compares the progressive canvas buffer with the final image and records
changed pixels/channels, missing progressive rows, and changed right-edge
pixels in the metadata's `diagnostic` object. These measurements describe
rendering differences; they do not change the decoded image. A complete image
can still contain missing or low-quality signal regions. Completion and
degraded labels are reception status, not a visual quality score.

## Runtime

The repository bundles a separate SSTVFlow package snapshot in
`vendor/sstvflow`, including a prebuilt pure-Python wheel. Installing
`requirements.txt` installs that wheel without building in the checkout, along
with NumPy, SciPy, and Pillow. Python 3.11 or newer is required. Pi-Sat's Python
subprocess bridge imports the installed decoder and adapts its streaming
events to Pi-Sat's API. No Rust worker or Cargo build is required.

The bundled wheel version is `0.1.1+pisat.20261008`; the decoder reports
SSTVFlow 0.1.1. SSTVFlow's PolyForm Noncommercial License 1.0.0 and required
copyright notice are included in `vendor/sstvflow/LICENSE` and the wheel.

Dependencies must be installed into the Python environment running the
`pi-sat` service. If an existing installation uses `.venv/bin/python`, run
`.venv/bin/python -m pip install -r requirements.txt` from the Pi-Sat directory.
Installing into system Python does not update that virtual environment.
The updater reads the service's configured Python executable and installs into
that environment. Virtual-environment installs run as the invoking user;
system-Python installs use the existing host-level installation path. The
updater checks decoder imports before restarting the service.
