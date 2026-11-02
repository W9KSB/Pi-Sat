(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const list = byId('audio-recordings-list');
  const player = byId('audio-recordings-player');
  const icons = {
    play: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M8 5v14l11-7z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
    stop: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>',
    download: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 3v12m0 0 4-4m-4 4-4-4M5 20h14"/></svg>',
  };
  let playback = null;
  let seek = null;
  let elapsed = null;
  let total = null;
  let pauseButton = null;
  let stopButton = null;
  let activeButton = null;
  let activeId = null;
  let activeDuration = 0;
  let pendingSeek = null;
  let scrubbing = false;

  function formatDuration(seconds) {
    const value = Math.max(0, Math.round(Number(seconds) || 0));
    return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`;
  }

  function formatPlaybackTime(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
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

  function setButtonActive(button, active, playing) {
    if (!button) return;
    button.dataset.active = String(active);
    button.dataset.playing = String(playing);
    button.setAttribute('aria-pressed', String(active));
  }

  function renderTransportState() {
    const active = Boolean(activeId && player);
    const paused = !player || player.paused;
    if (pauseButton) {
      const resume = active && paused;
      pauseButton.disabled = !active;
      pauseButton.className = `audio-recording-transport-button ${
        resume ? 'audio-recording-resume' : 'audio-recording-pause'
      }`;
      pauseButton.innerHTML = resume ? icons.play : icons.pause;
      pauseButton.title = resume ? 'Resume recording' : 'Pause recording';
      pauseButton.setAttribute('aria-label', pauseButton.title);
    }
    if (stopButton) stopButton.disabled = !active;
    if (seek) seek.disabled = !active;
    if (activeButton) setButtonActive(activeButton, active, !paused);
  }

  function makeIconButton(tag, className, icon, label) {
    const node = document.createElement(tag);
    node.className = className;
    node.innerHTML = icon;
    node.title = label;
    node.setAttribute('aria-label', label);
    return node;
  }

  function ensurePlaybackControls() {
    if (playback) return true;
    const headerBody = document.querySelector(
      '#audio-recordings-page .standard-page-header > .card-body',
    );
    if (!headerBody) return false;

    playback = document.createElement('div');
    playback.id = 'audio-recordings-playback';
    playback.className = 'audio-recording-transport';
    playback.setAttribute('role', 'group');
    playback.setAttribute('aria-label', 'Recording playback controls');

    elapsed = makeText('span', 'audio-recording-time', '0:00');
    elapsed.id = 'audio-recordings-elapsed';
    seek = document.createElement('input');
    seek.id = 'audio-recordings-seek';
    seek.className = 'audio-recording-range';
    seek.type = 'range';
    seek.min = '0';
    seek.max = '0';
    seek.step = '0.1';
    seek.value = '0';
    seek.disabled = true;
    seek.setAttribute('aria-label', 'Playback position');
    total = makeText('span', 'audio-recording-time', '0:00');
    total.id = 'audio-recordings-duration';
    pauseButton = makeIconButton(
      'button',
      'audio-recording-transport-button audio-recording-pause',
      icons.pause,
      'Pause recording',
    );
    pauseButton.type = 'button';
    pauseButton.disabled = true;
    stopButton = makeIconButton(
      'button',
      'audio-recording-transport-button audio-recording-stop',
      icons.stop,
      'Stop recording',
    );
    stopButton.type = 'button';
    stopButton.disabled = true;
    playback.append(elapsed, seek, total, pauseButton, stopButton);
    headerBody.append(playback);

    pauseButton.addEventListener('click', () => {
      if (!activeId || !player) return;
      if (player.paused) player.play().catch(() => stopPlayback());
      else player.pause();
      renderTransportState();
    });
    stopButton.addEventListener('click', stopPlayback);
    seek.addEventListener('pointerdown', () => {
      scrubbing = true;
    });
    seek.addEventListener('change', () => {
      scrubbing = false;
      updatePlaybackPosition();
    });
    seek.addEventListener('input', event => {
      if (!activeId || !player) return;
      const duration = Number(player.duration);
      const maximum = Number.isFinite(duration) && duration > 0 ? duration : activeDuration;
      const target = Math.min(maximum, Math.max(0, Number(event.target.value) || 0));
      pendingSeek = target;
      if (Number.isFinite(duration)) {
        player.currentTime = target;
        pendingSeek = null;
      }
      updatePlaybackPosition(true);
    });
    return true;
  }

  function updatePlaybackPosition(force = false) {
    if (scrubbing && !force) return;
    const playerDuration = Number(player?.duration);
    const duration = Number.isFinite(playerDuration) && playerDuration > 0
      ? playerDuration
      : activeDuration;
    const current = Math.min(
      duration,
      Math.max(0, Number(player?.currentTime) || 0),
    );
    if (seek) {
      seek.max = String(duration);
      seek.value = String(current);
      seek.disabled = !activeId;
      const elapsedText = formatPlaybackTime(current);
      const totalText = formatPlaybackTime(duration);
      if (elapsed) elapsed.textContent = elapsedText;
      if (total) total.textContent = totalText;
      seek.setAttribute('aria-valuetext', `${elapsedText} of ${totalText}`);
    }
  }

  function showPlayback(recording) {
    activeDuration = Math.max(0, Number(recording.duration_seconds) || 0);
    pendingSeek = null;
    scrubbing = false;
    if (elapsed) elapsed.textContent = '0:00';
    if (total) total.textContent = formatPlaybackTime(activeDuration);
    if (seek) {
      seek.max = String(activeDuration);
      seek.value = '0';
      seek.setAttribute('aria-valuetext', `0:00 of ${formatPlaybackTime(activeDuration)}`);
    }
    renderTransportState();
  }

  function resetTransport() {
    activeDuration = 0;
    pendingSeek = null;
    scrubbing = false;
    if (seek) {
      seek.max = '0';
      seek.value = '0';
      seek.removeAttribute('aria-valuetext');
    }
    if (elapsed) elapsed.textContent = '0:00';
    if (total) total.textContent = '0:00';
    renderTransportState();
  }

  function stopPlayback() {
    if (player) {
      player.pause();
      player.removeAttribute('src');
      player.load();
    }
    setButtonActive(activeButton, false, false);
    activeButton = null;
    activeId = null;
    resetTransport();
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
      const actions = document.createElement('div');
      actions.className = 'audio-recording-actions';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'audio-recording-icon-button audio-recording-play';
      button.innerHTML = icons.play;
      button.title = `Play recording from ${formatCreated(recording.created_at_utc)}`;
      button.setAttribute('aria-label', button.title);
      button.setAttribute('aria-pressed', 'false');
      button.addEventListener('click', () => {
        if (activeId === recording.id) {
          if (player?.paused) player.play().catch(() => stopPlayback());
          renderTransportState();
          return;
        }
        stopPlayback();
        activeId = recording.id;
        activeButton = button;
        showPlayback(recording);
        player.src = `/api/audio-recordings/${encodeURIComponent(recording.id)}/audio`;
        player.play().catch(() => stopPlayback());
        renderTransportState();
      });
      const download = document.createElement('a');
      download.className = 'audio-recording-icon-button audio-recording-download';
      download.innerHTML = icons.download;
      download.href = `/api/audio-recordings/${encodeURIComponent(recording.id)}/download`;
      download.setAttribute('download', recording.filename || '');
      download.title = `Download recording from ${formatCreated(recording.created_at_utc)}`;
      download.setAttribute('aria-label', download.title);
      actions.append(button, download);
      row.append(details, actions);
      list.append(row);
    }
  }

  async function load() {
    ensurePlaybackControls();
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
  player?.addEventListener('play', renderTransportState);
  player?.addEventListener('pause', renderTransportState);
  player?.addEventListener('loadedmetadata', () => {
    const duration = Number(player.duration);
    if (Number.isFinite(duration) && duration > 0) activeDuration = duration;
    if (pendingSeek !== null && Number.isFinite(duration)) {
      player.currentTime = pendingSeek;
      pendingSeek = null;
    }
    updatePlaybackPosition();
  });
  player?.addEventListener('timeupdate', updatePlaybackPosition);
  window.AudioRecordings = { load, readState, setEnabled, stopPlayback };
})();
