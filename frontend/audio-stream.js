(() => {
  let timer = 0;
  const byId = (id) => document.getElementById(id);
  const text = (id, value) => { const node = byId(id); if (node) node.textContent = value; };
  const number = (value) => Number(value || 0).toLocaleString();

  function render(status) {
    const enabled = Boolean(status.enabled);
    const running = Boolean(status.running);
    const listeners = Number(status.listeners || 0);
    const state = byId('audio-stream-state');
    if (state) {
      state.textContent = !enabled ? 'Disabled' : running ? 'Listening' : 'Unavailable';
      state.dataset.state = !enabled ? 'disabled' : running ? 'listening' : 'error';
    }
    text('audio-stream-detail', !enabled
      ? 'Passive listener is disabled in the config file.'
      : running
        ? `${listeners} external client${listeners === 1 ? '' : 's'} connected.`
        : (status.last_error || 'Listener is not running.'));
    text('audio-stream-running', running ? 'Listening' : enabled ? 'Unavailable' : 'Disabled');
    text('audio-stream-address', `${status.host || '--'}:${status.port ?? '--'}`);
    text('audio-stream-port', String(status.port ?? '--'));
    text('audio-stream-listeners', number(listeners));
    text('audio-stream-client-limit', number(status.max_clients));
    text('audio-stream-source', status.source || 'Existing Pi-Sat mono SUB/RX PCM fan-out');
    text('audio-stream-frames', number(status.frames_sent));
    text('audio-stream-bytes', number(status.audio_bytes));
    text('audio-stream-accepted', number(status.accepted));
    text('audio-stream-refused', number(status.refused));
    text('audio-stream-drops', number(status.overflow_dropped));
    text('audio-stream-error', status.last_error || 'None');
  }

  async function load() {
    try {
      const response = await fetch('/api/audio-stream', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) {
      render({ enabled: true, running: false, last_error: 'Status unavailable' });
      text('audio-stream-detail', `Unable to read listener status: ${error.message || error}`);
    }
    if (!timer) timer = window.setInterval(() => {
      if (document.querySelector('[data-module-view="audio-stream"]:not([hidden])')) load();
    }, 2000);
  }

  window.AudioStreamModule = { load };
})();
