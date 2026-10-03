# External DATA Decode

External DATA Decode sends receive audio from Pi-Sat to an external decoder
through the Windows RX Audio Stream Connector. Use it for an external SSTV
application, or for a compatible data modem feeding an SSDV decoder. The
connector transports audio; it does not decode SSTV tones, demodulate a data
modem's symbols, assemble packets, or reconstruct SSDV images.

SSDV support for certain formats remains work in progress. Successful audio
delivery does not establish support for a particular transmission: the external
modem, symbol rate, framing, and image format must all match. This module does not
provide universal SSDV or satellite telemetry decoding.

## Radio and audio path

The module requires the native IC-9700 receive connection with
`[icom] sample_rate = 48000`. It uses the existing radio connection and fixed
SUB/RX channel. Enabling it selects SUB **FM-DATA/FIL1** and switches the radio's
LAN output from normal AF to IF. Pi-Sat software-demodulates this input and sends
framed **48 kHz, mono, signed 16-bit little-endian PCM** over TCP. The client does
not receive raw IF or I/Q samples, decoded packets, or image files.
The IF path provides wider-bandwidth audio for data signals that normal filtered
AF audio may restrict.

This changes the shared receive source, not just the connector's audio. Normal
AF consumers, including Pi-Sat's built-in SSTV decoder, cannot use that input
normally while the IF path is active. For built-in SSTV, leave External DATA
Decode disabled. Browser PCM or WebRTC playback selection does not make the two
radio-source requirements compatible.
**Do not use built-in SSTV and External DATA Decode at the same time.**

The Windows connector only receives TCP audio and writes it to a selected output
device. Radio mode and source changes are performed by Pi-Sat's server module,
not by the connector.

## Configuration

The module page's **Enable** and **Disable** controls save the
`[audio_stream] enabled` setting, so the selection persists across service
restarts. Connect the radio before enabling the module.

Example configuration, initially disabled:

```ini
[audio_stream]
enabled = false
bind_host = 0.0.0.0
port = 8765
```

`bind_host = 0.0.0.0` listens on all IPv4 interfaces. Set it to the server's LAN
interface address to limit the listening interface. Enter the server's reachable
LAN address or hostname in the connector, not `0.0.0.0`. The legacy `channel`
setting does not select a different channel for External DATA Decode; this path
always uses SUB/RX.

The TCP stream is unauthenticated and unencrypted. Restrict the configured port
to intended clients with a firewall and use a trusted LAN. Do not forward the
port to the public Internet.

## Using an external decoder

1. Tune SUB to the transmission and enable **External DATA Decode** in Pi-Sat.
2. Open the Windows connector, enter the server address and port, and select an
   installed Windows output device.
3. To feed another application, select a virtual audio device's playback endpoint
   in the connector and its matching capture endpoint in the external decoder.
   The connector does not install that device or configure the decoder.
4. Select **Start** in the connector. Select the appropriate mode in the external
   SSTV application, or configure the required modem and SSDV decoding chain.
5. When finished, select **Stop** in the connector and **Disable** in Pi-Sat.

For executable usage and buffering details, see the
[Windows connector guide](../external-streamer/README.md).

## Stopping and restoring normal reception

Disabling the module while the radio is connected attempts to restore LAN AF
output. Pi-Sat also restores the previous SUB mode and filter if the radio still
has the FM-DATA/FIL1 settings selected by this module; intervening operator mode
changes are preserved. When the last external client disconnects, the current
decode session ends. If the module remains enabled, a later client can activate
the IF path again, so explicitly disable it before using normal AF reception.

Restoration cannot be guaranteed after a radio disconnect or a control-command
failure. Reconnect, disable External DATA Decode, and verify normal AF output and
the intended SUB mode/filter before using built-in SSTV or other AF consumers.
If the module reports that the radio disconnected during external decoding,
re-enable it only when ready to resume that workflow.

## Status and troubleshooting

The module page reports the listener address and port, audio source, connected
clients, frame and byte counts, refused connections, source packet drops, stream
queue drops, and errors. The connector reports frames, sequence gaps, bytes,
buffer drops, queued audio, and output underflows.

- No client connection: check the server address, configured port, listener
  status, and firewall access.
- Frames arrive but the external application receives no audio: verify that the
  connector's output and the application's input are the paired endpoints of
  the selected virtual audio device.
- Audio arrives but nothing decodes: confirm the external decoder's mode and,
  for packet data, its modem and framing settings. Stream counters indicate
  delivery, not successful tone or packet recognition.
- Buffer overflow or output underflows: inspect the reported error and playback
  device. The source and connector use bounded buffering and stop on overflow
  rather than silently skipping audio to catch up. Resolve the error before
  restarting the stream.
