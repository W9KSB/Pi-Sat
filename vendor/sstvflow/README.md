# SSTVFlow

SSTVFlow 0.1.1 is an independent Python streaming SSTV decoder supporting
Robot24, Robot36, Robot72, Martin M1/M2, and PD120/PD180.
This directory contains the source snapshot distributed
with Pi-Sat. Its Python import name is `sstv_decoder`.

The bundled distribution version is `0.1.1+pisat.20261008`, identifying this
snapshot separately from earlier 0.1.1 wheels. The decoder reports upstream
version 0.1.1. SSTVFlow is licensed under the PolyForm Noncommercial License
1.0.0; see [LICENSE](LICENSE).

Install it through Pi-Sat's `requirements.txt`. The bundled `wheels/` artifact
contains pure Python code and installs without building in the checkout.
Python 3.11 or newer, NumPy, SciPy, and Pillow are required.

When updating this source snapshot, rebuild its wheel with
`python -m pip wheel --no-deps --wheel-dir wheels .` from this directory.

The standalone command `decoder RATE` reads signed 16-bit little-endian mono
PCM from stdin and writes newline-delimited JSON events to stdout. The sample
rate must match the audio. The Python interface is `Decoder(rate, emit)` with
`feed(pcm)`, `control(command)`, and `eof()` methods.

Reception automatically recognizes VIS or repeated line structure, estimates
frequency and slant correction, and emits RGB rows and replacement revisions.
Adaptive pixel estimation and automatic noise reduction are enabled by default.
Completion and partial-result events contain an authoritative lossless PNG.
Headerless results retain unknown original vertical placement. Protocol-length
completion does not certify that every row was received correctly.

Images acquired with the wider pixel filter can select the narrower filter
during interference and restore the wider path after sustained clean sync
references. The line clock and input sample timeline remain unchanged. An
acquisition-selected narrow filter remains in effect for the image.

For Pi-Sat operation, see `docs/sstv-decoder.md` in the repository root. Pi-Sat
owns radio connectivity, SUB audio extraction, process transport, UI, and image
storage; those responsibilities are outside this decoder package.
