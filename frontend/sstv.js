(() => {
  'use strict';

  const byId = id => document.getElementById(id);
  const stateNode = byId('sstv-state');
  if (!stateNode) return;

  const canvas = byId('sstv-canvas');
  const context = canvas.getContext('2d');
  const enableButton = byId('sstv-enable');
  const progress = byId('sstv-progress');
  const progressWrap = progress.closest('[role="progressbar"]');
  const debugOutput = byId('sstv-debug-output');
  const debugFollow = byId('sstv-debug-follow');
  let enabled = false;
  let socket = null;
  let reconnectTimer = null;
  let currentSequence = 0;
  let lastLoggedState = null;
  let lastLoggedEnabled = null;
  const maxDebugEntries = 1200;

  function appendDebug(level, message) {
    if (!debugOutput || !message) return;
    const entry = document.createElement('div');
    entry.className = 'sstv-debug-entry';
    const normalizedLevel = ['warn', 'error'].includes(level) ? level : 'info';
    entry.dataset.level = normalizedLevel;
    const timestamp = document.createElement('span');
    timestamp.className = 'sstv-debug-time';
    const now = new Date();
    timestamp.textContent = `${now.toLocaleTimeString([], { hour12: false })}.${String(now.getMilliseconds()).padStart(3, '0')}`;
    const severity = document.createElement('span');
    severity.className = 'sstv-debug-level';
    severity.textContent = normalizedLevel.toUpperCase();
    const text = document.createElement('span');
    text.className = 'sstv-debug-message';
    text.textContent = String(message);
    entry.append(timestamp, severity, text);
    debugOutput.appendChild(entry);
    while (debugOutput.childElementCount > maxDebugEntries) {
      debugOutput.firstElementChild.remove();
    }
    if (debugFollow?.checked) debugOutput.scrollTop = debugOutput.scrollHeight;
  }

  function decodeRgb(value, width) {
    if (!Number.isInteger(width) || width <= 0 || typeof value !== 'string') return null;
    try {
      const bytes = Uint8Array.from(atob(value), character => character.charCodeAt(0));
      return bytes.length === width * 3 ? bytes : null;
    } catch (_) {
      return null;
    }
  }

  function prepareCanvas(width, height) {
    if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) return false;
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
      context.fillStyle = '#020609';
      context.fillRect(0, 0, width, height);
    }
    byId('sstv-canvas-wrap').classList.add('has-image');
    return true;
  }

  function drawLine(event) {
    const width = Number(event.width);
    const line = Number(event.line_index);
    const rgb = decodeRgb(event.rgb_base64, width);
    if (!rgb || canvas.width !== width || !Number.isInteger(line) || line < 0 || line >= canvas.height) return false;
    const image = context.createImageData(width, 1);
    for (let source = 0, target = 0; source < rgb.length; source += 3, target += 4) {
      image.data[target] = rgb[source];
      image.data[target + 1] = rgb[source + 1];
      image.data[target + 2] = rgb[source + 2];
      image.data[target + 3] = 255;
    }
    context.putImageData(image, 0, line);
    return true;
  }

  function setProgress(value) {
    const percent = Math.max(0, Math.min(100, Number(value) || 0));
    progress.style.width = `${percent}%`;
    progressWrap.setAttribute('aria-valuenow', String(Math.round(percent)));
    byId('sstv-progress-label').textContent = `${percent.toFixed(percent % 1 ? 1 : 0)}%`;
  }

  function stateDetail(status) {
    if (status.error) return status.error;
    const details = {
      Disabled: 'Decoder is off. Radio audio continues normally.',
      'Waiting for radio audio': 'Waiting for the existing native IC-9700 audio stream.',
      Listening: 'Live SUB/RX PCM is being analyzed for an SSTV VIS header.',
      'SSTV detected': `Detected ${status.mode || 'an SSTV transmission'}.`,
      Decoding: `Receiving ${status.mode || 'SSTV'} line ${status.line || 0} of ${status.total_lines || '--'}.`,
      'Decode complete': 'Image saved to the persistent gallery.',
      'Decode failed': 'The decoder could not complete the current operation.',
    };
    return details[status.state] || 'SSTV decoder status unavailable.';
  }

  function renderStatus(status, eventSequence = 0) {
    if (!status) return;
    const statusSequence = Number(eventSequence) || Number(status.sequence) || 0;
    if (statusSequence && statusSequence < currentSequence) return;
    currentSequence = Math.max(currentSequence, statusSequence);
    enabled = status.enabled === true;
    if (lastLoggedEnabled !== enabled) {
      appendDebug('info', `Decoder ${enabled ? 'enabled' : 'disabled'}.`);
      lastLoggedEnabled = enabled;
    }
    const state = status.state || 'Unknown';
    if (lastLoggedState !== state) {
      const level = state === 'Decode failed' ? 'error' : 'info';
      const detail = [
        status.mode,
        status.total_lines ? `line ${status.line || 0}/${status.total_lines}` : null,
        `level ${status.decoder_level_dbfs ?? 'silent'} dBFS`,
        `${Number(status.dropped_packets) || 0} missing packets total`,
        status.error,
      ].filter(Boolean).join('; ');
      appendDebug(level, `State: ${state}${detail ? ` (${detail})` : ''}.`);
      lastLoggedState = state;
    }
    stateNode.textContent = status.state || 'Disabled';
    stateNode.dataset.state = (status.state || 'Disabled').toLowerCase().replaceAll(' ', '-');
    byId('sstv-detail').textContent = stateDetail(status);
    enableButton.textContent = enabled ? 'Disable' : 'Enable';
    enableButton.classList.toggle('btn-danger', enabled);
    enableButton.classList.toggle('btn-primary', !enabled);
    enableButton.setAttribute('aria-pressed', String(enabled));
    byId('sstv-mode').textContent = status.mode || '--';
    byId('sstv-line').textContent = status.total_lines ? `${status.line || 0} / ${status.total_lines}` : '--';
    byId('sstv-source').textContent = status.audio_source || 'Native IC-9700 SUB/RX PCM';
    byId('sstv-engine').textContent = status.decoder || 'slowrx.rs';
    byId('sstv-quality').textContent = status.image_quality || 'clean';
    byId('sstv-losses').textContent = String(Number(status.dropped_packets) || 0);
    byId('sstv-decoder-level').textContent = Number.isFinite(status.decoder_level_dbfs)
      ? `${status.decoder_level_dbfs} dBFS`
      : 'silent';
    renderDecoderGain(status);
    const activelyDecoding = ['SSTV detected', 'Decoding'].includes(status.state);
    byId('sstv-active-indicator').hidden = !activelyDecoding;
    byId('sstv-active-indicator').querySelector('strong').textContent = status.state === 'SSTV detected'
      ? 'SSTV signal detected'
      : 'Image decoding in progress';
    byId('sstv-active-mode').textContent = activelyDecoding && status.mode ? `· ${status.mode}` : '';
    setProgress(status.progress_percent);
    if (status.current_image) {
      prepareCanvas(Number(status.current_image.width), Number(status.current_image.height));
    } else {
      byId('sstv-canvas-wrap').classList.remove('has-image');
      byId('sstv-canvas-empty').textContent = !enabled
        ? 'Enable the decoder to listen for an SSTV transmission.'
        : status.state === 'Waiting for radio audio'
          ? 'Waiting for radio audio.'
          : status.state === 'Decode failed'
            ? status.error || 'The decoder could not start.'
            : 'Listening for an SSTV transmission.';
    }
  }

  async function loadImage(url) {
    try {
      const response = await fetch(`${url}${url.includes('?') ? '&' : '?'}t=${Date.now()}`, { cache: 'no-store' });
      if (!response.ok) return;
      const bitmap = await createImageBitmap(await response.blob());
      prepareCanvas(bitmap.width, bitmap.height);
      context.drawImage(bitmap, 0, 0);
      bitmap.close?.();
    } catch (_) {
      // A live line may not exist yet, or a decode may end during the request.
    }
  }

  async function loadCurrentImage() {
    await loadImage('/api/sstv/current');
  }

  async function loadGalleryImage(image) {
    if (image?.id) await loadImage(`/api/sstv/images/${encodeURIComponent(image.id)}`);
  }

  function formatFrequency(value) {
    const frequency = Number(value);
    return Number.isFinite(frequency) ? `${(frequency / 1e6).toFixed(6)} MHz` : 'Frequency unavailable';
  }

  function openImage(image) {
    const dialog = byId('sstv-image-dialog');
    byId('sstv-large-image').src = `/api/sstv/images/${encodeURIComponent(image.id)}`;
    const acquisition = image.acquisition || {};
    const partial = image.partial === true;
    const partialText = partial && acquisition.recovered_lines
      ? `Partial: ${acquisition.recovered_lines}/${acquisition.expected_lines || '?'} rows, vertical position unknown`
      : partial ? 'Partial image' : '';
    byId('sstv-large-meta').textContent = [
      image.mode,
      partialText,
      formatFrequency(image.frequency_hz),
      image.satellite || 'Satellite unavailable',
      new Date(image.timestamp_utc).toLocaleString(),
    ].filter(Boolean).join(' · ');
    const download = byId('sstv-large-download');
    download.href = `/api/sstv/images/${encodeURIComponent(image.id)}/download`;
    download.download = image.filename || 'sstv.png';
    dialog.showModal();
  }

  function galleryCard(image) {
    const article = document.createElement('article');
    article.className = 'sstv-gallery-card';
    const preview = document.createElement('button');
    preview.type = 'button';
    preview.className = 'sstv-gallery-preview';
    preview.setAttribute('aria-label', `Open ${image.mode || 'SSTV'} image`);
    const picture = document.createElement('img');
    picture.src = `/api/sstv/images/${encodeURIComponent(image.id)}`;
    picture.alt = `${image.mode || 'SSTV'} decode`;
    preview.appendChild(picture);
    preview.addEventListener('click', () => openImage(image));
    const body = document.createElement('div');
    body.className = 'sstv-gallery-body';
    const title = document.createElement('strong');
    title.textContent = image.mode || 'SSTV';
    const time = document.createElement('time');
    time.dateTime = image.timestamp_utc;
    time.textContent = new Date(image.timestamp_utc).toLocaleString();
    const meta = document.createElement('span');
    meta.textContent = [formatFrequency(image.frequency_hz), image.satellite].filter(Boolean).join(' · ');
    const status = document.createElement('span');
    const acquisition = image.acquisition || {};
    status.textContent = image.partial && acquisition.recovered_lines
      ? `Partial: ${acquisition.recovered_lines}/${acquisition.expected_lines || '?'} rows; vertical position unknown`
      : `Status: ${image.decode_status || 'complete'}`;
    const download = document.createElement('a');
    download.className = 'btn btn-sm btn-outline-info';
    download.href = `/api/sstv/images/${encodeURIComponent(image.id)}/download`;
    download.download = image.filename || 'sstv.png';
    download.textContent = 'Download';
    body.append(title, time, meta, status, download);
    article.append(preview, body);
    return article;
  }

  async function loadGallery() {
    const gallery = byId('sstv-gallery');
    try {
      const response = await fetch('/api/sstv/images', { cache: 'no-store' });
      if (!response.ok) throw new Error('Gallery request failed');
      const images = await response.json();
      gallery.replaceChildren();
      if (!images.length) {
        const empty = document.createElement('p');
        empty.className = 'text-body-secondary mb-0';
        empty.textContent = 'No completed SSTV images yet.';
        gallery.appendChild(empty);
        return;
      }
      images.slice(0, 25).forEach(image => gallery.appendChild(galleryCard(image)));
    } catch (_) {
      gallery.textContent = 'SSTV gallery is unavailable.';
    }
  }

  async function loadState() {
    try {
      const response = await fetch('/api/sstv', { cache: 'no-store' });
      if (!response.ok) throw new Error('Status request failed');
      const status = await response.json();
      renderStatus(status);
      if (status.current_image) await loadCurrentImage();
    } catch (_) {
      byId('sstv-detail').textContent = 'SSTV backend is unavailable.';
    }
  }

  function handleEvent(event) {
    const sequence = Number(event.sequence) || 0;
    if (sequence && sequence <= currentSequence) return;
    if (sequence) currentSequence = sequence;
    if (event.type === 'debug_log') {
      appendDebug(event.level, event.message);
    } else if (event.type === 'status') {
      renderStatus(event.status, sequence);
    } else if (event.type === 'image_started') {
      prepareCanvas(Number(event.width), Number(event.height));
      setProgress(0);
      appendDebug(
        'info',
        `${event.partial ? 'Partial image acquired without VIS' : 'Image started'}: ${event.mode || 'SSTV'}, ${event.width}x${event.height}.`,
      );
    } else if (event.type === 'line_decoded') {
      const line = Number(event.line_index) + 1;
      const rendered = drawLine(event);
      if (rendered) {
        byId('sstv-line').textContent = `${line} / ${canvas.height}`;
        setProgress(100 * line / canvas.height);
        byId('sstv-detail').textContent = `Receiving ${event.mode || byId('sstv-mode').textContent || 'SSTV'} line ${line} of ${event.height || canvas.height}.`;
        appendDebug('info', `Decoder emitted line ${line}/${event.height || canvas.height}; rendered.`);
      } else {
        const width = Number(event.width);
        const lineIndex = Number(event.line_index);
        const rgb = decodeRgb(event.rgb_base64, width);
        const reason = !rgb
          ? 'invalid RGB payload'
          : canvas.width !== width
            ? `canvas width ${canvas.width} != event width ${width}`
            : !Number.isInteger(lineIndex) || lineIndex < 0 || lineIndex >= canvas.height
              ? `line index ${event.line_index} outside canvas height ${canvas.height}`
              : 'canvas draw failed';
        appendDebug('error', `Decoder emitted line ${line}/${event.height || '?'}; page rejected it: ${reason}.`);
      }
    } else if (event.type === 'image_complete') {
      setProgress(100);
      if (event.source === 'upload') loadGalleryImage(event.image);
      else loadCurrentImage();
      loadGallery();
      appendDebug(
        event.degraded ? 'warn' : 'info',
        `${event.degraded ? 'Degraded partial image saved' : 'Image saved to gallery'}${event.image?.filename ? `: ${event.image.filename}` : '.'}`,
      );
    } else if (event.type === 'audio_gap') {
      const count = Number(event.missing_packets) || 0;
      appendDebug(
        event.severity === 'major' ? 'warn' : 'info',
        event.severity === 'major'
          ? `Audio sequence gap: ${count} packets missing; restarting decoder at a clean VIS search boundary.`
          : `Audio sequence gap: ${count} packets missing; continuing and marking image degraded.`,
      );
    }
  }

  function connectEvents() {
    clearTimeout(reconnectTimer);
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${scheme}//${location.host}/api/sstv/events`);
    socket.onopen = () => appendDebug('info', 'Connected to live decoder event stream.');
    socket.onmessage = message => {
      try { handleEvent(JSON.parse(message.data)); } catch (_) { /* Ignore malformed events. */ }
    };
    socket.onclose = () => {
      appendDebug('warn', 'Decoder event stream disconnected; reconnecting.');
      socket = null;
      reconnectTimer = setTimeout(connectEvents, 1500);
    };
    socket.onerror = () => socket?.close();
  }

  enableButton.addEventListener('click', async () => {
    enableButton.disabled = true;
    try {
      const response = await fetch('/api/sstv/enabled', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !enabled }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'Unable to change decoder state');
      renderStatus(result);
    } catch (error) {
      byId('sstv-detail').textContent = error.message || 'Unable to change decoder state.';
    } finally {
      enableButton.disabled = false;
    }
  });

  byId('sstv-debug-clear')?.addEventListener('click', () => debugOutput?.replaceChildren());

  function renderDecoderGain(status) {
    const slider = byId('sstv-rx-gain');
    if (!slider || document.activeElement === slider) return;
    const gain = Number(status.rx_gain_db);
    if (!Number.isFinite(gain)) return;
    slider.value = String(gain);
    byId('sstv-rx-gain-value').textContent = `${gain} dB`;
  }

  const gainSlider = byId('sstv-rx-gain');
  let gainTimer = null;
  gainSlider?.addEventListener('input', () => {
    byId('sstv-rx-gain-value').textContent = `${gainSlider.value} dB`;
    clearTimeout(gainTimer);
    gainTimer = setTimeout(async () => {
      try {
        const response = await fetch('/api/sstv/decoder-level', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ gain_db: Number(gainSlider.value) }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || 'Decoder level was not accepted');
        renderStatus(result);
      } catch (error) {
        byId('sstv-detail').textContent = error.message || 'Unable to set the decoder level.';
      }
    }, 150);
  });

  const uploadForm = byId('sstv-upload-form');
  const uploadInput = byId('sstv-upload-file');
  const uploadButton = byId('sstv-upload-submit');
  const uploadStatus = byId('sstv-upload-status');
  uploadForm?.addEventListener('submit', async event => {
    event.preventDefault();
    const file = uploadInput?.files?.[0];
    if (!file) {
      uploadStatus.textContent = 'Choose an MP3 or WAV recording first.';
      return;
    }
    uploadButton.disabled = true;
    uploadStatus.textContent = 'Uploading and decoding recording...';
    try {
      const response = await fetch('/api/sstv/upload', {
        method: 'POST',
        headers: {
          'Content-Type': file.type || 'application/octet-stream',
          'X-Filename': encodeURIComponent(file.name),
        },
        body: file,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'Recording decode failed');
      const acquisition = result.acquisition || {};
      uploadStatus.textContent = result.partial && acquisition.recovered_lines
        ? `${result.mode || 'SSTV'} partial saved: ${acquisition.recovered_lines}/${acquisition.expected_lines || '?'} rows; vertical position unknown.`
        : `${result.mode || 'SSTV'} image saved to the gallery.`;
      uploadInput.value = '';
      await loadGalleryImage(result);
      await loadGallery();
    } catch (error) {
      uploadStatus.textContent = error.message || 'Recording decode failed.';
    } finally {
      uploadButton.disabled = false;
    }
  });

  window.PiSatSstv = { decodeRgb, drawLine, renderStatus };
  appendDebug('info', 'Console attached; showing live events from this page session.');
  loadState();
  loadGallery();
  connectEvents();
})();
