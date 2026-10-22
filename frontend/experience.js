/* The configured radio experience owns which interface and modules are loaded. */
(() => {
  'use strict';
  const nativeModules = new Set(['audio-stream', 'sstv-decoder', 'aprs', 'ft4-ft8']);
  const genericModules = new Set(['qso-finder', 'map']);
  const nativeSettings = new Set(['sstv', 'aprs', 'wsjtx']);
  let configured = null;
  let reloading = false;
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });

  function loadScript(source) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = source;
      script.onload = resolve;
      script.onerror = () => reject(new Error(`Could not load ${source}`));
      document.head.appendChild(script);
    });
  }

  function updateNavigation() {
    window.refreshExperienceNavigation?.();
  }

  async function initializeEnhanced() {
    try {
      for (const node of document.querySelectorAll('script[data-enhanced-src]')) {
        await loadScript(node.dataset.enhancedSrc);
      }
      updateNavigation();
      resolveReady(true);
    } catch (error) {
      const notice = document.createElement('p');
      notice.className = 'alert alert-danger';
      notice.setAttribute('role', 'alert');
      notice.textContent = 'The enhanced IC-9700 interface could not load. Reload Pi-Sat to retry.';
      document.querySelector('.app-shell')?.prepend(notice);
      resolveReady(false);
    }
  }

  const experience = {
    get enabled() { return configured === true; },
    get configured() { return configured; },
    ready,
    isPageAvailable(page) { return page !== 'radio' || this.enabled; },
    isModuleAvailable(id) { return genericModules.has(id) || (this.enabled && nativeModules.has(id)); },
    isSettingsSectionAvailable(section) { return !nativeSettings.has(section) || this.enabled; },
    applyStatus(enabled) {
      if (typeof enabled !== 'boolean') {
        resolveReady(false);
        return ready;
      }
      if (configured !== null) {
        if (configured !== enabled && !reloading) {
          reloading = true;
          // A saved experience change starts a fresh page so native audio,
          // sockets and module timers cannot remain attached to the other UI.
          window.location.reload();
        }
        return ready;
      }
      configured = enabled;
      if (enabled) void initializeEnhanced();
      else {
        updateNavigation();
        resolveReady(false);
      }
      return ready;
    },
  };
  window.PiSatExperience = experience;
})();
