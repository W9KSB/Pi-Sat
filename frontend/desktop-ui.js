/* Desktop presentation reuses the application's controls and shared radio session. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const workspace = $('desktop-workspace');
  const consoleRoot = $('radio-console');
  if (!workspace || !consoleRoot) return;
  const storage = {
    get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* Preferences remain usable without storage. */ } },
  };
  const move = (node, host) => { if (node && host) host.appendChild(node); };
  move(consoleRoot, $('desktop-console-host'));
  move($('pass-arc-map'), $('desktop-pass-chart'));
  new ResizeObserver(entries => {
    const { width, height } = entries[0].contentRect;
    const canvas = $('pass-arc-map');
    if (width > 0 && height > 0) {
      canvas.width = Math.round(width);
      canvas.height = Math.round(height);
      drawPassArc();
    }
  }).observe($('pass-arc-map'));
  move($('selected-satellite'), $('desktop-satellite-name'));
  const profile = $('frequency-profile-select')?.closest('.profile-select-box');
  if (profile) profile.querySelector('label').textContent = 'Transponder';
  move(profile, $('desktop-transponder-host'));
  const passes = document.querySelector('.pass-panel');
  const passMinimum = $('pass-min-elevation-label');
  if (passes) {
    passes.querySelector('h3').textContent = 'NEXT PASSES';
    passes.querySelector('.pass-list-header').innerHTML = '<span>Satellite</span><span>AOS (local)</span><span>Max EL</span>';
    passes.querySelector('.form-check-label').textContent = 'Enable Autotrack';
    move(passes, $('desktop-pass-list-host'));
  }
  const doppler = $('desktop-doppler-settings');
  move(document.querySelector('.control-panel-body'), doppler);
  move(document.querySelector('.home-rotator-row'), doppler);
  const advanced = $('rc-config-advanced');
  if (advanced) {
    const station = document.createElement('details');
    station.className = 'desktop-station-settings';
    const summary = document.createElement('summary');
    summary.textContent = 'Station controls and tracking map';
    station.appendChild(summary);
    move(passMinimum, station);
    move(document.querySelector('.home-header-controls'), station);
    move($('station'), station);
    move(document.querySelector('.map-wrap'), station);
    move(document.querySelector('.track-filter'), station);
    advanced.appendChild(station);
  }

  function setCollapsed(collapsed) {
    workspace.classList.toggle('passes-collapsed', collapsed);
    $('desktop-pass-content').hidden = collapsed;
    $('desktop-pass-toggle').setAttribute('aria-expanded', String(!collapsed));
    $('desktop-pass-toggle').setAttribute('aria-label', collapsed ? 'Open pass settings' : 'Close pass settings');
    storage.set('pi-sat.desktop.passes-collapsed', String(collapsed));
    window.dispatchEvent(new Event('resize'));
  }
  $('desktop-pass-toggle').addEventListener('click', () => setCollapsed(!workspace.classList.contains('passes-collapsed')));
  setCollapsed(storage.get('pi-sat.desktop.passes-collapsed') === 'true');

  const preferences = [
    { id: 'ui-scale', label: 'UI scale', type: 'range', min: 75, max: 150, initial: '100', suffix: '%' },
    { id: 'text-scale', label: 'Text scale', type: 'range', min: 85, max: 150, initial: '100', suffix: '%' },
    { id: 'density', label: 'Density', options: ['Compact', 'Comfortable', 'Spacious'], initial: 'Comfortable' },
    { id: 'frequency-size', label: 'Frequency readout size', options: ['Normal', 'Large', 'Extra Large'], initial: 'Normal' },
  ];
  const display = $('desktop-display-settings');
  function applyPreference(preference, value) {
    const property = preference.id;
    if (preference.type === 'range') {
      const number = Number(value);
      value = String(Number.isFinite(number) && number >= preference.min && number <= preference.max ? number : preference.initial);
      document.documentElement.style.setProperty(`--${property}`, Number(value) / 100);
    } else {
      value = preference.options.includes(value) ? value : preference.initial;
      if (property === 'density') document.documentElement.style.setProperty('--density-space', { Compact: '.75', Comfortable: '1', Spacious: '1.25' }[value]);
      if (property === 'frequency-size') document.documentElement.style.setProperty('--frequency-size', { Normal: '1', Large: '1.15', 'Extra Large': '1.3' }[value]);
    }
    storage.set(`pi-sat.desktop.${property}`, value);
    return value;
  }
  if (display) preferences.forEach(preference => {
    const label = document.createElement('label');
    label.className = 'desktop-appearance-setting';
    const title = document.createElement('span');
    title.textContent = preference.label;
    const input = document.createElement(preference.options ? 'select' : 'input');
    input.id = `desktop-${preference.id}`;
    if (preference.options) preference.options.forEach(value => input.add(new Option(value, value)));
    else { input.type = preference.type; input.min = preference.min; input.max = preference.max; input.step = 1; }
    const output = document.createElement('output');
    output.htmlFor = input.id;
    input.value = applyPreference(preference, storage.get(`pi-sat.desktop.${preference.id}`) || preference.initial);
    output.textContent = input.value + (preference.suffix || '');
    input.addEventListener('input', () => {
      input.value = applyPreference(preference, input.value);
      output.textContent = input.value + (preference.suffix || '');
      window.dispatchEvent(new Event('resize'));
    });
    label.append(title, input, output);
    display.appendChild(label);
  });

  function showPage(page) {
    const consolePage = page === 'home' || page === 'radio';
    document.body.classList.toggle('desktop-console-page', consolePage);
    if (consolePage) move(workspace, $(page === 'radio' ? 'radio-page' : 'home-page'));
    window.dispatchEvent(new Event('resize'));
  }
  function text(id, value) {
    const node = $(id);
    if (node && node.textContent !== value) node.textContent = value;
  }
  const read = id => $(id)?.textContent?.trim() || '—';
  function refreshStatus() {
    const now = new Date();
    const summary = getRadioTrackingSummary();
    const pass = getActiveOrNextPass();
    text('desktop-tracked-name', summary.name === 'None' ? 'No satellite selected' : summary.name);
    text('desktop-norad', String(selectedSatelliteNorad || latestTracking?.norad_id || '—'));
    if (pass) {
      text('desktop-pass-times', `AOS ${formatTimeOnly(new Date(pass.aos_utc), false)} · LOS ${formatTimeOnly(new Date(pass.los_utc), false)} · Max EL ${Number(pass.max_elevation_deg).toFixed(0)}°`);
      const active = now.getTime() >= Date.parse(pass.aos_utc) && now.getTime() <= Date.parse(pass.los_utc);
      const target = Date.parse(active ? pass.los_utc : pass.aos_utc);
      text('desktop-pass-countdown', `${active ? 'LOS' : 'AOS'} in ${formatCountdown(Math.max(0, (target - now.getTime()) / 1000))}`);
    } else {
      text('desktop-pass-times', 'AOS — · LOS — · Max EL —');
      text('desktop-pass-countdown', 'No upcoming pass');
    }
    text('desktop-rotator-connection', read('rotator-connected') === 'Yes' ? '●' : '○');
    $('desktop-rotator-connection').classList.toggle('connected', read('rotator-connected') === 'Yes');
    $('desktop-rotator-position').title = read('rotator-state-label');
    text('desktop-rotator-position', `AZ ${read('rotator-current-az')} / EL ${read('rotator-current-el')}`);
    text('desktop-rotator-target', `Target ${read('rotator-target-az')} / ${read('rotator-target-el')}`);
    text('desktop-radio-connection', read('rc-connection'));
    $('desktop-radio-connection').classList.toggle('connected', $('rc-connection')?.classList.contains('is-live'));
    text('desktop-main-frequency', read('rc-MAIN-frequency'));
    text('desktop-sub-frequency', read('rc-SUB-frequency'));
    $('desktop-clock').dateTime = now.toISOString();
    $('desktop-clock').replaceChildren(document.createTextNode(now.toLocaleTimeString()), Object.assign(document.createElement('small'), { textContent: now.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }) }));
    document.querySelectorAll('#pass-list .pass-item').forEach(button => {
      const selected = Number(button.dataset.noradId) === Number(selectedSatelliteNorad);
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    drawPassArc();
  }
  window.DesktopUI = { showPage };
  showPage(activePrimaryPageName);
  refreshStatus();
  setInterval(refreshStatus, 1000);
})();
