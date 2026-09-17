# Native Radio Console

The Radio page provides direct IC-9700 control, receive audio, microphone audio,
spectrum, waterfall, and meter displays over the radio's LAN interface.

## Connect and disconnect

Select **Connect** to start the native radio session. Pi-Sat remains
disconnected after startup and configuration changes until an operator connects
it.

The browser prepares receive and microphone audio as part of the Connect action.
Audio continues while navigating between Pi-Sat pages. **Disconnect** closes the
radio session and releases browser audio and microphone resources.

A lost state connection disables write controls. Connection retries show the
failed stage, attempt number, and retry delay.

## MAIN, SUB, and VFO control

The console shows MAIN/TX and SUB/RX separately. Frequency, mode, filter, A/B,
AF gain, RF gain, squelch, Dualwatch, and SUB RIT controls use radio readback.
Unknown A/B state remains unknown until the operator selects it.

Listening selection is independent of the radio's operating selection:

- **MAIN** plays the left PCM channel and follows MAIN with the scope.
- **SUB** plays the right PCM channel and follows SUB with the scope.
- **Both** preserves stereo audio and follows SUB with the scope.

SUB and Both require stereo receive audio. Dualwatch controls whether the SUB
receiver is active. The local monitor-volume slider changes browser playback
only.

Frequency entry, tuning buttons, keyboard input, passband dragging, and the
mouse wheel use the selected tuning step. Tuning lock, disconnect, stale scope
data, pointer cancellation, or a listening-side change cancels a pending drag
or wheel update.

## Spectrum and waterfall

The spectrum starts with the radio session and follows the listening side.
Displayed span can be set from 5 kHz to 1 MHz. Display fill, grid, max hold,
Smooth, and per-side display levels are browser-only controls and do not change
radio gain or scope amplitude.

The waterfall scrolls once for each complete radio sweep. The amplitude scale is
relative and is not calibrated in dBm.

The shaded passband uses the active frequency, mode, and available filter-width
readback. FM and DV use fixed widths; SSB, CW, and AM use the radio's adjustable
filter width. SUB RIT shifts the shading. The display does not model Twin PBT,
CW pitch, or the complete DSP filter shape.

## Meters

Select either meter to cycle through S, Power, SWR, and Compression. Transmit
views are labelled MAIN TX and wait for PTT state. Values use approximate
interpolation between Icom's documented meter points and should not be treated
as laboratory measurements.

## Microphone and PTT

Microphone capture requires HTTPS or localhost and browser permission. Choose a
system/default or named browser input, then set browser gain as needed. The
level indicator shows captured audio; it does not measure RF output.

Capture does not key PTT. The IC-9700's `DATA OFF MOD`, `DATA MOD`, and LAN
modulation level determine which source reaches the transmitter. Radio input
changes are unavailable while transmitting.

Receive and microphone audio use separate connections. If microphone audio is
lost while Pi-Sat holds PTT, Pi-Sat commands a release. Disconnect, connection
loss, and shutdown also attempt to release PTT. A `tx_unconfirmed` warning
means the radio may still be transmitting and must be checked directly.

## Limitations

- Split operation is disabled; alternate-VFO transmit frequency is not exposed.
- SAT mode is not enabled by the native console.
- Front-panel A/B changes are not discovered automatically. Manual MAIN/SUB
  changes are reconciled during radio readback.
- CI-V cannot lock out simultaneous front-panel operation during a transaction.
- `full_duplex` and `scope_enabled` configuration keys are accepted but are
  not independent console controls. Both audio requires stereo RX and
  Dualwatch; the scope follows listening.
- Browser-controlled PTT is not intended for unattended transmission. There is
  no separate hardware watchdog.

## Radio references

- [Icom IC-9700 CI-V guide](https://www.icomfrance.com/uploads/files/produit/not-IC-9700_ENG_CI-V_1-en.pdf)
- [Icom IC-9700 basic manual](https://www.icomeurope.com/wp-content/uploads/2019/07/IC-9700_ENG_Basic_0a.pdf)
