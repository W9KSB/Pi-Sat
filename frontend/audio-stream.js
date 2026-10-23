(() => {
  let timer = 0;
  let currentStatus = null;
  let saving = false;
  let statusSequence = 0;
  const byId = (id) => document.getElementById(id);
  const text = (id, value) => { const node = byId(id); if (node) node.textContent = value; };
  const number = (value) => Number(value || 0).toLocaleString();
  const startButton = byId('audio-stream-start');
  const stopButton = byId('audio-stream-stop');

  function renderButtons() {
    if (startButton) startButton.disabled = saving || !currentStatus || currentStatus.enabled === true;
    if (stopButton) stopButton.disabled = saving || !currentStatus || currentStatus.enabled !== true;
    startButton?.setAttribute('aria-busy', String(saving && currentStatus?.enabled !== true));
    stopButton?.setAttribute('aria-busy', String(saving && currentStatus?.enabled === true));
  }

  function render(status) {
    currentStatus = status;
    const enabled = Boolean(status.enabled);
    const running = Boolean(status.running);
    const active = Boolean(status.active);
    const listeners = Number(status.listeners || 0);
    const demodulation = status.demodulation || 'FM';
    const state = byId('audio-stream-state');
    if (state) {
      state.textContent = !enabled ? 'Disabled' : !running ? 'Unavailable' : active ? 'Active' : 'Ready';
      state.dataset.state = !enabled ? 'disabled' : running ? 'listening' : 'error';
    }
    text('audio-stream-detail', !enabled
      ? 'External DATA Decode is disabled.'
      : !running
        ? (status.last_error || 'External DATA Decode server is unavailable.')
        : active
          ? `${listeners} external client${listeners === 1 ? '' : 's'} connected. IF audio is being demodulated as ${demodulation}.`
          : (status.last_error || 'Waiting for the external Windows client.'));
    text('audio-stream-running', running ? 'Listening' : enabled ? 'Unavailable' : 'Disabled');
    text('audio-stream-address', `${status.host || '--'}:${status.port ?? '--'}`);
    text('audio-stream-port', String(status.port ?? '--'));
    text('audio-stream-listeners', number(listeners));
    text('audio-stream-client-limit', number(status.max_clients));
    text('audio-stream-source', active ? `${demodulation} demodulated IF · 48 kHz mono PCM16` : 'Normal AF audio');
    text('audio-stream-frames', number(status.frames_sent));
    text('audio-stream-bytes', number(status.audio_bytes));
    text('audio-stream-accepted', number(status.accepted));
    text('audio-stream-refused', number(status.refused));
    text('audio-stream-drops', number(status.overflow_dropped));
    text('audio-stream-source-drops', number(status.source_dropped));
    text('audio-stream-error', status.last_error || 'None');
    renderButtons();
  }

  async function load(force = false) {
    if (saving && !force) return;
    const sequence = ++statusSequence;
    try {
      const response = await fetch('/api/audio-stream', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const status = await response.json();
      if (sequence !== statusSequence) return;
      render(status);
    } catch (error) {
      if (sequence !== statusSequence) return;
      currentStatus = null;
      const state = byId('audio-stream-state');
      if (state) { state.textContent = 'Unavailable'; state.dataset.state = 'error'; }
      text('audio-stream-detail', `Unable to read External DATA Decode status: ${error.message || error}`);
      renderButtons();
    }
    if (!timer) timer = window.setInterval(() => {
      if (document.querySelector('[data-module-view="audio-stream"]:not([hidden])')) load();
    }, 2000);
  }

  async function setEnabled(enabled) {
    if (saving || !currentStatus || Boolean(currentStatus.enabled) === enabled) return;
    ++statusSequence;
    saving = true;
    renderButtons();
    const state = byId('audio-stream-state');
    if (state) { state.textContent = enabled ? 'Starting' : 'Stopping'; state.dataset.state = 'pending'; }
    text('audio-stream-detail', `${enabled ? 'Starting' : 'Stopping'} External DATA Decode stream...`);
    try {
      const response = await fetch('/api/audio-stream/enabled', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
      render(result);
    } catch (error) {
      if (state) { state.textContent = 'Error'; state.dataset.state = 'error'; }
      text('audio-stream-detail', `${enabled ? 'Start' : 'Stop'} stream failed: ${error.message || error}`);
    } finally {
      saving = false;
      renderButtons();
    }
  }

  startButton?.addEventListener('click', () => setEnabled(true));
  stopButton?.addEventListener('click', () => setEnabled(false));
  window.AudioStreamModule = { load, start: () => setEnabled(true), stop: () => setEnabled(false) };
})();
