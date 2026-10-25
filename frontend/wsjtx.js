(() => {
  'use strict';

  const byId = id => document.getElementById(id);
  const page = byId('wsjtx-page');
  if (!page) return;
  const MAX_DECODES = 100;
  const POLL_INTERVAL_MS = 2000;
  const STALE_AFTER_MS = 8000;
  const list = byId('wsjtx-decodes');
  const enableButton = byId('wsjtx-enable');
  const resetButton = byId('wsjtx-reset');
  const engineSelect = byId('wsjtx-engine');
  const decodeDepthSelect = byId('wsjtx-decode-depth');
  const modeButtons = [
    { button: byId('wsjtx-mode-ft4'), mode: 'FT4' },
    { button: byId('wsjtx-mode-ft8'), mode: 'FT8' },
  ].filter(item => item.button);
  let snapshot = null;
  let configuredMode = '';
  let modeSettingsLoaded = false;
  let modeSaving = false;
  let modeError = '';
  let selectedEngineKey = '';
  let manualEngineSelection = false;
  let selectedDecodeKey = '';
  let sequence = -1;
  let revision = 0;
  let connectionEpoch = 0;
  let receivedAt = 0;
  let unavailable = false;
  let pending = false;
  let haltPending = false;
  let actionSequence = 0;
  let stopped = false;
  let socket = null;
  let reconnectTimer = null;
  let pollPending = false;
  const clearedKeys = new Set();
  const seenDecodeKeys = new Set();
  const engineHistory = new Map();
  let spectrumGeneration = null;
  let spectrumLayout = '';
  let waterfallAt = null;
  let spectrumRow = null;
  let spectrumRowPixels = null;
  let spectrumSpan = 3000;
  let actionError = '';

  const text = (id, value) => { byId(id).textContent = value; };
  const number = (value, suffix = '', digits = null) => Number.isFinite(value)
    ? `${digits === null ? value : value.toFixed(digits)}${suffix}` : '—';
  const utc = decode => {
    const value = decode?.time_utc;
    if (!value) return '—';
    const match = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})(\.\d+)?(?:Z|\+00:00)$/.exec(value);
    return match ? match[1] + (Number(match[2]) ? match[2] : '') : value;
  };
  const client = () => snapshot?.clients?.find(item => item.key === selectedEngineKey);
  const stale = () => unavailable || !receivedAt || Date.now() - receivedAt > STALE_AFTER_MS;
  const flags = decode => [
    decode.low_confidence ? 'Low confidence' : '',
    decode.off_air ? 'Off-air / replay' : '',
    decode.aborted ? 'Aborted / incomplete' : '',
  ].filter(Boolean).join(' · ');
  const transmission = entry => entry.transmitted === true || entry.direction === 'tx';
  const transmissionKey = entry => `tx:${entry.key}`;
  const normalizeMode = value => {
    const mode = String(value || '').toUpperCase();
    return mode === 'FT4' || mode === 'FT8' ? mode : '';
  };
  const reportedMode = () => normalizeMode(snapshot?.engine?.tx?.mode || client()?.status?.mode);
  const modeLocked = () => snapshot?.engine?.running === true || client()?.connected === true;
  const selectedMode = () => reportedMode() || configuredMode || 'FT4';

  function activityEntries(limit = true) {
    const rx = (snapshot?.decodes || []).filter(entry => entry.client_key === selectedEngineKey);
    const managed = snapshot?.engine?.tx;
    const tx = (snapshot?.qso?.transmissions || []).filter(entry =>
      entry.client_key === selectedEngineKey && managed?.client_key === selectedEngineKey && Number.isInteger(entry.generation) && entry.generation === managed.generation
    ).map(entry => ({ ...entry, key: transmissionKey(entry) }));
    const entries = [...rx, ...tx].filter(entry => !clearedKeys.has(entry.key))
      .sort((a, b) => String(b.time_utc || '').localeCompare(String(a.time_utc || '')));
    return limit ? entries.slice(0, MAX_DECODES) : entries;
  }

  function offsetLimits() {
    const mode = snapshot?.engine?.tx?.mode || client()?.status?.mode;
    return { min: 200, max: Math.max(200, Math.floor(spectrumSpan - (mode === 'FT4' ? 90 : 50))) };
  }

  function appendMonitor(key, message, level = 'info') {
    window.appendModuleMonitorLog?.('wsjtx', { key, message, level });
  }

  function decodeRow(decode) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'wsjtx-decode-row';
    row.dataset.key = decode.key;
    row.setAttribute('aria-pressed', String(decode.key === selectedDecodeKey));
    const calls = String(decode.message || '').toUpperCase().split(/\s+/);
    const mine = String(snapshot?.qso?.my_call || '').toUpperCase();
    const target = String(snapshot?.qso?.target_call || '').toUpperCase();
    if (mine && calls.includes(mine)) row.className += ' wsjtx-directed';
    if (target && calls.includes(target)) row.className += ' wsjtx-partner';
    const sent = transmission(decode);
    if (sent) row.className += ' wsjtx-transmitted';
    const values = [utc(decode), sent ? 'TX' : number(decode.snr_db), sent ? '—' : number(decode.dt_seconds, '', 2), number(decode.frequency_hz), decode.mode || '—', decode.message || ''];
    for (const [index, value] of values.entries()) {
      const cell = document.createElement('span');
      cell.className = index === 5 ? 'wsjtx-decode-message' : 'wsjtx-decode-value';
      cell.textContent = value;
      if (index === 5 && flags(decode)) {
        const label = document.createElement('small');
        label.className = 'wsjtx-decode-flags';
        label.textContent = flags(decode);
        cell.append(label);
      }
      row.append(cell);
    }
    row.addEventListener('click', () => {
      selectedDecodeKey = decode.key;
      for (const item of list.children) item.setAttribute('aria-pressed', String(item.dataset.key === selectedDecodeKey));
    });
    if (!sent) row.addEventListener('dblclick', () => command('/qso/target', { decode_key: decode.key }));
    return row;
  }

  function renderDecodes() {
    const decodes = activityEntries();
    text('wsjtx-count', `${decodes.length} ${decodes.length === 1 ? 'message' : 'messages'}${decodes.length === MAX_DECODES ? ' (latest 100)' : ''}`);
    list.replaceChildren();
    if (!decodes.length) {
      const empty = document.createElement('p');
      empty.className = 'text-body-secondary p-3 mb-0';
      empty.textContent = !snapshot ? 'WSJT-X backend is unavailable.' : !snapshot.enabled
        ? snapshot.engine ? 'Select Start RX to receive FT4 / FT8 decodes.' : 'Enable listening to receive WSJT-X decodes.' : !client()
          ? snapshot.engine ? 'Waiting for the managed WSJT-X engine to report its status.' : 'Waiting for a WSJT-X engine. Enable UDP reporting in WSJT-X to this listener.'
          : client().connected !== true || stale()
            ? 'Engine status is stale. Waiting for WSJT-X updates.' : 'Listening. No received decodes in this view yet.';
      list.append(empty);
    }
    for (const decode of decodes) list.append(decodeRow(decode));
  }

  function renderModeSetting() {
    const selected = selectedMode();
    const disabled = modeSaving || !modeSettingsLoaded || modeLocked();
    for (const { button, mode } of modeButtons) {
      button.setAttribute('aria-pressed', String(mode === selected));
      button.disabled = disabled;
    }
    text('wsjtx-mode-setting-detail', modeError
      || (modeSaving ? `Saving ${selectedMode()} mode...`
        : modeLocked() ? 'Stop RX before changing the FT4 / FT8 mode.'
          : `${selectedMode()} applies when WSJT-X RX starts.`));
    byId('wsjtx-mode-setting-detail').dataset.state = modeError ? 'error' : modeSaving ? 'pending' : 'ready';
  }

  function renderEngine() {
    const engine = client();
    const status = engine?.status || {};
    const managed = snapshot?.engine;
    const live = snapshot?.enabled === true && snapshot?.running === true && !stale();
    const connected = live && engine?.connected === true;
    const label = stale() ? 'Status unavailable / stale' : managed?.state === 'error' ? 'RX setup failed' : !snapshot?.enabled ? 'Disabled'
      : !snapshot.running ? (snapshot.state || 'Listener stopped') : connected ? 'WSJT-X connected' : 'WSJT-X listening';
    text('wsjtx-state', label);
    byId('wsjtx-state').dataset.state = connected ? 'connected' : live ? 'listening' : 'inactive';
    text('wsjtx-detail', unavailable ? 'Backend connection lost. Reconnecting; readouts show last reported values.'
      : stale() ? 'Waiting for fresh listener status; readouts show last reported values.'
        : managed?.state === 'error' ? managed.detail : managed?.detail || snapshot?.detail || 'Enable UDP reporting in WSJT-X to receive its status and decodes.');
    const decodeDepth = managed?.decode_depth || 'fast';
    decodeDepthSelect.value = ['fast', 'normal', 'deep'].includes(decodeDepth) ? decodeDepth : 'fast';
    decodeDepthSelect.disabled = pending || Boolean(managed?.running) || !snapshot || stale();
    text('wsjtx-decode-depth-detail', managed?.running ? 'Stop RX before changing decoder depth.' : 'Decoder depth applies when RX starts.');
    const active = managed ? managed.running === true : snapshot?.enabled === true;
    enableButton.textContent = managed ? active ? 'Stop RX' : 'Start RX' : active ? 'Disable listening' : 'Enable listening';
    enableButton.setAttribute('aria-pressed', String(active));
    enableButton.disabled = pending || !snapshot || stale();
    // Reset is a recovery action: it stays available for a managed engine even
    // while status is stale, so an operator is never locked out of recovery.
    resetButton.disabled = pending || !managed;
    text('wsjtx-mode', reportedMode() || configuredMode || '—');
    renderModeSetting();
    renderQso();
  }

  function renderQso() {
    const qso = snapshot?.qso;
    const resumable = qso?.suspended === true && qso?.can_resume === true;
    const disabled = pending || stale() || qso?.tx_available !== true;
    text('wsjtx-my-call', qso?.my_call || '—');
    text('wsjtx-my-grid', qso?.my_grid || '—');
    text('wsjtx-target-call', qso?.target_call || '—');
    text('wsjtx-target-grid', qso?.target_grid || 'Double-click a decode');
    text('wsjtx-tx-state', stale() ? 'TX status stale' : qso?.transmitting ? 'Transmitting'
      : qso?.suspended ? 'TX suspended' : qso?.enabled ? 'TX enabled' : 'TX off');
    byId('wsjtx-tx-state').dataset.state = qso?.transmitting ? 'transmitting'
      : qso?.suspended ? 'suspended' : qso?.enabled ? 'enabled' : 'inactive';
    const tx = byId('wsjtx-tx-enable');
    tx.textContent = qso?.suspended ? 'Resume TX' : qso?.enabled ? 'Disable TX' : 'Enable TX';
    tx.setAttribute('aria-pressed', String(qso?.enabled === true));
    tx.disabled = qso?.suspended ? pending || stale() || !resumable : disabled;
    byId('wsjtx-halt').disabled = haltPending || !qso
      || !(pending || qso.enabled || qso.transmitting || qso.suspended);
    byId('wsjtx-cq').disabled = disabled;
    byId('wsjtx-auto').checked = qso?.auto_sequence !== false;
    byId('wsjtx-auto').disabled = disabled;
    byId('wsjtx-tx-slot').value = qso?.tx_slot || 'auto';
    byId('wsjtx-tx-slot').disabled = disabled;
    text('wsjtx-retries', `Retries ${qso?.retries || 0} / ${qso?.max_retries ?? 3}`);
    text('wsjtx-qso-detail', actionError || (stale() ? 'Backend status is stale. Waiting for fresh QSO state.' : qso?.detail || 'Start RX to begin a QSO.'));
    text('wsjtx-next-slot', qso?.enabled && qso?.next_slot_utc ? `Next TX slot ${utc({ time_utc: qso.next_slot_utc })} UTC` : '');
    const messages = byId('wsjtx-messages');
    messages.disabled = disabled;
    if (messages.children.length !== 6) messages.replaceChildren();
    for (let n = 1; n <= 6; n += 1) {
      const entry = qso?.messages?.find(item => item.number === n);
      let label = messages.children[n - 1];
      if (!label) {
        label = document.createElement('label');
        label.className = 'wsjtx-message-choice';
        const radio = document.createElement('input');
        radio.type = 'radio'; radio.name = 'wsjtx-message'; radio.value = String(n);
        radio.addEventListener('change', () => { if (radio.checked) void command('/qso/message', { number: n }); });
        const name = document.createElement('span'); name.className = 'wsjtx-message-number'; name.textContent = `Tx${n}`;
        const content = document.createElement('span'); content.className = 'wsjtx-message-text';
        label.append(radio, name, content); messages.append(label);
      }
      const radio = label.children[0];
      radio.checked = qso?.selected_message === n; radio.disabled = disabled;
      label.children[2].textContent = entry?.text || '—';
    }
    const contacts = byId('wsjtx-contacts'); contacts.replaceChildren();
    if (!qso?.contacts?.length) {
      const empty = document.createElement('p'); empty.className = 'text-body-secondary p-3 mb-0'; empty.textContent = 'No completed contacts yet.'; contacts.append(empty);
    }
    for (const contact of (qso?.contacts || []).slice().reverse()) {
      const row = document.createElement('div'); row.className = 'wsjtx-contact';
      const call = document.createElement('strong'); call.textContent = contact.call;
      const summary = document.createElement('span'); summary.textContent = [utc({ time_utc: contact.time_off_utc || contact.time_on_utc }), contact.mode, contact.grid, contact.satellite].filter(Boolean).join(' · ');
      const reports = document.createElement('small'); reports.textContent = `Sent ${contact.report_sent || '—'} · Received ${contact.report_received || '—'}`;
      row.append(call, summary, reports); contacts.append(row);
    }
    renderSpectrum();
  }

  function renderSpectrum() {
    const qso = snapshot?.qso;
    const spectrum = qso?.spectrum;
    spectrumSpan = Number.isFinite(spectrum?.span_hz) && spectrum.span_hz > 0 ? spectrum.span_hz : 3000;
    const estimated = !spectrum || spectrum.estimated === true;
    const filter = [1, 2, 3].includes(spectrum?.filter) ? `FIL${spectrum.filter} SSB passband` : 'SSB passband';
    const incompatibleMode = spectrum?.ssb_compatible === false;
    const spanLabel = incompatibleMode ? `estimated audio span · SUB ${spectrum.receive_mode}` : estimated ? 'estimated SSB width' : filter;
    text('wsjtx-spectrum-span', `0–${Math.round(spectrumSpan)} Hz · ${spanLabel}`);
    const limits = offsetLimits();
    const input = byId('wsjtx-offset'); input.min = String(limits.min); input.max = String(limits.max); input.disabled = pending || stale() || qso?.tx_available !== true;
    if (document.activeElement !== input) input.value = String(qso?.tx_offset_hz ?? 1500);
    const hit = byId('wsjtx-spectrum-hit'); hit.setAttribute('aria-valuemin', String(limits.min)); hit.setAttribute('aria-valuemax', String(limits.max)); hit.setAttribute('aria-valuenow', String(qso?.tx_offset_hz ?? 1500)); hit.setAttribute('aria-disabled', String(input.disabled));
    for (const [id, offset] of [['wsjtx-tx-marker', qso?.tx_offset_hz], ['wsjtx-rx-marker', qso?.rx_offset_hz]]) {
      const marker = byId(id); marker.hidden = !Number.isFinite(offset); marker.style.left = `${Math.max(0, Math.min(100, (offset || 0) / spectrumSpan * 100))}%`;
    }
    const bins = Array.isArray(spectrum?.bins) && spectrum.available !== false ? spectrum.bins : [];
    const invalidSsbWidth = estimated && !incompatibleMode && spectrum?.reported_bandwidth_hz > 3600;
    text('wsjtx-spectrum-detail', stale() ? 'Display is stale. Waiting for backend updates.'
      : incompatibleMode ? `SUB is reporting ${spectrum.receive_mode}. FT4/FT8 needs USB or LSB; the audio span is estimated.`
        : invalidSsbWidth ? `Radio reports a ${Math.round(spectrum.reported_bandwidth_hz)} Hz filter width, which is outside the SSB range. The audio span is estimated.`
          : bins.length ? 'Averaged signal energy above the local noise floor. Lines mark UTC transmit periods. Click to set TX offset.'
            : spectrum?.detail || 'Waiting for receive audio. The waterfall shows signal energy; decoded messages appear below.');
    if (page.hidden || document.hidden) return;
    const waterfall = byId('wsjtx-waterfall'); const water = waterfall.getContext?.('2d');
    const scale = byId('wsjtx-frequency-scale'); const axis = scale.getContext?.('2d');
    if (!water || !axis) return;
    const width = Math.min(1024, Math.max(1, Math.round(waterfall.clientWidth || 1200)));
    const layout = `${width}:${spectrumSpan}:${selectedMode()}:${snapshot?.engine?.tx?.generation}`;
    const changedLayout = spectrumLayout !== layout;
    if (changedLayout) {
      spectrumLayout = layout; spectrumGeneration = null; waterfallAt = null;
      for (const image of [waterfall, scale]) image.width = width;
      water.fillStyle = '#03081b'; water.fillRect(0, 0, width, waterfall.height);
      axis.fillStyle = '#07131f'; axis.fillRect(0, 0, width, scale.height);
      axis.font = '11px sans-serif'; axis.fillStyle = '#9cb9c9'; axis.strokeStyle = '#345069';
      const step = 500 * Math.max(1, Math.ceil(spectrumSpan * 65 / width / 500));
      for (let hz = 0; hz <= spectrumSpan; hz += step) {
        const x = hz / spectrumSpan * width;
        axis.beginPath(); axis.moveTo(x, scale.height - 5); axis.lineTo(x, scale.height); axis.stroke();
        axis.fillText(`${hz}`, Math.max(2, Math.min(x + 3, width - 30)), 14);
      }
    }
    const generation = !stale() && bins.length ? spectrum.generation : 'unavailable';
    if (spectrumGeneration === generation && !changedLayout) return;
    spectrumGeneration = generation;
    if (generation === 'unavailable') return;
    const levels = signalLevels(bins, spectrumSpan);
    if (!spectrumRow) spectrumRow = document.createElement('canvas');
    if (spectrumRow.width !== levels.length || !spectrumRowPixels) {
      spectrumRow.width = levels.length; spectrumRow.height = 1;
      spectrumRowPixels = spectrumRow.getContext('2d').createImageData(levels.length, 1);
    }
    for (let i = 0; i < levels.length; i++) {
      const color = Math.round(levels[i] * 255) * 4;
      spectrumRowPixels.data.set(signalPalette.subarray(color, color + 4), i * 4);
    }
    spectrumRow.getContext('2d').putImageData(spectrumRowPixels, 0, 0);
    const at = Number.isFinite(spectrum.timestamp_ms) ? spectrum.timestamp_ms : Date.now();
    const elapsed = waterfallAt === null ? 1 : (at - waterfallAt) / 1000;
    if (elapsed <= 0) return;
    const shift = Math.min(waterfall.height, Math.max(1, Math.round(elapsed * 2)));
    if (shift < waterfall.height) water.drawImage(waterfall, 0, 0, width, waterfall.height - shift, 0, shift, width, waterfall.height - shift);
    water.fillStyle = '#03081b'; water.fillRect(0, 0, width, shift);
    water.imageSmoothingEnabled = false;
    // A gap stays empty rather than inventing continuous transmissions while disconnected.
    water.drawImage(spectrumRow, 0, 0, levels.length, 1, 0, 0, width, Math.min(shift, 2));
    const period = selectedMode() === 'FT4' ? 7500 : 15000;
    const previous = waterfallAt === null ? at - 1000 : waterfallAt;
    const boundary = Math.floor(at / period) * period;
    if (Math.floor(previous / period) !== Math.floor(at / period)) {
      const y = Math.min(shift - 1, Math.round((at - boundary) / 1000 * 2));
      water.fillStyle = '#67be70'; water.fillRect(0, y, width, 1);
      const date = new Date(boundary);
      const label = [date.getUTCHours(), date.getUTCMinutes(), date.getUTCSeconds()].map(value => String(value).padStart(2, '0')).join(':') + (date.getUTCMilliseconds() ? '.5' : '');
      water.fillStyle = '#d5e6ef'; water.font = '10px sans-serif'; water.fillText(label, 4, y + 11);
    }
    waterfallAt = at;
  }

  function signalLevels(bins, span) {
    // Broad lower-percentile anchors follow passband slope while rejecting narrow peaks.
    const count = bins.length, block = Math.max(8, Math.round(count * 300 / span));
    const anchors = [];
    for (let start = 0; start < count; start += block) {
      const group = bins.slice(start, start + block).filter(Number.isFinite).sort((a, b) => a - b);
      anchors.push({ x: Math.min(count - 1, start + (block - 1) / 2), floor: group[Math.floor(group.length * 0.2)] ?? -100 });
    }
    const levels = new Float32Array(count);
    let anchor = 0;
    for (let i = 0; i < count; i++) {
      while (anchor + 1 < anchors.length && anchors[anchor + 1].x < i) anchor++;
      const a = anchors[anchor], b = anchors[Math.min(anchors.length - 1, anchor + 1)];
      const mix = b.x > a.x ? Math.max(0, Math.min(1, (i - a.x) / (b.x - a.x))) : 0;
      const floor = a.floor + (b.floor - a.floor) * mix;
      levels[i] = Number.isFinite(bins[i]) ? Math.max(0, Math.min(1, (bins[i] - floor - 5) / 35)) : 0;
    }
    return levels;
  }

  const signalPalette = (() => {
    const stops = [[0, 3, 8, 27], [0.25, 5, 22, 114], [0.48, 35, 112, 180], [0.65, 220, 218, 45], [0.85, 255, 105, 35], [1, 255, 236, 180]];
    const colors = new Uint8ClampedArray(256 * 4);
    let stop = 0;
    for (let i = 0; i < 256; i++) {
      const level = i / 255;
      while (stop + 1 < stops.length - 1 && stops[stop + 1][0] < level) stop++;
      const a = stops[stop], b = stops[stop + 1], mix = (level - a[0]) / (b[0] - a[0]);
      for (let channel = 0; channel < 3; channel++) colors[i * 4 + channel] = Math.round(a[channel + 1] + (b[channel + 1] - a[channel + 1]) * mix);
      colors[i * 4 + 3] = 255;
    }
    return colors;
  })();

  async function command(path, payload, allowStale = false) {
    const halt = path === '/qso/enabled' && payload.enabled === false;
    const resume = path === '/qso/enabled' && payload.enabled === true
      && snapshot?.qso?.suspended === true && snapshot?.qso?.can_resume === true;
    if ((pending && !halt) || (halt && haltPending) || !snapshot
        || (!allowStale && (stale() || (snapshot.qso?.tx_available !== true && !resume)))) return;
    const action = ++actionSequence;
    if (halt) haltPending = true;
    pending = true; actionError = ''; renderEngine();
    const requestEpoch = connectionEpoch;
    try {
      const response = await fetch(`/api/wsjtx${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Unable to update QSO state.');
      if (requestEpoch === connectionEpoch && action === actionSequence) renderSnapshot(result);
    } catch (error) {
      if (requestEpoch === connectionEpoch && action === actionSequence) {
        actionError = error.message || 'Unable to update QSO state.';
        text('wsjtx-qso-detail', actionError);
        appendMonitor(`command:${Date.now()}`, actionError, 'error');
      }
    } finally {
      if (halt) haltPending = false;
      if (action === actionSequence) pending = false;
      renderControlsAfterCommand();
    }
  }

  function renderControlsAfterCommand() {
    byId('wsjtx-tx-slot').value = snapshot?.qso?.tx_slot || 'auto';
    const resumable = snapshot?.qso?.suspended === true && snapshot?.qso?.can_resume === true;
    const disabled = pending || !snapshot || stale() || snapshot.qso?.tx_available !== true;
    enableButton.disabled = pending || !snapshot || stale();
    resetButton.disabled = pending || !snapshot?.engine;
    for (const id of ['wsjtx-tx-enable', 'wsjtx-cq', 'wsjtx-auto', 'wsjtx-tx-slot', 'wsjtx-offset', 'wsjtx-messages']) byId(id).disabled = disabled;
    byId('wsjtx-tx-enable').disabled = snapshot?.qso?.suspended
      ? pending || !snapshot || stale() || !resumable : disabled;
    decodeDepthSelect.disabled = pending || Boolean(snapshot?.engine?.running) || !snapshot || stale();
    byId('wsjtx-halt').disabled = haltPending || !snapshot?.qso
      || !(pending || snapshot.qso.enabled || snapshot.qso.transmitting || snapshot.qso.suspended);
    for (const label of byId('wsjtx-messages').children) if (label.children[0]?.type === 'radio') label.children[0].disabled = disabled;
    byId('wsjtx-spectrum-hit').setAttribute('aria-disabled', String(disabled));
  }

  function renderTracking() {
    if (typeof getRadioTrackingSummary !== 'function') return;
    const summary = getRadioTrackingSummary();
    for (const field of ['label', 'name', 'az', 'el', 'los']) text(`wsjtx-tracking-${field}`, summary[field]);
  }

  function renderSnapshot(status) {
    if (!status || typeof status.enabled !== 'boolean' || !Array.isArray(status.clients) || !Array.isArray(status.decodes)) return;
    const nextSequence = Number(status.sequence) || 0;
    if (nextSequence < sequence) return;
    sequence = nextSequence;
    revision += 1;
    receivedAt = Date.now();
    unavailable = false;
    snapshot = status;
    const selectedClient = status.clients.find(item => item.key === selectedEngineKey);
    const connectedClient = status.clients.find(item => item.connected === true);
    if (!selectedClient || (!manualEngineSelection && selectedClient.connected !== true && connectedClient)) {
      if (!selectedClient) manualEngineSelection = false;
      selectedEngineKey = (connectedClient || status.clients[0])?.key || '';
      selectedDecodeKey = '';
    }
    engineSelect.replaceChildren();
    const engines = status.clients.length ? status.clients : [{ key: '', id: 'No engine', connected: false }];
    for (const item of engines) {
      const option = document.createElement('option');
      option.value = item.key;
      option.textContent = `${item.id}${item.key ? ` · ${item.host}:${item.port}` : ''}${item.key && item.connected !== true ? ' (stale)' : ''}`;
      engineSelect.append(option);
    }
    engineSelect.value = selectedEngineKey;
    engineSelect.disabled = status.clients.length < 2;
    const retainedKeys = new Set([...status.decodes.map(decode => decode.key), ...(status.qso?.transmissions || []).map(transmissionKey)]);
    for (const key of clearedKeys) if (!retainedKeys.has(key)) clearedKeys.delete(key);
    for (const key of seenDecodeKeys) if (!retainedKeys.has(key)) seenDecodeKeys.delete(key);
    for (const item of status.clients) {
      const reported = `${item.connected === true ? 'Connected' : 'Stale / disconnected'}; ${typeof item.status?.transmitting === 'boolean' ? item.status.transmitting ? 'transmitting' : 'not transmitting' : 'TX state unknown'}`;
      if (engineHistory.get(item.key) !== reported) {
        appendMonitor(`engine:${item.key}:${sequence}`, `${item.last_seen_utc || 'UTC unknown'} · ${item.id} · ${reported}`);
        engineHistory.set(item.key, reported);
      }
    }
    for (const key of engineHistory.keys()) if (key !== 'managed-audio' && !status.clients.some(item => item.key === key)) engineHistory.delete(key);
    const audio = status.engine?.audio;
    const audioState = audio ? `${audio.state} · ${audio.detail || ''}` : '';
    if (engineHistory.get('managed-audio') !== audioState) {
      if (audioState) appendMonitor(`audio:${sequence}`, `Pi-Sat SUB AF · ${audioState}`, audio?.state === 'error' ? 'error' : 'info');
      engineHistory.set('managed-audio', audioState);
    }
    for (const decode of status.decodes.slice(0, MAX_DECODES).reverse()) {
      if (!seenDecodeKeys.has(decode.key) && !clearedKeys.has(decode.key)) appendMonitor(`decode:${decode.key}`, `${utc(decode)} · RX · ${decode.client_id} · ${decode.message}`);
      seenDecodeKeys.add(decode.key);
    }
    for (const entry of (status.qso?.transmissions || []).slice(0, MAX_DECODES).reverse()) {
      const key = transmissionKey(entry);
      if (!seenDecodeKeys.has(key)) appendMonitor(key, `${utc(entry)} · TX · ${entry.mode || '—'} · ${entry.message}`);
      seenDecodeKeys.add(key);
    }
    renderEngine();
    renderDecodes();
    renderTracking();
  }

  async function loadState() {
    if (pollPending || stopped) return;
    pollPending = true;
    const requestRevision = revision;
    const requestEpoch = connectionEpoch;
    try {
      const response = await fetch('/api/wsjtx', { cache: 'no-store' });
      if (!response.ok) throw new Error('WSJT-X backend is unavailable.');
      const status = await response.json();
      if (!stopped && requestRevision === revision && requestEpoch === connectionEpoch) renderSnapshot(status);
    } catch (_) {
      if (!stopped && requestRevision === revision && requestEpoch === connectionEpoch) {
        unavailable = true;
        renderEngine();
        renderDecodes();
      }
    } finally { pollPending = false; }
  }

  function connectEvents() {
    if (stopped) return;
    clearTimeout(reconnectTimer);
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    try { socket = new WebSocket(`${scheme}//${location.host}/api/wsjtx/events`); }
    catch (_) { reconnectTimer = setTimeout(connectEvents, 1500); return; }
    socket.onopen = () => {
      connectionEpoch += 1;
      // A restarted backend begins a new sequence on a new event connection.
      sequence = -1;
    };
    socket.onmessage = message => {
      try {
        const event = JSON.parse(message.data);
        if (event.type === 'status') renderSnapshot(event.status);
      } catch (_) { /* Ignore malformed event frames. */ }
    };
    socket.onclose = () => {
      socket = null;
      if (!stopped) reconnectTimer = setTimeout(connectEvents, 1500);
    };
    socket.onerror = () => socket?.close();
  }

  async function loadModeSetting() {
    try {
      const response = await fetch('/api/settings', { cache: 'no-store' });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'FT mode settings are unavailable.');
      configuredMode = normalizeMode(result?.settings?.wsjtx?.mode) || 'FT4';
      modeSettingsLoaded = true;
      modeError = '';
    } catch (error) {
      modeError = error.message || 'Unable to read the saved FT mode.';
    }
    renderModeSetting();
  }

  async function chooseMode(mode) {
    if (!normalizeMode(mode) || modeSaving || !modeSettingsLoaded || modeLocked()) return;
    if (configuredMode === mode) return;
    modeSaving = true;
    modeError = '';
    renderModeSetting();
    try {
      const response = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: { wsjtx: { mode } } }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'Unable to save the FT mode.');
      configuredMode = normalizeMode(result?.settings?.wsjtx?.mode) || mode;
      text('wsjtx-mode', reportedMode() || configuredMode);
    } catch (error) {
      modeError = error.message || 'Unable to save the FT mode.';
    } finally {
      modeSaving = false;
      renderModeSetting();
    }
  }

  for (const { button, mode } of modeButtons) {
    button.addEventListener('click', () => chooseMode(mode));
  }

  engineSelect.addEventListener('change', () => {
    manualEngineSelection = true;
    selectedEngineKey = engineSelect.value;
    selectedDecodeKey = '';
    renderEngine();
    renderDecodes();
  });
  decodeDepthSelect.addEventListener('change', async () => {
    if (!snapshot || pending || stale()) return;
    const depth = decodeDepthSelect.value;
    pending = true;
    renderEngine();
    try {
      const response = await fetch('/api/wsjtx/decode-depth', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ depth }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Unable to change decoder depth.');
      renderSnapshot(result);
    } catch (error) {
      actionError = error.message || 'Unable to change decoder depth.';
      text('wsjtx-decode-depth-detail', actionError);
      renderEngine();
    } finally {
      pending = false;
      renderControlsAfterCommand();
    }
  });
  byId('wsjtx-clear').addEventListener('click', () => {
    for (const entry of activityEntries(false)) clearedKeys.add(entry.key);
    selectedDecodeKey = '';
    renderDecodes();
  });
  enableButton.addEventListener('click', async () => {
    if (!snapshot || pending || stale()) return;
    const action = ++actionSequence;
    pending = true;
    renderEngine();
    const requestEpoch = connectionEpoch;
    try {
      const managed = snapshot.engine;
      if (managed && managed.running !== true) manualEngineSelection = false;
      const response = await fetch(managed ? '/api/wsjtx/engine/enabled' : '/api/wsjtx/enabled', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: managed ? managed.running !== true : !snapshot.enabled }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Unable to change listening state.');
      if (requestEpoch === connectionEpoch && action === actionSequence) renderSnapshot(result);
    } catch (error) {
      if (requestEpoch === connectionEpoch && action === actionSequence) text('wsjtx-detail', error.message || 'Unable to change listening state.');
    } finally {
      if (action === actionSequence) pending = false;
      renderControlsAfterCommand();
    }
  });

  resetButton.addEventListener('click', async () => {
    if (!snapshot?.engine || pending) return;
    const action = ++actionSequence;
    pending = true;
    actionError = '';
    renderEngine();
    const requestEpoch = connectionEpoch;
    try {
      const response = await fetch('/api/wsjtx/reset', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Unable to reset WSJT-X.');
      if (requestEpoch !== connectionEpoch || action !== actionSequence) return;
      manualEngineSelection = false;
      renderSnapshot(result);
      // The backend stops the engine before answering; a resource it could not
      // release is reported as an engine error instead of a clean reset.
      if (result.engine?.state === 'error') throw new Error(result.engine.detail || 'WSJT-X reset did not finish.');
    } catch (error) {
      if (requestEpoch === connectionEpoch && action === actionSequence) {
        const message = error.message || 'Unable to reset WSJT-X.';
        text('wsjtx-detail', message);
        appendMonitor(`reset:${Date.now()}`, message, 'error');
      }
    } finally {
      if (action === actionSequence) pending = false;
      renderControlsAfterCommand();
    }
  });

  byId('wsjtx-tx-enable').addEventListener('click', () => command('/qso/enabled', { enabled: snapshot?.qso?.enabled !== true }));
  byId('wsjtx-halt').addEventListener('click', () => command('/qso/enabled', { enabled: false }, true));
  byId('wsjtx-cq').addEventListener('click', () => command('/qso/cq', {}));
  byId('wsjtx-auto').addEventListener('change', () => command('/qso/auto', { enabled: byId('wsjtx-auto').checked }));
  byId('wsjtx-tx-slot').addEventListener('change', () => command('/qso/slot', { slot: byId('wsjtx-tx-slot').value }));
  function setOffset(hz) {
    if (!Number.isFinite(hz)) return;
    const limits = offsetLimits();
    void command('/qso/offset', { hz: Math.round(Math.max(limits.min, Math.min(limits.max, hz))) });
  }
  byId('wsjtx-offset').addEventListener('change', () => setOffset(Number(byId('wsjtx-offset').value)));
  byId('wsjtx-spectrum-hit').addEventListener('click', event => {
    const bounds = byId('wsjtx-spectrum-hit').getBoundingClientRect();
    if (bounds.width > 0) setOffset((event.clientX - bounds.left) / bounds.width * spectrumSpan);
  });
  byId('wsjtx-spectrum-hit').addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    setOffset(event.key === 'Home' ? 0 : event.key === 'End' ? spectrumSpan : (snapshot?.qso?.tx_offset_hz ?? 1500) + (event.key === 'ArrowRight' ? 1 : -1) * (event.shiftKey ? 100 : 10));
  });

  const pollTimer = setInterval(() => {
    if (!page.hidden && !document.hidden) { void loadState(); renderEngine(); renderTracking(); }
  }, POLL_INTERVAL_MS);
  window.addEventListener('pagehide', () => {
    stopped = true;
    clearInterval(pollTimer);
    clearTimeout(reconnectTimer);
    if (socket) { socket.onclose = null; socket.close(); }
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted && stopped) window.location.reload();
  });
  window.addEventListener('resize', renderSpectrum);
  window.PiSatWsjtx = { renderSnapshot, decodeRow, renderTracking };
  void loadState();
  void loadModeSetting();
  connectEvents();
})();
