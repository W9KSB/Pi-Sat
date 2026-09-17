# Radio State Updates

Pi-Sat can receive pushed frequency and mode changes from Hamlib for managed
local radios. This reduces repeated CAT frequency reads while preserving the
normal serialized `rigctld` connection for commands, PTT, setup, and
VFO-addressed operations.

## Requirements

- Hamlib 4.6 or newer
- A local radio managed by Pi-Sat through `rigctld`
- A Hamlib backend that advertises generic asynchronous data support
- Radio-originated change notifications enabled when the radio requires them

Raspberry Pi OS Trixie's Hamlib 4.6.2 package is supported. Pi-Sat does not
upgrade Hamlib or install an additional Python package for this feature.

External or network `rigctld` endpoints use polling because Pi-Sat cannot
configure their pushed-state feed.

## Configuration

Each local radio provides a **Radio State Updates** setting:

- **Automatic (recommended)** enables pushed updates when the installed Hamlib
  version and selected backend support them. Pi-Sat uses polling until a valid
  pushed frequency event arrives.
- **Polling only** disables pushed updates and reads radio state through the
  normal CAT connection.

For Icom radios, enable CI-V Transceive so front-panel changes can be published.
Pi-Sat does not change that radio menu setting.

The radio status can report:

- **Available**: the listener is running, but no valid pushed property has been
  received. Normal polling remains active.
- **Real-time updates active**: pushed frequency updates are in use.
- **Polling**: pushed updates are disabled, unavailable, or temporarily
  unhealthy.

## Operating behavior

Pi-Sat periodically reconciles pushed frequency state with a CAT read. Listener
failure, a `rigctld` reconnect, or repeated disagreement returns the radio to
normal polling. A valid pushed event enables real-time updates again when
Automatic is selected.

Manual dial changes enter the same offset reconciliation used by polled state.
Virtual RIT remains a software RX-only adjustment and is not sent as a radio RIT
command.

PTT remains protected by a direct read before transmit-sensitive setup or TX
writes. On a configured shared radio, RX tracking and Virtual RIT can continue
during transmit; protected TX and setup writes wait until release. Only the
current TX target is applied afterward.

## Supported Icom behavior

The IC-9700 Hamlib backend publishes generic frequency and mode events when CI-V
Transceive is enabled. Pi-Sat routes those events to the configured RX and TX
roles and ignores unrelated VFO aliases. PTT is read through CAT.

The IC-706MkIIG backend also advertises generic asynchronous data support, but
its VFO targeting is more limited. Radios or backends that do not expose the
required capability use polling.

## Troubleshooting

- Confirm `rigctld --version` reports Hamlib 4.6 or newer.
- In Settings, save the radio and use **Test Radio** to review the reported
  state-update capability.
- If the status remains **Available**, turn the radio dial or allow Pi-Sat to
  send a frequency update. Polling remains active until an event is received.
- If front-panel changes do not appear, confirm the radio's change-notification
  or CI-V Transceive setting.
- If the status falls back to **Polling**, check the radio connection and
  `journalctl -u pi-sat -n 100 --no-pager`.
- Select **Polling only** when pushed updates are unstable for a particular
  radio/backend combination.

Polling is a supported operating mode and retains the same radio-control and
tracking functions at a higher CAT read cadence.
