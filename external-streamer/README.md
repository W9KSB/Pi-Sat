# Pi-Sat RX Audio Stream Connector

This Windows x64 connector receives audio from Pi-Sat's **External DATA Decode**
module and plays it through a selected installed Windows output device. It is an
audio bridge, not an SSTV decoder, data modem, or SSDV image assembler.

## When to use it

- Route receive audio to an external SSTV application instead of using Pi-Sat's
  built-in SSTV decoder.
- Route receive audio to a compatible data modem and downstream SSDV decoder.
  The modem, symbol rate, framing, and image format must match the transmission.
  SSDV support for certain formats remains work in progress; the connector does
  not provide universal SSDV or satellite telemetry decoding.

Pi-Sat's built-in SSTV page does not require this connector. For that page, leave
External DATA Decode disabled and use normal AF receive audio.

## Setup

1. Connect the native IC-9700 radio in Pi-Sat with a 48 kHz receive sample rate.
   Tune SUB to the transmission and enable **External DATA Decode** in Pi-Sat.
2. Run `external-streamer.exe`. Enter the Pi-Sat server's LAN address or hostname
   and the configured TCP port, which defaults to `8765`.
3. Select a Windows **Output device**. For an external decoder, use the playback
   endpoint of an installed virtual audio device and select its corresponding
   capture endpoint as the decoder's input. The connector does not install a
   virtual audio device. Use **Refresh devices** after changing available devices.
4. Select **Start**, then configure the external decoder for the transmission.
   Selecting speakers instead of a virtual audio device provides audible
   monitoring, not an input connection to another application.
5. Select **Stop** when finished, then disable **External DATA Decode** in Pi-Sat
   before returning to normal AF reception or built-in SSTV decoding.

The connector itself sends no radio-control commands. Enabling the server module
does change the shared radio path: Pi-Sat selects SUB FM-DATA/FIL1 and LAN IF
output, then software-demodulates that input to 48 kHz mono signed 16-bit PCM.
The IF path provides wider bandwidth for data signals than normal filtered AF
audio. **Do not use External DATA Decode and built-in SSTV at the same time.**
The connector receives this demodulated audio, not raw IF, I/Q, packets, or SSDV
images. Normal AF consumers, including the built-in SSTV decoder, cannot use
that shared input normally while the IF path is active. See
[External DATA Decode](../docs/rx-audio-stream.md) for configuration and restoration
behavior.

## Buffering and diagnostics

Playback starts after 250 ms of audio is buffered. The receiver holds up to five
seconds of PCM; if that fills, it stops and reports an error instead of skipping
samples. The window reports frames, sequence gaps, bytes, buffer drops, queued
audio, and output underflows. Increasing gaps, output underflows, or a buffer
error indicate a delivery/playback problem; these counters do not measure modem
or image-decoding success. Check the selected output device and the server's
status before restarting with **Start**.

The TCP stream is unauthenticated and unencrypted. Use a trusted LAN and restrict
the configured port with a firewall. Do not expose it directly to the Internet.

## Building

The Python source is in `src/`. To rebuild on Windows, install the
packages from `requirements.txt` and run `build.ps1` from this directory.
