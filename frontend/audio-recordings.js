(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const list = byId('audio-recordings-list');
  const player = byId('audio-recordings-player');
  let activeButton = null;
  let activeId = null;

  function formatDuration(seconds) {
    const value = Math.max(0, Math.round(Number(seconds) || 0));
    return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`;
  }

  function formatSize(bytes) {
    const value = Math.max(0, Number(bytes) || 0);
    return value >= 1024 * 1024
      ? `${(value / (1024 * 1024)).toFixed(1)} MB`
      : `${Math.max(1, Math.round(value / 1024))} KB`;
  }

  function formatCreated(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Saved recording';
    return date.toLocaleString([], {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    });
  }

  function setButtonPlaying(button, playing) {
    if (!button) return;
    button.textContent = playing ? 'Stop' : 'Play';
    button.setAttribute('aria-pressed', String(playing));
  }

  function stopPlayback() {
    if (player) {
      player.pause();
      player.removeAttribute('src');
      player.load();
    }
    setButtonPlaying(activeButton, false);
    activeButton = null;
    activeId = null;
  }

  function makeText(tag, className, value) {
    const node = document.createElement(tag);
    node.className = className;
    node.textContent = value;
    return node;
  }

  function render(recordings) {
    if (!list) return;
    stopPlayback();
    list.replaceChildren();
    if (!recordings.length) {
      list.append(makeText('p', 'audio-recordings-empty', 'No recordings saved.'));
      return;
    }
    for (const recording of recordings) {
      const row = document.createElement('article');
      row.className = 'audio-recording-row';
      row.dataset.recordingId = recording.id;
      const details = document.createElement('div');
      details.className = 'audio-recording-details';
      details.append(
        makeText('strong', '', formatCreated(recording.created_at_utc)),
        makeText(
          'span',
          '',
          `${formatDuration(recording.duration_seconds)} · ${formatSize(recording.size_bytes)}`,
        ),
      );
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-outline-primary audio-recording-action';
      button.textContent = 'Play';
      button.setAttribute('aria-pressed', 'false');
      button.addEventListener('click', () => {
        if (activeId === recording.id && !player?.paused) {
          stopPlayback();
          return;
        }
        stopPlayback();
        activeId = recording.id;
        activeButton = button;
        setButtonPlaying(button, true);
        player.src = `/api/audio-recordings/${encodeURIComponent(recording.id)}/audio`;
        player.play().catch(() => stopPlayback());
      });
      row.append(details, button);
      list.append(row);
    }
  }

  async function load() {
    try {
      const response = await fetch('/api/audio-recordings', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      render(Array.isArray(result.recordings) ? result.recordings : []);
    } catch {
      if (!list) return;
      list.replaceChildren(
        makeText('p', 'audio-recordings-empty', 'Audio recordings are not available.'),
      );
    }
  }

  async function readState() {
    const response = await fetch('/api/audio-recordings/state', { cache: 'no-store' });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    return result;
  }

  async function setEnabled(enabled) {
    const response = await fetch('/api/audio-recordings/enabled', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    return result;
  }

  player?.addEventListener('ended', stopPlayback);
  player?.addEventListener('error', stopPlayback);
  window.AudioRecordings = { load, readState, setEnabled, stopPlayback };
})();
