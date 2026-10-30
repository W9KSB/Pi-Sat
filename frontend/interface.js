(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const icon = name => `<svg aria-hidden="true"><use href="#ui-${name}"></use></svg>`;
  const navigation = (page, label, name, native = false) => `<button type="button" class="station-nav-link" data-page="${page}"${native ? ' data-native-only hidden' : ''}>${icon(name)}<span>${label}</span></button>`;
  const shell = document.createElement('div');
  shell.className = 'station-shell';
  shell.innerHTML = `
    <header class="station-topbar">
      <a class="station-brand" href="#home"><img src="/assets/satellite-clipart.png" alt=""><span><strong>Pi-Sat</strong><small>Ground Station Controller</small></span></a>
      <section class="station-pass" aria-label="Satellite pass progression"><div class="station-pass-heading"><strong id="pass-satellite-name">No satellite selected</strong><span id="pass-profile-name"></span></div><div class="station-pass-status"><span id="pass-progress-state">Waiting for pass data</span><span id="pass-countdown"></span></div></section>
      <div class="station-operations"><div data-native-only hidden class="station-operation"><button type="button" id="station-radio-power" class="operation-face" aria-label="Connect radio" aria-pressed="false" disabled>${icon('radio')}</button><span>Radio</span><small id="station-radio-status" role="status">Disconnected</small></div><div id="station-device-controls"></div></div>
      <div class="station-rotator-position"><small>Rotator position</small><span>AZ <strong id="station-rotator-az">—</strong></span><span>EL <strong id="station-rotator-el">—</strong></span></div>
    </header>
    <nav class="station-navigation" aria-label="Primary navigation"><div class="station-nav-main">
      ${navigation('home', 'Home', 'home')}
      <div data-native-only hidden><button type="button" id="station-modules-toggle" class="station-nav-link" aria-expanded="false" aria-controls="station-module-menu">${icon('packet')}<span>Modules</span>${icon('chevron')}</button><div id="station-module-menu" class="station-module-menu" hidden>
        <button type="button" data-module="sstv-decoder" id="modules-nav-sstv-decoder">SSTV Decoder</button><button type="button" data-module="aprs" id="modules-nav-aprs">APRS</button><button type="button" data-module="ft4-ft8" id="modules-nav-ft4-ft8">FT4 / FT8</button><button type="button" data-module="audio-stream" id="modules-nav-audio-stream">External DATA Decode</button>
      </div></div><div class="station-nav-divider"></div>${navigation('satellites', 'Satellites', 'satellite')}${navigation('audio-recordings', 'Audio Recordings', 'record')}${navigation('map', 'Map', 'map')}${navigation('qso-finder', 'QSO Finder', 'search')}
      </div><div class="station-nav-bottom">${navigation('monitor', 'Station Monitor', 'monitor')}${navigation('settings', 'Settings', 'settings')}<div class="station-identity"><span id="station-experience">Legacy / Dual radio</span><small id="station-location"></small></div></div></nav>
    <div class="station-main"></div>
    <aside class="station-pass-sidebar"><button type="button" id="station-sidebar-toggle" aria-expanded="true" aria-controls="station-pass-content">${icon('chevron')}<span>Pass settings</span></button><div id="station-pass-content"><h2>Pass data</h2><div id="station-selection"></div><div id="station-profile"></div><div id="station-passes"></div><details class="station-rotator-details"><summary>Rotator controls</summary><div id="station-rotator-controls"></div></details></div></aside>`;
  const main = document.querySelector('main.app-shell');
  main.before(shell);
  shell.querySelector('.station-main').append(main);
  const definitions = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  definitions.setAttribute('class', 'station-icon-definitions');
  definitions.setAttribute('aria-hidden', 'true');
  definitions.innerHTML = `<defs>
    <symbol id="ui-home" viewBox="0 0 24 24"><path d="m3 10 9-7 9 7v11h-7v-7h-4v7H3z"/></symbol>
    <symbol id="ui-radio" viewBox="0 0 24 24"><rect x="3" y="7" width="18" height="14" rx="2"/><path d="M6 7 18 2M6 11h8M6 15h5M6 18h5"/><circle cx="17" cy="16" r="2"/></symbol>
    <symbol id="ui-target" viewBox="0 0 24 24"><circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2"/><path d="M12 1v4m0 14v4M1 12h4m14 0h4"/></symbol>
    <symbol id="ui-rotator" viewBox="0 0 24 24"><path d="M7 3a8 8 0 0 0 11 11L7 3Zm6 6 6-6M9 16l-2 5m5-5 2 5M4 21h13M18 3h3v3"/></symbol>
    <symbol id="ui-satellite" viewBox="0 0 24 24"><path d="m9 8 7 7-5 5-7-7zM7 6 4 3 1 6l3 3m11 8 3 3 3-3-3-3M14 4a6 6 0 0 1 6 6M9 15l-4 4"/></symbol>
    <symbol id="ui-record" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/></symbol>
    <symbol id="ui-packet" viewBox="0 0 24 24"><path d="m3 7 9-4 9 4-9 5zM3 7v10l9 4 9-4V7M12 12v9"/></symbol>
    <symbol id="ui-map" viewBox="0 0 24 24"><path d="m3 5 6-2 6 2 6-2v16l-6 2-6-2-6 2zM9 3v16M15 5v16"/></symbol>
    <symbol id="ui-search" viewBox="0 0 24 24"><circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/></symbol>
    <symbol id="ui-monitor" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4M5 11h3l2-5 4 8 2-4h3"/></symbol>
    <symbol id="ui-settings" viewBox="0 0 24 24"><path d="m9 3 1-2h4l1 2 3 2 3 1v4l-2 2v3l2 2-2 4h-3l-2 2h-4l-1-2-3-1H3l-2-4 2-2v-3L1 9l2-4h3z" transform="translate(2 1) scale(.83)"/><circle cx="12" cy="12" r="3"/></symbol>
    <symbol id="ui-chevron" viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></symbol>
  </defs>`;
  document.body.append(definitions);

  const stationMain = shell.querySelector('.station-main');
  const passSidebar = shell.querySelector('.station-pass-sidebar');
  const homeHeader = document.querySelector('#home-page > header');
  homeHeader.hidden = true;
  const controlHost = byId('station-device-controls');
  for (const [control, label, name] of [['rx', 'RX tracking', 'target'], ['tx', 'TX tracking', 'target'], ['rotator', 'Rotator', 'rotator']]) {
    const toggle = byId(`${control}-control-toggle`);
    const wrapper = document.createElement('label');
    wrapper.className = 'station-operation';
    toggle.className = 'operation-input';
    toggle.setAttribute('aria-label', label);
    wrapper.append(toggle);
    const face = document.createElement('span');
    face.className = 'operation-face';
    face.innerHTML = icon(name);
    const title = document.createElement('span');
    title.textContent = label;
    const status = document.createElement('small');
    status.id = `station-${control}-status`;
    status.textContent = 'Off';
    wrapper.append(face, title, status);
    controlHost.append(wrapper);
    toggle.addEventListener('change', () => renderControlState());
  }
  const arc = byId('pass-arc-map');
  arc.width = 640; arc.height = 64;
  shell.querySelector('.station-pass').append(arc);
  const selection = byId('selected-satellite').closest('.summary-box');
  byId('station-selection').append(selection);
  byId('station-profile').append(document.querySelector('.profile-row'));
  byId('station-passes').append(document.querySelector('.track-filter'), document.querySelector('.pass-panel'));
  byId('station-rotator-controls').append(byId('rotator-state-label').closest('.card'));
  const controlsCard = document.querySelector('.control-panel-card');
  controlsCard.querySelector(':scope > .card-header').hidden = true;
  const manual = byId('manual-offsets-toggle').closest('label');
  const sync = byId('sync-rx-tx-toggle').closest('label');
  sync.classList.add('station-sync');
  sync.querySelector('.form-check-label').textContent = 'Sync TX/RX';
  const reset = byId('reset-rx-offset');
  reset.textContent = 'Reset Offsets';
  const deck = document.createElement('div');
  deck.className = 'station-legacy-deck';
  const center = document.createElement('div');
  center.className = 'station-offset-actions';
  center.append(sync, reset);
  for (const [role, title] of [['tx', 'TX Radio'], ['rx', 'RX Radio']]) {
    const panel = byId(`${role}-link-panel`);
    panel.querySelector('h3').textContent = `${title} (${role === 'tx' ? 'Uplink' : 'Downlink'})`;
    const frequency = document.createElement('div');
    frequency.className = 'station-tuned-frequency';
    frequency.innerHTML = `<span>Current tuned frequency</span><output id="${role}-frequency">—</output><small id="${role}-frequency-status" role="status">Waiting for device readback</small>`;
    panel.querySelector('.card-body').prepend(frequency);
    byId(role === 'rx' ? 'tracking-target-rx' : 'tracking-calculated-tx').parentElement.hidden = true;
    deck.append(panel);
    if (role === 'tx') deck.append(center);
  }
  controlsCard.querySelector('.card-body').prepend(deck);
  const manualHost = document.createElement('div');
  manualHost.className = 'station-manual-offsets';
  manualHost.append(manual);
  byId('station-selection').append(manualHost);
  document.querySelector('.selection-summary-grid').hidden = true;
  const nativeHost = document.createElement('div');
  nativeHost.id = 'station-native-console';
  nativeHost.hidden = true;
  controlsCard.before(nativeHost);
  const mapCard = byId('satellite-map').closest('.card');
  mapCard.classList.add('station-map-card');
  mapCard.classList.remove('h-100');
  byId('satellite-map').width = 1400;
  byId('satellite-map').height = 280;
  document.querySelector('.home-dashboard-row').prepend(mapCard);
  document.querySelector('.home-tracking-section').hidden = true;
  document.querySelector('.home-control-section').classList.remove('col-xl-8');
  byId('station-location').append(byId('station'));
  const moduleToggle = byId('station-modules-toggle');
  moduleToggle.addEventListener('click', () => {
    const expanded = moduleToggle.getAttribute('aria-expanded') !== 'true';
    moduleToggle.setAttribute('aria-expanded', String(expanded));
    byId('station-module-menu').hidden = !expanded;
  });
  const passToggle = byId('station-sidebar-toggle');
  const passContent = byId('station-pass-content');
  const mobilePassLayout = window.matchMedia('(max-width: 740px)');
  let desktopPassesOpen = true;
  let mobilePassesOpen = false;
  function syncPassSidebarLayout() {
    const mobile = mobilePassLayout.matches;
    const focusedElement = passSidebar.contains(document.activeElement) ? document.activeElement : null;
    if (mobile) shell.insertBefore(passSidebar, stationMain);
    else stationMain.after(passSidebar);

    const expanded = mobile ? mobilePassesOpen : desktopPassesOpen;
    shell.classList.toggle('passes-mobile', mobile);
    shell.classList.toggle('passes-open', expanded);
    shell.classList.toggle('passes-collapsed', !expanded);
    passToggle.setAttribute('aria-expanded', String(expanded));
    passContent.hidden = !expanded;
    if (focusedElement && !expanded) passToggle.focus({ preventScroll: true });
    else if (focusedElement && document.activeElement !== focusedElement) focusedElement.focus({ preventScroll: true });
  }
  passToggle.addEventListener('click', () => {
    if (mobilePassLayout.matches) mobilePassesOpen = !mobilePassesOpen;
    else desktopPassesOpen = !desktopPassesOpen;
    syncPassSidebarLayout();
    window.dispatchEvent(new Event('resize'));
  });
  if (typeof mobilePassLayout.addEventListener === 'function') mobilePassLayout.addEventListener('change', syncPassSidebarLayout);
  else mobilePassLayout.addListener(syncPassSidebarLayout);
  syncPassSidebarLayout();
  byId('station-radio-power').addEventListener('click', () => byId('rc-power')?.click());

  let nativeEnabled = false;
  let configured = null;
  let nativeLoading = null;
  const nativePages = new Set(['radio', 'modules', 'aprs', 'ft4-ft8', 'audio-stream', 'sstv-decoder']);
  function renderControlState() {
    for (const control of ['rx', 'tx', 'rotator']) {
      const enabled = byId(`${control}-control-toggle`).checked;
      byId(`station-${control}-status`).textContent = enabled ? 'Enabled' : 'Off';
    }
  }
  function moveRadioConsole(page) {
    const consoleRoot = byId('radio-console');
    if (!consoleRoot) return;
    const host = page === 'home' && nativeEnabled ? nativeHost : byId('radio-page');
    if (consoleRoot.parentElement !== host) host.append(consoleRoot);
  }
  async function loadNativeScripts() {
    for (const template of document.querySelectorAll('script[data-native-src]')) {
      const src = template.dataset.nativeSrc;
      if (document.querySelector(`script[data-native-loaded="${src}"]`)) continue;
      await new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = src;
        script.dataset.nativeLoaded = src;
        script.onload = resolve;
        script.onerror = () => { script.remove(); reject(new Error('Unable to load the native interface. Reload to retry.')); };
        document.head.append(script);
      });
    }
    moveRadioConsole(typeof pageFromHash === 'function' ? pageFromHash() : 'home');
  }
  function applyMode(enabled) {
    if (typeof enabled !== 'boolean') return;
    if (configured !== null && configured !== enabled) {
      // Reload ends the old UI session before a different configured interface starts.
      window.location.reload();
      return;
    }
    configured = enabled;
    nativeEnabled = enabled;
    document.body.dataset.experience = enabled ? 'native' : 'legacy';
    document.querySelectorAll('[data-native-only]').forEach(node => { node.hidden = !enabled; });
    byId('station-experience').textContent = enabled ? 'Native IC-9700' : 'Legacy / Dual radio';
    nativeHost.hidden = !enabled;
    controlsCard.hidden = enabled;
    mapCard.hidden = enabled;
    renderControlState();
    if (enabled && !nativeLoading) {
      nativeLoading = loadNativeScripts().then(() => {
        if (typeof globalThis.showPage === 'function') globalThis.showPage(globalThis.pageFromHash());
      }).catch(error => {
        byId('station-radio-status').textContent = error.message;
        nativeLoading = null;
      });
    }
    document.querySelectorAll('#monitor-page .monitor-sstv-downloads').forEach(node => { node.hidden = !enabled; });
    if (typeof globalThis.showPage === 'function' && !enabled && nativePages.has(globalThis.pageFromHash())) globalThis.showPage('home');
  }
  function showPage(page) {
    const home = page === 'home';
    document.body.dataset.interfacePage = page;
    shell.classList.toggle('show-pass-sidebar', home);
    moveRadioConsole(page);
    if (nativePages.has(page) && page !== 'radio') {
      moduleToggle.setAttribute('aria-expanded', 'true');
      byId('station-module-menu').hidden = false;
    }
    document.querySelectorAll('.station-navigation [data-page]').forEach(button => {
      button.classList.toggle('active', button.dataset.page === page);
    });
    byId('station-module-menu').querySelectorAll('[data-module]').forEach(button => {
      const active = button.dataset.module === page;
      button.classList.toggle('active', active);
      button.setAttribute('aria-current', active ? 'page' : 'false');
    });
  }
  function renderTracking(tracking) {
    byId('pass-satellite-name').textContent = tracking?.satellite_name || 'No satellite selected';
    byId('pass-profile-name').textContent = tracking?.transponder_name || '';
  }
  function renderRadio(state) {
    const button = byId('station-radio-power');
    const connecting = state.connecting === true;
    const connected = state.connected === true;
    button.disabled = state.enabled !== true;
    button.setAttribute('aria-pressed', String(connected || connecting));
    button.setAttribute('aria-label', connected || connecting ? 'Disconnect radio' : 'Connect radio');
    byId('station-radio-status').textContent = connecting ? 'Connecting' : connected ? 'Connected' : state.last_error ? 'Connection error' : 'Disconnected';
  }
  function renderRotator(state) {
    for (const [axis, field] of [['az', 'current_azimuth_deg'], ['el', 'current_elevation_deg']]) {
      byId(`station-rotator-${axis}`).textContent = Number.isFinite(state[field]) ? `${state[field].toFixed(1)}°` : '—';
    }
  }
  window.PiSatInterface = { applyMode, showPage, renderTracking, renderRadio, renderRotator, renderControlState,
    isAvailable: page => !nativePages.has(page) || nativeEnabled,
    get nativeEnabled() { return nativeEnabled; },
  };
  document.body.dataset.experience = 'legacy';
})();
