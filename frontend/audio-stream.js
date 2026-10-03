(() => {
  let timer = 0;
  let currentStatus = null;
  let saving = false;
  const byId = (id) => document.getElementById(id);
  const text = (id, value) => { const node = byId(id); if (node) node.textContent = value; };
  const number = (value) => Number(value || 0).toLocaleString();

  function render(status) {
    currentStatus = status;
    const enabled = Boolean(status.enabled);
    const running = Boolean(status.running);
    const active = Boolean(status.active);
    const listeners = Number(status.listeners || 0);
    const state = byId('audio-stream-state');
    if (state) {
      state.textContent = !enabled ? 'Disabled' : !running ? 'Unavailable' : active ? 'Active' : 'Ready';
      state.dataset.state = !enabled ? 'disabled' : running ? 'listening' : 'error';
    }
    const button = byId('audio-stream-enable');
    if (button) {
      button.textContent = enabled ? 'Disable' : 'Enable';
      button.setAttribute('aria-pressed', String(enabled));
      button.disabled = saving;
    }
    text('audio-stream-detail', !enabled
      ? 'External DATA Decode is disabled.'
      : !running
        ? (status.last_error || 'External DATA Decode server is unavailable.')
        : active
          ? `${listeners} external client${listeners === 1 ? '' : 's'} connected. IF audio is being demodulated.`
          : (status.last_error || 'Waiting for the external Windows client.'));
    text('audio-stream-running', running ? 'Listening' : enabled ? 'Unavailable' : 'Disabled');
    text('audio-stream-address', `${status.host || '--'}:${status.port ?? '--'}`);
    text('audio-stream-port', String(status.port ?? '--'));
    text('audio-stream-listeners', number(listeners));
    text('audio-stream-client-limit', number(status.max_clients));
    text('audio-stream-source', active ? 'Demodulated IF · 48 kHz mono PCM16' : 'Normal AF audio');
    text('audio-stream-frames', number(status.frames_sent));
    text('audio-stream-bytes', number(status.audio_bytes));
    text('audio-stream-accepted', number(status.accepted));
    text('audio-stream-refused', number(status.refused));
    text('audio-stream-drops', number(status.overflow_dropped));
    text('audio-stream-source-drops', number(status.source_dropped));
    text('audio-stream-error', status.last_error || 'None');
  }

  async function load(force = false) {
    if (saving && !force) return;
    try {
      const response = await fetch('/api/audio-stream', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) {
      currentStatus = null;
      const state = byId('audio-stream-state');
      if (state) { state.textContent = 'Unavailable'; state.dataset.state = 'error'; }
      const button = byId('audio-stream-enable');
      if (button) button.disabled = true;
      text('audio-stream-detail', `Unable to read External DATA Decode status: ${error.message || error}`);
    }
    if (!timer) timer = window.setInterval(() => {
      if (document.querySelector('[data-module-view="audio-stream"]:not([hidden])')) load();
    }, 2000);
  }

  async function toggle() {
    if (saving || !currentStatus) return;
    const button = byId('audio-stream-enable');
    const enabled = !Boolean(currentStatus.enabled);
    saving = true;
    if (button) button.disabled = true;
    text('audio-stream-detail', `${enabled ? 'Enabling' : 'Disabling'} External DATA Decode...`);
    try {
      const response = await fetch('/api/audio-stream/enabled', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
      await load(true);
    } catch (error) {
      text('audio-stream-detail', `External DATA Decode ${enabled ? 'enable' : 'disable'} failed: ${error.message || error}`);
    } finally {
      saving = false;
      if (button) button.disabled = !currentStatus;
    }
  }

  byId('audio-stream-enable')?.addEventListener('click', toggle);
  window.AudioStreamModule = { load };
})();
