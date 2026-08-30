# Radio State Updates

Pi-Sat can use Hamlib's generic asynchronous radio-state publisher for managed local radios. This reduces repeated CAT frequency reads without adding manufacturer-specific protocol handling to Pi-Sat.

## Architecture

The normal rigctld TCP connection remains strictly synchronous and serialized. Pi-Sat continues using it for frequency and mode writes, PTT reads, setup, split, and VFO-addressed operations.

For a local radio whose Hamlib backend advertises async support, Pi-Sat starts a separate loopback UDP listener and launches its managed rigctld with:

```text
-C async=1,multicast_data_addr=127.0.0.1,multicast_data_port=<private-port>,poll_interval=0
```

Hamlib publishes JSON radio snapshots to that private port. Pi-Sat parses generic VFO entries such as `freq`, `mode`, `rx`, and `tx`; it never parses CI-V, Yaesu AI, or another manufacturer's CAT protocol. The UDP feed cannot be mistaken for a reply on the synchronous rigctld socket.

Hamlib 4.5 introduced the underlying async callback and snapshot publisher, but its `rigctld` multicast configuration surface differs from the current interface. Hamlib 4.6 exposes the `async`, `multicast_data_addr`, `multicast_data_port`, and `poll_interval` configuration tokens used by Pi-Sat, so Hamlib 4.6 or newer is required for this optimization. Raspberry Pi OS Trixie's Hamlib 4.6.2 package is supported. Pi-Sat checks `rigctld --version` and `rigctld -m <model> -u`, including the backend's `Has async data support` capability. A listener that starts is **available**. A property becomes **verified** only after a changed value arrives through the UDP feed. Normal polling continues while support is merely available.

After frequency updates are verified, Pi-Sat normally reads observed frequency from pushed events and performs a reconciliation CAT read about every five seconds. Missing individual command echoes does not make the feed unhealthy because Hamlib or the radio may legitimately coalesce or omit them. If a reconciliation read finds a frequency different from the last pushed state, that polled value is treated as the recovered final external state and enters the existing manual-tuning reconciliation without immediately disabling async updates. Three consecutive reconciliation intervals that disagree revoke frequency verification and resume normal polling until a new valid pushed event re-verifies the feed. A matching pushed event or reconciliation read resets that count. Listener failure and rigctld reconnects also restore polling immediately. This means a missed final manual-tuning step is detected within approximately five seconds even when Hamlib publishes an intermediate step but omits the settled value. The tested Icom async decoder does not provide a separately verifiable PTT event, so Pi-Sat reads PTT immediately before a protected TX or setup write. For an already-configured shared radio, RX frequency writes remain available during transmit so Doppler tracking and Virtual RIT continue working; TX frequency and all other protected radio writes remain deferred. The tracking loop continually replaces the desired TX target, so the first update after PTT release sends only the current target rather than replaying skipped changes.

`Polling only` disables the listener and preserves the previous polling behavior. External/network rigctld endpoints also remain polling-only because Pi-Sat cannot safely assume that another process was configured to publish an async feed.

## Command echoes and manual tuning

Pi-Sat keeps the 16 most recent frequency and mode writes for two seconds. Matching uses property, RX/TX role, VFO when available, exact value, and monotonic time. This recognizes delayed and out-of-order echoes without allowing an old command to hide a later manual return to the same frequency.

A matching self-echo updates observed state and verifies the async path, but it does not change manual offsets or Virtual RIT and does not trigger a write. An unmatched external frequency event enters the existing manual tuning reconciliation and sanity checks. Virtual RIT remains a software RX-only addition to the normal Doppler target; it does not add a hardware RIT CAT command.

## Hamlib backend findings

- IC-9700, model 3081: the stable Hamlib backend advertises async data support and uses Hamlib's generic Icom async frame handlers. Frequency and mode callbacks are exposed. Hamlib 4.6.2 reports Icom transceive changes on its generic current-VFO cache and publishes several overlapping cached VFO names. For a shared radio, Pi-Sat first uses an exact recent-command match, then a bounded generic frequency-proximity check against the last synchronized RX/TX states, and finally the explicitly configured VFO name. Frequency-proximity routing is limited to two MHz and requires a clear advantage over the other role. Named aliases that match neither role are ignored and covered by reconciliation polling.
- IC-706MkIIG, model 3011: the stable backend also advertises the same generic async support and handlers. It is a useful secondary verification of the abstraction, but it has less targetable-VFO capability than the IC-9700.
- In the current Icom handler, frequency and mode changes are fired as generic Hamlib events. PTT is not fired by that handler, so Pi-Sat keeps polling PTT.

Radio configuration still matters. On an Icom radio, CI-V Transceive must be enabled for the radio to originate change notifications. Pi-Sat does not configure or expose that manufacturer-specific setting.

## IC-9700 manual test

1. Install Hamlib 4.6 or newer and confirm `rigctld --version`. Raspberry Pi OS Trixie's packaged Hamlib 4.6.2 is supported.
2. Enable CI-V Transceive on the radio. Keep the existing address, baud, USB/LAN, and shared Main/Sub settings used by the station.
3. In Pi-Sat, set **Radio State Updates** to **Automatic (recommended)** and click **Test Radio**. The test must connect normally and should report async **available**; it does not require turning the dial.
4. Start tracking. Confirm the status changes to **Real-time updates active** after a frequency event or Pi-Sat command echo is received.
5. Turn the RX dial or tune from another controller. Confirm Pi-Sat reacts promptly and the existing manual RX offset changes once, without an oscillating write loop.
6. Exercise several Doppler updates and inspect DEBUG logs for self-echo classification. Confirm Virtual RIT stays unchanged.
7. Transmit briefly. Confirm RX frequency and Virtual RIT remain adjustable while TX frequency/setup writes stay deferred. On release, confirm only the latest TX target is applied.
8. Compare CAT traffic before and after verification. Frequency reads should fall from the normal tracking cadence to approximately one reconciliation read every five seconds; PTT reads remain responsive.
9. Stop/restart rigctld or disconnect/reconnect the radio. Confirm Pi-Sat reports fallback polling and later re-establishes or re-verifies async state.

## Backend without async support

1. Add the radio normally and leave **Radio State Updates** on **Automatic**.
2. Click **Test Radio**. Normal connectivity should succeed, while the state-update result reports that pushed updates are unavailable and polling will be used.
3. Start tracking and confirm frequency, mode setup, PTT safety, manual offsets, and Virtual RIT behave as before.
4. Optionally select **Polling only**, save, and repeat the test. Confirm no async listener starts and normal polling remains active.

No automatic Hamlib upgrade or additional Python package is required.
