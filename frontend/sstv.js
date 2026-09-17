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
  let enabled = false;
  let socket = null;
  let reconnectTimer = null;
  let currentSequence = 0;

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

  function renderStatus(status) {
    if (!status) return;
    enabled = status.enabled === true;
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
    byId('sstv-decoder-level').textContent = Number.isFinite(status.decoder_level_dbfs)
      ? `${status.decoder_level_dbfs} dBFS`
      : 'silent';
    renderDecoderGain(status);
    const activelyDecoding = ['SSTV detected', 'Decoding'].includes(status.state);
    byId('sstv-active-indicator').hidden = !activelyDecoding;
    byId('sstv-active-mode').textContent = activelyDecoding && status.mode ? `· ${status.mode}` : '';
    setProgress(status.progress_percent);
    if (status.current_image) {
      prepareCanvas(Number(status.current_image.width), Number(status.current_image.height));
    } else if (!enabled) {
      byId('sstv-canvas-wrap').classList.remove('has-image');
      byId('sstv-canvas-empty').textContent = 'Enable the decoder to listen for an SSTV transmission.';
    }
    currentSequence = Math.max(currentSequence, Number(status.sequence) || 0);
  }

  async function loadCurrentImage() {
    try {
      const response = await fetch(`/api/sstv/current?t=${Date.now()}`, { cache: 'no-store' });
      if (!response.ok) return;
      const bitmap = await createImageBitmap(await response.blob());
      prepareCanvas(bitmap.width, bitmap.height);
      context.drawImage(bitmap, 0, 0);
      bitmap.close?.();
    } catch (_) {
      // A live line may not exist yet, or a decode may end during the request.
    }
  }

  function formatFrequency(value) {
    const frequency = Number(value);
    return Number.isFinite(frequency) ? `${(frequency / 1e6).toFixed(6)} MHz` : 'Frequency unavailable';
  }

  function openImage(image) {
    const dialog = byId('sstv-image-dialog');
    byId('sstv-large-image').src = `/api/sstv/images/${encodeURIComponent(image.id)}`;
    byId('sstv-large-meta').textContent = [
      image.mode,
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
    status.textContent = `Status: ${image.decode_status || 'complete'}`;
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
    if (event.type === 'status') {
      renderStatus(event.status);
    } else if (event.type === 'image_started') {
      prepareCanvas(Number(event.width), Number(event.height));
      setProgress(0);
    } else if (event.type === 'line_decoded') {
      if (drawLine(event)) {
        const line = Number(event.line_index) + 1;
        byId('sstv-line').textContent = `${line} / ${canvas.height}`;
        setProgress(100 * line / canvas.height);
      }
    } else if (event.type === 'image_complete') {
      setProgress(100);
      loadCurrentImage();
      loadGallery();
    }
  }

  function connectEvents() {
    clearTimeout(reconnectTimer);
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${scheme}//${location.host}/api/sstv/events`);
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

  window.PiSatSstv = { decodeRgb, drawLine, renderStatus };
  loadState();
  loadGallery();
  connectEvents();
})();
