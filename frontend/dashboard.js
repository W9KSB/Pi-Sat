(() => {
  'use strict';

  const COLORS = ['#80d8ef', '#ffc861', '#83e28d', '#eaa5f3', '#ff9696', '#b8b6ff'];
  const REFRESH_KEY = 'pi-sat.station-monitor.refresh-minutes';
  const activeStates = new Set(['SSTV detected', 'Decoding', 'Finishing queued audio']);
  const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));

  function refreshMinutes(value) {
    const minutes = Number(value);
    return Number.isFinite(minutes) && minutes >= 1 ? Math.min(60, Math.round(minutes)) : 3;
  }

  function visiblePasses(passes, now = Date.now()) {
    return (passes || []).filter(pass => {
      const aos = Date.parse(pass.aos_utc), los = Date.parse(pass.los_utc);
      return Number.isFinite(aos) && Number.isFinite(los) && los > now && aos < now + 86400000;
    }).sort((a, b) => Date.parse(a.aos_utc) - Date.parse(b.aos_utc));
  }

  function passDay(value, timeZone) {
    const date = new Date(value);
    const parts = new Intl.DateTimeFormat('en-US', { timeZone, year: 'numeric', month: 'long', day: 'numeric' }).formatToParts(date);
    const part = name => parts.find(item => item.type === name).value;
    const day = Number(part('day'));
    const suffix = day % 100 >= 11 && day % 100 <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[day % 10] || 'th');
    return { key: `${part('year')}-${part('month')}-${day}`, label: `${part('month')} ${day}${suffix}` };
  }

  function mapPositions(satellites, positions) {
    const checked = new Set((satellites || []).map(satellite => Number(satellite.norad_id)));
    return (positions || []).filter(position => checked.has(Number(position.norad_id))
      && finite(position.latitude_deg) && finite(position.longitude_deg));
  }

  function nextMapPosition(positions, currentNorad, advance = false) {
    if (!positions.length) return null;
    const index = positions.findIndex(position => Number(position.norad_id) === Number(currentNorad));
    return positions[index < 0 ? 0 : (index + (advance ? 1 : 0)) % positions.length];
  }

  function readOnlyClient(fetcher) {
    return async (path, signal) => {
      if (!/^\/api\/(?:dashboard|sstv(?:\/(?:current|images(?:\/[a-zA-Z0-9_-]+)?))?)$/.test(path)) {
        throw new Error('Unsupported monitor data source');
      }
      const response = await fetcher(path, { method: 'GET', cache: 'no-store', signal });
      if (!response.ok) throw new Error(`Monitor data unavailable (${response.status})`);
      return response;
    };
  }

  function rgbRow(event) {
    const width = Number(event.width), row = Number(event.line_index), height = Number(event.height);
    if (!Number.isInteger(width) || width < 1 || width > 2048 || !Number.isInteger(height)
        || height < 1 || height > 2048 || !Number.isInteger(row) || row < 0 || row >= height
        || typeof event.rgb_base64 !== 'string') return null;
    try {
      const bytes = atob(event.rgb_base64);
      if (bytes.length !== width * 3) return null;
      const pixels = new Uint8ClampedArray(width * 4);
      for (let i = 0, j = 0; i < bytes.length; i += 3, j += 4) {
        pixels[j] = bytes.charCodeAt(i);
        pixels[j + 1] = bytes.charCodeAt(i + 1);
        pixels[j + 2] = bytes.charCodeAt(i + 2);
        pixels[j + 3] = 255;
      }
      return { width, height, row, pixels };
    } catch (_) { return null; }
  }

  class SstvDisplay {
    constructor(canvas, nodes, client, bitmap, formatTime) {
      this.canvas = canvas;
      this.context = canvas.getContext('2d');
      this.nodes = nodes;
      this.client = client;
      this.bitmap = bitmap;
      this.formatTime = formatTime;
      this.active = false;
      this.imageId = null;
      this.sequence = 0;
      this.generation = 0;
      this.imageRequest = 0;
      this.rows = new Map();
      this.lastSaved = null;
      this.savedDisplayed = null;
      this.galleryLoaded = false;
      this.needsResync = true;
      this.stateName = null;
    }

    prepare(width, height, clear = false) {
      if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1
          || width > 2048 || height > 2048) return false;
      if (this.canvas.width !== width || this.canvas.height !== height) {
        this.canvas.width = width;
        this.canvas.height = height;
        clear = true;
      }
      if (clear) {
        this.context.fillStyle = '#020609';
        this.context.fillRect(0, 0, width, height);
      }
      this.canvas.hidden = false;
      this.nodes.empty.hidden = true;
      return true;
    }

    begin(image) {
      this.generation += 1;
      this.imageRequest += 1;
      this.rows.clear();
      this.active = true;
      this.savedDisplayed = null;
      this.imageId = image.image_id || null;
      this.prepare(Number(image.width), Number(image.height), true);
      this.nodes.title.textContent = 'Receiving SSTV';
      this.nodes.mode.textContent = image.mode || 'SSTV';
      this.nodes.received.textContent = 'Image in progress';
    }

    paint(row) {
      const image = this.context.createImageData(row.width, 1);
      image.data.set(row.pixels);
      this.context.putImageData(image, 0, row.row);
    }

    async loadImage(path, live = false) {
      const generation = this.generation, request = ++this.imageRequest;
      try {
        const response = await this.client(path);
        const image = await this.bitmap(await response.blob());
        try {
          if (generation !== this.generation || request !== this.imageRequest || live !== this.active) return false;
          if (!this.prepare(image.width, image.height)) return false;
          this.context.drawImage(image, 0, 0);
          // Rows received while the PNG was in flight remain authoritative.
          if (live) for (const { row } of this.rows.values()) this.paint(row);
          return true;
        } finally { image.close?.(); }
      } catch (_) { return false; }
    }

    async showSaved() {
      const saved = this.lastSaved;
      if (this.active || !saved?.id || this.savedDisplayed === saved.id) return;
      if (await this.loadImage(`/api/sstv/images/${encodeURIComponent(saved.id)}`)) {
        this.savedDisplayed = saved.id;
        this.nodes.title.textContent = 'Last received image';
        this.nodes.mode.textContent = [saved.mode, saved.partial ? 'Partial' : null].filter(Boolean).join(' · ');
        this.nodes.received.textContent = `Received ${this.formatTime(saved.timestamp_utc)}`;
      }
    }

    async refreshGallery() {
      try {
        const images = await (await this.client('/api/sstv/images')).json();
        if (!Array.isArray(images)) return;
        this.galleryLoaded = true;
        if (images[0]) this.rememberSaved(images[0]);
        await this.showSaved();
      } catch (_) { /* Retain the displayed image until gallery data returns. */ }
    }

    rememberSaved(image) {
      if (image?.id && (!this.lastSaved || Date.parse(image.timestamp_utc) >= Date.parse(this.lastSaved.timestamp_utc))) {
        this.lastSaved = image;
      }
    }

    async status(status) {
      this.nodes.state.textContent = status.state || 'Unknown';
      const stateChanged = this.stateName !== status.state;
      this.stateName = status.state;
      const resync = this.needsResync;
      this.needsResync = false;
      const active = activeStates.has(status.state) && status.current_image;
      if (active) {
        const newImage = !this.active || this.imageId !== status.current_image.image_id;
        if (newImage) this.begin(status.current_image);
        this.nodes.received.textContent = status.total_lines ? `Line ${status.line || 0} / ${status.total_lines}` : 'Image in progress';
        if (newImage || resync) await this.loadImage('/api/sstv/current', true);
      } else {
        if (this.active) {
          this.active = false;
          this.generation += 1;
          this.rows.clear();
        }
        if (stateChanged || resync || !this.galleryLoaded) await this.refreshGallery();
      }
    }

    handle(event) {
      if (event.type === 'heartbeat') return;
      const sequence = Number(event.sequence) || Number(event.status?.sequence) || 0;
      if (sequence && sequence <= this.sequence) return;
      const gap = this.sequence && sequence > this.sequence + 1;
      if (sequence) this.sequence = sequence;
      if (event.type === 'status') {
        void this.status(event.status || {});
      } else if (event.type === 'image_started') {
        this.begin(event);
      } else if (event.type === 'line_decoded') {
        if (event.image_id && this.imageId && event.image_id !== this.imageId) return;
        const row = rgbRow(event), revision = Number(event.revision) || 0;
        if (!row) return;
        if (!this.active || this.imageId !== event.image_id) this.begin(event);
        if (!row || row.width !== this.canvas.width || row.height !== this.canvas.height) return;
        if (this.rows.has(row.row) && revision <= this.rows.get(row.row).revision) return;
        this.rows.set(row.row, { row, revision });
        this.paint(row);
        this.nodes.state.textContent = 'Decoding';
        this.nodes.received.textContent = `Line ${Math.max(...this.rows.keys()) + 1} / ${row.height}`;
      } else if (event.type === 'image_complete') {
        this.rememberSaved(event.image);
        if (event.source !== 'upload' || !this.active) {
          this.active = false;
          this.generation += 1;
          this.rows.clear();
          this.nodes.state.textContent = event.image?.partial ? 'Partial image saved' : 'Image saved';
          void this.showSaved();
        }
      }
      if (gap && this.active && event.type !== 'status') void this.loadImage('/api/sstv/current', true);
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { refreshMinutes, visiblePasses, passDay, mapPositions, nextMapPosition, readOnlyClient, rgbRow, SstvDisplay };
  }
  if (typeof document === 'undefined') return;

  const byId = id => document.getElementById(id);
  const client = readOnlyClient(window.fetch.bind(window));
  let snapshot = null, lastUpdated = 0, refreshTimer = null, refreshRunning = false;
  let stopped = false, timezone = 'UTC', minutes = 3, passSignature = '';
  let socket = null, reconnectTimer = null, reconnectDelay = 1500, lastEventAt = 0;
  const colors = new Map();
  let mapNorad = null;
  const mapCanvas = byId('world-map'), mapContext = mapCanvas.getContext('2d');
  const world = new Image();
  world.onload = drawMap;
  world.src = '/assets/world-map-equirectangular.png';

  function time(value, date = false) {
    if (!value || !Number.isFinite(Date.parse(value))) return '—';
    return new Intl.DateTimeFormat(undefined, { timeZone: timezone, hour: '2-digit', minute: '2-digit', hour12: false,
      ...(date ? { month: 'short', day: 'numeric' } : {}) }).format(new Date(value));
  }

  const viewer = new SstvDisplay(byId('sstv-image'), {
    title: byId('sstv-title'), state: byId('sstv-state'), mode: byId('sstv-mode'),
    received: byId('sstv-received'), empty: byId('sstv-empty'),
  }, client, window.createImageBitmap.bind(window), value => time(value, true));

  function color(norad) { return colors.get(Number(norad)) || COLORS[0]; }
  function point(lat, lon) { return { x: (Number(lon) + 180) / 360 * mapCanvas.width, y: (90 - Number(lat)) / 180 * mapCanvas.height }; }
  function coordinate(item) { return finite(item?.latitude_deg) && finite(item?.longitude_deg); }

  function drawMap() {
    const ctx = mapContext, width = mapCanvas.width, height = mapCanvas.height;
    ctx.fillStyle = '#071c29';
    ctx.fillRect(0, 0, width, height);
    if (world.complete && world.naturalWidth) ctx.drawImage(world, 0, 0, width, height);
    ctx.fillStyle = 'rgba(4, 12, 20, .25)';
    ctx.fillRect(0, 0, width, height);
    ctx.strokeStyle = 'rgba(133, 189, 213, .24)';
    ctx.lineWidth = 1;
    for (let lon = -180; lon <= 180; lon += 30) {
      const p = point(0, lon); ctx.beginPath(); ctx.moveTo(p.x, 0); ctx.lineTo(p.x, height); ctx.stroke();
    }
    for (let lat = -90; lat <= 90; lat += 30) {
      const p = point(lat, 0); ctx.beginPath(); ctx.moveTo(0, p.y); ctx.lineTo(width, p.y); ctx.stroke();
    }
    const positions = mapPositions(snapshot?.satellites, snapshot?.positions);
    const position = nextMapPosition(positions, mapNorad);
    mapNorad = position?.norad_id ?? null;
    if (position) {
      ctx.strokeStyle = color(position.norad_id);
      ctx.lineWidth = 2;
      ctx.setLineDash([8, 6]);
      ctx.beginPath();
      let previous = null;
      for (const item of position.ground_track || []) {
        if (!coordinate(item)) { previous = null; continue; }
        const p = point(item.latitude_deg, item.longitude_deg);
        if (!previous || Math.abs(p.x - previous.x) > width / 2) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
        previous = p;
      }
      ctx.stroke(); ctx.setLineDash([]);
    }
    if (coordinate(snapshot?.qth)) marker(snapshot.qth, 'QTH', '#ff8080', false);
    if (position) {
      marker(position, position.satellite_name || '', color(position.norad_id), true);
    }
    const index = positions.findIndex(item => item === position);
    byId('map-satellite').textContent = position ? `· ${position.satellite_name} (${index + 1}/${positions.length})` : '';
    const coordinateText = (value, positive, negative) => `${Math.abs(Number(value)).toFixed(2)}° ${Number(value) >= 0 ? positive : negative}`;
    byId('map-position').textContent = position ? [
      coordinateText(position.latitude_deg, 'N', 'S'), coordinateText(position.longitude_deg, 'E', 'W'),
      finite(position.azimuth_deg) ? `Az ${Number(position.azimuth_deg).toFixed(0)}°` : null,
      finite(position.elevation_deg) ? `El ${Number(position.elevation_deg).toFixed(0)}°` : null,
      snapshot?.generated_at_utc ? `Position at ${time(snapshot.generated_at_utc)}` : null,
    ].filter(Boolean).join(' · ') : '';
    for (const item of byId('map-legend').children) item.classList.toggle('current', Number(item.dataset.norad) === Number(mapNorad));
    mapCanvas.setAttribute('aria-label', position ? `World map showing ${position.satellite_name} and station location`
      : 'World map showing station location');
  }

  function rotateMap() {
    if (document.hidden) return;
    const positions = mapPositions(snapshot?.satellites, snapshot?.positions);
    mapNorad = nextMapPosition(positions, mapNorad, true)?.norad_id ?? null;
    drawMap();
  }

  function marker(item, label, ink, active) {
    const ctx = mapContext, p = point(item.latitude_deg, item.longitude_deg);
    ctx.fillStyle = ink; ctx.strokeStyle = '#eff7ff'; ctx.lineWidth = active ? 3 : 1;
    ctx.beginPath(); ctx.arc(p.x, p.y, active ? 9 : 6, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.font = '600 20px system-ui';
    const textWidth = ctx.measureText(label).width;
    const x = Math.max(5, Math.min(p.x + 12, mapCanvas.width - textWidth - 8));
    const y = Math.max(24, Math.min(p.y - 12, mapCanvas.height - 8));
    ctx.lineWidth = 5; ctx.strokeStyle = '#071018'; ctx.strokeText(label, x, y);
    ctx.fillStyle = ink; ctx.fillText(label, x, y);
  }

  function renderLegend() {
    colors.clear();
    const legend = byId('map-legend'); legend.replaceChildren();
    (snapshot.satellites || []).forEach((satellite, i) => {
      colors.set(Number(satellite.norad_id), COLORS[i % COLORS.length]);
      const item = document.createElement('span'); item.className = 'legend-item';
      item.dataset.norad = satellite.norad_id;
      item.style.setProperty('--sat-color', color(satellite.norad_id));
      const dot = document.createElement('span'); dot.className = 'legend-dot';
      item.append(dot, document.createTextNode(satellite.name)); legend.append(item);
    });
  }

  function renderPasses(force = false) {
    if (!snapshot) return;
    const now = Date.now(), passes = visiblePasses(snapshot.passes, now);
    const signature = JSON.stringify([timezone, passes.map(pass => [pass.norad_id, pass.aos_utc, pass.los_utc, pass.max_elevation_deg,
      Date.parse(pass.aos_utc) <= now])]);
    if (!force && signature === passSignature) return;
    passSignature = signature;
    const track = byId('passes-track'); track.replaceChildren(); track.classList.remove('scrolling');
    const panel = track.closest('.passes-panel');
    for (const name of ['--aos-width', '--los-width', '--el-width']) panel.style.removeProperty(name);
    byId('passes-empty').hidden = passes.length > 0;
    byId('passes-window').hidden = !passes.length;
    byId('passes-empty').textContent = !snapshot.pass_cache_available ? 'Pass schedule is not available yet'
      : 'No remaining passes in the next 24 hours';
    if (!passes.length) return;
    const cycle = document.createElement('div'); cycle.className = 'pass-cycle'; cycle.setAttribute('role', 'list');
    let previousDay = null;
    for (const pass of passes) {
      const day = passDay(pass.aos_utc, timezone);
      if (day.key !== previousDay) {
        const separator = document.createElement('h3'); separator.className = 'pass-date'; separator.textContent = day.label;
        cycle.append(separator);
        previousDay = day.key;
      }
      const row = document.createElement('div'); row.className = 'pass-row'; row.setAttribute('role', 'listitem');
      row.style.setProperty('--sat-color', color(pass.norad_id));
      const active = Date.parse(pass.aos_utc) <= now; row.classList.toggle('active', active);
      const name = document.createElement('span'); name.className = 'pass-name'; name.textContent = pass.satellite_name;
      name.title = pass.satellite_name;
      const aos = document.createElement('time'), los = document.createElement('time'), elevation = document.createElement('span');
      aos.dateTime = pass.aos_utc; aos.textContent = time(pass.aos_utc);
      los.dateTime = pass.los_utc; los.textContent = time(pass.los_utc);
      elevation.textContent = finite(pass.max_elevation_deg) ? `${Math.round(Number(pass.max_elevation_deg))}°` : '—';
      row.append(name, aos, los, elevation); cycle.append(row);
    }
    track.append(cycle);
    const columns = [panel.querySelector('.pass-columns'), ...cycle.querySelectorAll('.pass-row')];
    ['--aos-width', '--los-width', '--el-width'].forEach((name, i) => {
      const width = Math.max(...columns.map(row => row.children[i + 1].getBoundingClientRect().width));
      panel.style.setProperty(name, `${Math.ceil(width)}px`);
    });
    // Repeat short schedules so the continuous loop never scrolls into blank space.
    const baseHeight = cycle.offsetHeight;
    if (baseHeight > 0) {
      const repeats = Math.ceil(byId('passes-window').clientHeight / baseHeight);
      const originals = [...cycle.children];
      for (let n = 1; n < repeats; n += 1) for (const row of originals) {
        const copy = row.cloneNode(true); copy.setAttribute('aria-hidden', 'true'); cycle.append(copy);
      }
    }
    const copy = cycle.cloneNode(true); copy.setAttribute('aria-hidden', 'true'); track.append(copy);
    track.style.setProperty('--scroll-duration', `${Math.max(20, cycle.offsetHeight / 12)}s`);
    track.classList.add('scrolling');
  }

  function setState(id, label, state) { const node = byId(id); node.textContent = label; node.dataset.state = state; }
  function tracking(id, device) {
    const value = device || {};
    if (value.enabled === false) setState(id, 'Off', 'off');
    else if (value.connected === false) setState(id, 'Disconnected', 'error');
    else setState(id, value.active ? 'Active' : 'Idle', value.active ? 'active' : 'off');
  }
  function frequency(value) { return finite(value) ? `${(Number(value) / 1e6).toFixed(6)} MHz` : '—'; }

  function renderStatus() {
    const status = snapshot.status || {}, radio = status.radio || {}, rotator = status.rotator || {}, target = status.target || {};
    byId('radio-name').textContent = radio.name || 'Not configured';
    setState('radio-state', radio.connected ? 'Connected' : 'Disconnected', radio.connected ? 'active' : 'off');
    tracking('rx-state', status.rx); tracking('tx-state', status.tx); tracking('rotator-state', rotator);
    byId('rx-frequency').textContent = frequency(status.rx?.frequency_hz);
    byId('tx-frequency').textContent = frequency(status.tx?.frequency_hz);
    byId('rotator-position').textContent = `Az ${finite(rotator.azimuth_deg) ? Number(rotator.azimuth_deg).toFixed(0) + '°' : '—'} · El ${finite(rotator.elevation_deg) ? Number(rotator.elevation_deg).toFixed(0) + '°' : '—'}`;
    byId('tracking-target').textContent = target.name || 'None';
    byId('target-detail').textContent = target.name ? (target.pass_active ? 'Pass in progress' : 'Waiting for pass') : 'No tracking target';
  }

  function tick() {
    if (lastUpdated) {
      const age = Math.floor((Date.now() - lastUpdated) / 1000);
      byId('updated-at').textContent = `${document.body.classList.contains('stale') ? 'Unavailable · ' : ''}Updated ${age < 60 ? 'just now' : Math.floor(age / 60) + ' min ago'}`;
      if (age > minutes * 60 + 20) document.body.classList.add('stale');
    }
    byId('station-clock').textContent = time(new Date().toISOString(), true);
    if (socket?.readyState === WebSocket.OPEN && Date.now() - lastEventAt > 15000) socket.close();
    renderPasses();
  }

  async function refresh() {
    if (refreshRunning || stopped) return;
    refreshRunning = true;
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const data = await (await client('/api/dashboard', controller.signal)).json();
      if (stopped) return;
      snapshot = data;
      try { new Intl.DateTimeFormat(undefined, { timeZone: data.timezone }); timezone = data.timezone || 'UTC'; }
      catch (_) { timezone = 'UTC'; }
      lastUpdated = Date.now(); document.body.classList.remove('stale');
      byId('passes-zone').textContent = timezone.replaceAll('_', ' ');
      renderLegend(); renderStatus(); renderPasses(); drawMap();
      byId('map-message').textContent = !data.satellites?.length ? 'Check satellites on the Home page to display them here'
        : !data.positions_available ? 'Satellite positions unavailable · orbital data is not ready'
          : data.pass_cache_stale ? 'Pass schedule has not refreshed recently' : '';
    } catch (_) {
      document.body.classList.add('stale');
      if (!lastUpdated) byId('updated-at').textContent = 'Station unavailable';
    } finally { clearTimeout(timeout); refreshRunning = false; tick(); }
    void viewer.refreshGallery();
  }

  function scheduleRefresh() {
    clearInterval(refreshTimer);
    refreshTimer = setInterval(() => { void refresh(); }, minutes * 60000);
  }

  function connectEvents() {
    if (stopped || socket) return;
    clearTimeout(reconnectTimer);
    viewer.sequence = 0;
    viewer.needsResync = true;
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/sstv/events`);
    socket = ws;
    ws.onopen = () => { reconnectDelay = 1500; lastEventAt = Date.now(); };
    ws.onmessage = message => {
      lastEventAt = Date.now();
      try { viewer.handle(JSON.parse(message.data)); } catch (_) { /* Ignore malformed display events. */ }
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null;
      byId('sstv-state').textContent = 'Stream unavailable · reconnecting';
      if (!stopped) {
        reconnectTimer = setTimeout(connectEvents, reconnectDelay);
        reconnectDelay = Math.min(30000, reconnectDelay * 2);
      }
    };
    ws.onerror = () => ws.close();
  }

  try { minutes = refreshMinutes(localStorage.getItem(REFRESH_KEY)); } catch (_) { /* Storage can be unavailable in kiosk mode. */ }
  byId('refresh-minutes').value = minutes;
  byId('refresh-minutes').addEventListener('change', event => {
    minutes = refreshMinutes(event.target.value); event.target.value = minutes;
    try { localStorage.setItem(REFRESH_KEY, String(minutes)); } catch (_) { /* The interval still applies without storage. */ }
    scheduleRefresh();
  });
  document.addEventListener('visibilitychange', () => document.body.classList.toggle('page-hidden', document.hidden));
  window.addEventListener('resize', () => renderPasses(true));
  window.addEventListener('pagehide', () => {
    stopped = true; clearInterval(refreshTimer); clearInterval(tickTimer); clearInterval(mapTimer); clearTimeout(reconnectTimer);
    if (socket) { const ws = socket; socket = null; ws.close(); }
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    stopped = false; scheduleRefresh(); tickTimer = setInterval(tick, 1000); mapTimer = setInterval(rotateMap, 5000); connectEvents(); void refresh();
  });
  let tickTimer = setInterval(tick, 1000);
  let mapTimer = setInterval(rotateMap, 5000);
  scheduleRefresh(); connectEvents(); void refresh();
})();
