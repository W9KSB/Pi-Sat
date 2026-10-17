# FT4 / FT8 module

WSJT-X runs on the same Pi/Linux device as Pi-Sat. Pi-Sat owns the native radio
connection, tuning, modes, tracking, and audio. An unmodified stock WSJT-X
instance handles decoding and generates transmit audio. Separate virtual audio
paths carry Pi-Sat's SUB receive audio into WSJT-X and its transmit audio back to
Pi-Sat. Pi-Sat sends that audio through the existing native radio connection and
controls PTT. UDP messages carry engine status, decodes, and message controls.

## One-time setup

1. Install stock WSJT-X, PulseAudio client tools, Xvfb, and the X11 and Linux
   accessibility libraries on the Pi-Sat host:

   ```sh
   sh install/install_wsjtx.sh
   ```

   Run the helper as the same Linux user that runs Pi-Sat, without prefixing it
   with `sudo`; it requests sudo for package installation and user lingering.
   It supports Debian/Ubuntu package management. WSJT-X remains a separately
   installed application; it is not bundled into Pi-Sat.
2. Ensure the Pi-Sat service user can run `pactl info`. A PulseAudio-compatible
   server must be available to that user, through PulseAudio or PipeWire's Pulse
   compatibility service. A desktop audio session belonging to another user is
   insufficient. The helper keeps a reachable audio server unchanged. If none
   is reachable, it starts existing PipeWire services or installs and starts
   PulseAudio when PipeWire is absent. It enables user lingering so those
   services can run at boot and after logout, then checks audio server access.
   If the check fails, resolve the reported service error before starting RX.
   The managed engine discovers the current user's local audio socket when
   `PULSE_SERVER` is unset, including when Pi-Sat runs as a system service without
   the SSH session's `XDG_RUNTIME_DIR`. It uses the same server for audio tools
   and WSJT-X. An explicit `PULSE_SERVER` environment setting takes precedence.
3. In Pi-Sat **Settings > Other**, enter your **WSJT-X callsign** and leave
   **executable** as `wsjtx`, or set the absolute path to the stock executable.
   The WSJT-X grid comes from Pi-Sat's existing station grid setting. Select FT8
   or FT4 in the WSJT-X mode setting before starting RX.
4. Connect the native IC-9700 in Pi-Sat, tune SUB to the desired receive frequency,
   and select **USB-DATA** through Pi-Sat. Set MAIN to the intended uplink and
   **USB-DATA**, or **LSB-DATA** when required by an inverting transponder. The
   module also accepts ordinary USB on SUB and USB/LSB on MAIN. Leave
   **External DATA Decode** disabled; this module consumes normal AF audio rather
   than FM-demodulated IF.
5. Open **Modules > FT4 / FT8** and select **Start RX**.

Pi-Sat creates its own WSJT-X profile with **Rig = None**, local UDP reporting,
and separate virtual input/output devices. It launches WSJT-X on a private
virtual display and forwards SUB AF audio from the existing radio session. No
manual WSJT-X settings are required for the managed workflow. Personal WSJT-X
profiles are kept separate. Controls absent from the stock UDP protocol use
confirmed accessibility controls on this managed instance's private display
and session bus. Pi-Sat identifies the main window by its owned process and
unique set of transmit controls, independently of its title or version text.
Audio frequency values are set directly. Transmit enable activates the verified
button using its current bounds on the private display. Xvfb, `libx11-6`, `libxtst6`,
`at-spi2-core`, and `dbus` are required for transmit control. Pi-Sat confirms
that stock Auto Seq is off and the selected transmit slot is set before arming.
Native state readback and UDP status must agree before transmitting. Missing,
ambiguous, disabled, or incompatible controls prevent transmit; Pi-Sat does not
guess a target window or retry a toggle blindly.
It does not change your desktop's accessibility settings.

The managed instance starts in the configured FT4 or FT8 mode. **Stop RX** halts
the managed transmission and stops the instance, display, audio bridges, and
virtual audio devices owned by this module. It does not disconnect the radio.
The managed engine starts stopped, with TX disabled, after a Pi-Sat restart.

**Reset** sits next to **Start RX** and performs the same shutdown without
requiring a healthy session. Use it when a start failed or the receive engine
reports a stuck WSJT-X audio device: it disables automatic transmit, stops the
managed instance, and releases only the audio devices and listener this module
owns, then leaves the engine stopped for an explicit **Start RX**. If a device
could not be released or PTT release could not be confirmed, Reset reports that
result instead of reporting success.

## Operating view

The top **Digital audio** spectrum and waterfall are calculated from Pi-Sat's
SUB AF audio. Their software span follows the radio's reported SSB filter width,
including FIL1 when selected. If the width is unavailable, the display uses an
explicitly labelled 3000 Hz estimate. It is not the radio's RF scope or a
waterfall supplied by WSJT-X.

The managed stock decoder is configured to search across the supported audio
range, including the full displayed SSB passband. A received FT4/FT8 signal does
not need to match the held TX offset or a particular audio pitch. Keep all its
tones within the radio's usable receive filter; the decoder cannot recover
tones removed by that filter.

Click the passband, or edit **Hold TX offset**, to choose the transmit audio
offset. It stays fixed when selecting another sender. RX is marked green and TX
red. Allowed offsets start at 200 Hz and end 50 Hz below the displayed passband
edge for FT8, or 90 Hz below it for FT4. This changes the audio tones, without
tuning the radio or changing its filter. A change during a burst applies to the
next burst. Radio tuning, modes, filters, and tracking remain controlled through
Pi-Sat's existing radio controls.

**Band Activity** lists decodes with UTC time, signal report, time offset, audio
frequency offset, mode, and message. It also shows actual transmitted messages
with a TX label. Replies addressed to your callsign and the selected station are
highlighted. A single click selects a row without enabling transmit. The
satellite strip uses Pi-Sat's existing tracking state.

## Making a contact

1. Start RX and verify that the receive source and managed engine are healthy.
   Confirm the MAIN uplink frequency and modulation level before enabling TX.
2. Choose your held TX audio offset in the digital passband.
3. Double-click a fresh received message to set its sender as the target and
   **enable TX**. The reply uses the next complete opposite FT8/FT4 time slot. In response to
   `CQ K1ABC FN42`, station `W9KSB` in `EN50` sends `K1ABC W9KSB EN50`.
4. Leave **Auto sequence** enabled to advance when the target replies to you.
   You can choose a different Tx message with its radio selector; automatic
   progression continues from that stage. Changes during a burst apply to the
   following burst.
5. Use **Halt TX** or **Disable TX** to stop the managed transmission. **Call CQ**
   clears the target and enables the CQ message in the even time slot.

The six message choices are:

| Choice | Message |
| --- | --- |
| Tx1 | Target call, your call, your four-character grid |
| Tx2 | Target call, your call, signal report, such as `-12` |
| Tx3 | Target call, your call, acknowledged report, such as `R-12` |
| Tx4 | Target call, your call, `RR73` |
| Tx5 | Target call, your call, `73` |
| Tx6 | `CQ`, your call, your four-character grid |

Pi-Sat selects the report from the target's decoded SNR. An incoming grid
normally advances to Tx2, a plain report to Tx3, an acknowledged report to Tx4,
and `RRR`, `RR73`, or `73` to Tx5. Progression uses fresh messages from the target
addressed to your callsign. Off-air, stale, low-confidence, ambiguous, and
unsupported nonstandard messages cannot select a target or advance a contact.
The current sequencer requires a standard callsign and a valid station grid.

Each selected stage gets its initial transmission and up to **three unanswered
repeats**. After those four completed bursts, Pi-Sat disables TX. Advancing or
manually choosing a stage starts a new repeat count. Disabling Auto sequence
leaves message selection to the operator. A reconnect, mode change, source
failure, or engine restart disarms TX; it never resumes automatically.

The native radio transmit stream is reserved while FT4/FT8 TX is enabled, so
APRS or browser microphone audio cannot share that stream. Pi-Sat selects the
radio's transport modulation input and requires a nonzero LAN modulation level.
Verify modulation, PTT release, and RF output on the actual radio before relying
on an unattended sequence.

Pi-Sat's existing Doppler behavior holds the MAIN uplink tuning while PTT is
active; SUB receive tracking continues. The uplink is not retuned throughout an
FT4/FT8 burst. This can matter during a satellite pass with rapidly changing
Doppler.

## Contacts and diagnostics

The bottom-right **QSO Log** retains completed contacts across restarts and
provides **Export ADIF**. Automatic logging requires exchanged reports, an
acknowledgement, and a completed closing `RR73` or `73` transmission. A decoded
CQ or an incomplete attempt does not create a completed contact. The stored
record includes UTC times, mode, callsign, grid, reports, native frequencies
adjusted for audio offsets, and the selected satellite when available. Contacts
are saved in `data/wsjtx/contacts.json`; keep that file when updating Pi-Sat.

Engine, receive, and sequencing diagnostics are on **Monitor**. Its separate
**FT4 / FT8 logs** and **SSTV logs** toggles show cyan and violet entries
respectively, alongside the existing logging controls. SSTV diagnostic downloads
are also on Monitor. The FT4/FT8 module contains operating controls and contact
history, without a diagnostic log console.

The bridge uses a bounded queue without intentionally skipping audio. An audio
overrun, controller change, radio disconnect, IF-source conflict, or playback
failure stops the managed receive session and reports the reason. Resolve it,
then start RX again.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `executable` | `wsjtx` | Stock WSJT-X executable name or absolute path |
| `callsign` | empty | Station callsign for the isolated WSJT-X profile |
| `mode` | `FT8` | Decoder mode on the next RX start: FT4 or FT8 |
| `udp_host` | `127.0.0.1` | Local IPv4 UDP listener address |
| `udp_port` | `2237` | Local reporting port |
| `client_timeout_seconds` | `45` | Time without packets before an instance is stale |
| `max_decodes` | `300` | Maximum retained decoded messages |

Configuration changes apply on the next RX start. The managed engine reports to
`127.0.0.1`, so the listener must bind that address or `0.0.0.0`. Its private
configuration and log are under `data/wsjtx/engine/`. Do not edit its profile
while it is running; Pi-Sat recreates the managed configuration at startup.

Decode history is in memory. UTC clock times come from WSJT-X; the UDP decode
record has no calendar date, so Pi-Sat infers the nearest UTC day. File decodes
are marked off-air; their original recording date is unavailable. UDP supplies
neither raw audio nor the WSJT-X waterfall; Pi-Sat builds its audio display from
the native receive stream.

Keep the host's existing system time synchronization enabled and healthy.
Pi-Sat does not currently manage the time service.

## Audio server connection troubleshooting

If **Start RX** reports `pactl failed: Connection refused`, run these commands
as the Pi-Sat service user:

```sh
pactl info
systemctl show pi-sat --property=User --value
ls -l /run/user/$(id -u)/pulse/native
```

The service user must match the owner of the local audio server. A working
`pactl info` in a different user's login session does not establish access for
Pi-Sat. If an explicit `PULSE_SERVER` is configured in Pi-Sat's service
environment, confirm that it points to the running server. After updating the
backend code, restart Pi-Sat before trying **Start RX** again.
