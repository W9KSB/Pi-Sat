let currentRxFrequencyHz = null;
let latestTracking = null;
let selectedSatelliteNorad = null;
let selectedFrequencyProfileIndex = 0;
let selectedManagedSatelliteNorad = null;
let latestPasses = [];
let satellitesCache = [];
let managedSatellitesCache = [];
let managedSatelliteProfilesByNorad = new Map();
let managedSatellitePassesByNorad = new Map();
let trackedSatelliteNorads = new Set();
let activeAutotrackPassKey = null;
let syncRxTx = true;
let frontendLogMessages = [];
let backendLogMessages = [];
let hamlibRadioModels = [];
let hamlibRotatorModels = [];
let serialDevices = [];
let qthTimezone = 'UTC';
let rotatorControlEnabled = false;
let mapRefreshPending = false;
let mapRefreshRequestedAtMs = 0;
let syncToggleUpdatePending = false;
let trackedSatelliteLocations = [];
let stationLatitudeDeg = null;
let stationLongitudeDeg = null;
let groundTrackPoints = [];
let groundTrackNoradId = null;
let groundTrackFetchedAtMs = 0;
let qsoFinderResult = null;
let qsoOpportunities = [];
let selectedQsoOpportunityIndex = -1;
const MAP_REFRESH_TIMEOUT_MS = 5000;
const hiddenSettingsKeys = {
  server: new Set(['host', 'port', 'gui_resources_caching']),
  my_satellites: new Set(['autotrack_next_pass']),
  rx: new Set(['cat_debug_logging']),
  tx: new Set(['cat_debug_logging']),
  rotator: new Set(['home_azimuth_deg', 'home_elevation_deg', 'cat_debug_logging']),
  tle: new Set(['cache_dir']),
  profiles: new Set(['satellites_file']),
};
const lastLoggedErrors = {
  tracking: '',
  rotator: '',
  rx: '',
  tx: '',
};
const worldMapImage = new Image();
worldMapImage.src = '/assets/world-map-equirectangular.png';

function addLog(message) {
  if (!message) {
    return;
  }
  if (frontendLogMessages[0]?.message === message) {
    return;
  }
  frontendLogMessages.unshift({
    timestampMs: Date.now(),
    message,
    level: 'INFO',
    source: 'ui',
  });
  frontendLogMessages = frontendLogMessages.slice(0, 100);
  renderLogs();
}

function logErrorState(source, message) {
  const normalized = String(message || '').trim();
  const previous = lastLoggedErrors[source] || '';
  if (!normalized) {
    lastLoggedErrors[source] = '';
    return;
  }
  if (previous === normalized) {
    return;
  }
  lastLoggedErrors[source] = normalized;
  addLog(normalized);
}

function renderLogs() {
  const list = document.getElementById('log-list');
  if (!list) {
    return;
  }
  list.replaceChildren();
  const entries = [...backendLogMessages, ...frontendLogMessages]
    .sort((left, right) => (right.timestampMs || 0) - (left.timestampMs || 0))
    .slice(0, 100);
  entries.forEach((entry) => {
    const item = document.createElement('div');
    item.className = 'log-item';
    const time = document.createElement('span');
    time.className = 'log-time';
    time.textContent = formatLogTime(entry.timestampMs);
    const message = document.createElement('span');
    message.textContent = formatLogMessage(entry);
    item.append(time, message);
    list.appendChild(item);
  });
}

function formatLogTime(timestampMs) {
  if (!timestampMs) {
    return '--:--:--';
  }
  return new Date(timestampMs).toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    timeZone: qthTimezone,
  });
}

function formatLogMessage(entry) {
  const level = entry.level ? `[${entry.level}] ` : '';
  return `${level}${entry.message || ''}`;
}

async function refreshMonitorLogs() {
  try {
    const response = await fetch('/api/monitor/logs');
    const result = await response.json();
    backendLogMessages = Array.isArray(result.entries)
      ? result.entries.map((entry) => ({
        timestampMs: Number(entry.timestamp_ms) || 0,
        message: entry.message || '',
        level: entry.level || '',
        source: entry.source || 'backend',
      }))
      : [];
    renderLogs();
  } catch (error) {
    // Keep the last successful log snapshot if monitor refresh fails.
  }
}

async function loadStatus() {
  const response = await fetch('/api/status');
  const status = await response.json();

  document.getElementById('station').textContent =
    `${status.station.name}: ${status.station.latitude_deg}, ${status.station.longitude_deg}`;
  stationLatitudeDeg = Number(status.station.latitude_deg);
  stationLongitudeDeg = Number(status.station.longitude_deg);
  qthTimezone = status.station.timezone || 'UTC';
  const rxToggle = document.getElementById('rx-control-toggle');
  const txToggle = document.getElementById('tx-control-toggle');
  const rotatorToggle = document.getElementById('rotator-control-toggle');
  if (rxToggle) {
    rxToggle.checked = Boolean(status.devices.rx_enabled);
  }
  if (txToggle) {
    txToggle.checked = Boolean(status.devices.tx_enabled);
  }
  if (rotatorToggle) {
    rotatorToggle.checked = Boolean(status.devices.rotator_enabled);
    rotatorControlEnabled = rotatorToggle.checked;
  }
}

function showPage(pageName) {
  const selectedPage = ['home', 'satellites', 'qso-finder', 'monitor', 'map', 'settings'].includes(pageName)
    ? pageName
    : 'home';
  document.querySelectorAll('[data-page-view]').forEach((view) => {
    view.hidden = view.dataset.pageView !== selectedPage;
  });
  document.querySelectorAll('[data-page]').forEach((button) => {
    button.classList.toggle('active', button.dataset.page === selectedPage);
  });
  if (selectedPage === 'settings') {
    loadSettings();
  } else if (selectedPage === 'satellites') {
    loadMySatellites();
  } else if (selectedPage === 'monitor') {
    loadMonitor();
  } else if (selectedPage === 'qso-finder') {
    loadQsoFinderPage();
  } else if (selectedPage === 'map') {
    loadTrackedSatelliteLocations();
    drawTrackedSatellitesMap();
  } else {
    drawMap();
  }
}

function pageFromHash() {
  const page = window.location.hash.replace('#', '');
  return ['home', 'satellites', 'qso-finder', 'monitor', 'map', 'settings'].includes(page) ? page : 'home';
}

async function loadSatellites() {
  const response = await fetch('/api/satellites');
  satellitesCache = await response.json();
  const profileSelect = document.getElementById('frequency-profile-select');

  function updateSelectedFrequencyProfile() {
    const satellite = getSelectedSatellite();
    if (!satellite) {
      return;
    }
    selectedFrequencyProfileIndex = Number(profileSelect.value) || 0;
    const profile = satellite.frequency_profiles[selectedFrequencyProfileIndex];
    renderFrequencyProfileDetails(profile);
  }

  profileSelect.onchange = () => {
    updateSelectedFrequencyProfile();
    syncTrackingForSelection();
  };

  renderTrackFilter();
  renderQsoSatelliteOptions();
  if (selectedSatelliteNorad) {
    await selectSatelliteByNorad(selectedSatelliteNorad);
  } else if (latestTracking?.norad_id) {
    restoreSelectionFromTracking();
  }
}

async function loadSdrFrequency() {
  const frequencyElement = document.getElementById('rx-frequency');

  try {
    const response = await fetch('/api/devices/sdr/frequency');
    const result = await response.json();

    if (!result.connected) {
      frequencyElement.textContent = result.error || 'Not connected';
      return;
    }

    frequencyElement.textContent =
      `${Number(result.frequency_hz).toLocaleString()} Hz`;
    currentRxFrequencyHz = Number(result.frequency_hz);
  } catch (error) {
    frequencyElement.textContent = 'Read failed';
  }
}

async function stepSdrFrequency(event) {
  const stepKhz = Number(event.currentTarget.dataset.rxStepKhz);
  await stepTrackingOffset('rx', stepKhz * 1000);
}

async function stepTxFrequency(event) {
  const stepKhz = Number(event.currentTarget.dataset.txStepKhz);
  await stepTrackingOffset('tx', stepKhz * 1000);
}

async function stepTrackingOffset(role, stepHz) {
  try {
    const response = await fetch(`/api/tracking/${role}/step`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        step_hz: stepHz,
        norad_id: selectedSatelliteNorad,
        frequency_profile_index: selectedFrequencyProfileIndex,
        sync_offsets: syncRxTx,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      addLog(result.detail || 'Offset step failed.');
      return;
    }
    renderTracking(result);
  } catch (error) {
    addLog('Offset step failed.');
  }
}

function getSelectedFrequencyProfile() {
  const satellite = getSelectedSatellite();
  return satellite?.frequency_profiles?.[selectedFrequencyProfileIndex] || null;
}

async function updateSyncMode(event) {
  const requestedValue = event.currentTarget.checked;
  syncRxTx = requestedValue;
  syncToggleUpdatePending = true;
  addLog(syncRxTx ? 'RX/TX sync enabled.' : 'RX/TX sync disabled.');
  try {
    const response = await fetch('/api/tracking/offset-sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        enabled: requestedValue,
        norad_id: selectedSatelliteNorad,
        frequency_profile_index: selectedFrequencyProfileIndex,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.detail || 'Sync mode update failed.');
    }
    syncToggleUpdatePending = false;
    renderTracking(result);
  } catch (error) {
    syncToggleUpdatePending = false;
    syncRxTx = !requestedValue;
    event.currentTarget.checked = syncRxTx;
    addLog('Sync mode update failed.');
  }
}

async function postTrackingAction(path, statusText, options = {}) {
  addLog(statusText);

  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: path.endsWith('/start') || path.endsWith('/reset-offset')
        ? JSON.stringify({
          norad_id: selectedSatelliteNorad,
          frequency_profile_index: selectedFrequencyProfileIndex,
          sync_offsets: syncRxTx,
        })
        : '{}',
    });
    const result = await response.json();
    if (!response.ok) {
      addLog(result.detail || 'Tracking command failed.');
      return;
    }
    if (!options.suppressRender) {
      renderTracking(result);
    }
    addLog(result.error || '');
    return result;
  } catch (error) {
    addLog('Tracking command failed.');
    return null;
  }
}

function syncTrackingForSelection(options = {}) {
  if (!selectedSatelliteNorad) {
    return Promise.resolve();
  }
  return postTrackingAction(
    '/api/tracking/rx/start',
    'Updating tracked satellite...',
    options
  );
}

async function loadPasses() {
  const list = document.getElementById('pass-list');
  try {
    const selectedNorads = Array.from(trackedSatelliteNorads);
    if (!selectedNorads.length) {
      latestPasses = [];
      activeAutotrackPassKey = null;
      renderPasses(latestPasses);
      return;
    }
    const query = selectedNorads.length
      ? `?norad_ids=${encodeURIComponent(selectedNorads.join(','))}`
      : '';
    const response = await fetch(`/api/passes/next${query}`);
    latestPasses = await response.json();
    qthTimezone = latestPasses[0]?.timezone || qthTimezone;
    renderPasses(latestPasses);
    if (!selectedSatelliteNorad && !latestTracking?.norad_id && latestPasses.length) {
      await selectSatelliteByNorad(latestPasses[0].norad_id);
    }
  } catch (error) {
    list.textContent = 'Pass prediction failed.';
    addLog('Pass prediction failed.');
  }
}

function renderPasses(passes) {
  const list = document.getElementById('pass-list');
  list.replaceChildren();
  if (!passes.length) {
    list.textContent = 'No upcoming passes found.';
    return;
  }

  passes.forEach((satellitePass) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'pass-item btn';
    button.dataset.noradId = satellitePass.norad_id;
    button.dataset.aosUtc = satellitePass.aos_utc;
    const aos = new Date(satellitePass.aos_utc);
    const los = new Date(satellitePass.los_utc);
    button.innerHTML = `
      <span>${escapeHtml(satellitePass.satellite_name)}</span>
      <span>${formatLocalDateOnly(aos)}</span>
      <span>${formatLocalTimeRange(aos, los)}</span>
      <span>${formatAzimuthPath(satellitePass)}</span>
      <span>${Number(satellitePass.max_elevation_deg).toFixed(1)}&deg;</span>
    `;
    button.addEventListener('click', () => selectPass(satellitePass, { source: 'manual' }));
    list.appendChild(button);
  });
}

async function selectPass(satellitePass, options = {}) {
  if (options.source === 'manual') {
    const autotrackDisabled = await setAutotrackEnabled(false, {
      persist: true,
      logOnDisable: true,
    });
    if (!autotrackDisabled) {
      return;
    }
  }
  beginMapRefresh(satellitePass.norad_id, satellitePass.satellite_name);
  drawMap();
  await selectSatelliteByNorad(satellitePass.norad_id, { suppressRender: true });
  addLog(`${satellitePass.satellite_name} pass loaded.`);
  await loadTracking(true);
  drawMap();
}

function renderTrackFilter() {
  const filter = document.getElementById('track-filter-options');
  if (!filter) {
    return;
  }
  if (!trackedSatelliteNorads.size) {
    trackedSatelliteNorads = new Set(
      satellitesCache.map((satellite) => Number(satellite.norad_id))
    );
  }
  filter.replaceChildren();
  satellitesCache.forEach((satellite) => {
    const label = document.createElement('label');
    label.className = 'form-check';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'form-check-input';
    checkbox.checked = trackedSatelliteNorads.has(Number(satellite.norad_id));
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) {
        trackedSatelliteNorads.add(Number(satellite.norad_id));
      } else {
        trackedSatelliteNorads.delete(Number(satellite.norad_id));
      }
      selectedSatelliteNorad = trackedSatelliteNorads.has(Number(selectedSatelliteNorad))
        ? selectedSatelliteNorad
        : null;
      loadPasses();
    });
    const text = document.createElement('span');
    text.className = 'form-check-label';
    text.textContent = `${satellite.name} (${satellite.norad_id})`;
    label.append(checkbox, text);
    filter.appendChild(label);
  });
}

async function selectSatelliteByNorad(noradId, options = {}) {
  const satellite = satellitesCache.find((item) => {
    return Number(item.norad_id) === Number(noradId);
  });
  if (!satellite) {
    return;
  }
  beginMapRefresh(satellite.norad_id, satellite.name);
  selectedSatelliteNorad = satellite.norad_id;
  selectedFrequencyProfileIndex = 0;
  document.getElementById('selected-satellite').textContent = satellite.name;
  document.getElementById('selected-satellite-azimuth').textContent = 'Az --';
  document.getElementById('selected-satellite-elevation').textContent = 'El --';
  renderFrequencyProfileOptions(satellite);
  const result = await syncTrackingForSelection(options);
  if (
    options.suppressRender
    && Number(satellite.norad_id) === Number(result?.norad_id)
    && hasValidCoordinate(result?.latitude_deg)
    && hasValidCoordinate(result?.longitude_deg)
  ) {
    renderTracking(result);
  }
}

function restoreSelectionFromTracking() {
  const satellite = satellitesCache.find((item) => {
    return Number(item.norad_id) === Number(latestTracking.norad_id);
  });
  if (!satellite) {
    return;
  }
  const profileIndex = satellite.frequency_profiles.findIndex((profile) => {
    return profile.name === latestTracking.transponder_name;
  });
  selectedSatelliteNorad = satellite.norad_id;
  selectedFrequencyProfileIndex = profileIndex >= 0 ? profileIndex : 0;
  document.getElementById('selected-satellite').textContent = satellite.name;
  document.getElementById('selected-satellite-azimuth').textContent = 'Az --';
  document.getElementById('selected-satellite-elevation').textContent = 'El --';
  renderFrequencyProfileOptions(satellite);
}

function renderFrequencyProfileOptions(satellite) {
  const profileSelect = document.getElementById('frequency-profile-select');
  profileSelect.replaceChildren();
  const profiles = satellite.frequency_profiles.length
    ? satellite.frequency_profiles
    : [{ name: 'Tracking only - no frequency profile' }];
  profiles.forEach((profile, index) => {
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = profile.name;
    profileSelect.appendChild(option);
  });
  profileSelect.value = String(selectedFrequencyProfileIndex);
  const profile = satellite.frequency_profiles[selectedFrequencyProfileIndex];
  renderFrequencyProfileDetails(profile);
  updateTxProfileState(profile);
}

function getSelectedSatellite() {
  return satellitesCache.find((satellite) => {
    return Number(satellite.norad_id) === Number(selectedSatelliteNorad);
  });
}

function renderFrequencyProfileDetails(profile) {
  const details = document.getElementById('frequency-profile-details');
  if (!profile) {
    details.textContent = 'No frequency profile selected.';
    const profileBadge = document.getElementById('profile-direction-badge');
    if (profileBadge) {
      profileBadge.className = 'badge text-bg-secondary';
      profileBadge.textContent = '--';
    }
    updateTxProfileState(null);
    return;
  }

  const rxRange = formatFrequencyRange(
    profile.downlink_low,
    profile.downlink_high
  );
  const rx = `${formatHz(profile.preferred_downlink)} ${profile.downlink_mode || ''}`.trim();
  const profileBadge = document.getElementById('profile-direction-badge');
  if (isRxOnlyProfile(profile)) {
    details.innerHTML = `
      <strong>${escapeHtml(profile.name)}</strong>
      <span class="badge text-bg-primary ms-2">RX-only</span>
      <div class="small mt-1">RX ${escapeHtml(rx)} <span class="text-body-secondary">(${escapeHtml(rxRange)})</span></div>
    `;
    if (profileBadge) {
      profileBadge.className = 'badge text-bg-primary';
      profileBadge.textContent = 'RX-only';
    }
  } else {
    const txRange = formatFrequencyRange(
      profile.uplink_low,
      profile.uplink_high
    );
    const tx = `${formatHz(profile.preferred_uplink)} ${profile.uplink_mode || ''}`.trim();
    const polarity = profile.inverted ? 'inverted' : 'non-inverted';
    details.innerHTML = `
      <strong>${escapeHtml(profile.name)}</strong>
      <span class="badge text-bg-success ms-2">RX/TX</span>
      <span class="badge text-bg-secondary ms-1">${polarity}</span>
      <div class="small mt-1">RX ${escapeHtml(rx)} <span class="text-body-secondary">(${escapeHtml(rxRange)})</span> | TX ${escapeHtml(tx)} <span class="text-body-secondary">(${escapeHtml(txRange)})</span></div>
    `;
    if (profileBadge) {
      profileBadge.className = 'badge text-bg-success';
      profileBadge.textContent = profile.inverted ? 'RX/TX inverted' : 'RX/TX';
    }
  }
  updateTxProfileState(profile);
}

function isRxOnlyProfile(profile) {
  return !profile
    || profile.type === 'rx_only'
    || !Number(profile.preferred_uplink);
}

function updateTxProfileState(profile) {
  const rxOnly = isRxOnlyProfile(profile);
  const txPanel = document.getElementById('tx-link-panel');
  const syncToggle = document.getElementById('sync-rx-tx-toggle');
  const syncLabel = syncToggle?.closest('label');
  if (txPanel) {
    txPanel.classList.toggle('link-panel-disabled', rxOnly);
  }
  const txBadge = document.getElementById('tx-profile-badge');
  if (txBadge) {
    txBadge.className = rxOnly ? 'badge text-bg-secondary' : 'badge text-bg-success';
    txBadge.textContent = rxOnly ? 'Disabled (RX-only)' : 'Active';
  }
  document.querySelectorAll('[data-tx-step-khz]').forEach((button) => {
    button.disabled = rxOnly;
  });
  if (syncToggle) {
    syncToggle.disabled = rxOnly;
    if (rxOnly) {
      syncToggle.checked = false;
    } else {
      syncToggle.checked = syncRxTx;
    }
  }
  if (syncLabel) {
    syncLabel.classList.toggle('control-disabled', rxOnly);
  }
}

function checkAutotrackNextPass() {
  const autoTrackToggle = document.getElementById('auto-track-toggle');
  if (autoTrackToggle && !autoTrackToggle.checked) {
    return;
  }
  if (!latestPasses.length) {
    return;
  }
  const nextPass = latestPasses[0];
  const now = Date.now();
  const aos = new Date(nextPass.aos_utc).getTime();
  const los = new Date(nextPass.los_utc).getTime();
  const passKey = `${nextPass.norad_id}:${nextPass.aos_utc}`;
  if (now > los) {
    activeAutotrackPassKey = null;
  }
  if (activeAutotrackPassKey === passKey) {
    return;
  }
  activeAutotrackPassKey = passKey;
  selectPass(nextPass, { source: 'auto' });
}

async function persistAutotrackSetting(enabled) {
  if (!enabled) {
    activeAutotrackPassKey = null;
  }
  const response = await fetch('/api/my-satellites/options', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      autotrack_next_pass: enabled,
    }),
  });
  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.detail || 'Autotrack update failed.');
  }
}

async function setAutotrackEnabled(enabled, options = {}) {
  const { persist = true, logOnDisable = false, logOnEnable = false } = options;
  const autoTrackToggle = document.getElementById('auto-track-toggle');
  const previous = autoTrackToggle ? autoTrackToggle.checked : enabled;
  if (autoTrackToggle) {
    autoTrackToggle.checked = enabled;
  }
  if (!persist) {
    return true;
  }
  try {
    await persistAutotrackSetting(enabled);
    if (enabled) {
      if (logOnEnable) {
        addLog('Autotrack enabled.');
      }
      if (latestPasses.length) {
        activeAutotrackPassKey = null;
        await selectPass(latestPasses[0], { source: 'auto' });
      }
      checkAutotrackNextPass();
    } else if (logOnDisable) {
      addLog('Autotrack disabled.');
    }
    return true;
  } catch (error) {
    if (autoTrackToggle) {
      autoTrackToggle.checked = previous;
    }
    addLog(error.message || 'Autotrack update failed.');
    return false;
  }
}

async function updateAutotrackSetting(event) {
  await setAutotrackEnabled(event.currentTarget.checked, {
    persist: true,
    logOnDisable: true,
    logOnEnable: true,
  });
}

async function loadTracking(force = false) {
  if (mapRefreshPending && !force) {
    if (Date.now() - mapRefreshRequestedAtMs < MAP_REFRESH_TIMEOUT_MS) {
      return;
    }
    mapRefreshPending = false;
  }
  try {
    const response = await fetch('/api/tracking/rx');
    const result = await response.json();
    renderTracking(result);
    maybeRefreshGroundTrack(result);
  } catch (error) {
    addLog('Tracking read failed.');
  }
}

async function loadRotator() {
  if (!rotatorControlEnabled) {
    renderRotator({
      connected: false,
      pass_active: false,
      manual_controls_enabled: false,
      state_label: 'Rotator control is off',
      current_azimuth_deg: null,
      current_elevation_deg: null,
      target_azimuth_deg: null,
      target_elevation_deg: null,
      error: '',
    });
    return;
  }
  try {
    const response = await fetch('/api/devices/rotator');
    const result = await response.json();
    renderRotator(result);
  } catch (error) {
    addLog('Rotator read failed.');
  }
}

async function loadTrackedSatelliteLocations() {
  const mapUpdate = document.getElementById('tracked-map-update');
  try {
    const selectedNorads = Array.from(trackedSatelliteNorads);
    const query = selectedNorads.length
      ? `?norad_ids=${encodeURIComponent(selectedNorads.join(','))}`
      : '';
    const response = await fetch(`/api/tracked-satellites/positions${query}`);
    const result = await response.json();
    trackedSatelliteLocations = result.positions || [];
    if (result.timezone) {
      qthTimezone = result.timezone;
    }
    mapUpdate.textContent = formatClockForQth(new Date());
    drawTrackedSatellitesMap();
  } catch (error) {
    trackedSatelliteLocations = [];
    mapUpdate.textContent = '--';
    addLog('Tracked satellite map update failed.');
    drawTrackedSatellitesMap();
  }
}

function renderQsoSatelliteOptions() {
  const select = document.getElementById('qso-satellite-filter');
  if (!select) {
    return;
  }
  const currentValue = select.value || 'all';
  select.replaceChildren();

  const allOption = document.createElement('option');
  allOption.value = 'all';
  allOption.textContent = 'All Tracked Satellites';
  select.appendChild(allOption);

  satellitesCache.forEach((satellite) => {
    const option = document.createElement('option');
    option.value = String(satellite.norad_id);
    option.textContent = satellite.name;
    select.appendChild(option);
  });

  const validValues = new Set(['all', ...satellitesCache.map((satellite) => String(satellite.norad_id))]);
  select.value = validValues.has(currentValue) ? currentValue : 'all';
}

function loadQsoFinderPage() {
  renderQsoSatelliteOptions();
  renderQsoOpportunityList();
  renderQsoDetails();
  drawQsoMap();
}

async function searchQsoFinder(event) {
  event?.preventDefault();
  const status = document.getElementById('qso-finder-status');
  const grid1 = document.getElementById('qso-grid-1').value.trim().toUpperCase();
  const grid2 = document.getElementById('qso-grid-2').value.trim().toUpperCase();
  const noradId = document.getElementById('qso-satellite-filter').value;
  const minElevationDeg = Number(document.getElementById('qso-min-elevation').value);
  const hours = Number(document.getElementById('qso-hours').value);
  const minDurationMinutes = Number(document.getElementById('qso-min-duration').value);

  if (!grid1 || !grid2) {
    status.textContent = 'Enter both grid locators.';
    return;
  }

  status.textContent = 'Searching overlap windows...';
  try {
    const response = await fetch('/api/qso-finder/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        grid_1: grid1,
        grid_2: grid2,
        norad_id: noradId,
        min_elevation_deg: minElevationDeg,
        hours,
        min_duration_minutes: minDurationMinutes,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      status.textContent = result.detail || 'QSO search failed.';
      return;
    }
    qsoFinderResult = result;
    qsoOpportunities = Array.isArray(result.opportunities) ? result.opportunities : [];
    selectedQsoOpportunityIndex = qsoOpportunities.length ? 0 : -1;
    renderQsoOpportunityList();
    renderQsoDetails();
    drawQsoMap();
    status.textContent = qsoOpportunities.length
      ? `${qsoOpportunities.length} overlap window${qsoOpportunities.length === 1 ? '' : 's'} found.`
      : 'No overlap windows found in the selected search window.';
  } catch (error) {
    status.textContent = 'QSO search failed.';
  }
}

function renderQsoOpportunityList() {
  const list = document.getElementById('qso-opportunity-list');
  if (!list) {
    return;
  }
  list.replaceChildren();

  if (!qsoOpportunities.length) {
    const empty = document.createElement('p');
    empty.className = 'text-body-secondary mb-0';
    empty.textContent = qsoFinderResult
      ? 'No overlap windows found in the current search.'
      : 'Enter two grid locators to search.';
    list.appendChild(empty);
    return;
  }

  qsoOpportunities.forEach((opportunity, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'qso-opportunity-item btn';
    if (index === selectedQsoOpportunityIndex) {
      button.classList.add('active');
    }

    const overlapStart = new Date(opportunity.overlap_start_utc);
    const overlapEnd = new Date(opportunity.overlap_end_utc);
    const title = document.createElement('div');
    title.className = 'qso-opportunity-title';
    title.textContent = opportunity.satellite_name || 'Unknown Satellite';

    const meta = document.createElement('div');
    meta.className = 'qso-opportunity-meta';
    meta.textContent = `${formatDateOnlyForTimezone(overlapStart, 'UTC')}  ${formatTimeRangeForTimezone(overlapStart, overlapEnd, 'UTC')} UTC`;

    const submeta = document.createElement('div');
    submeta.className = 'qso-opportunity-submeta';
    submeta.textContent = `${formatDuration(opportunity.overlap_duration_seconds)} overlap`;

    button.append(title, meta, submeta);
    button.addEventListener('click', () => {
      selectedQsoOpportunityIndex = index;
      renderQsoOpportunityList();
      renderQsoDetails();
      drawQsoMap();
    });
    list.appendChild(button);
  });
}

function getSelectedQsoOpportunity() {
  if (selectedQsoOpportunityIndex < 0 || selectedQsoOpportunityIndex >= qsoOpportunities.length) {
    return null;
  }
  return qsoOpportunities[selectedQsoOpportunityIndex];
}

function renderQsoDetails() {
  const selectedSatellite = document.getElementById('qso-selected-satellite');
  const overlapSummary = document.getElementById('qso-overlap-summary');
  const opportunity = getSelectedQsoOpportunity();

  if (!selectedSatellite || !overlapSummary) {
    return;
  }
  if (!opportunity) {
    selectedSatellite.textContent = 'No overlap selected.';
    overlapSummary.textContent = 'No overlap selected.';
    document.getElementById('qso-grid-1-title').textContent = 'Grid 1';
    document.getElementById('qso-grid-2-title').textContent = 'Grid 2';
    document.getElementById('qso-grid-1-timezone').textContent = '--';
    document.getElementById('qso-grid-2-timezone').textContent = '--';
    document.getElementById('qso-grid-1-start').textContent = '--';
    document.getElementById('qso-grid-1-end').textContent = '--';
    document.getElementById('qso-grid-1-peak').textContent = '--';
    document.getElementById('qso-grid-2-start').textContent = '--';
    document.getElementById('qso-grid-2-end').textContent = '--';
    document.getElementById('qso-grid-2-peak').textContent = '--';
    document.getElementById('qso-window-start-local').textContent = 'Start Local --';
    document.getElementById('qso-window-start-utc').textContent = 'Start UTC --';
    document.getElementById('qso-window-end-local').textContent = 'Stop Local --';
    document.getElementById('qso-window-end-utc').textContent = 'Stop UTC --';
    document.getElementById('qso-duration').textContent = 'Duration --';
    return;
  }

  const overlapStart = new Date(opportunity.overlap_start_utc);
  const overlapEnd = new Date(opportunity.overlap_end_utc);
  const grid1Pass = opportunity.grid_1?.pass || {};
  const grid2Pass = opportunity.grid_2?.pass || {};

  selectedSatellite.textContent = `${opportunity.satellite_name} (${opportunity.norad_id})`;
  overlapSummary.textContent = `${formatDateOnlyForTimezone(overlapStart, 'UTC')}  ${formatTimeRangeForTimezone(overlapStart, overlapEnd, 'UTC')} UTC`;
  document.getElementById('qso-grid-1-title').textContent = opportunity.grid_1?.locator || 'Grid 1';
  document.getElementById('qso-grid-2-title').textContent = opportunity.grid_2?.locator || 'Grid 2';
  document.getElementById('qso-grid-1-timezone').textContent = grid1Pass.timezone || '--';
  document.getElementById('qso-grid-2-timezone').textContent = grid2Pass.timezone || '--';
  document.getElementById('qso-grid-1-start').textContent = formatDisplayDateTime(grid1Pass.aos_utc, grid1Pass.timezone);
  document.getElementById('qso-grid-1-end').textContent = formatDisplayDateTime(grid1Pass.los_utc, grid1Pass.timezone);
  document.getElementById('qso-grid-1-peak').textContent = formatNumber(grid1Pass.max_elevation_deg, 1, ' deg');
  document.getElementById('qso-grid-2-start').textContent = formatDisplayDateTime(grid2Pass.aos_utc, grid2Pass.timezone);
  document.getElementById('qso-grid-2-end').textContent = formatDisplayDateTime(grid2Pass.los_utc, grid2Pass.timezone);
  document.getElementById('qso-grid-2-peak').textContent = formatNumber(grid2Pass.max_elevation_deg, 1, ' deg');
  document.getElementById('qso-window-start-local').textContent = `Start Local ${formatDateTimeForTimezone(overlapStart, qthTimezone)}`;
  document.getElementById('qso-window-start-utc').textContent = `Start UTC ${formatDateTimeForTimezone(overlapStart, 'UTC')}`;
  document.getElementById('qso-window-end-local').textContent = `Stop Local ${formatDateTimeForTimezone(overlapEnd, qthTimezone)}`;
  document.getElementById('qso-window-end-utc').textContent = `Stop UTC ${formatDateTimeForTimezone(overlapEnd, 'UTC')}`;
  document.getElementById('qso-duration').textContent = `Duration ${formatDuration(opportunity.overlap_duration_seconds)}`;
}

function drawQsoMap() {
  const canvas = document.getElementById('qso-map');
  if (!canvas) {
    return;
  }
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  const opportunity = getSelectedQsoOpportunity();

  ctx.clearRect(0, 0, width, height);
  const view = buildQsoMapView(opportunity);
  drawMapBackground(ctx, width, height, view);
  drawMapGrid(ctx, width, height, view);

  if (!opportunity) {
    ctx.fillStyle = '#d4dee7';
    ctx.font = '16px Arial';
    ctx.fillText('Search for overlap windows to render the map.', 20, 30);
    return;
  }

  drawQsoTrack(ctx, width, height, opportunity.track_points || [], view);
  drawQsoFootprint(ctx, width, height, opportunity.footprint_points || [], view);
  drawQsoGridMarker(
    ctx,
    width,
    height,
    Number(opportunity.grid_1?.latitude_deg),
    Number(opportunity.grid_1?.longitude_deg),
    opportunity.grid_1?.locator || 'Grid 1',
    '#59d66f',
    view
  );
  drawQsoGridMarker(
    ctx,
    width,
    height,
    Number(opportunity.grid_2?.latitude_deg),
    Number(opportunity.grid_2?.longitude_deg),
    opportunity.grid_2?.locator || 'Grid 2',
    '#ffd45b',
    view
  );

  if (hasValidCoordinate(opportunity.midpoint?.latitude_deg) && hasValidCoordinate(opportunity.midpoint?.longitude_deg)) {
    drawMapSatellite(
      ctx,
      width,
      height,
      Number(opportunity.midpoint.latitude_deg),
      Number(opportunity.midpoint.longitude_deg),
      opportunity.satellite_name || '',
      view
    );
  }

  drawQsoLegend(ctx, width, height, opportunity);
}

function drawQsoTrack(ctx, width, height, points, view = null) {
  if (!Array.isArray(points) || points.length < 2) {
    return;
  }
  ctx.strokeStyle = '#2bb7ff';
  ctx.lineWidth = 2;
  ctx.globalAlpha = 0.95;
  ctx.beginPath();
  let started = false;
  let lastX = 0;
  points.forEach((point) => {
    if (!hasValidCoordinate(point.latitude_deg) || !hasValidCoordinate(point.longitude_deg)) {
      return;
    }
    const { x, y } = latLonToMapPoint(
      Number(point.latitude_deg),
      Number(point.longitude_deg),
      width,
      height,
      view
    );
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
      lastX = x;
      return;
    }
    if (Math.abs(x - lastX) > width * 0.5) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
    lastX = x;
  });
  ctx.stroke();
  ctx.globalAlpha = 1;
}

function drawQsoFootprint(ctx, width, height, points, view = null) {
  if (!Array.isArray(points) || points.length < 4) {
    return;
  }
  const projected = points
    .filter((point) => hasValidCoordinate(point.latitude_deg) && hasValidCoordinate(point.longitude_deg))
    .map((point) => latLonToMapPoint(Number(point.latitude_deg), Number(point.longitude_deg), width, height, view));
  if (projected.length < 4) {
    return;
  }
  ctx.fillStyle = 'rgba(79, 163, 255, 0.14)';
  ctx.strokeStyle = 'rgba(110, 196, 255, 0.62)';
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  projected.forEach((point, index) => {
    if (index === 0) {
      ctx.moveTo(point.x, point.y);
    } else {
      ctx.lineTo(point.x, point.y);
    }
  });
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
}

function drawQsoGridMarker(ctx, width, height, lat, lon, label, color, view = null) {
  if (!hasValidCoordinate(lat) || !hasValidCoordinate(lon)) {
    return;
  }
  const { x, y } = latLonToMapPoint(lat, lon, width, height, view);
  ctx.fillStyle = color;
  ctx.strokeStyle = 'rgba(6, 17, 26, 0.92)';
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  ctx.arc(x, y, 5, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();

  ctx.fillStyle = '#f3f6f8';
  ctx.font = '12px Arial';
  ctx.textAlign = 'left';
  const safeLabel = label || '';
  ctx.fillText(
    safeLabel,
    Math.min(x + 10, width - ctx.measureText(safeLabel).width - 6),
    Math.max(y - 10, 14)
  );
}

function buildQsoMapView(opportunity) {
  if (!opportunity) {
    return null;
  }
  const centerLatCandidates = [
    opportunity.midpoint?.latitude_deg,
    opportunity.grid_1?.latitude_deg,
    opportunity.grid_2?.latitude_deg,
  ].filter((value) => hasValidCoordinate(value));
  const centerLonCandidates = [
    opportunity.midpoint?.longitude_deg,
    opportunity.grid_1?.longitude_deg,
    opportunity.grid_2?.longitude_deg,
  ].filter((value) => hasValidCoordinate(value));

  if (!centerLatCandidates.length || !centerLonCandidates.length) {
    return null;
  }

  const centerLat = Number(centerLatCandidates[0]);
  const centerLon = normalizeLon(Number(centerLonCandidates[0]));
  const latSpan = 80;
  const lonSpan = 140;
  const minLat = Math.max(-90, Math.min(90 - latSpan, centerLat - (latSpan / 2)));

  return {
    minLat,
    maxLat: minLat + latSpan,
    centerLon,
    lonSpan,
  };
}

function drawQsoLegend(ctx, width, height, opportunity) {
  ctx.fillStyle = 'rgba(4, 12, 19, 0.84)';
  ctx.fillRect(10, height - 24, 316, 16);

  ctx.strokeStyle = '#2bb7ff';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(18, height - 16);
  ctx.lineTo(42, height - 16);
  ctx.stroke();

  ctx.fillStyle = '#d5e4f1';
  ctx.font = '10px Arial';
  ctx.fillText('TRACK', 46, height - 13);

  ctx.fillStyle = '#59d66f';
  ctx.beginPath();
  ctx.arc(94, height - 16, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#d5e4f1';
  ctx.fillText(opportunity.grid_1?.locator || 'GRID 1', 102, height - 13);

  ctx.fillStyle = '#ffd45b';
  ctx.beginPath();
  ctx.arc(164, height - 16, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#d5e4f1';
  ctx.fillText(opportunity.grid_2?.locator || 'GRID 2', 172, height - 13);

  ctx.fillStyle = 'rgba(79, 163, 255, 0.18)';
  ctx.fillRect(234, height - 20, 18, 8);
  ctx.strokeStyle = 'rgba(110, 196, 255, 0.62)';
  ctx.lineWidth = 1;
  ctx.strokeRect(234, height - 20, 18, 8);
  ctx.fillStyle = '#d5e4f1';
  ctx.fillText('FOOTPRINT', 258, height - 13);
}

function renderRotator(result) {
  document.getElementById('rotator-state-label').textContent =
    result.state_label || 'Rotator control is off';
  document.getElementById('rotator-connected').innerHTML = result.connected
    ? '<span class="badge text-bg-success">Yes</span>'
    : '<span class="badge text-bg-secondary">No</span>';
  document.getElementById('rotator-current-az').textContent =
    formatNumber(result.current_azimuth_deg, 2, ' deg');
  document.getElementById('rotator-current-el').textContent =
    formatNumber(result.current_elevation_deg, 2, ' deg');
  document.getElementById('rotator-target-az').textContent =
    formatNumber(result.target_azimuth_deg, 2, ' deg');
  document.getElementById('rotator-target-el').textContent =
    formatNumber(result.target_elevation_deg, 2, ' deg');
  const manualEnabled = Boolean(result.manual_controls_enabled);
  setRotatorManualControlState(manualEnabled);
  if (manualEnabled) {
    syncRotatorManualInputs(result);
  }
  logErrorState('rotator', result.error || '');
}

function setRotatorManualControlState(enabled) {
  document.getElementById('rotator-manual-az').disabled = !enabled;
  document.getElementById('rotator-manual-el').disabled = !enabled;
  document.getElementById('rotator-home-button').disabled = !enabled;
  document.getElementById('rotator-send-button').disabled = !enabled;
}

function syncRotatorManualInputs(result) {
  const azInput = document.getElementById('rotator-manual-az');
  const elInput = document.getElementById('rotator-manual-el');
  if (Number.isFinite(Number(result.current_azimuth_deg))) {
    azInput.value = Math.round(Number(result.current_azimuth_deg));
  }
  if (Number.isFinite(Number(result.current_elevation_deg))) {
    elInput.value = Math.round(Number(result.current_elevation_deg));
  }
}

async function sendManualRotatorPosition() {
  const azimuth = Number(document.getElementById('rotator-manual-az').value);
  const elevation = Number(document.getElementById('rotator-manual-el').value);
  try {
    const response = await fetch('/api/devices/rotator/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        azimuth_deg: azimuth,
        elevation_deg: elevation,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      addLog(result.detail || 'Manual rotator move failed.');
      return;
    }
    renderRotator(result);
    addLog('Manual rotator move sent.');
  } catch (error) {
    addLog('Manual rotator move failed.');
  }
}

async function sendRotatorHome() {
  try {
    const response = await fetch('/api/devices/rotator/home', {
      method: 'POST',
    });
    const result = await response.json();
    if (!response.ok) {
      addLog(result.detail || 'Rotator home command failed.');
      return;
    }
    renderRotator(result);
    addLog('Rotator sent to home.');
  } catch (error) {
    addLog('Rotator home command failed.');
  }
}

async function loadSettings() {
  const form = document.getElementById('settings-form');
  const status = document.getElementById('settings-status');
  try {
    const response = await fetch('/api/settings');
    const result = await response.json();
    const [radioModels, rotatorModels, devices] = await Promise.all([
      loadHamlibRadioModels(),
      loadHamlibRotatorModels(),
      loadSerialDevices(),
    ]);
    hamlibRadioModels = radioModels;
    hamlibRotatorModels = rotatorModels;
    serialDevices = devices;
    form.replaceChildren();
    Object.entries(result.schema).forEach(([section, keys]) => {
      if (section === 'my_satellites') {
        return;
      }
      const visibleKeys = keys.filter((key) => !hiddenSettingsKeys[section]?.has(key));
      if (!visibleKeys.length) {
        return;
      }
      const fieldset = document.createElement('fieldset');
      const legend = document.createElement('legend');
      legend.textContent = section;
      fieldset.appendChild(legend);

      visibleKeys.forEach((key) => {
        const row = buildSettingControl(
          section,
          key,
          result.settings[section][key] ?? ''
        );
        fieldset.appendChild(row);
      });

      if (['rx', 'tx', 'rotator'].includes(section)) {
        fieldset.appendChild(buildDeviceTestControls(section));
      }

      form.appendChild(fieldset);
    });
    applyConnectivityState();
    bindConnectivityState();
    status.textContent = '';
  } catch (error) {
    status.textContent = 'Settings load failed.';
  }
}

async function loadHamlibRadioModels() {
  try {
    const response = await fetch('/api/hamlib/radio-models');
    const result = await response.json();
    return result.models || [];
  } catch (error) {
    return [];
  }
}

async function loadSerialDevices() {
  try {
    const response = await fetch('/api/serial-devices');
    const result = await response.json();
    return result.devices || [];
  } catch (error) {
    return [];
  }
}

function ensureManagedSatelliteSelection() {
  if (!managedSatellitesCache.length) {
    selectedManagedSatelliteNorad = null;
    return;
  }
  const stillExists = managedSatellitesCache.some(
    (satellite) => Number(satellite.norad_id) === Number(selectedManagedSatelliteNorad)
  );
  if (!stillExists) {
    selectedManagedSatelliteNorad = Number(managedSatellitesCache[0].norad_id);
  }
}

function renderManagedSatelliteList() {
  const list = document.getElementById('my-satellite-list');
  if (!list) {
    return;
  }
  list.replaceChildren();
  if (!managedSatellitesCache.length) {
    const empty = document.createElement('div');
    empty.className = 'text-body-secondary small';
    empty.textContent = 'No tracked satellites yet.';
    list.appendChild(empty);
    return;
  }

  managedSatellitesCache.forEach((satellite) => {
    const profiles = managedSatelliteProfilesByNorad.get(Number(satellite.norad_id)) || [];
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'list-group-item list-group-item-action d-flex justify-content-between align-items-start gap-3';
    if (Number(satellite.norad_id) === Number(selectedManagedSatelliteNorad)) {
      button.classList.add('active');
    }
    const label = document.createElement('div');
    label.className = 'text-start';
    const title = document.createElement('div');
    title.className = 'fw-semibold';
    title.textContent = satellite.name;
    const subtitle = document.createElement('div');
    subtitle.className = 'small opacity-75';
    subtitle.textContent = `NORAD ${satellite.norad_id}`;
    label.append(title, subtitle);
    const badge = document.createElement('span');
    badge.className = 'badge text-bg-secondary align-self-center';
    badge.textContent = `${profiles.length} profile${profiles.length === 1 ? '' : 's'}`;
    button.append(label, badge);
    button.addEventListener('click', () => {
      selectedManagedSatelliteNorad = Number(satellite.norad_id);
      renderManagedSatelliteList();
      renderManagedSatelliteDetail();
    });
    list.appendChild(button);
  });
}

function renderManagedSatelliteDetail() {
  const title = document.getElementById('selected-my-satellite-name');
  const meta = document.getElementById('selected-my-satellite-meta');
  const profiles = document.getElementById('selected-my-satellite-profiles');
  const passes = document.getElementById('selected-my-satellite-passes');
  const updateButton = document.getElementById('selected-my-satellite-update-btn');
  const removeButton = document.getElementById('selected-my-satellite-remove-btn');
  const satellite = managedSatellitesCache.find(
    (item) => Number(item.norad_id) === Number(selectedManagedSatelliteNorad)
  );

  if (!satellite) {
    title.textContent = 'Select a satellite to view its profiles.';
    meta.textContent = '';
    profiles.replaceChildren();
    passes.replaceChildren();
    const empty = document.createElement('p');
    empty.className = 'text-body-secondary mb-0';
    empty.textContent = 'Choose a satellite on the left.';
    profiles.appendChild(empty);
    const emptyPasses = document.createElement('p');
    emptyPasses.className = 'text-body-secondary mb-0';
    emptyPasses.textContent = 'Choose a satellite on the left.';
    passes.appendChild(emptyPasses);
    if (updateButton) {
      updateButton.disabled = true;
      updateButton.onclick = null;
    }
    if (removeButton) {
      removeButton.disabled = true;
      removeButton.onclick = null;
    }
    return;
  }

  const loadedProfiles = managedSatelliteProfilesByNorad.get(Number(satellite.norad_id)) || [];
  const loadedPasses = managedSatellitePassesByNorad.get(Number(satellite.norad_id)) || [];
  title.textContent = satellite.name;
  meta.textContent = `NORAD ${satellite.norad_id} - ${loadedProfiles.length} frequency profile${loadedProfiles.length === 1 ? '' : 's'}`;
  profiles.replaceChildren(buildFrequencyProfileList(loadedProfiles));
  passes.replaceChildren(buildSatellitePassList(loadedPasses));
  if (updateButton) {
    updateButton.disabled = false;
    updateButton.onclick = () => updateFrequencyProfiles(satellite.norad_id);
  }
  if (removeButton) {
    removeButton.disabled = false;
    removeButton.onclick = () => removeMySatellite(satellite.norad_id);
  }
}

async function loadMySatellites() {
  const status = document.getElementById('my-satellite-status');
  try {
    const [myResponse, profileResponse, passResponse] = await Promise.all([
      fetch('/api/my-satellites'),
      fetch('/api/satellites'),
      fetch('/api/my-satellites/passes?hours=48'),
    ]);
    const result = await myResponse.json();
    const profileSatellites = await profileResponse.json();
    const passGroups = await passResponse.json();
    const profilesByNorad = new Map(
      profileSatellites.map((satellite) => [
        Number(satellite.norad_id),
        satellite.frequency_profiles || [],
      ])
    );
    const passesByNorad = new Map(
      (passGroups.passes || []).map((item) => [
        Number(item.norad_id),
        item.passes || [],
      ])
    );
    managedSatellitesCache = result.satellites || [];
    managedSatelliteProfilesByNorad = profilesByNorad;
    managedSatellitePassesByNorad = passesByNorad;
    document.getElementById('min-pass-elevation').value =
      result.min_pass_elevation_deg;
    const autoTrackToggle = document.getElementById('auto-track-toggle');
    if (autoTrackToggle) {
      autoTrackToggle.checked = Boolean(result.autotrack_next_pass);
    }
    const passMinLabel = document.getElementById('pass-min-elevation-label');
    if (passMinLabel) {
      passMinLabel.textContent = `(min el ${Number(result.min_pass_elevation_deg).toFixed(1)} deg)`;
    }
    ensureManagedSatelliteSelection();
    renderManagedSatelliteList();
    renderManagedSatelliteDetail();
    status.textContent = '';
  } catch (error) {
    status.textContent = 'My Satellites load failed.';
  }
}
async function loadMonitor() {
  const status = document.getElementById('monitor-status');
  try {
    const [settingsResponse] = await Promise.all([
      fetch('/api/settings'),
      refreshMonitorLogs(),
    ]);
    const result = await settingsResponse.json();
    document.getElementById('monitor-rx-cat-debug').checked =
      String(result.settings?.rx?.cat_debug_logging || '').toLowerCase() === 'true';
    document.getElementById('monitor-tx-cat-debug').checked =
      String(result.settings?.tx?.cat_debug_logging || '').toLowerCase() === 'true';
    document.getElementById('monitor-rotator-cat-debug').checked =
      String(result.settings?.rotator?.cat_debug_logging || '').toLowerCase() === 'true';
    status.textContent = '';
  } catch (error) {
    status.textContent = 'Monitor load failed.';
  }
}

async function updateMonitorDebug() {
  const status = document.getElementById('monitor-status');
  status.textContent = 'Saving debug settings...';
  try {
    const response = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        settings: {
          rx: {
            cat_debug_logging: document.getElementById('monitor-rx-cat-debug').checked ? 'true' : 'false',
          },
          tx: {
            cat_debug_logging: document.getElementById('monitor-tx-cat-debug').checked ? 'true' : 'false',
          },
          rotator: {
            cat_debug_logging: document.getElementById('monitor-rotator-cat-debug').checked ? 'true' : 'false',
          },
        },
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      status.textContent = result.detail || 'Debug settings save failed.';
      return;
    }
    status.textContent = 'Debug settings saved and connections reloaded.';
  } catch (error) {
    status.textContent = 'Debug settings save failed.';
  }
}

function buildFrequencyProfileList(profiles) {
  const container = document.createElement('div');
  container.className = 'profile-list';
  if (!profiles.length) {
    const empty = document.createElement('p');
    empty.className = 'profile-empty';
    empty.textContent = 'No frequency profiles loaded.';
    container.appendChild(empty);
    return container;
  }

  profiles.forEach((profile) => {
    const item = document.createElement('div');
    item.className = 'profile-item';
    const name = document.createElement('strong');
    name.textContent = profile.name;
    const tags = document.createElement('span');
    tags.className = 'profile-tags';
    if (isRxOnlyProfile(profile)) {
      tags.append(
        buildProfileTag('RX-only'),
        buildProfileTag(profile.downlink_mode || '---')
      );
    } else {
      tags.append(
        buildProfileTag('RX/TX'),
        buildProfileTag(profile.type === 'linear' ? 'Linear' : profile.type || 'Profile')
      );
    }
    if (profile.inverted) {
      tags.appendChild(buildProfileTag('Inverting'));
    }
    const details = document.createElement('span');
    details.className = 'profile-frequency-line';
    const rxRange = formatFrequencyRange(profile.downlink_low, profile.downlink_high);
    if (isRxOnlyProfile(profile)) {
      details.textContent = `RX ${rxRange} ${profile.downlink_mode || ''}`.trim();
    } else {
      const txRange = formatFrequencyRange(profile.uplink_low, profile.uplink_high);
      details.textContent = `RX ${rxRange} ${profile.downlink_mode || ''} | TX ${txRange} ${profile.uplink_mode || ''}`.trim();
    }
    item.append(name, tags, details);
    container.appendChild(item);
  });
  return container;
}

async function loadHamlibRotatorModels() {
  try {
    const response = await fetch('/api/hamlib/rotator-models');
    const result = await response.json();
    return result.models || [];
  } catch (error) {
    return [];
  }
}

function buildDeviceTestControls(section) {
  const row = document.createElement('div');
  row.className = 'settings-section-actions';

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn btn-outline-primary btn-sm';
  button.textContent = `Test ${formatSectionLabel(section)}`;
  button.addEventListener('click', () => testDevice(section));

  const status = document.createElement('span');
  status.className = 'settings-section-status';
  status.id = `test-status-${section}`;

  row.append(button, status);
  return row;
}

function collectSectionSettings(section) {
  const form = document.getElementById('settings-form');
  const settings = {};
  Array.from(form.elements).forEach((element) => {
    if (!element.name || element.type === 'checkbox') {
      return;
    }
    const [elementSection, key] = element.name.split('.');
    if (elementSection !== section) {
      return;
    }
    settings[key] = element.value;
  });
  return settings;
}

function setDeviceTestStatus(section, message, isError = false) {
  const status = document.getElementById(`test-status-${section}`);
  if (!status) {
    return;
  }
  status.textContent = message || '';
  status.classList.toggle('is-error', Boolean(isError));
}

function formatDeviceTestResult(section, result) {
  if (!result?.details) {
    return result?.message || 'Test complete.';
  }
  const details = result.details;
  if (result.ok) {
    if ((section === 'rx' || section === 'tx') && Number.isFinite(Number(details.frequency_hz))) {
      return `${result.message} ${Number(details.frequency_hz).toLocaleString()} Hz`;
    }
    if (section === 'rotator') {
      const azimuth = Number(details.azimuth_deg);
      const elevation = Number(details.elevation_deg);
      if (Number.isFinite(azimuth) && Number.isFinite(elevation)) {
        return `${result.message} Az ${azimuth.toFixed(1)} deg, El ${elevation.toFixed(1)} deg`;
      }
    }
    return result.message || 'Test succeeded.';
  }
  const error = String(details.error || '').trim();
  if (error) {
    return `${result.message} ${error}`;
  }
  return result.message || 'Test failed.';
}

async function testDevice(section) {
  setDeviceTestStatus(section, `Testing ${formatSectionLabel(section)}...`, false);
  try {
    const response = await fetch(`/api/device-tests/${section}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        settings: collectSectionSettings(section),
      }),
    });
    const result = await response.json();
    const message = response.ok
      ? formatDeviceTestResult(section, result)
      : (result.detail || `Test ${formatSectionLabel(section)} failed.`);
    setDeviceTestStatus(section, message, !response.ok || !result.ok);
    addLog(message);
  } catch (error) {
    const message = `Test ${formatSectionLabel(section)} failed.`;
    setDeviceTestStatus(section, message, true);
    addLog(message);
  }
}

function buildSatellitePassList(passes) {
  const container = document.createElement('div');
  container.className = 'satellite-pass-list';
  if (!passes.length) {
    const empty = document.createElement('p');
    empty.className = 'profile-empty mb-0';
    empty.textContent = 'No known passes in the current cache window.';
    container.appendChild(empty);
    return container;
  }

  passes.forEach((satellitePass) => {
    const item = document.createElement('div');
    item.className = 'satellite-pass-item';
    const aos = new Date(satellitePass.aos_utc);
    const los = new Date(satellitePass.los_utc);

    const dateLine = document.createElement('div');
    dateLine.className = 'satellite-pass-time';
    dateLine.textContent = `${formatLocalDateOnly(aos)}  ${formatLocalTimeRange(aos, los)}`;

    const azLine = document.createElement('div');
    azLine.className = 'satellite-pass-meta';
    azLine.textContent = `AZ Path ${formatAzimuthPath(satellitePass)}`;

    const elevationLine = document.createElement('div');
    elevationLine.className = 'satellite-pass-meta';
    elevationLine.textContent = `Max El ${Number(satellitePass.max_elevation_deg).toFixed(1)} deg`;

    item.append(dateLine, azLine, elevationLine);
    container.appendChild(item);
  });
  return container;
}

function buildProfileTag(text) {
  const tag = document.createElement('span');
  tag.className = 'profile-tag';
  tag.textContent = text;
  return tag;
}

async function updateFrequencyProfiles(noradId) {
  const status = document.getElementById('my-satellite-status');
  status.textContent = 'Updating frequency profiles...';
  try {
    const response = await fetch(`/api/frequency-profiles/${noradId}/update`, {
      method: 'POST',
    });
    const result = await response.json();
    if (!response.ok) {
      status.textContent = result.detail || 'Frequency profile update failed.';
      return;
    }
    const names = result.frequency_profiles.map((profile) => profile.name).join(', ');
    status.textContent = `Updated ${result.imported} frequency profile(s): ${names}`;
    await loadSatellites();
    await loadMySatellites();
  } catch (error) {
    status.textContent = 'Frequency profile update failed.';
  }
}

async function addMySatellite(event) {
  event.preventDefault();
  const status = document.getElementById('my-satellite-status');
  const noradId = Number(document.getElementById('my-satellite-norad').value);
  const name = document.getElementById('my-satellite-name').value.trim();
  if (!Number.isInteger(noradId) || noradId <= 0) {
    status.textContent = 'Enter a valid NORAD ID.';
    return;
  }
  try {
    const response = await fetch('/api/my-satellites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ norad_id: noradId, name }),
    });
    const result = await response.json();
    if (!response.ok) {
      status.textContent = result.detail || 'Satellite add failed.';
      return;
    }
    status.textContent = 'Satellite added.';
    document.getElementById('my-satellite-norad').value = '';
    document.getElementById('my-satellite-name').value = '';
    await loadMySatellites();
    await loadSatellites();
    await loadPasses();
  } catch (error) {
    status.textContent = 'Satellite add failed.';
  }
}

async function removeMySatellite(noradId) {
  const status = document.getElementById('my-satellite-status');
  try {
    const response = await fetch(`/api/my-satellites/${noradId}`, {
      method: 'DELETE',
    });
    const result = await response.json();
    if (!response.ok) {
      status.textContent = result.detail || 'Satellite remove failed.';
      return;
    }
    status.textContent = 'Satellite removed.';
    await loadMySatellites();
    await loadSatellites();
    await loadPasses();
  } catch (error) {
    status.textContent = 'Satellite remove failed.';
  }
}

async function saveMySatelliteOptions(event) {
  event.preventDefault();
  const status = document.getElementById('my-satellite-status');
  const minElevation = Number(document.getElementById('min-pass-elevation').value);
  if (!Number.isFinite(minElevation)) {
    status.textContent = 'Enter a valid minimum elevation.';
    return;
  }
  if (minElevation < 0 || minElevation > 90) {
    status.textContent = 'Minimum elevation must be between 0 and 90 degrees.';
    return;
  }
  try {
    const response = await fetch('/api/my-satellites/options', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        min_pass_elevation_deg: minElevation,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      status.textContent = result.detail || 'Pass options save failed.';
      return;
    }
    status.textContent = `Minimum pass elevation saved at ${minElevation} deg.`;
    await loadMySatellites();
    await loadPasses();
  } catch (error) {
    status.textContent = 'Pass options save failed.';
  }
}

function buildSettingControl(section, key, value) {
  const row = document.createElement('label');
  row.className = 'settings-row';
  row.dataset.settingKey = key;

  const labelText = document.createElement('span');
  labelText.className = 'form-label mb-0';
  labelText.textContent = formatSettingLabel(key);

  const fieldName = `${section}.${key}`;
  let control;

  if (isBooleanSetting(key)) {
    row.classList.add('settings-row-toggle');
    control = document.createElement('input');
    control.type = 'checkbox';
    control.className = 'form-check-input';
    control.setAttribute('role', 'switch');
    control.name = fieldName;
    control.checked = String(value).toLowerCase() === 'true';
    const hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.name = fieldName;
    hidden.value = control.checked ? 'true' : 'false';
    control.addEventListener('change', () => {
      hidden.value = control.checked ? 'true' : 'false';
    });
    const switchWrap = document.createElement('span');
    switchWrap.className = 'form-check form-switch mb-0';
    switchWrap.appendChild(control);
    row.append(labelText, hidden, switchWrap);
    return row;
  }

  if (key === 'connectivity') {
    control = document.createElement('select');
    control.className = 'form-select';
    ['network', 'local'].forEach((optionValue) => {
      const option = document.createElement('option');
      option.value = optionValue;
      option.textContent = optionValue;
      control.appendChild(option);
    });
    control.value = value || 'network';
  } else if (key === 'serial_port' && (section === 'rx' || section === 'tx' || section === 'rotator')) {
    control = buildSerialPortSelect(value);
  } else if (key === 'model_id' && (section === 'rx' || section === 'tx')) {
    control = buildHamlibModelSelect(value);
  } else if (key === 'model_id' && section === 'rotator') {
    control = buildHamlibRotatorModelSelect(value);
  } else if (key === 'target_vfo' && (section === 'rx' || section === 'tx')) {
    control = buildVfoSelect(value);
  } else if (section === 'tle' && key === 'source_url') {
    control = document.createElement('textarea');
    control.className = 'form-control multiline-setting';
    control.rows = 4;
    control.value = value;
    row.classList.add('settings-row-wide');
    } else {
      control = document.createElement('input');
      control.className = 'form-control';
      control.value = value;
      if (isNumericSetting(key)) {
        control.inputMode = 'decimal';
        control.classList.add('compact-input');
        row.classList.add(compactRowClassForKey(key));
      }
      if (isWideSetting(key)) {
        row.classList.add('settings-row-wide');
      }
  }

  control.name = fieldName;
  row.append(labelText, control);
  if (
    key === 'serial_port'
    && (section === 'rx' || section === 'tx' || section === 'rotator')
    && control.dataset.deviceMissing === 'true'
  ) {
    const warning = document.createElement('span');
    warning.className = 'settings-inline-warning';
    warning.textContent = 'Selected device not connected.';
    row.appendChild(warning);
  }
  return row;
}

function buildSerialPortSelect(value) {
  const control = document.createElement('select');
  control.className = 'form-select';
  const blank = document.createElement('option');
  blank.value = '';
  blank.textContent = serialDevices.length
    ? 'Select serial device'
    : 'No /dev/serial/by-id devices found';
  control.appendChild(blank);

  const selectedValue = String(value || '');
  const known = serialDevices.some((device) => device.path === selectedValue);
  if (selectedValue && !known) {
    const current = document.createElement('option');
    current.value = selectedValue;
    current.textContent = selectedValue;
    control.appendChild(current);
  }
  serialDevices.forEach((device) => {
    const option = document.createElement('option');
    option.value = device.path;
    option.textContent = device.label || device.name || device.path;
    control.appendChild(option);
  });
  if (selectedValue && !known) {
    control.dataset.deviceMissing = 'true';
  }
  control.value = selectedValue;
  return control;
}

function buildHamlibModelSelect(value) {
  const control = document.createElement('select');
  control.className = 'form-select';
  const blank = document.createElement('option');
  blank.value = '';
  blank.textContent = hamlibRadioModels.length
    ? 'Select Hamlib radio model'
    : 'Hamlib rigctl model list unavailable';
  control.appendChild(blank);

  const selectedValue = String(value || '');
  const hasSelectedValue = hamlibRadioModels.some((model) => {
    return String(model.model_id) === selectedValue;
  });
  if (selectedValue && !hasSelectedValue) {
    const current = document.createElement('option');
    current.value = selectedValue;
    current.textContent = `Current model ${selectedValue}`;
    control.appendChild(current);
  }

  hamlibRadioModels.forEach((model) => {
    const option = document.createElement('option');
    option.value = String(model.model_id);
    option.textContent = `${model.model_id} - ${model.label}`;
    control.appendChild(option);
  });
  control.value = selectedValue;
  return control;
}

function formatSectionLabel(section) {
  if (section === 'rx') {
    return 'RX';
  }
  if (section === 'tx') {
    return 'TX';
  }
  if (section === 'rotator') {
    return 'Rotator';
  }
  return section;
}

function buildHamlibRotatorModelSelect(value) {
  const control = document.createElement('select');
  control.className = 'form-select';
  const blank = document.createElement('option');
  blank.value = '';
  blank.textContent = hamlibRotatorModels.length
    ? 'Select Hamlib rotator model'
    : 'Hamlib rotator model list unavailable';
  control.appendChild(blank);

  const selectedValue = String(value || '');
  const hasSelectedValue = hamlibRotatorModels.some((model) => {
    return String(model.model_id) === selectedValue;
  });
  if (selectedValue && !hasSelectedValue) {
    const current = document.createElement('option');
    current.value = selectedValue;
    current.textContent = `Current model ${selectedValue}`;
    control.appendChild(current);
  }

  hamlibRotatorModels.forEach((model) => {
    const option = document.createElement('option');
    option.value = String(model.model_id);
    option.textContent = `${model.model_id} - ${model.label}`;
    control.appendChild(option);
  });
  control.value = selectedValue;
  return control;
}

function buildVfoSelect(value) {
  const control = document.createElement('select');
  control.className = 'form-select';
  [
    ['current', 'Current VFO'],
    ['A', 'VFO A'],
    ['B', 'VFO B'],
  ].forEach(([optionValue, optionLabel]) => {
    const option = document.createElement('option');
    option.value = optionValue;
    option.textContent = optionLabel;
    control.appendChild(option);
  });
  control.value = String(value || 'current');
  return control;
}

async function saveSettings(event) {
  event.preventDefault();
  const status = document.getElementById('settings-status');
  const form = event.currentTarget;
  const settings = {};

  Array.from(form.elements).forEach((element) => {
    if (!element.name) {
      return;
    }
    if (element.type === 'checkbox') {
      return;
    }
    const [section, key] = element.name.split('.');
    if (!settings[section]) {
      settings[section] = {};
    }
    settings[section][key] = element.value;
  });

  status.textContent = 'Saving settings...';
  try {
    const response = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
    });
    const result = await response.json();
    if (!response.ok) {
      status.textContent = result.detail || 'Settings save failed.';
      return;
    }
    status.textContent = 'Settings saved and connections reloaded.';
    loadStatus();
    await loadSettings();
    loadRotator();
    syncTrackingForSelection();
  } catch (error) {
    status.textContent = 'Settings save failed.';
  }
}

async function updateDeviceControl(event) {
  addLog('Updating device control...');
  const toggleId = event.currentTarget.id;
  try {
    const response = await fetch('/api/device-controls', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        rx_enabled: document.getElementById('rx-control-toggle').checked,
        tx_enabled: document.getElementById('tx-control-toggle').checked,
        rotator_enabled: document.getElementById('rotator-control-toggle').checked,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      addLog(result.detail || 'Device control update failed.');
      event.currentTarget.checked = !event.currentTarget.checked;
      return;
    }
    addLog('Device control updated.');
    loadStatus();
    rotatorControlEnabled = document.getElementById('rotator-control-toggle').checked;
    loadRotator();
    if (toggleId !== 'rotator-control-toggle') {
      syncTrackingForSelection();
    }
  } catch (error) {
    addLog('Device control update failed.');
    event.currentTarget.checked = !event.currentTarget.checked;
  }
}

async function refreshTleData() {
  const status = document.getElementById('settings-status');
  status.textContent = 'Refreshing TLE data...';
  try {
    const response = await fetch('/api/tle/refresh', { method: 'POST' });
    const result = await response.json();
    if (!response.ok) {
      status.textContent = result.detail || 'TLE refresh failed.';
      return;
    }
    status.textContent = `TLE data refreshed (${result.refreshed_at_utc || 'complete'}).`;
    await loadPasses();
    await loadTrackedSatelliteLocations();
    await loadTracking(true);
    drawMap();
    drawTrackedSatellitesMap();
  } catch (error) {
    status.textContent = 'TLE refresh failed.';
  }
}

function isBooleanSetting(key) {
  return key === 'enabled'
    || key === 'write_enabled'
    || key === 'cat_debug_logging'
    || key === 'return_home_after_pass'
    || key.startsWith('tx_inhibit');
}

function isNumericSetting(key) {
  return key === 'port'
    || key === 'baud'
    || key === 'model_id'
    || key === 'timeout_s'
    || key === 'latitude_deg'
    || key === 'longitude_deg'
    || key === 'elevation_m'
    || key === 'stale_after_hours'
    || key === 'frequency_deadband_hz'
    || key === 'cat_rate_limit_hz'
    || key === 'tracking_update_interval_ms'
    || key === 'min_elevation_deg';
}

function isWideSetting(key) {
  return key === 'source_url'
    || key === 'cache_dir'
    || key === 'satellites_file';
}

function compactRowClassForKey(key) {
  if (key === 'stale_after_hours' || key === 'timeout_s' || key === 'min_elevation_deg') {
    return 'settings-row-compact-sm';
  }
  if (key === 'port' || key === 'baud' || key === 'model_id' || key === 'tracking_update_interval_ms') {
    return 'settings-row-compact-md';
  }
  if (key === 'latitude_deg' || key === 'longitude_deg' || key === 'elevation_m' || key === 'frequency_deadband_hz' || key === 'cat_rate_limit_hz') {
    return 'settings-row-compact-lg';
  }
  return 'settings-row-compact-md';
}

function formatSettingLabel(key) {
  const replacements = {
    host: 'Host',
    port: 'Port',
    baud: 'Baud',
    model_id: 'Model ID',
    serial_port: 'Serial Port',
    write_enabled: 'Write Enabled',
    timeout_s: 'Timeout (s)',
    latitude_deg: 'Latitude (deg)',
    longitude_deg: 'Longitude (deg)',
    elevation_m: 'Elevation (m)',
    source_url: 'TLE Source URLs',
    stale_after_hours: 'Stale After (hours)',
    satellites_file: 'Satellites File',
    min_elevation_deg: 'Minimum Elevation (deg)',
    home_azimuth_deg: 'Home Azimuth (deg)',
    home_elevation_deg: 'Home Elevation (deg)',
    return_home_after_pass: 'Return Home When Pass Ends',
    cat_debug_logging: 'CAT Debug Logging',
    target_vfo: 'Target VFO',
    tracking_update_interval_ms: 'Tracking Update Interval (ms)',
    tx_inhibit_below_horizon: 'TX Inhibit Below Horizon',
    tx_inhibit_on_cat_loss: 'TX Inhibit On CAT Loss',
    tx_inhibit_without_valid_pass: 'TX Inhibit Without Valid Pass',
    frequency_deadband_hz: 'Frequency Deadband (Hz)',
    cat_rate_limit_hz: 'CAT Rate Limit (Hz)',
    gui_resources_caching: 'GUI Resources Caching',
    name: 'Name',
    connectivity: 'Connectivity',
    enabled: 'Enabled',
  };
  if (replacements[key]) {
    return replacements[key];
  }
  return key
    .split('_')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function renderTracking(result) {
  if (
    mapRefreshPending
    && selectedSatelliteNorad
    && Number.isFinite(Number(result?.norad_id))
    && Number(result.norad_id) !== Number(selectedSatelliteNorad)
  ) {
    return;
  }
  latestTracking = result;
  const updateAtMs = Date.parse(result?.last_update_at_utc || '') || 0;
  if (
    Number(result.norad_id) === Number(selectedSatelliteNorad)
    && updateAtMs >= mapRefreshRequestedAtMs
  ) {
    mapRefreshPending = false;
  }
  const syncToggle = document.getElementById('sync-rx-tx-toggle');
  if (
    syncToggle
    && typeof result.sync_offsets === 'boolean'
    && !syncToggleUpdatePending
  ) {
    syncRxTx = result.sync_offsets;
    syncToggle.checked = result.sync_offsets;
  }
  document.getElementById('selected-satellite-azimuth').textContent =
    `Az ${formatNumber(result.azimuth_deg, 2, ' deg')}`;
  document.getElementById('selected-satellite-elevation').textContent =
    `El ${formatNumber(result.elevation_deg, 2, ' deg')}`;
  document.getElementById('tracking-rx-center').textContent =
    formatHz(result.downlink_center_hz);
  document.getElementById('tracking-rx-doppler').textContent =
    formatSignedHz(result.downlink_doppler_hz);
  document.getElementById('tracking-rx-offset').textContent =
    formatSignedHz(result.user_downlink_offset_hz);
  document.getElementById('tracking-target-rx').textContent =
    formatHz(result.target_rx_hz);
  document.getElementById('tracking-tx-center').textContent =
    formatHz(result.uplink_center_hz);
  document.getElementById('tracking-tx-doppler').textContent =
    formatSignedHz(result.uplink_doppler_hz);
  document.getElementById('tracking-calculated-tx').textContent =
    formatHz(result.calculated_tx_hz);
  document.getElementById('tracking-tx-offset').textContent =
    formatSignedHz(result.mapped_user_uplink_offset_hz);
  renderTxReadoutsForSelectedProfile();
  logErrorState('tracking', result.error || '');
  drawMap();
}

function renderTxReadoutsForSelectedProfile() {
  const profile = getSelectedFrequencyProfile();
  updateTxProfileState(profile);
  if (!isRxOnlyProfile(profile)) {
    return;
  }
  document.getElementById('tracking-tx-center').textContent = 'Not used';
  document.getElementById('tracking-tx-doppler').textContent = 'Not used';
  document.getElementById('tracking-calculated-tx').textContent = 'Not used';
  document.getElementById('tracking-tx-offset').textContent = 'Not used';
}

function drawMap() {
  const canvas = document.getElementById('satellite-map');
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;

  ctx.clearRect(0, 0, width, height);
  const mapTracking = getCurrentMapTracking();
  const hasSubpoint = mapTracking
    && !mapRefreshPending
    && hasValidCoordinate(mapTracking.latitude_deg)
    && hasValidCoordinate(mapTracking.longitude_deg);
  drawMapBackground(ctx, width, height, null);
  drawMapGrid(ctx, width, height, null);
  drawGroundTrack(ctx, width, height);
  drawHomeMarker(ctx, width, height);
  if (
    hasSubpoint
  ) {
    drawMapSatellite(
      ctx,
      width,
      height,
      Number(mapTracking.latitude_deg),
      Number(mapTracking.longitude_deg),
      mapTracking.satellite_name || document.getElementById('selected-satellite').textContent,
      null
    );
    document.getElementById('map-update').textContent = formatClockForQth(new Date());
  } else {
    document.getElementById('map-update').textContent = '--';
  }
  drawMapLegend(ctx, width, height);
  drawPassArc();
}

function drawTrackedSatellitesMap() {
  const canvas = document.getElementById('tracked-satellites-map');
  if (!canvas) {
    return;
  }
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;

  ctx.clearRect(0, 0, width, height);
  drawMapBackground(ctx, width, height, null);
  drawMapGrid(ctx, width, height, null);

  trackedSatelliteLocations.forEach((position) => {
    if (!hasValidCoordinate(position.latitude_deg) || !hasValidCoordinate(position.longitude_deg)) {
      return;
    }
    drawMapSatellite(
      ctx,
      width,
      height,
      Number(position.latitude_deg),
      Number(position.longitude_deg),
      position.satellite_name || '',
      null
    );
  });
}

function beginMapRefresh(noradId, satelliteName) {
  mapRefreshPending = true;
  mapRefreshRequestedAtMs = Date.now();
  latestTracking = {
    active: true,
    norad_id: noradId,
    satellite_name: satelliteName,
    latitude_deg: null,
    longitude_deg: null,
    azimuth_deg: null,
    elevation_deg: null,
    last_update_at_utc: null,
  };
}

function getCurrentMapTracking() {
  if (!latestTracking) {
    return null;
  }
  if (
    selectedSatelliteNorad
    && Number(latestTracking.norad_id) !== Number(selectedSatelliteNorad)
  ) {
    return null;
  }
  return latestTracking;
}

function hasValidCoordinate(value) {
  if (value === null || value === undefined || value === '') {
    return false;
  }
  return Number.isFinite(Number(value));
}

function buildFollowMapView(lat, lon) {
  const latSpan = 60;
  const lonSpan = 110;
  const minLat = Math.max(-90, Math.min(90 - latSpan, lat - latSpan / 2));
  return {
    minLat,
    maxLat: minLat + latSpan,
    centerLon: normalizeLon(lon),
    lonSpan,
  };
}

function normalizeLon(lon) {
  let normalized = lon;
  while (normalized < -180) {
    normalized += 360;
  }
  while (normalized >= 180) {
    normalized -= 360;
  }
  return normalized;
}

function latLonToMapPoint(lat, lon, width, height, view = null) {
  if (view) {
    let deltaLon = normalizeLon(lon - view.centerLon);
    const x = ((deltaLon + view.lonSpan / 2) / view.lonSpan) * width;
    const y = ((view.maxLat - lat) / (view.maxLat - view.minLat)) * height;
    return { x, y };
  }
  return {
    x: ((lon + 180) / 360) * width,
    y: ((90 - lat) / 180) * height,
  };
}

function drawMapBackground(ctx, width, height, view) {
  if (worldMapImage.complete && worldMapImage.naturalWidth > 0) {
    if (view) {
      drawCroppedWorldMap(ctx, width, height, view);
    } else {
      ctx.drawImage(worldMapImage, 0, 0, width, height);
    }
    ctx.fillStyle = 'rgba(3, 9, 14, 0.2)';
    ctx.fillRect(0, 0, width, height);
  } else {
    ctx.fillStyle = '#0b2333';
    ctx.fillRect(0, 0, width, height);
    drawMapLandHints(ctx, width, height);
  }
}

function drawCroppedWorldMap(ctx, width, height, view) {
  const imageWidth = worldMapImage.naturalWidth;
  const imageHeight = worldMapImage.naturalHeight;
  const sourceY = ((90 - view.maxLat) / 180) * imageHeight;
  const sourceHeight = ((view.maxLat - view.minLat) / 180) * imageHeight;
  const startLon = view.centerLon - view.lonSpan / 2;
  const endLon = view.centerLon + view.lonSpan / 2;

  drawLonSegment(startLon, endLon, 0, width);

  function drawLonSegment(lonStart, lonEnd, destX, destWidth) {
    if (lonStart < -180) {
      const splitWidth = ((-180 - lonStart) / (lonEnd - lonStart)) * destWidth;
      drawLonSegment(lonStart + 360, 180, destX, splitWidth);
      drawLonSegment(-180, lonEnd, destX + splitWidth, destWidth - splitWidth);
      return;
    }
    if (lonEnd > 180) {
      const splitWidth = ((180 - lonStart) / (lonEnd - lonStart)) * destWidth;
      drawLonSegment(lonStart, 180, destX, splitWidth);
      drawLonSegment(-180, lonEnd - 360, destX + splitWidth, destWidth - splitWidth);
      return;
    }
    const sourceX = ((lonStart + 180) / 360) * imageWidth;
    const sourceWidth = ((lonEnd - lonStart) / 360) * imageWidth;
    ctx.drawImage(
      worldMapImage,
      sourceX,
      sourceY,
      sourceWidth,
      sourceHeight,
      destX,
      0,
      destWidth,
      height
    );
  }
}

function drawMapGrid(ctx, width, height, view = null) {
  ctx.strokeStyle = '#356987';
  ctx.lineWidth = 1;
  ctx.globalAlpha = 0.75;

  const minLon = view ? view.centerLon - view.lonSpan / 2 : -180;
  const maxLon = view ? view.centerLon + view.lonSpan / 2 : 180;
  const minLat = view ? view.minLat : -90;
  const maxLat = view ? view.maxLat : 90;

  for (let lon = Math.ceil(minLon / 10) * 10; lon <= maxLon; lon += 10) {
    const point = latLonToMapPoint(minLat, normalizeLon(lon), width, height, view);
    ctx.beginPath();
    ctx.moveTo(point.x, 0);
    ctx.lineTo(point.x, height);
    ctx.stroke();
  }

  for (let lat = Math.ceil(minLat / 10) * 10; lat <= maxLat; lat += 10) {
    const point = latLonToMapPoint(lat, view ? view.centerLon : 0, width, height, view);
    ctx.beginPath();
    ctx.moveTo(0, point.y);
    ctx.lineTo(width, point.y);
    ctx.stroke();
  }

  ctx.globalAlpha = 1;
}

function drawMapLandHints(ctx, width, height) {
  const continents = [
    [[72, -168], [55, -135], [58, -105], [46, -65], [25, -82], [8, -78], [-15, -65], [-55, -72], [-35, -55], [8, -38], [28, -98]],
    [[72, -25], [58, 20], [35, 35], [10, 50], [-35, 20], [-35, -18], [5, -18], [35, -8]],
    [[70, 35], [72, 115], [55, 170], [22, 145], [5, 105], [22, 72], [40, 45]],
    [[8, 95], [-8, 140], [-38, 155], [-45, 115], [-10, 112]],
    [[-62, -180], [-70, -90], [-62, 0], [-70, 90], [-62, 180], [-82, 180], [-82, -180]],
  ];

  ctx.fillStyle = '#3f7f5f';
  ctx.strokeStyle = '#74a87e';
  ctx.lineWidth = 1;

  continents.forEach((continent) => {
    ctx.beginPath();
    continent.forEach(([lat, lon], index) => {
      const point = latLonToMapPoint(lat, lon, width, height);
      if (index === 0) {
        ctx.moveTo(point.x, point.y);
      } else {
        ctx.lineTo(point.x, point.y);
      }
    });
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  });
}

function drawMapSatellite(ctx, width, height, lat, lon, label, view = null) {
  const { x, y } = latLonToMapPoint(lat, lon, width, height, view);
  ctx.fillStyle = 'rgba(255, 88, 88, 0.28)';
  ctx.beginPath();
  ctx.arc(x, y, 7, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#ff5c5c';
  ctx.strokeStyle = 'rgba(255, 244, 244, 0.92)';
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  ctx.arc(x, y, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();

  ctx.fillStyle = '#f3f6f8';
  ctx.font = '11px Arial';
  ctx.textAlign = 'left';
  const satelliteLabel = label || '';
  ctx.fillText(
    satelliteLabel,
    Math.min(x + 10, width - ctx.measureText(satelliteLabel).width - 6),
    Math.max(y - 10, 14)
  );
}

function maybeRefreshGroundTrack(trackingSnapshot) {
  const noradId = Number(trackingSnapshot?.norad_id);
  if (!Number.isFinite(noradId) || noradId <= 0) {
    groundTrackPoints = [];
    groundTrackNoradId = null;
    return;
  }
  const nowMs = Date.now();
  const shouldRefresh = (
    groundTrackNoradId !== noradId
    || nowMs - groundTrackFetchedAtMs > 60000
  );
  if (!shouldRefresh) {
    return;
  }
  groundTrackNoradId = noradId;
  groundTrackFetchedAtMs = nowMs;
  loadGroundTrack(noradId);
}

async function loadGroundTrack(noradId) {
  try {
    const response = await fetch(
      `/api/tracking/ground-track?norad_id=${encodeURIComponent(String(noradId))}&minutes_before=55&minutes_after=55&step_seconds=45`
    );
    const result = await response.json();
    if (!response.ok) {
      return;
    }
    if (Number(result.norad_id) !== Number(groundTrackNoradId)) {
      return;
    }
    groundTrackPoints = Array.isArray(result.points) ? result.points : [];
    drawMap();
  } catch (error) {
    // Keep prior track if refresh fails.
  }
}

function drawGroundTrack(ctx, width, height) {
  if (!Array.isArray(groundTrackPoints) || groundTrackPoints.length < 2) {
    return;
  }
  ctx.strokeStyle = '#2bb7ff';
  ctx.lineWidth = 1.8;
  ctx.globalAlpha = 0.95;
  ctx.beginPath();
  let started = false;
  let lastX = 0;
  for (const point of groundTrackPoints) {
    if (!hasValidCoordinate(point.latitude_deg) || !hasValidCoordinate(point.longitude_deg)) {
      continue;
    }
    const { x, y } = latLonToMapPoint(
      Number(point.latitude_deg),
      Number(point.longitude_deg),
      width,
      height
    );
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
      lastX = x;
      continue;
    }
    if (Math.abs(x - lastX) > width * 0.5) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
    lastX = x;
  }
  ctx.stroke();
  ctx.globalAlpha = 1;
}

function drawHomeMarker(ctx, width, height) {
  if (!hasValidCoordinate(stationLatitudeDeg) || !hasValidCoordinate(stationLongitudeDeg)) {
    return;
  }
  const { x, y } = latLonToMapPoint(
    Number(stationLatitudeDeg),
    Number(stationLongitudeDeg),
    width,
    height
  );
  ctx.fillStyle = '#59d66f';
  ctx.strokeStyle = 'rgba(6, 17, 26, 0.9)';
  ctx.lineWidth = 1.25;
  ctx.beginPath();
  ctx.arc(x, y, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
}

function drawMapLegend(ctx, width, height) {
  const activeLabel = (
    getCurrentMapTracking()?.satellite_name
    || document.getElementById('selected-satellite')?.textContent
    || ''
  ).trim();
  const showActiveLabel = activeLabel && activeLabel !== 'None';
  const legendWidth = showActiveLabel ? 248 : 174;
  ctx.fillStyle = 'rgba(4, 12, 19, 0.84)';
  ctx.fillRect(10, height - 20, legendWidth, 12);
  ctx.strokeStyle = '#2bb7ff';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(16, height - 14);
  ctx.lineTo(40, height - 14);
  ctx.stroke();
  ctx.fillStyle = '#d5e4f1';
  ctx.font = '10px Arial';
  ctx.fillText('GROUND TRACK', 44, height - 11);
  ctx.fillStyle = '#59d66f';
  ctx.beginPath();
  ctx.arc(135, height - 14, 3, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillText('HOME', 144, height - 11);
  if (showActiveLabel) {
    ctx.fillStyle = 'rgba(255, 88, 88, 0.28)';
    ctx.beginPath();
    ctx.arc(194, height - 14, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#ff5c5c';
    ctx.strokeStyle = 'rgba(255, 244, 244, 0.92)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(194, height - 14, 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = '#d5e4f1';
    ctx.fillText(activeLabel.toUpperCase(), 202, height - 11);
  }
}

function getActiveOrNextPass() {
  const nowMs = Date.now();
  const selectedNorad = Number(selectedSatelliteNorad || latestTracking?.norad_id);
  if (!selectedNorad || !Array.isArray(latestPasses) || !latestPasses.length) {
    return null;
  }
  const bySat = latestPasses
    .filter((entry) => Number(entry.norad_id) === selectedNorad)
    .sort((left, right) => new Date(left.aos_utc).getTime() - new Date(right.aos_utc).getTime());
  if (!bySat.length) {
    return null;
  }
  const active = bySat.find((entry) => {
    const aosMs = new Date(entry.aos_utc).getTime();
    const losMs = new Date(entry.los_utc).getTime();
    return nowMs >= aosMs && nowMs <= losMs;
  });
  return active || bySat[0];
}

function drawPassArc() {
  const canvas = document.getElementById('pass-arc-map');
  if (!canvas) {
    return;
  }
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = '#091320';
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = '#2d5872';
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, width - 1, height - 1);

  const pass = getActiveOrNextPass();
  if (!pass) {
    updateLiveTrackingTitle(null);
    ctx.fillStyle = '#b2c1cd';
    ctx.font = '12px Arial';
    ctx.fillText('No pass data available', 12, 24);
    return;
  }

  const baseY = height - 18;
  const startX = 18;
  const endX = width - 18;
  const peakX = (startX + endX) / 2;
  const peakHeight = Math.max(16, Math.min(56, Number(pass.max_elevation_deg) * 0.52));
  const peakY = baseY - peakHeight;

  ctx.strokeStyle = '#325f7f';
  ctx.beginPath();
  ctx.moveTo(startX, baseY);
  ctx.lineTo(endX, baseY);
  ctx.stroke();

  ctx.strokeStyle = '#2bb7ff';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(startX, baseY);
  ctx.quadraticCurveTo(peakX, peakY, endX, baseY);
  ctx.stroke();

  const nowMs = Date.now();
  const aosMs = new Date(pass.aos_utc).getTime();
  const losMs = new Date(pass.los_utc).getTime();
  const progress = Math.max(0, Math.min(1, (nowMs - aosMs) / Math.max(1, losMs - aosMs)));
  const satX = startX + (endX - startX) * progress;
  const satT = progress;
  const satY = ((1 - satT) * (1 - satT) * baseY) + (2 * (1 - satT) * satT * peakY) + (satT * satT * baseY);
  ctx.fillStyle = '#ffd45b';
  ctx.beginPath();
  ctx.arc(satX, satY, 4, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = '#5fd0ff';
  ctx.font = '10px Arial';
  ctx.textAlign = 'left';
  ctx.fillText('AOS', startX, 16);
  ctx.fillStyle = '#9ecbf1';
  ctx.fillText(formatTimeOnly(new Date(pass.aos_utc), false), startX, 28);
  ctx.fillStyle = '#5fd0ff';
  ctx.textAlign = 'center';
  ctx.fillText('MAX', peakX, 16);
  ctx.fillStyle = '#9ecbf1';
  ctx.fillText(`${Number(pass.max_elevation_deg).toFixed(0)} deg`, peakX, 28);
  ctx.fillStyle = '#ff7777';
  ctx.textAlign = 'right';
  ctx.fillText('LOS', endX, 16);
  ctx.fillStyle = '#ff9e9e';
  ctx.fillText(formatTimeOnly(new Date(pass.los_utc), false), endX, 28);

  const secondsToLos = Math.max(0, Math.floor((losMs - nowMs) / 1000));
  updateLiveTrackingTitle(nowMs >= aosMs && nowMs <= losMs ? secondsToLos : null);
  ctx.textAlign = 'center';
  ctx.font = '10px Arial';
  ctx.fillStyle = '#a4b9ca';
  ctx.fillText('W', startX, height - 6);
  ctx.fillText('N', peakX, height - 6);
  ctx.fillText('E', endX, height - 6);
}

function updateLiveTrackingTitle(secondsToLos) {
  const title = document.getElementById('live-tracking-title');
  if (!title) {
    return;
  }
  if (Number.isFinite(secondsToLos)) {
    title.textContent = `Live Tracking - LOS: ${formatCountdown(secondsToLos)}`;
    return;
  }
  title.textContent = 'Live Tracking';
}

function formatCountdown(seconds) {
  const totalSeconds = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;
  if (hours > 0) {
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(remainingSeconds).padStart(2, '0')}`;
  }
  return `${String(minutes).padStart(2, '0')}:${String(remainingSeconds).padStart(2, '0')}`;
}

function formatTimeOnly(date, includeSeconds = true) {
  return date.toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
    ...(includeSeconds ? { second: '2-digit' } : {}),
    timeZone: qthTimezone,
  });
}

function formatHz(value) {
  if (!Number.isFinite(Number(value))) {
    return '--';
  }
  return `${Number(value).toLocaleString()} Hz`;
}

function formatFrequencyRange(low, high) {
  if (!Number.isFinite(Number(low))) {
    return '--';
  }
  if (!Number.isFinite(Number(high)) || Number(low) === Number(high)) {
    return formatHz(low);
  }
  return `${formatHz(low)} to ${formatHz(high)}`;
}

function formatSignedHz(value) {
  if (!Number.isFinite(Number(value))) {
    return '--';
  }
  const number = Number(value);
  const sign = number > 0 ? '+' : '';
  return `${sign}${number.toLocaleString()} Hz`;
}

function formatCoordinate(value) {
  if (!Number.isFinite(Number(value))) {
    return '--';
  }
  return `${Number(value).toFixed(2)} deg`;
}

function formatNumber(value, digits, suffix) {
  if (!Number.isFinite(Number(value))) {
    return '--';
  }
  return `${Number(value).toFixed(digits)}${suffix}`;
}

function formatLocalDateTime(date) {
  return date.toLocaleString([], {
    month: 'numeric',
    day: 'numeric',
    year: '2-digit',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: qthTimezone,
  });
}

function formatDateOnlyForTimezone(date, timezone) {
  return date.toLocaleDateString([], {
    month: 'numeric',
    day: 'numeric',
    year: '2-digit',
    timeZone: timezone || qthTimezone,
  });
}

function formatDateTimeForTimezone(date, timezone) {
  return date.toLocaleString([], {
    month: 'numeric',
    day: 'numeric',
    year: '2-digit',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: timezone || qthTimezone,
  });
}

function formatLocalDateOnly(date) {
  return date.toLocaleDateString([], {
    month: 'numeric',
    day: 'numeric',
    year: '2-digit',
    timeZone: qthTimezone,
  });
}

function formatLocalTimeRange(start, end) {
  return formatTimeRangeForTimezone(start, end, qthTimezone);
}

function formatTimeRangeForTimezone(start, end, timezone) {
  const startParts = new Intl.DateTimeFormat('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    timeZone: timezone || qthTimezone,
  }).formatToParts(start);
  const endParts = new Intl.DateTimeFormat('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    timeZone: timezone || qthTimezone,
  }).formatToParts(end);

  const startTime = startParts.filter((part) => part.type !== 'dayPeriod').map((part) => part.value).join('').trim();
  const startMeridiem = startParts.find((part) => part.type === 'dayPeriod')?.value || '';
  const endTime = endParts.filter((part) => part.type !== 'dayPeriod').map((part) => part.value).join('').trim();
  const endMeridiem = endParts.find((part) => part.type === 'dayPeriod')?.value || '';
  if (startMeridiem && startMeridiem === endMeridiem) {
    return `${startTime} - ${endTime} ${endMeridiem}`;
  }
  return `${startTime} ${startMeridiem} - ${endTime} ${endMeridiem}`.trim();
}

function formatDisplayDateTime(value, timezone) {
  if (!value) {
    return '--';
  }
  return formatDateTimeForTimezone(new Date(value), timezone);
}

function formatAzimuthPath(pass) {
  const start = formatNumber(pass.start_azimuth_deg, 0, '');
  const middle = formatNumber(pass.middle_azimuth_deg, 0, '');
  const end = formatNumber(pass.end_azimuth_deg, 0, '');
  return `${start} - ${middle} - ${end}`;
}

function formatClockForQth(date) {
  return date.toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    timeZone: qthTimezone,
  });
}

function formatDuration(totalSeconds) {
  if (!Number.isFinite(Number(totalSeconds)) || Number(totalSeconds) < 0) {
    return '--';
  }
  const seconds = Math.floor(Number(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m ${remainder}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${remainder}s`;
  }
  return `${remainder}s`;
}

function bindConnectivityState() {
  ['rx', 'tx', 'rotator'].forEach((section) => {
    const connectivity = document.querySelector(`[name="${section}.connectivity"]`);
    if (connectivity) {
      connectivity.addEventListener('change', applyConnectivityState);
    }
  });
}

function applyConnectivityState() {
  ['rx', 'tx', 'rotator'].forEach((section) => {
    const connectivity = document.querySelector(`[name="${section}.connectivity"]`);
    if (!connectivity) {
      return;
    }
    const isLocal = connectivity.value === 'local';
    setSettingDisabled(section, 'host', isLocal);
    setSettingDisabled(section, 'port', isLocal);
    setSettingDisabled(section, 'serial_port', !isLocal);
    setSettingDisabled(section, 'baud', !isLocal);
    setSettingDisabled(section, 'model_id', !isLocal);
    if (section === 'rx' || section === 'tx') {
      setSettingDisabled(section, 'target_vfo', !isLocal);
    }
  });
}

function setSettingDisabled(section, key, disabled) {
  const element = document.querySelector(`[name="${section}.${key}"]`);
  if (!element) {
    return;
  }
  element.disabled = disabled;
  const row = element.closest('.settings-row');
  if (row) {
    row.classList.toggle('control-disabled', disabled);
  }
}

async function refreshSatellitePasses() {
  const status = document.getElementById('my-satellite-status');
  status.textContent = 'Refreshing satellite passes...';
  try {
    const response = await fetch('/api/passes/refresh', { method: 'POST' });
    const result = await response.json();
    if (!response.ok) {
      status.textContent = result.detail || 'Pass refresh failed.';
      return;
    }
    status.textContent = `Passes refreshed (${result.pass_count} total in 72h window).`;
    await loadPasses();
    await loadTracking();
    drawMap();
  } catch (error) {
    status.textContent = 'Pass refresh failed.';
  }
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

loadStatus();
document.getElementById('sync-rx-tx-toggle').checked = syncRxTx;
loadSdrFrequency();
loadRotator();
loadSettings();
loadMySatellites();
initializePassControls();
drawMap();
worldMapImage.addEventListener('load', () => {
  drawMap();
  drawTrackedSatellitesMap();
  drawQsoMap();
});
showPage(pageFromHash());
window.addEventListener('hashchange', () => showPage(pageFromHash()));
document.querySelectorAll('[data-page]').forEach((button) => {
  button.addEventListener('click', () => {
    window.location.hash = button.dataset.page;
  });
});
document
  .getElementById('rx-frequency-form')
  .addEventListener('submit', (event) => event.preventDefault());
document
  .getElementById('rx-control-toggle')
  .addEventListener('change', updateDeviceControl);
document
  .getElementById('tx-control-toggle')
  .addEventListener('change', updateDeviceControl);
document
  .getElementById('rotator-control-toggle')
  .addEventListener('change', updateDeviceControl);
document
  .getElementById('sync-rx-tx-toggle')
  .addEventListener('change', updateSyncMode);
document
  .querySelectorAll('[data-rx-step-khz]')
  .forEach((button) => button.addEventListener('click', stepSdrFrequency));
document
  .querySelectorAll('[data-tx-step-khz]')
  .forEach((button) => button.addEventListener('click', stepTxFrequency));
document
  .getElementById('reset-rx-offset')
  .addEventListener('click', () =>
    postTrackingAction('/api/tracking/rx/reset-offset', 'Resetting RX offset...')
  );
document
  .getElementById('settings-form')
  .addEventListener('submit', saveSettings);
document
  .getElementById('refresh-tle-btn')
  .addEventListener('click', refreshTleData);
document
  .getElementById('my-satellite-form')
  .addEventListener('submit', addMySatellite);
document
  .getElementById('my-satellite-options')
  .addEventListener('submit', saveMySatelliteOptions);
document
  .getElementById('qso-finder-form')
  .addEventListener('submit', searchQsoFinder);
document
  .getElementById('refresh-passes-btn')
  .addEventListener('click', refreshSatellitePasses);
document
  .getElementById('auto-track-toggle')
  .addEventListener('change', updateAutotrackSetting);
document
  .getElementById('monitor-rx-cat-debug')
  .addEventListener('change', updateMonitorDebug);
document
  .getElementById('monitor-tx-cat-debug')
  .addEventListener('change', updateMonitorDebug);
document
  .getElementById('monitor-rotator-cat-debug')
  .addEventListener('change', updateMonitorDebug);
document
  .getElementById('rotator-send-button')
  .addEventListener('click', sendManualRotatorPosition);
document
  .getElementById('rotator-home-button')
  .addEventListener('click', sendRotatorHome);
setInterval(loadSdrFrequency, 1000);
setInterval(loadTracking, 1000);
setInterval(loadRotator, 2000);
setInterval(loadPasses, 60000);
setInterval(checkAutotrackNextPass, 1000);
setInterval(drawMap, 10000);
setInterval(loadTrackedSatelliteLocations, 10000);
setInterval(() => {
  if (pageFromHash() === 'monitor') {
    refreshMonitorLogs();
  }
}, 5000);

async function initializePassControls() {
  await loadTracking();
  await loadSatellites();
  await loadPasses();
  await loadTrackedSatelliteLocations();
  rotatorControlEnabled = document.getElementById('rotator-control-toggle').checked;
}




