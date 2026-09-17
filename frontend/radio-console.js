/* Radio-page presentation. The session and audio belong to the app, not a tab. */
(() => {
  'use strict';
  const root = document.getElementById('radio-console');
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const formatFrequency = (hz) => Number.isFinite(hz) && hz > 0
    ? Math.round(hz).toString().padStart(9, '0').replace(/\B(?=(\d{3})+(?!\d))/g, '.') : '---.---.---';
  const band = (hz) => hz >= 144e6 && hz < 148e6 ? '2 m' : hz >= 420e6 && hz < 450e6 ? '70 cm'
    : hz >= 1240e6 && hz < 1300e6 ? '23 cm' : '—';
  function button(label, action, extra = '') {
    return `<button type="button" class="rc-button" data-action="${action}" ${extra}>${label}</button>`;
  }
  function pathPanel(side, role) {
    return `<section class="rc-path rc-panel" data-side="${side}" aria-label="${side} ${role}">
      <div class="rc-path-heading"><h2><i class="rc-led"></i>${side} <span class="rc-role">${role}</span></h2>
        <span class="rc-freshness" id="rc-${side}-fresh">NO DATA</span></div>
      <div class="rc-frequency-row"><button type="button" id="rc-${side}-dial" class="rc-frequency" data-action="edit" data-side="${side}" data-radio title="Click to enter Hz; mouse wheel tunes using STEP">
        <span id="rc-${side}-frequency">---.---.---</span><small>MHz</small></button>
        <div class="rc-frequency-steps" aria-label="${side} step tuning">
          ${button('+', 'step', `data-side="${side}" data-value="1" data-radio aria-label="Increase ${side} frequency by one step"`)}
          ${button('−', 'step', `data-side="${side}" data-value="-1" data-radio aria-label="Decrease ${side} frequency by one step"`)}</div>
        ${side === 'MAIN' ? `<div class="rc-transmit"><button type="button" class="rc-ptt" id="rc-ptt" data-action="ptt" data-radio>PTT</button><span id="rc-ptt-state">PTT · UNKNOWN</span></div>` : ''}</div>
      <form class="rc-frequency-editor" id="rc-${side}-editor" hidden>
        <label for="rc-${side}-input">${side} frequency (Hz)</label>
        <input id="rc-${side}-input" type="number" min="1" max="9999999999" step="1" required autocomplete="off">
        <button class="rc-button" type="submit" data-radio>Write &amp; verify</button>
        ${button('Cancel', 'cancel', `data-side="${side}"`)}<output id="rc-${side}-write-status" role="status"></output></form>
      <button type="button" class="rc-meter" data-action="meter" data-side="${side}" title="Click to cycle S meter, Power, SWR and Compression">
        <span class="rc-meter-heading"><span id="rc-${side}-meter-name">S-METER ▾</span><span id="rc-${side}-meter-value">Not available</span></span>
        <span class="rc-meter-track"><i id="rc-${side}-meter-fill"></i></span><span id="rc-${side}-meter-scale" class="rc-meter-scale"><span>S0</span><span>S9</span><span>S9 +60</span></span></button>
      <div class="rc-path-controls">
        <label>BAND <select id="rc-${side}-band" disabled title="Radio band readback; band switching is not implemented"><option value="">—</option><option>2 m</option><option>70 cm</option><option>23 cm</option></select></label>
        <label>MODE <select id="rc-${side}-mode" data-radio-select="mode" data-side="${side}" data-radio><option value="">Unknown</option>${['FM', 'USB', 'LSB', 'CW', 'CW-R', 'AM', 'DV', 'DD'].map(mode => `<option>${mode}</option>`).join('')}</select></label>
        <label>VFO <select id="rc-${side}-vfo" data-radio-select="vfo" data-side="${side}" data-radio title="A/B is independent on each physical side"><option value="">Unknown</option>${['A', 'B'].map(vfo => `<option value="${vfo}">${vfo} (${side === 'MAIN' ? 'Main' : 'Sub'})</option>`).join('')}</select></label>
        <label>FILTER PRESET <select id="rc-${side}-filter" data-radio-select="filter" data-side="${side}" data-radio><option value="">Unknown</option>${[1, 2, 3].map(filter => `<option value="${filter}">FIL${filter}</option>`).join('')}</select></label>
      </div>
      <div class="rc-secondary-controls">${[['af_gain', 'RADIO AF'], ['rf_gain', 'RF GAIN'], ['squelch', 'SQUELCH']].map(([control, label]) => `<label class="rc-level-control">${label}
        <input type="range" min="0" max="255" step="1" value="0" id="rc-${side}-${control}" data-level="${control}" data-side="${side}" data-radio aria-label="${side} ${label}">
        <output id="rc-${side}-${control}-value">—</output></label>`).join('')}
      ${side === 'SUB' ? `<form id="rc-rit-form" class="rc-rit-controls"><label>SUB RIT (Hz)<input id="rc-rit-offset" type="number" min="-9990" max="9990" step="10" value="0" required data-radio title="Adjust with arrows or mouse wheel in 10 Hz steps"></label>
        <output id="rc-rit-toggle" aria-live="polite">RIT —</output>${button('Reset', 'rit-reset', 'data-radio')}
        <output id="rc-rit-readback" class="visually-hidden">No RIT readback</output></form>` : ''}</div>
      <small id="rc-${side}-sql-state" class="rc-control-note">Squelch state unknown</small>
    </section>`;
  }
  root.innerHTML = `
    <div class="rc-session rc-panel"><div class="rc-session-identity">
      <span id="rc-connection" class="rc-connection"><i class="rc-led"></i>Disconnected</span></div>
      <div class="rc-listen"><span>LISTEN</span>${button('MAIN · TX', 'listen', 'data-value="MAIN" aria-pressed="false"')}
        ${button('SUB · RX', 'listen', 'data-value="SUB" aria-pressed="true"')}
        ${button('Both', 'listen', 'data-value="BOTH" aria-pressed="false" title="Dual audio: MAIN left, SUB right; requires stereo and both receivers active"')}</div>
      ${button('Dualwatch —', 'dualwatch', 'id="rc-dualwatch" data-radio title="Turn the radio SUB receiver on or off"')}
      <button id="rc-power" type="button" class="rc-button rc-power" data-action="power"><span aria-hidden="true">⏻</span> Connect</button></div>
    <section class="rc-scope rc-panel" aria-label="Spectrum and waterfall">
      <div class="rc-scope-toolbar"><h2>Spectrum <span>/ Waterfall</span></h2>
        <span id="rc-scope-source" class="rc-scope-source">Spectrum follows listening · SUB</span>
        <span id="rc-scope-range">Center — · Span —</span>
        <label class="rc-span-label">WIDTH <select id="rc-span" data-radio title="Full visible spectrum width; selects centered scope mode"><option value="">Radio span</option>${[5000, 10000, 20000, 50000, 100000, 200000, 500000, 1000000].map(width => `<option value="${width}">${width / 1000} kHz</option>`).join('')}</select></label>
        <label class="rc-span-label">STEP <select id="rc-step" title="Spectrum mouse-wheel and drag tuning step"><option value="10">10 Hz</option><option value="100" selected>100 Hz</option><option value="1000">1 kHz</option><option value="10000">10 kHz</option></select></label>
        <label class="rc-lock"><input id="rc-lock" type="checkbox"> Lock</label>
        <span id="rc-passband-label">Passband —</span>
        <div class="rc-scope-tools">${button('Max hold', 'hold', 'aria-pressed="false"')}${button('Fill', 'fill', 'aria-pressed="true"')}${button('Grid', 'grid', 'aria-pressed="true"')}${button('Smooth', 'smooth', 'aria-pressed="true" title="Glide the waterfall between sweeps and soften the trace; display only, no radio writes"')}</div></div>
      <div class="rc-scope-display">
        <div class="rc-scope-plot"><canvas id="rc-spectrum" width="1400" height="220" aria-label="Live spectrum, relative amplitude"></canvas>
          <span class="rc-scope-unit">DISPLAY dB</span><div id="rc-scope-message" class="rc-scope-message">Connect to receive spectrum</div></div>
        <div id="rc-scope-axis" class="rc-scope-axis"><span>—</span><span>—</span><span>—</span><span>—</span><span>— MHz</span></div>
        <canvas id="rc-waterfall" width="475" height="110" aria-label="Live scrolling waterfall"></canvas>
        <div class="rc-level-window" aria-label="Spectrum display level range">
          <span class="rc-level-track" aria-hidden="true"><i id="rc-level-visible"></i></span>
          <input id="rc-level-min" class="rc-level-slider" type="range" min="-160" max="0" step="1" value="-160"
            aria-label="Display noise floor minimum dB" title="Bottom display level; display scaling only">
          <input id="rc-level-max" class="rc-level-slider" type="range" min="-160" max="0" step="1" value="0"
            aria-label="Display maximum dB" title="Top display level; display scaling only">
          <output id="rc-level-min-value" class="rc-level-value rc-level-value-min">−160 dB</output>
          <output id="rc-level-max-value" class="rc-level-value rc-level-value-max">0 dB</output>
        </div>
      </div>
    </section>
    <div class="rc-deck">${pathPanel('MAIN', 'TX')}${pathPanel('SUB', 'RX')}</div>
    <footer class="rc-audio rc-panel"><div class="rc-volume"><label for="rc-volume">MONITOR VOLUME</label><input id="rc-volume" type="range" min="0" max="100" value="70"><output id="rc-volume-value">70%</output></div>
      <span id="radio-audio-status" role="status">Audio off · starts with Connect</span>
      <span class="rc-persistent">Audio follows you across tabs</span></footer>
    <details class="rc-microphone rc-panel" id="rc-microphone-panel"><summary>Microphone &amp; transmit audio configuration</summary>
      <div class="rc-mic-settings"><label>Laptop / USB microphone<select id="rc-mic-device" title="Microphone used for transmit audio; choosing another one reopens capture on it"><option value="">System default microphone</option></select></label>
        ${button('Refresh microphones', 'mic-refresh')}
        <label>Browser mic gain<input id="rc-mic-gain" type="range" min="0" max="200" value="100"><output id="rc-mic-gain-value">100%</output></label>
        <label>Mic level<meter id="rc-mic-meter" min="0" max="1" value="0" high="0.95" optimum="0.4"></meter><output id="rc-mic-level">Mic off</output></label>
      </div><p id="rc-mic-requirements">Connect starts the microphone and streams it to Pi-Sat; capture does not key PTT.</p>
      <div class="rc-mic-settings">
        <div class="rc-mic-mode" role="group" aria-label="Radio microphone input source">
          <span>Radio mic input</span>
          ${button('MIC', 'mic-mode', 'id="rc-mic-mode-mic" data-radio data-value="mic" aria-pressed="false" title="Set DATA OFF MOD and DATA MOD to the front microphone"')}
          ${button('LAN', 'mic-mode', 'id="rc-mic-mode-lan" data-radio data-value="lan" aria-pressed="false" title="Set DATA OFF MOD and DATA MOD to the LAN audio input"')}
          <output id="rc-mic-radio-config">Radio input not read</output>
        </div>
        <form id="rc-lan-mod-form"><label>Radio LAN MOD level (0–255)<input id="rc-lan-mod" type="number" min="0" max="255" step="1" required data-radio></label><button type="submit" class="rc-button" data-radio>Apply level</button></form>
      </div><p>DATA OFF MOD and DATA MOD are stored on the radio, and Pi-Sat sets the LAN input each time it connects. The buttons are a manual exception for this session; the LAN MOD level only matters while the LAN input is selected.</p>
    </details>
    <div class="rc-notices"><span id="rc-status" role="status">Checking radio configuration…</span><span id="rc-command-status" role="status"></span></div>`;

  const state = { enabled: false, connected: false, main: {}, sub: {} };
  let busy = false;
  // A refresh joins an existing radio session; it never connects the radio.
  let wantAudio = true;
  let audioStarting = false;
  let audioRetryAfter = 0;
  let streamHealthy = false;
  let stateSocketHealthy = false;
  let lastStateTime = 0;
  let restFallbackRunning = false;
  let scopeFallbackRunning = false;
  let scopeFallbackSequence = 0;
  let lastScopeSequence = null;
  let lastScopeTime = 0;
  let socket;
  let scope = null;
  // Decoded sweeps waiting for the next paint. rAF coalescing must not silently
  // discard waterfall rows, but a hidden tab must not queue unbounded history.
  const MAX_PENDING_ROWS = 12;
  let pendingRows = [];
  let paintRequested = false;
  let levelPaintRequested = false;
  let maxBins = null;
  let waterfallRows = [];
  const traceY = new Float32Array(512);
  const traceSoft = new Float32Array(512);
  let traceGradient = null;
  let traceGradientHeight = -1;
  // The waterfall keeps its rows in an offscreen buffer one row taller than the
  // visible canvas. Each sweep inserts a row at the top and the visible canvas
  // is drawn at a fractional offset, so 4-5 sweeps/s glide instead of jumping.
  let waterfallBufferCanvas = null;
  let scrollFrame = 0;
  let scrollOffset = 0;
  let scrollLastAt = 0;
  let sweepIntervalMs = 0;
  let sweepLastAt = 0;
  let scopeSyncQueued = true;
  let scopeSyncRunning = false;
  let requestedSpan = null;
  let drag = null;
  let wheelTune = null;
  let wheelTimer = null;
  const readoutSteps = { MAIN: 0, SUB: 0 };
  let readoutTimer = null;
  const meterViews = { MAIN: 's', SUB: 's' };
  const draftInputs = new Set();
  const DISPLAY_STORAGE_KEY = 'pi-sat.radio.scope-display.v1';
  function loadDisplayFlags() {
    const flags = { hold: false, fill: true, grid: true, smooth: true };
    try {
      const saved = JSON.parse(localStorage.getItem(DISPLAY_STORAGE_KEY) || '{}');
      for (const key of Object.keys(flags)) if (typeof saved[key] === 'boolean') flags[key] = saved[key];
    } catch { /* Storage can be unavailable in privacy-restricted browser contexts. */ }
    return flags;
  }
  const display = loadDisplayFlags();
  function saveDisplayFlags() {
    try { localStorage.setItem(DISPLAY_STORAGE_KEY, JSON.stringify(display)); }
    catch { /* Display toggles still work when browser storage is unavailable. */ }
  }
  function syncDisplayControls() {
    root.querySelectorAll('[data-action="hold"],[data-action="fill"],[data-action="grid"],[data-action="smooth"]')
      .forEach(node => node.setAttribute('aria-pressed', String(display[node.dataset.action] === true)));
  }
  const DISPLAY_DB_LIMITS = { min: -160, max: 0, gap: 10 };
  const LEVEL_STORAGE_KEY = 'pi-sat.radio.scope-levels.v2';
  const defaultLevelRange = () => ({ min: DISPLAY_DB_LIMITS.min, max: DISPLAY_DB_LIMITS.max });
  function validLevelRange(value) {
    const min = Math.max(DISPLAY_DB_LIMITS.min, Math.min(DISPLAY_DB_LIMITS.max, Number(value?.min)));
    const max = Math.max(DISPLAY_DB_LIMITS.min, Math.min(DISPLAY_DB_LIMITS.max, Number(value?.max)));
    return Number.isFinite(min) && Number.isFinite(max) && max - min >= DISPLAY_DB_LIMITS.gap
      ? { min, max } : defaultLevelRange();
  }
  function loadLevelRanges() {
    const ranges = { MAIN: defaultLevelRange(), SUB: defaultLevelRange() };
    try {
      const saved = JSON.parse(localStorage.getItem(LEVEL_STORAGE_KEY) || '{}');
      for (const key of Object.keys(ranges)) if (saved[key]) ranges[key] = validLevelRange(saved[key]);
    } catch { /* Storage can be unavailable in privacy-restricted browser contexts. */ }
    return ranges;
  }
  const levelRanges = loadLevelRanges();
  // Fixed relative-level color mapping shared by the trace and waterfall.
  // Warm colors start below full scale so ordinary strong signals are visible;
  // this changes display contrast, never the received amplitudes or RF scale.
  const intensityStops = [[0, [1, 5, 20]], [0.08, [0, 12, 100]], [0.20, [0, 72, 250]],
    [0.34, [0, 220, 255]], [0.46, [55, 245, 115]], [0.58, [255, 236, 45]],
    [0.70, [255, 62, 32]], [1, [255, 35, 30]]];
  const palette = Array.from({ length: 161 }, (_, value) => {
    const level = value / 160;
    const upper = intensityStops.findIndex(([at]) => at >= level);
    if (upper <= 0) return intensityStops[0][1];
    const [low, a] = intensityStops[upper - 1], [high, b] = intensityStops[upper];
    return a.map((v, channel) => Math.round(v + (b[channel] - v) * (level - low) / (high - low)));
  });
  function scopeSideKey(bounds = scope) {
    return bounds?.side === 'MAIN' ? 'MAIN' : bounds?.side === 'SUB' ? 'SUB' : listeningScopeSide();
  }
  function levelRange(bounds = scope) {
    return levelRanges[scopeSideKey(bounds)];
  }
  function scaledBin(value, range = levelRange()) {
    const displayDb = Number(value) - 160;
    return Math.max(0, Math.min(160, Math.round((displayDb - range.min) / (range.max - range.min) * 160)));
  }
  function saveLevelRanges() {
    try { localStorage.setItem(LEVEL_STORAGE_KEY, JSON.stringify(levelRanges)); }
    catch { /* Display controls still work when browser storage is unavailable. */ }
  }
  function syncLevelControls(bounds = scope) {
    const range = levelRange(bounds);
    const minPercent = (range.min - DISPLAY_DB_LIMITS.min) / (DISPLAY_DB_LIMITS.max - DISPLAY_DB_LIMITS.min) * 100;
    const maxPercent = (range.max - DISPLAY_DB_LIMITS.min) / (DISPLAY_DB_LIMITS.max - DISPLAY_DB_LIMITS.min) * 100;
    $('rc-level-min').value = range.min;
    $('rc-level-max').value = range.max;
    $('rc-level-visible').style.bottom = `${minPercent}%`;
    $('rc-level-visible').style.height = `${maxPercent - minPercent}%`;
    for (const [edge, value, percent] of [['min', range.min, minPercent], ['max', range.max, maxPercent]]) {
      const output = $(`rc-level-${edge}-value`);
      output.textContent = `${value} dB`;
      output.style.bottom = `calc(${Math.max(4, Math.min(96, percent))}% - 7px)`;
    }
    root.querySelector('.rc-level-window')?.setAttribute('data-side', scopeSideKey(bounds));
  }
  function updateLevelRange(edge, requestedValue) {
    const key = scopeSideKey();
    const current = levelRanges[key];
    const value = Math.max(DISPLAY_DB_LIMITS.min, Math.min(DISPLAY_DB_LIMITS.max, Number(requestedValue)));
    if (!Number.isFinite(value)) return;
    if (edge === 'min') current.min = Math.min(value, current.max - DISPLAY_DB_LIMITS.gap);
    else current.max = Math.max(value, current.min + DISPLAY_DB_LIMITS.gap);
    levelRanges[key] = validLevelRange(current);
    saveLevelRanges();
    syncLevelControls();
    scheduleLevelPaint();
  }
  const consoleApi = {
    listen: 'SUB', volume: 0.7, formatFrequency,
    error(message) { $('rc-command-status').textContent = message; },
    render(update) {
      // High-rate scope frames must not rebuild meters, menus or audio controls.
      if (Object.keys(update).length === 1 && Object.hasOwn(update, 'scope')) {
        acceptScope(update.scope);
        return;
      }
      const wasConnected = Boolean(state.connected);
      const wasDualwatch = state.dualwatch;
      Object.assign(state, update);
      if (state.dualwatch === true && wasDualwatch !== true) scopeSyncQueued = true;
      if (Boolean(state.connected) !== wasConnected) {
        scopeSyncQueued = true;
        lastScopeSequence = null;
        lastScopeTime = 0;
      }
      for (const side of ['MAIN', 'SUB']) {
        const data = state[side.toLowerCase()] || {};
        $(`rc-${side}-frequency`).textContent = formatFrequency(data.frequency_hz);
        $(`rc-${side}-band`).value = band(data.frequency_hz) === '—' ? '' : band(data.frequency_hz);
        for (const field of ['mode', 'filter', 'vfo']) {
          const input = $(`rc-${side}-${field}`);
          if (document.activeElement !== input) input.value = data[field] == null ? '' : String(data[field]);
        }
        for (const control of ['af_gain', 'rf_gain', 'squelch']) {
          const input = $(`rc-${side}-${control}`);
          if (!draftInputs.has(input.id) && document.activeElement !== input && Number.isFinite(data[control])) input.value = data[control];
          $(`rc-${side}-${control}-value`).textContent = Number.isFinite(data[control]) ? `${Math.round(data[control] / 255 * 100)}%` : '—';
          input.title = Number.isFinite(data[control]) ? `Radio readback: ${data[control]}/255` : 'No radio readback';
        }
        const meter = meterDisplay(meterViews[side], data, { ...state, connected: state.connected && streamHealthy });
        if (side === 'SUB' && meterViews[side] === 's' && state.dualwatch === false) { meter.text = 'SUB receiver off'; meter.percent = 0; }
        $(`rc-${side}-meter-name`).textContent = `${meter.name} ▾`;
        $(`rc-${side}-meter-value`).textContent = meter.text;
        $(`rc-${side}-meter-fill`).style.width = `${meter.percent}%`;
        const scale = $(`rc-${side}-meter-scale`);
        const labels = meter.labels.map(label => `<span>${label}</span>`).join('');
        if (scale.innerHTML !== labels) scale.innerHTML = labels;
        $(`rc-${side}-sql-state`).textContent = data.squelch_open == null ? 'Squelch state unknown' : `Squelch ${data.squelch_open ? 'OPEN' : 'CLOSED'}`;
        const panel = root.querySelector(`.rc-path[data-side="${side}"]`);
        panel.classList.toggle('is-target', listeningScopeSide() === side);
        panel.classList.toggle('is-live', state.connected && streamHealthy);
        panel.classList.toggle('is-transmitting', side === 'MAIN' && state.ptt === true && state.connected);
        $(`rc-${side}-fresh`).textContent = !state.connected ? 'DISCONNECTED' : !data.frequency_hz ? 'NO DATA' : !streamHealthy ? 'STALE'
          : data.age_s > 6 ? `LAST READ · ${data.age_s}s` : 'READBACK';
      }
      root.querySelectorAll('[data-radio]').forEach(node => { node.disabled = !state.connected || !streamHealthy; });
      $('rc-command-status').setAttribute('aria-busy', String(busy));
      // Unknown radio levels are not displayed as adjustable zeroes.
      root.querySelectorAll('[data-level]').forEach(node => { node.disabled ||= !Number.isFinite(state[node.dataset.side.toLowerCase()]?.[node.dataset.level]); });
      const connected = Boolean(state.connected);
      const audioAvailable = state.audio_available !== false;
      const connectionStage = String(state.connection_stage || 'connection')
        .replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase());
      const retrying = state.connecting && Number.isFinite(state.retry_in_s);
      $('rc-connection').classList.toggle('is-live', connected && streamHealthy);
      $('rc-connection').lastChild.textContent = !streamHealthy ? ' Status unavailable' : connected ? ' Connected'
        : retrying ? ` Retrying in ${state.retry_in_s}s` : state.connecting ? ' Connecting' : ' Disconnected';
      $('rc-power').textContent = connected || state.connecting ? '⏻ Disconnect' : '⏻ Connect';
      $('rc-power').classList.toggle('is-on', connected);
      // An unhealthy state stream must never trap an active/retrying session.
      // Keep Disconnect available even when all other radio writes are gated.
      $('rc-power').disabled = !state.enabled || (!streamHealthy && !connected && !state.connecting);
      // A commanded key or release stays visible while the radio confirms it:
      // the radio is still transmitting until it reports that it stopped.
      const transmitting = state.ptt === true || state.ptt_pending === true;
      const releasing = state.ptt === true && state.ptt_pending === false;
      if (state.tx_unconfirmed) {
        // A possible stuck transmitter must never look like a normal session.
        $('rc-status').textContent = 'TX NOT CONFIRMED · the radio may still be transmitting. Check the radio, then disconnect and reconnect.';
      } else if (state.connecting) {
        $('rc-status').textContent = `${state.last_error ? `${state.last_error} · ` : ''}${retrying ? 'Retrying' : 'Connecting at'} ${connectionStage}${state.connection_attempt ? ` · attempt ${state.connection_attempt}` : ''}`;
      } else {
        $('rc-status').textContent = state.last_error || (!state.enabled ? 'Enable advanced Icom radio control in Settings.'
          : connected ? `Single radio owner · network control${audioAvailable ? ' and audio' : ' · audio reconnecting'} · MAIN = TX · SUB = RX`
            : 'Radio off. Press Connect to start control and audio.');
      }
      $('rc-ptt-state').textContent = !connected ? 'PTT · UNKNOWN'
        : releasing ? 'PTT · RELEASING'
        : transmitting ? 'TRANSMITTING'
        : state.ptt == null ? 'PTT · UNKNOWN' : 'PTT · RX';
      $('rc-ptt').textContent = transmitting ? 'RELEASE PTT' : 'PTT';
      $('rc-ptt').classList.toggle('is-transmitting', connected && transmitting);
      // Unknown PTT is not an invitation to transmit blindly.
      $('rc-ptt').disabled ||= state.ptt == null && state.ptt_pending == null;
      // Capture now starts with the session rather than from its own button, so
      // locking the list while capture ran left the device unchangeable for the
      // whole connection. Only a change already in flight disables it.
      $('rc-mic-device').disabled = radioMicStarting;
      // Capture starts with the radio session; there is no separate enable step.
      $('rc-mic-requirements').textContent = !window.isSecureContext
        ? 'Microphone blocked on HTTP. Open Pi-Sat using HTTPS or localhost; RX audio still works on HTTP.'
        : radioMicStream ? 'Microphone capture is on and streaming to Pi-Sat; it does not key PTT.'
          : radioMicStarting ? 'Waiting for microphone permission…'
            : 'Connect starts the microphone and streams it to Pi-Sat; capture does not key PTT.';
      const micConfig = state.microphone;
      const sourceNames = ['MIC', 'ACC', 'MIC+ACC', 'USB', 'MIC+USB', 'LAN'];
      const micSourceName = code => Number.isInteger(code) ? (sourceNames[code] || `Code ${code}`) : '—';
      const dataOffMod = micConfig?.data_off_mod, dataMod = micConfig?.data_mod;
      const micModeKnown = Number.isInteger(dataOffMod) && Number.isInteger(dataMod);
      const micMode = !micModeKnown ? '—'
        : dataOffMod === dataMod ? micSourceName(dataOffMod)
          : `MIXED (${micSourceName(dataOffMod)} / ${micSourceName(dataMod)})`;
      $('rc-mic-radio-config').textContent = `Current radio mic input: ${micMode} · LAN MOD ${micConfig?.lan_mod_level ?? '—'}/255`;
      for (const [id, code] of [['rc-mic-mode-mic', 0], ['rc-mic-mode-lan', 5]]) {
        const modeButton = $(id);
        modeButton.setAttribute('aria-pressed', String(micModeKnown && dataOffMod === code && dataMod === code));
        // A known readback and a receiving radio are both required: the
        // backend rejects a microphone change while the radio is keyed.
        modeButton.disabled = !state.connected || !streamHealthy || !micModeKnown || transmitting;
      }
      if (!draftInputs.has('rc-lan-mod') && document.activeElement !== $('rc-lan-mod') && Number.isFinite(micConfig?.lan_mod_level)) $('rc-lan-mod').value = micConfig.lan_mod_level;
      $('rc-dualwatch').textContent = state.dualwatch == null ? 'Dualwatch —' : state.dualwatch ? 'Dualwatch ON' : 'Dualwatch OFF';
      $('rc-dualwatch').disabled ||= state.dualwatch == null;
      $('rc-dualwatch').setAttribute('aria-pressed', String(state.dualwatch === true));
      const rit = state.sub_rit;
      $('rc-rit-toggle').textContent = rit?.enabled == null ? 'RIT —' : rit.enabled ? 'RIT ON' : 'RIT OFF';
      $('rc-rit-readback').textContent = Number.isFinite(rit?.offset_hz) ? `Readback ${rit.offset_hz > 0 ? '+' : ''}${rit.offset_hz} Hz` : 'No RIT readback';
      if (!draftInputs.has('rc-rit-offset') && Number.isFinite(rit?.offset_hz)) $('rc-rit-offset').value = rit.offset_hz;
      // Only a genuinely ended session tears capture down. Keep audio resources
      // alive across transient connection-health changes.
      if (!connected && !state.connecting && (radioAudioSocket || radioMicStream || radioAudioContext)) stopRadioAudio();
      if (connected && !audioAvailable) {
        $('radio-audio-status').textContent = state.audio?.last_error || 'Radio audio unavailable; retrying automatically.';
      }
      if (connected && audioAvailable && wantAudio && !audioStarting && (!radioAudioSocket || radioAudioContext?.state !== 'running') && Date.now() >= audioRetryAfter) {
        audioStarting = true;
        audioRetryAfter = Date.now() + 3000;
        void startRadioAudio().finally(() => { audioStarting = false; });
      }
      if (connected && streamHealthy && !busy && scopeSyncQueued && !scopeSyncRunning) void syncListeningScope();
      if (state.scope) acceptScope(state.scope);
      $('rc-scope-source').textContent = `Spectrum follows listening · ${listeningScopeSide()}`;
      if (!connected) $('rc-scope-message').textContent = scope ? 'Disconnected · scope history retained' : 'Connect to receive spectrum';
      else if (listeningScopeSide() === 'SUB' && state.dualwatch === false) $('rc-scope-message').textContent = 'SUB receiver off · enable Dualwatch';
      else if (Date.now() - lastScopeTime >= 3000) $('rc-scope-message').textContent = 'Waiting for current scope data';
      $('rc-scope-message').hidden = connected && Date.now() - lastScopeTime < 3000 && !(listeningScopeSide() === 'SUB' && state.dualwatch === false);
      if (!connected || !streamHealthy) { drag = null; wheelTune = null; readoutSteps.MAIN = readoutSteps.SUB = 0; }
      scheduleSpectrumPaint();
    },
  };
  window.RadioConsole = consoleApi;

  function resumeAudioOnInteraction() {
    if (!state.connected || !wantAudio) return;
    // Autoplay-blocked resume() can stay pending. Retry synchronously inside
    // the gesture even when the original startup is still awaiting permission.
    if (radioAudioContext && radioAudioContext.state !== 'running') {
      void radioAudioContext.resume().catch(error => {
        $('radio-audio-status').textContent = `Browser audio could not resume: ${error.message || error}`;
      });
    }
    if (!audioStarting && (!radioAudioSocket || radioAudioContext?.state !== 'running')) {
      audioRetryAfter = 0;
      consoleApi.render({});
    }
  }
  document.addEventListener('click', resumeAudioOnInteraction, true);
  document.addEventListener('keydown', resumeAudioOnInteraction, true);

  function listeningScopeSide() {
    // Both audio still has one hardware scope; default to the RX path.
    return consoleApi.listen === 'MAIN' ? 'MAIN' : 'SUB';
  }
  function setListening(value) {
    if (!['MAIN', 'SUB', 'BOTH'].includes(value)) return;
    const previousSide = listeningScopeSide();
    drag = null;
    wheelTune = null;
    consoleApi.listen = value;
    root.querySelectorAll('[data-action="listen"]').forEach(item => item.setAttribute('aria-pressed', String(item.dataset.value === value)));
    if (previousSide !== listeningScopeSide()) {
      lastScopeTime = 0;
      scopeSyncQueued = true;
    }
    consoleApi.render({});
  }
  async function syncListeningScope() {
    if (scopeSyncRunning || busy || !state.connected || !streamHealthy) return;
    scopeSyncRunning = true;
    try {
      // Do not lose a listening change during another scope request. Coalesce
      // rapid choices to the latest one rather than sending concurrent writes.
      while (scopeSyncQueued && state.connected && streamHealthy && !busy) {
        scopeSyncQueued = false;
        const result = await radioAction('/api/radio/scope', { physical_side: listeningScopeSide(), enabled: true, ...(requestedSpan ? { span_hz: requestedSpan } : {}) });
        if (!result) consoleApi.error('Spectrum could not start. See the radio command error in Monitor.');
      }
    } finally { scopeSyncRunning = false; }
  }

  async function command(action, body) {
    if (busy || !state.connected || !streamHealthy) return null;
    busy = true;
    // Busy state remains available to assistive technology, but ordinary
    // successful clicks should not fill the operator notice area with CI-V
    // implementation details. radioAction() still surfaces real failures.
    consoleApi.error('');
    consoleApi.render({});
    try {
      const result = await radioAction(`/api/radio/${action}`, body);
      return result;
    } finally { busy = false; consoleApi.render({}); }
  }
  async function power() {
    if (busy) return;
    busy = true;
    if (state.connected || state.connecting) {
      wantAudio = false;
      // Do not leave an app-issued PTT active when explicitly disconnecting.
      if (state.ptt === true && !await radioAction('/api/radio/ptt', { enabled: false })) {
        busy = false; consoleApi.render({}); return;
      }
      stopRadioAudio();
      await radioAction('/api/radio/disconnect');
    } else {
      wantAudio = true;
      try { await prepareRadioAudio(); }
      catch (error) { consoleApi.error(`Browser audio: ${error.message}`); }
      // Ask for the microphone on the same click that connects the radio, so
      // the browser permission prompt appears while the session opens and the
      // operator never has to find a second button.
      const microphoneRequest = startRadioMicrophone();
      consoleApi.render({ connecting: true });
      const result = await radioAction('/api/radio/connect');
      if (!result) { wantAudio = false; consoleApi.render({ connecting: false }); }
      await microphoneRequest;
    }
    busy = false;
    consoleApi.render({});
  }
  root.addEventListener('click', async (event) => {
    const node = event.target.closest('[data-action]');
    if (!node || node.disabled) return;
    const { action, side, value } = node.dataset;
    if (action === 'power') await power();
    if (action === 'step') queueReadoutStep(side, Number(value));
    if (action === 'listen') {
      setListening(value);
    }
    if (action === 'meter') {
      const views = ['s', 'power', 'swr', 'compression'];
      meterViews[side] = views[(views.indexOf(meterViews[side]) + 1) % views.length];
      consoleApi.render({});
    }
    if (action === 'rit-reset') { $('rc-rit-offset').value = '0'; queueRit(); }
    if (action === 'dualwatch') await command('dualwatch', { enabled: !state.dualwatch });
    if (action === 'mic-refresh') await refreshRadioMicrophones();
    if (action === 'mic-mode') {
      const result = await command('microphone-config', { source: value });
      if (result && result.microphone) consoleApi.render({ microphone: result.microphone });
    }
    if (action === 'edit') {
      $(`rc-${side}-editor`).hidden = false;
      $(`rc-${side}-input`).value = state[side.toLowerCase()]?.frequency_hz || '';
      $(`rc-${side}-input`).focus();
    }
    if (action === 'cancel') $(`rc-${side}-editor`).hidden = true;
    if (action === 'ptt') {
      const key = state.ptt !== true;
      await command('ptt', { enabled: key });
    }
    if (Object.hasOwn(display, action)) {
      display[action] = !display[action];
      if (action === 'hold') maxBins = null;
      node.setAttribute('aria-pressed', String(display[action]));
      saveDisplayFlags();
      if (action === 'smooth') {
        // Turning smoothing off drops the glide and shows whole rows again.
        if (display.smooth) requestScroll();
        else { scrollOffset = 0; paintWaterfall(); }
      }
      paintSpectrum();
    }
  });
  for (const side of ['MAIN', 'SUB']) {
    $(`rc-${side}-dial`).addEventListener('wheel', event => {
      if (!readoutReady(side) || event.deltaY === 0) return;
      event.preventDefault();
      queueReadoutStep(side, event.deltaY < 0 ? 1 : -1);
    }, { passive: false });
    for (const field of ['mode', 'vfo', 'filter']) {
      const input = $(`rc-${side}-${field}`);
      input.addEventListener('change', async () => {
        const value = input.value;
        if (!value || input.disabled) return;
        if (field === 'vfo') {
          await command('select', { physical_side: side, vfo: value });
        } else {
          await command(field, { physical_side: side, [field]: field === 'filter' ? Number(value) : value });
        }
        // A menu choice is a request, not an authoritative radio readback.
        input.value = state[side.toLowerCase()]?.[field] == null ? '' : String(state[side.toLowerCase()][field]);
      });
    }
    $(`rc-${side}-editor`).addEventListener('submit', async event => {
      event.preventDefault();
      if ($('rc-lock').checked) { consoleApi.error('Tuning is locked.'); return; }
      const status = $(`rc-${side}-write-status`);
      const frequency = Number($(`rc-${side}-input`).value);
      if (!Number.isSafeInteger(frequency) || frequency <= 0 || frequency >= 10000000000) {
        status.textContent = 'Enter a whole frequency in Hz, for example 435250000.'; return;
      }
      if (busy) { status.textContent = 'Another command is pending. Try again when it finishes.'; return; }
      status.textContent = 'Writing and verifying…';
      // The VFO selector already applies A/B on the chosen receiver. Do not
      // invent an A/B readback or require one to tune that receiver's current VFO.
      const result = await command('frequency', { physical_side: side, frequency_hz: frequency });
      status.textContent = result ? '' : $('rc-command-status').textContent || 'Write failed; check the connection.';
      if (result) $(`rc-${side}-editor`).hidden = true;
    });
  }
  $('rc-volume').addEventListener('input', event => {
    consoleApi.volume = Number(event.target.value) / 100;
    $('rc-volume-value').textContent = `${event.target.value}%`;
  });
  function bindAdjustment(node, route, payload, min, max, step) {
    let timer = null;
    let pending = null;
    async function flush() {
      timer = null;
      if (!state.connected || !streamHealthy) { pending = null; draftInputs.delete(node.id); return; }
      if (busy) { timer = setTimeout(flush, 80); return; }
      const value = pending;
      pending = null;
      const result = await command(route, payload(value));
      if (pending === null && result) { draftInputs.delete(node.id); consoleApi.render({}); }
    }
    function queue() {
      const value = Number(node.value);
      if (!Number.isInteger(value) || value < min || value > max || node.disabled) return;
      pending = value;
      draftInputs.add(node.id);
      if (!timer) timer = setTimeout(flush, 80);
    }
    node.addEventListener('input', () => draftInputs.add(node.id));
    node.addEventListener('change', queue);
    node.addEventListener('wheel', event => {
      if (node.disabled || !state.connected || !streamHealthy || !event.deltaY) return;
      event.preventDefault();
      node.value = String(Math.max(min, Math.min(max, Number(node.value) + (event.deltaY < 0 ? step : -step))));
      queue();
    }, { passive: false });
    return queue;
  }
  for (const side of ['MAIN', 'SUB']) {
    for (const control of ['af_gain', 'rf_gain', 'squelch']) {
      bindAdjustment($(`rc-${side}-${control}`), 'level', value => ({ physical_side: side, control, value }), 0, 255, 3);
    }
  }
  $('rc-span').addEventListener('change', () => {
    requestedSpan = Number($('rc-span').value) || null;
    scopeSyncQueued = true;
    void syncListeningScope();
  });
  for (const edge of ['min', 'max']) {
    $(`rc-level-${edge}`).addEventListener('input', event => updateLevelRange(edge, event.target.value));
  }
  const queueRit = bindAdjustment($('rc-rit-offset'), 'sub-rit', offset_hz => ({ offset_hz, enabled: offset_hz !== 0 }), -9990, 9990, 10);
  $('rc-rit-form').addEventListener('submit', event => {
    event.preventDefault();
    queueRit();
  });
  for (const id of ['rc-rit-offset', 'rc-lan-mod']) $(id).addEventListener('input', () => draftInputs.add(id));
  $('rc-mic-device').addEventListener('change', event => { void selectRadioMicrophone(event.target.value); });
  // The browser withholds this computer's input names until the page has been
  // granted capture, so re-read the list each time the section is opened rather
  // than trusting whatever it showed the first time.
  $('rc-microphone-panel').addEventListener('toggle', event => {
    if (event.target.open) void refreshRadioMicrophones();
  });
  void refreshRadioMicrophones();
  $('rc-mic-gain').addEventListener('input', event => {
    applyRadioMicGain(Number(event.target.value) / 100);
    $('rc-mic-gain-value').textContent = `${event.target.value}%`;
  });
  $('rc-lan-mod-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (await command('microphone-config', { lan_mod_level: Number($('rc-lan-mod').value) })) {
      draftInputs.delete('rc-lan-mod'); consoleApi.render({});
    }
  });

  function spectrumReady() {
    return state.connected && streamHealthy && !$('rc-lock').checked && scope
      && !(listeningScopeSide() === 'SUB' && state.dualwatch === false)
      && scope.side === listeningScopeSide() && Date.now() - lastScopeTime < 3000;
  }
  function pointerHz(event, bounds = scope) {
    const rect = $('rc-spectrum').getBoundingClientRect();
    return bounds.low + Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * (bounds.high - bounds.low);
  }
  function dragDelta(start, current, step) {
    return Math.sign(current - start) * Math.round(Math.abs(current - start) / step) * step;
  }
  const spectrumCanvas = $('rc-spectrum');
  spectrumCanvas.addEventListener('pointerdown', event => {
    if (event.button !== 0 || !spectrumReady() || busy || scopeSyncRunning) return;
    const side = listeningScopeSide(), data = state[side.toLowerCase()];
    const passband = passbandRange(data, side === 'SUB' ? state.sub_rit : null);
    const at = pointerHz(event);
    if (!passband || at < passband.low || at > passband.high) return;
    wheelTune = null;
    drag = { side, pointerId: event.pointerId, start: at, low: scope.low, high: scope.high,
      frequency: data.frequency_hz, step: Number($('rc-step').value), delta: 0 };
    spectrumCanvas.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  spectrumCanvas.addEventListener('pointermove', event => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    drag.delta = dragDelta(drag.start, pointerHz(event, drag), drag.step);
    scheduleSpectrumPaint();
  });
  spectrumCanvas.addEventListener('pointerup', event => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    const done = drag; drag = null;
    spectrumCanvas.releasePointerCapture(event.pointerId);
    scheduleSpectrumPaint();
    if (spectrumReady() && !busy && done.side === listeningScopeSide() && done.delta) {
      void command('tune', { physical_side: done.side, delta_hz: done.delta, expected_frequency_hz: done.frequency });
    }
  });
  for (const name of ['pointercancel', 'lostpointercapture']) spectrumCanvas.addEventListener(name, () => { drag = null; scheduleSpectrumPaint(); });
  spectrumCanvas.addEventListener('wheel', event => {
    if (!spectrumReady() || drag || event.deltaY === 0) return;
    event.preventDefault();
    const side = listeningScopeSide();
    if (!wheelTune || wheelTune.side !== side) wheelTune = { side, delta: 0 };
    wheelTune.delta += (event.deltaY < 0 ? 1 : -1) * Number($('rc-step').value);
    if (!wheelTimer) wheelTimer = setTimeout(flushWheelTune, 80);
  }, { passive: false });
  async function flushWheelTune() {
    wheelTimer = null;
    if (!wheelTune || !spectrumReady()) { wheelTune = null; return; }
    if (busy || scopeSyncRunning) { wheelTimer = setTimeout(flushWheelTune, 80); return; }
    const pending = wheelTune; wheelTune = null;
    if (pending.delta && pending.side === listeningScopeSide()) await command('tune', { physical_side: pending.side, delta_hz: pending.delta });
  }

  function readoutReady(side) {
    return ['MAIN', 'SUB'].includes(side) && state.connected && streamHealthy && !$('rc-lock').checked
      && !(side === 'SUB' && state.dualwatch === false)
      && Number.isFinite(state[side.toLowerCase()]?.frequency_hz);
  }
  function queueReadoutStep(side, direction) {
    if (!readoutReady(side) || ![1, -1].includes(direction)) return;
    readoutSteps[side] += direction * Number($('rc-step').value);
    if (!readoutTimer) readoutTimer = setTimeout(flushReadoutSteps, 80);
  }
  async function flushReadoutSteps() {
    readoutTimer = null;
    for (const side of ['MAIN', 'SUB']) if (!readoutReady(side)) readoutSteps[side] = 0;
    const side = ['MAIN', 'SUB'].find(value => readoutSteps[value] !== 0);
    if (!side) return;
    if (busy || scopeSyncRunning) { readoutTimer = setTimeout(flushReadoutSteps, 80); return; }
    const delta = readoutSteps[side]; readoutSteps[side] = 0;
    await command('tune', { physical_side: side, delta_hz: delta });
    if (!readoutTimer && (readoutSteps.MAIN || readoutSteps.SUB)) readoutTimer = setTimeout(flushReadoutSteps, 80);
  }

  function bcd(bytes) {
    let result = 0;
    for (let i = bytes.length - 1; i >= 0; i -= 1) {
      if ((bytes[i] & 15) > 9 || (bytes[i] >> 4) > 9) return null;
      result = result * 100 + (bytes[i] >> 4) * 10 + (bytes[i] & 15);
    }
    return result;
  }
  function decodeScope(hex) {
    if (typeof hex !== 'string' || !/^[a-f\d]+$/i.test(hex) || hex.length % 2) return null;
    const bytes = Uint8Array.from(hex.match(/../g), byte => parseInt(byte, 16));
    // IC-9700 LAN waveform: one division, metadata then 475 bins (0..160).
    if (bytes.length !== 497 || bytes[0] !== 254 || bytes[1] !== 254 || bytes[4] !== 39 || bytes[5] !== 0
        || bytes[6] > 1 || bytes[7] !== 1 || bytes[8] !== 1 || bytes[9] > 1 || bytes[20] !== 0 || bytes[496] !== 253) return null;
    const first = bcd(bytes.slice(10, 15));
    const second = bcd(bytes.slice(15, 20));
    if (first == null || second == null) return null;
    const low = bytes[9] === 0 ? first - second : first;
    const high = bytes[9] === 0 ? first + second : second;
    const bins = bytes.slice(21, 496);
    if (high <= low || bins.some(value => value > 160)) return null;
    return { side: bytes[6] ? 'SUB' : 'MAIN', low, high, bins };
  }
  consoleApi.decodeScope = decodeScope;
  function passbandRange(data, rit = null) {
    const base = data?.frequency_hz, width = data?.bandwidth_hz;
    const frequency = base + (rit?.enabled === true && Number.isFinite(rit.offset_hz) ? rit.offset_hz : 0);
    if (!Number.isFinite(frequency) || !Number.isFinite(width) || frequency <= 0 || width <= 0) return null;
    if (data.mode === 'USB') return { low: frequency, high: frequency + width };
    if (data.mode === 'LSB') return { low: frequency - width, high: frequency };
    if (['FM', 'AM', 'CW', 'CW-R', 'DV', 'DD'].includes(data.mode)) return { low: frequency - width / 2, high: frequency + width / 2 };
    return null;
  }
  function meterDisplay(view, receiver, radio) {
    const definitions = {
      s: { name: 'S-METER', points: [[0, 0], [120, 9], [241, 69]], labels: ['S0', 'S9', 'S9 +60'] },
      power: { name: 'POWER · MAIN TX', points: [[0, 0], [143, 50], [213, 100]], labels: ['0%', '50%', '100%'] },
      swr: { name: 'SWR · MAIN TX', points: [[0, 1], [48, 1.5], [80, 2], [120, 3]], labels: ['1:1', '2:1', '3:1'] },
      compression: { name: 'COMP · MAIN TX', points: [[0, 0], [130, 15], [210, 25.5]], labels: ['0 dB', '15 dB', '25.5 dB'] },
    };
    const definition = definitions[view];
    const raw = view === 's' ? receiver.s_meter : radio.tx_meters?.[view];
    const result = { name: definition.name, labels: definition.labels, text: 'No readback', percent: 0 };
    if (!radio.connected) return { ...result, text: 'Disconnected' };
    if (view !== 's' && radio.ptt !== true) return { ...result, text: 'Available during TX' };
    const age = view === 's' ? receiver.age_s : radio.tx_meters?.age_s;
    if (!Number.isFinite(raw) || age == null) return result;
    if (age > 6) return { ...result, text: 'Stale readback' };
    const points = definition.points;
    let value = points[points.length - 1][1];
    const over = raw > points[points.length - 1][0];
    for (let i = 1; i < points.length; i++) {
      if (raw <= points[i][0]) {
        const [x0, y0] = points[i - 1], [x1, y1] = points[i];
        value = y0 + (raw - x0) / (x1 - x0) * (y1 - y0); break;
      }
    }
    // Interpolation between Icom's documented calibration points is approximate.
    const suffix = over ? '+' : '';
    result.text = view === 's' ? (value <= 9 ? `≈ S${value.toFixed(1)}` : `≈ S9 +${Math.round(value - 9)}${suffix} dB`)
      : view === 'power' ? `≈ ${value.toFixed(0)}${suffix}%` : view === 'swr' ? `≈ ${value.toFixed(1)}${suffix}:1` : `≈ ${value.toFixed(1)}${suffix} dB`;
    result.percent = Math.min(100, Math.max(0, raw / points[points.length - 1][0] * 100));
    return result;
  }
  function acceptScope(update) {
    state.scope = update;
    if (!update?.packet || update.sequence === lastScopeSequence) return;
    lastScopeSequence = update.sequence;
    const next = decodeScope(update.packet);
    if (!next || next.side !== listeningScopeSide()) return;
    // Keep every decoded sweep. Holding only the newest pending frame meant a
    // late or throttled paint silently discarded waterfall rows, so the
    // waterfall advanced slower than the radio sweeps it is showing.
    pendingRows.push(next);
    if (pendingRows.length > MAX_PENDING_ROWS) pendingRows.splice(0, pendingRows.length - MAX_PENDING_ROWS);
    lastScopeTime = Date.now();
    if (state.connected) $('rc-scope-message').hidden = true;
    scheduleSpectrumPaint();
  }
  function scheduleSpectrumPaint() {
    if (paintRequested) return;
    paintRequested = true;
    requestAnimationFrame(() => {
      paintRequested = false;
      const rows = pendingRows;
      pendingRows = [];
      for (const next of rows) if (next.side === listeningScopeSide()) drawScope(next);
      paintSpectrum();
    });
  }
  function scheduleLevelPaint() {
    if (levelPaintRequested) return;
    levelPaintRequested = true;
    requestAnimationFrame(() => {
      levelPaintRequested = false;
      repaintWaterfall();
      paintSpectrum();
    });
  }
  function waterfallBuffer() {
    const waterfall = $('rc-waterfall');
    if (!waterfall) return null;
    // One extra row of history means the glide never exposes a blank line at
    // the bottom: the visible window moves from rows 0..N-1 to rows 1..N.
    const height = waterfall.height + 1;
    if (!waterfallBufferCanvas || waterfallBufferCanvas.width !== waterfall.width || waterfallBufferCanvas.height !== height) {
      waterfallBufferCanvas = document.createElement('canvas');
      waterfallBufferCanvas.width = waterfall.width;
      waterfallBufferCanvas.height = height;
      waterfallRows = [];
    }
    return waterfallBufferCanvas;
  }
  function paintWaterfall() {
    const waterfall = $('rc-waterfall');
    const buffer = waterfallBuffer();
    if (!waterfall || !buffer) return;
    // A fractional offset with smoothing enabled is what turns discrete sweep
    // insertions into continuous motion.
    waterfall.getContext('2d').drawImage(buffer, 0, -scrollOffset);
  }
  function scrollActive() {
    return display.smooth && Boolean(scope) && Boolean(state.connected) && Boolean(waterfallBufferCanvas);
  }
  function requestScroll() {
    if (scrollFrame || !scrollActive()) return;
    scrollLastAt = 0;
    scrollFrame = requestAnimationFrame(scrollStep);
  }
  function scrollStep(now) {
    scrollFrame = 0;
    if (!scrollActive()) { scrollOffset = 0; return; }
    const at = Number.isFinite(now) ? now : Date.now();
    const delta = scrollLastAt ? Math.min(100, at - scrollLastAt) : 0;
    scrollLastAt = at;
    if (delta > 0) scrollOffset = Math.min(1, scrollOffset + delta / (sweepIntervalMs || 200));
    paintWaterfall();
    scrollFrame = requestAnimationFrame(scrollStep);
  }
  function drawScope(next) {
    const waterfall = $('rc-waterfall');
    const buffer = waterfallBuffer();
    if (!waterfall || !buffer) return;
    const ctx = buffer.getContext('2d');
    const changedBounds = !scope || scope.side !== next.side || scope.low !== next.low || scope.high !== next.high;
    if (changedBounds) {
      maxBins = null;
      const width = next.high - next.low;
      const shift = scope ? (scope.low - next.low) / width * waterfall.width : 0;
      if (scope && scope.side === next.side && scope.high - scope.low === width && Math.abs(shift) < waterfall.width) {
        // Recenter existing history with the frequency axis instead of flashing
        // the whole waterfall blank on every small tuning adjustment.
        if (shift) {
          ctx.drawImage(buffer, shift, 0);
          ctx.clearRect(shift > 0 ? 0 : waterfall.width + shift, 0, Math.abs(shift), buffer.height);
          const binShift = Math.round(shift);
          waterfallRows = waterfallRows.map((values) => {
            const shifted = new Array(waterfall.width).fill(null);
            values.forEach((value, index) => {
              const target = index + binShift;
              if (target >= 0 && target < shifted.length) shifted[target] = value;
            });
            return shifted;
          });
        }
      } else {
        ctx.clearRect(0, 0, waterfall.width, buffer.height);
        waterfallRows = [];
      }
    }
    scope = next;
    syncLevelControls(next);
    lastScopeTime = Date.now();
    maxBins = next.bins.map((value, index) => Math.max(value, maxBins?.[index] || 0));
    if (changedBounds) {
      $('rc-scope-range').textContent = `Center ${formatFrequency((next.low + next.high) / 2)} · Span ${((next.high - next.low) / 1000).toLocaleString()} kHz`;
      [...$('rc-scope-axis').children].forEach((node, index) => { node.textContent = `${((next.low + (next.high - next.low) * index / 4) / 1e6).toFixed(3)}${index === 4 ? ' MHz' : ''}`; });
    }
    ctx.drawImage(buffer, 0, 0, waterfall.width, buffer.height - 1, 0, 1, waterfall.width, buffer.height - 1);
    const row = ctx.createImageData(waterfall.width, 1);
    next.bins.forEach((value, index) => {
      row.data.set(palette[scaledBin(value)], index * 4); row.data[index * 4 + 3] = 255;
    });
    ctx.putImageData(row, 0, 0);
    waterfallRows.unshift([...next.bins]);
    if (waterfallRows.length > buffer.height) waterfallRows.length = buffer.height;
    // A fresh sweep restarts the glide from the top and refines the measured
    // sweep interval, so scroll speed follows the radio instead of a guess.
    scrollOffset = 0;
    const now = Date.now();
    if (sweepLastAt) sweepIntervalMs = Math.max(40, Math.min(1000, (now - sweepLastAt) * 0.25 + (sweepIntervalMs || 200) * 0.75));
    sweepLastAt = now;
    paintWaterfall();
    requestScroll();
  }
  function repaintWaterfall() {
    const waterfall = $('rc-waterfall');
    const buffer = waterfallBuffer();
    if (!waterfall || !buffer) return;
    const ctx = buffer.getContext('2d');
    ctx.clearRect(0, 0, waterfall.width, buffer.height);
    const rows = Math.min(waterfallRows.length, buffer.height);
    if (rows) {
      const image = ctx.createImageData(waterfall.width, rows);
      waterfallRows.slice(0, rows).forEach((values, y) => {
        values.forEach((value, x) => {
          const offset = (y * waterfall.width + x) * 4;
          image.data.set(palette[Number.isFinite(value) ? scaledBin(value) : 0], offset);
          image.data[offset + 3] = 255;
        });
      });
      ctx.putImageData(image, 0, 0);
    }
    paintWaterfall();
  }
  function paintSpectrum() {
    const canvas = $('rc-spectrum');
    const ctx = canvas.getContext('2d');
    const width = canvas.width, height = canvas.height;
    ctx.clearRect(0, 0, width, height);
    if (display.grid) {
      ctx.strokeStyle = '#183c5066'; ctx.lineWidth = 1; ctx.beginPath();
      for (let x = 0; x <= width; x += width / 20) { ctx.moveTo(x, 0); ctx.lineTo(x, height); }
      for (let y = 0; y <= height; y += height / 5) { ctx.moveTo(0, y); ctx.lineTo(width, y); }
      ctx.stroke();
    }
    if (!scope) return;
    const levels = levelRange(scope);
    const data = state[scope.side.toLowerCase()] || {};
    const passband = passbandRange(data, scope.side === 'SUB' ? state.sub_rit : null);
    const offset = drag?.side === scope.side ? drag.delta : 0;
    const xForHz = hz => (hz - scope.low) / (scope.high - scope.low) * width;
    $('rc-passband-label').textContent = passband ? `${scope.side} · ${data.mode} · ${(data.bandwidth_hz / 1000).toLocaleString()} kHz${drag ? ' · drag preview' : ''}` : 'Passband width unknown';
    if (passband && scope.side === listeningScopeSide()) {
      const left = Math.max(0, xForHz(passband.low + offset)), right = Math.min(width, xForHz(passband.high + offset));
      if (right > left) {
        const shade = ctx.createLinearGradient(0, 0, 0, height);
        shade.addColorStop(0, '#33bdeb32'); shade.addColorStop(1, '#168bdd12');
        ctx.fillStyle = shade; ctx.fillRect(left, 0, right - left, height);
        ctx.strokeStyle = '#49cfff8c'; ctx.lineWidth = 1; ctx.strokeRect(left, 0, right - left, height);
      }
      const tunedX = xForHz(data.frequency_hz + offset);
      if (tunedX >= 0 && tunedX <= width) {
        ctx.strokeStyle = '#f0cc8b'; ctx.beginPath(); ctx.moveTo(tunedX, 0); ctx.lineTo(tunedX, height); ctx.stroke();
      }
    }
    const trace = (values, color, fill) => {
      // Scale every bin once per frame, then reuse the trace edge for the fill
      // and the bright outline instead of re-deriving all 475 points twice.
      // Smoothing is display-only: a light three-point average takes the edge
      // off bin-to-bin noise, and the underlying bins never change.
      const scaleY = (height - 12) / 160;
      let points = values;
      if (display.smooth && values.length > 2) {
        for (let index = 0; index < values.length; index++) {
          const before = values[index === 0 ? 0 : index - 1];
          const after = values[index === values.length - 1 ? index : index + 1];
          traceSoft[index] = (before + 2 * values[index] + after) / 4;
        }
        points = traceSoft;
      }
      for (let index = 0; index < values.length; index++) traceY[index] = height - scaledBin(points[index], levels) * scaleY;
      const stepX = width / (values.length - 1);
      const edge = () => {
        ctx.beginPath();
        for (let index = 0; index < values.length; index++) {
          const x = index * stepX;
          if (!index) ctx.moveTo(x, traceY[index]); else ctx.lineTo(x, traceY[index]);
        }
      };
      edge();
      if (fill) {
        ctx.lineTo(width, height); ctx.lineTo(0, height); ctx.closePath();
        if (!traceGradient || traceGradientHeight !== height) {
          traceGradient = ctx.createLinearGradient(0, height, 0, 12);
          for (const [level, rgb] of intensityStops) {
            traceGradient.addColorStop(level, `rgba(${rgb.join(',')},${0.85 + level * 0.15})`);
          }
          traceGradientHeight = height;
        }
        ctx.fillStyle = traceGradient; ctx.fill();
        // Rebuild only the trace edge, so the bright outline does not close
        // around the bottom/sides of the filled polygon.
        edge();
      }
      ctx.strokeStyle = color; ctx.lineWidth = fill ? 1 : 1.2; ctx.stroke();
    };
    if (display.hold && maxBins) trace(maxBins, '#be995b', false);
    trace(scope.bins, '#b7eeff', display.fill);
  }
  function subscribe() {
    try {
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/radio/state`);
    } catch (error) {
      socket = null;
      consoleApi.error(`Radio state WebSocket could not start: ${error.message || error}. Using status polling.`);
      setTimeout(subscribe, 2000);
      return;
    }
    socket.onmessage = event => {
      try {
        const update = JSON.parse(event.data);
        stateSocketHealthy = true;
        if (Object.keys(update).some(key => key !== 'scope')) { streamHealthy = true; lastStateTime = Date.now(); }
        renderRadioState(update);
      }
      catch { consoleApi.error('Invalid radio state update'); }
    };
    socket.onclose = () => {
      stateSocketHealthy = false;
      streamHealthy = false; consoleApi.render({});
      consoleApi.error('Live radio state connection lost; using status polling while reconnecting…');
      setTimeout(subscribe, 2000);
    };
  }
  async function refreshStateFromRest() {
    if (restFallbackRunning) return;
    restFallbackRunning = true;
    try {
      const result = await loadRadioState();
      if (result) {
        streamHealthy = true;
        lastStateTime = Date.now();
        consoleApi.render({});
      }
    } finally { restFallbackRunning = false; }
  }
  async function refreshScopeFromRest() {
    if (scopeFallbackRunning || stateSocketHealthy || !state.connected) return;
    scopeFallbackRunning = true;
    try {
      const result = await fetchJson('/api/radio/scope-data', { packets: [] });
      for (const packet of result?.packets || []) {
        consoleApi.render({ scope: { sequence: `rest-${scopeFallbackSequence += 1}`, packet } });
      }
    } finally { scopeFallbackRunning = false; }
  }
  // Start the essential state channel before optional spectrum rendering.
  subscribe();
  new ResizeObserver(entries => {
    const width = Math.round(entries[0].contentRect.width * Math.min(devicePixelRatio || 1, 2));
    if (width > 0 && $('rc-spectrum').width !== width) { $('rc-spectrum').width = width; paintSpectrum(); }
  }).observe($('rc-spectrum'));
  syncLevelControls();
  syncDisplayControls();
  paintSpectrum();
  consoleApi.render({});
  void refreshStateFromRest();
  // Age indicators and audio recovery remain active while other tabs are shown.
  // REST is a low-rate control-state fallback; spectrum still requires WebSocket.
  setInterval(() => {
    if (Date.now() - lastStateTime > 6000) {
      streamHealthy = false;
      void refreshStateFromRest();
    }
    consoleApi.render({});
  }, 1000);
  // Five frames per second preserves a usable waterfall if only the state
  // WebSocket is unavailable. The normal WebSocket path remains preferred.
  setInterval(() => { void refreshScopeFromRest(); }, 200);
})();
