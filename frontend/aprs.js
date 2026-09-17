(() => {
  'use strict';

  const byId = id => document.getElementById(id);
  const stateNode = byId('aprs-state');
  if (!stateNode) return;

  const MAX_PACKETS = 25;
  const POLL_INTERVAL_MS = 2000;
  const enableButton = byId('aprs-enable');
  const list = byId('aprs-packets');
  const armToggle = byId('aprs-arm');
  const sendBeaconButton = byId('aprs-send-beacon');
  const sendStatusButton = byId('aprs-send-status');
  const sendMessageButton = byId('aprs-send-message');
  const addresseeInput = byId('aprs-tx-addressee');
  const textInput = byId('aprs-tx-text');
  const seen = new Set();
  let enabled = false;
  let armed = false;
  let socket = null;
  let reconnectTimer = null;
  let pollTimer = null;
  let currentSequence = 0;
  let lastPacketCount = -1;

  function stateDetail(status) {
    if (status.error) return status.error;
    const details = {
      Disabled: 'Decoder is off. Radio audio continues normally.',
      'Waiting for radio audio': 'Waiting for the existing native IC-9700 audio stream.',
      Listening: 'Monitoring MAIN for 1200 baud APRS traffic.',
      'Decode failed': 'The decoder could not complete the current operation.',
    };
    return details[status.state] || 'APRS decoder status unavailable.';
  }

  function emptyMessage() {
    return enabled ? 'Listening. No APRS packets received yet.' : 'Enable the decoder to listen for APRS traffic.';
  }

  function renderEmpty() {
    list.replaceChildren();
    const empty = document.createElement('p');
    empty.className = 'text-body-secondary mb-0';
    empty.textContent = emptyMessage();
    list.appendChild(empty);
  }

  function renderStatus(status) {
    if (!status) return;
    enabled = status.enabled === true;
    stateNode.textContent = status.state || 'Disabled';
    stateNode.dataset.state = (status.state || 'Disabled').toLowerCase().replaceAll(' ', '-');
    byId('aprs-detail').textContent = stateDetail(status);
    enableButton.textContent = enabled ? 'Disable' : 'Enable';
    enableButton.classList.toggle('btn-danger', enabled);
    enableButton.classList.toggle('btn-primary', !enabled);
    enableButton.setAttribute('aria-pressed', String(enabled));
    byId('aprs-source').textContent = status.audio_source || 'Native IC-9700 RX PCM';
    byId('aprs-engine').textContent = status.decoder || 'Dire Wolf';
    byId('aprs-count').textContent = `${status.packet_count || 0} received`;
    byId('aprs-retained').textContent = `${status.retained || 0} / ${status.retention || MAX_PACKETS}`;
    renderInput(status);
    if (!list.querySelector('.aprs-packet')) {
      renderEmpty();
    }
    currentSequence = Math.max(currentSequence, Number(status.sequence) || 0);
  }

  function levelText(value) {
    return Number.isFinite(value) ? `${value} dBFS` : 'silent';
  }

  function renderInput(status) {
    const parts = [];
    if (status.input_rate) parts.push(`${status.input_rate} Hz`);
    if (status.input_channels) parts.push(status.input_channels === 2 ? 'stereo' : 'mono');
    if (status.input_channel) parts.push(`using ${status.input_channel}`);
    if (status.input_samples) parts.push(`${status.input_samples} samples`);
    byId('aprs-input').textContent = parts.length ? parts.join(' | ') : 'No audio yet';
    byId('aprs-levels').textContent =
      `Left ${levelText(status.input_level_left_dbfs)}, right ${levelText(status.input_level_right_dbfs)}`;
    byId('aprs-kiss').textContent = status.kiss_connected
      ? `Connected on ${status.kiss_port}`
      : `No connection on ${status.kiss_port} (${status.kiss_failures || 0} failed attempts)`;
    byId('aprs-decoder-level').textContent = levelText(status.decoder_level_dbfs);
    renderDecoderGain(status);
  }

  function renderDecoderGain(status) {
    const slider = byId('aprs-rx-gain');
    if (!slider || document.activeElement === slider) return;
    const gain = Number(status.rx_gain_db);
    if (!Number.isFinite(gain)) return;
    slider.value = String(gain);
    byId('aprs-rx-gain-value').textContent = `${gain} dB`;
  }

  async function loadDecoderLog() {
    const node = byId('aprs-decoder-log');
    try {
      const response = await fetch('/api/aprs/decoder-log', { cache: 'no-store' });
      if (!response.ok) throw new Error('Log request failed');
      const result = await response.json();
      const lines = Array.isArray(result.lines) ? result.lines : [];
      node.textContent = lines.length ? lines.join('\n') : 'No decoder output yet.';
      node.scrollTop = node.scrollHeight;
    } catch (_) {
      node.textContent = 'Decoder log is unavailable.';
    }
  }

  function renderTransmit(status) {
    if (!status) return;
    armed = status.armed === true;
    armToggle.checked = armed;
    byId('aprs-tx-state').textContent = status.state || (armed ? 'Ready' : 'Disarmed');
    byId('aprs-tx-last').textContent = status.last_tx_utc
      ? new Date(status.last_tx_utc).toLocaleString()
      : 'Never';
    byId('aprs-tx-detail').textContent = status.error || status.warning || '';
    renderTransmitControls();
  }

  function renderTransmitControls() {
    sendBeaconButton.disabled = !armed;
    sendStatusButton.disabled = !armed;
    sendMessageButton.disabled = !armed;
    addresseeInput.disabled = !armed;
    textInput.disabled = !armed;
  }

  function transmitButton(button, body) {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const response = await fetch('/api/aprs/transmit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body()),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || 'Unable to transmit');
        renderTransmit(result.status);
      } catch (error) {
        byId('aprs-tx-detail').textContent = error.message || 'Unable to transmit.';
      } finally {
        renderTransmitControls();
      }
    });
  }

  function packetCard(entry) {
    const article = document.createElement('article');
    article.className = 'aprs-packet';
    article.dataset.packetId = entry.id || '';

    const header = document.createElement('div');
    header.className = 'aprs-packet-head';
    const source = document.createElement('strong');
    source.textContent = entry.source || 'Unknown';
    const kind = document.createElement('span');
    kind.className = 'aprs-kind';
    kind.textContent = entry.kind || 'Other';
    const time = document.createElement('time');
    time.dateTime = entry.timestamp_utc || '';
    time.textContent = entry.timestamp_utc ? new Date(entry.timestamp_utc).toLocaleTimeString() : '';
    header.append(source, kind, time);

    const summary = document.createElement('p');
    summary.className = 'aprs-summary mb-0';
    summary.textContent = entry.summary || '';

    const details = document.createElement('details');
    details.className = 'aprs-raw';
    const toggle = document.createElement('summary');
    toggle.textContent = 'Raw';
    const code = document.createElement('code');
    code.textContent = entry.raw || entry.info || '';
    details.append(toggle, code);

    article.append(header, summary, details);
    return article;
  }

  function renderPackets(entries) {
    const items = Array.isArray(entries) ? entries.slice(0, MAX_PACKETS) : [];
    seen.clear();
    list.replaceChildren();
    if (!items.length) {
      renderEmpty();
      return;
    }
    items.forEach(entry => {
      if (entry && entry.id) seen.add(entry.id);
      list.appendChild(packetCard(entry));
    });
  }

  function prependPacket(entry) {
    if (!entry || typeof entry !== 'object') return;
    if (entry.id && seen.has(entry.id)) return;
    if (entry.id) seen.add(entry.id);
    [...list.children].forEach(child => {
      if (!child.classList.contains('aprs-packet')) child.remove();
    });
    list.prepend(packetCard(entry));
    while (list.children.length > MAX_PACKETS) list.lastElementChild.remove();
  }

  async function loadPackets() {
    try {
      const response = await fetch('/api/aprs/packets', { cache: 'no-store' });
      if (!response.ok) throw new Error('Packet request failed');
      renderPackets(await response.json());
    } catch (_) {
      list.textContent = 'APRS packets are unavailable.';
    }
  }

  async function loadState() {
    try {
      const response = await fetch('/api/aprs', { cache: 'no-store' });
      if (!response.ok) throw new Error('Status request failed');
      const status = await response.json();
      // Never let a slow response overwrite newer live data.
      const sequence = Number(status.sequence) || 0;
      if (sequence && sequence < currentSequence) return;
      renderStatus(status);
      renderTransmit(status.transmit);
      const count = Number(status.packet_count) || 0;
      if (count !== lastPacketCount) {
        lastPacketCount = count;
        // The events socket pushes packets instantly; polling is the fallback
        // when it is reconnecting or unavailable.
        if (!socket || socket.readyState !== 1) loadPackets();
      }
    } catch (_) {
      byId('aprs-detail').textContent = 'APRS backend is unavailable.';
    }
  }

  function startStatusPolling() {
    clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      const page = byId('aprs-page');
      if (!page || page.hidden || document.hidden) return;
      loadState();
    }, POLL_INTERVAL_MS);
  }

  function handleEvent(event) {
    const sequence = Number(event.sequence) || 0;
    if (sequence && sequence <= currentSequence) return;
    if (sequence) currentSequence = sequence;
    if (event.type === 'status') {
      renderStatus(event.status);
    } else if (event.type === 'packet') {
      prependPacket(event.packet);
    }
  }

  function connectEvents() {
    clearTimeout(reconnectTimer);
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${scheme}//${location.host}/api/aprs/events`);
    socket.onmessage = message => {
      try { handleEvent(JSON.parse(message.data)); } catch (_) { /* Ignore malformed events. */ }
    };
    socket.onclose = () => {
      socket = null;
      reconnectTimer = setTimeout(connectEvents, 1500);
    };
    socket.onerror = () => socket?.close();
  }

  enableButton.addEventListener('click', async () => {
    enableButton.disabled = true;
    try {
      const response = await fetch('/api/aprs/enabled', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !enabled }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'Unable to change decoder state');
      renderStatus(result);
    } catch (error) {
      byId('aprs-detail').textContent = error.message || 'Unable to change decoder state.';
    } finally {
      enableButton.disabled = false;
    }
  });

  armToggle.addEventListener('change', async () => {
    armToggle.disabled = true;
    try {
      const response = await fetch('/api/aprs/transmit/arm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ armed: armToggle.checked }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'Unable to change arming');
      renderTransmit(result);
    } catch (error) {
      armToggle.checked = !armToggle.checked;
      byId('aprs-tx-detail').textContent = error.message || 'Unable to change arming.';
    } finally {
      armToggle.disabled = false;
    }
  });

  transmitButton(sendBeaconButton, () => ({ kind: 'beacon' }));
  transmitButton(sendStatusButton, () => ({ kind: 'status', text: textInput.value }));
  transmitButton(sendMessageButton, () => ({
    kind: 'message',
    addressee: addresseeInput.value,
    text: textInput.value,
  }));

  let logTimer = null;
  const logDetails = byId('aprs-decoder-log')?.closest('details') || null;
  byId('aprs-log-refresh').addEventListener('click', loadDecoderLog);

  const gainSlider = byId('aprs-rx-gain');
  let gainTimer = null;
  gainSlider?.addEventListener('input', () => {
    byId('aprs-rx-gain-value').textContent = `${gainSlider.value} dB`;
    clearTimeout(gainTimer);
    gainTimer = setTimeout(async () => {
      try {
        const response = await fetch('/api/aprs/decoder-level', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ gain_db: Number(gainSlider.value) }),
        });
        if (!response.ok) throw new Error('Decoder level was not accepted.');
        renderStatus(await response.json());
      } catch (error) {
        byId('aprs-detail').textContent = error.message || 'Unable to set the decoder level.';
      }
    }, 150);
  });

  logDetails?.addEventListener('toggle', () => {
    clearInterval(logTimer);
    logTimer = null;
    if (!logDetails.open) return;
    loadDecoderLog();
    logTimer = setInterval(loadDecoderLog, 3000);
  });

  renderTransmitControls();

  window.PiSatAprs = { packetCard, prependPacket, renderStatus };
  loadState();
  loadPackets();
  connectEvents();
  startStatusPolling();
})();
