/* NovaStar Monitor — Frontend Application
 *
 * Classic (non-module) script. Everything lives inside an IIFE so this file
 * shares no globals with wall_view.js — the two used to collide on tempColor()
 * and linkColor(), which silently broke the dashboard's stat colouring.
 *
 * Two rules hold throughout:
 *   1. Nothing reaches innerHTML without passing through esc(). Device names,
 *      firmware strings, MACs, model codes, serials and alert text all come
 *      from hardware or user input and none of it is trusted.
 *   2. No inline on* handlers. Every control carries data-action (plus
 *      data-device-id / data-error-id) and is handled by one delegated
 *      listener, so markup never has to embed executable strings.
 */
(function () {
  'use strict';

  // ── Constants ──
  const MAX_ALERTS = 500;
  const REST_FALLBACK_MS = 5000;
  // Mirrors DEFAULT_POLL_INTERVAL in device_manager.py (imported by app.py) —
  // the server owns this number. It moved from 10 to 30 when the per-cycle
  // per-card sweep was removed; showing 10 here told the operator the poller
  // was three times busier than it is.
  const DEFAULT_POLL_INTERVAL = 30;
  // Mirrors DEFAULT_VOLTAGE_MIN in app.py. It moved from 4.7 to 3.8: 4.7 is
  // above the entire healthy band this hardware reports (4.10–4.40 V), so it
  // raised a false low-voltage alert per card per cycle. app.py keeps 4.7 only
  // as LEGACY_VOLTAGE_MIN, to migrate old settings files away from it — this
  // form must never write it back.
  const DEFAULT_VOLTAGE_MIN = 3.8;
  const DEFAULT_TEMP_WARNING = 60;
  const DEFAULT_TEMP_CRITICAL = 75;
  // Re-check the emergency stop this often. The halt flag is process-global
  // and can be set from another browser tab, from the API, or by the server
  // itself, so the button must not rely on this tab having been the one to
  // flip it. This is a local request — it sends nothing to the hardware.
  const HALT_POLL_MS = 5000;

  // ── State ──
  let socket = null;
  let alerts = [];
  // Whether an error list has actually arrived. `alerts = []` is the same
  // value for "the server says there are no active errors" and "nothing has
  // told us anything yet" — and the second was rendering as a confident
  // "Active Errors 0" in success green on first paint, before the socket had
  // said a word, and again after a REST fallback whose fetch failed and was
  // swallowed. A green zero the server never sent is a false all-clear.
  let alertsLoaded = false;
  let appSettings = {};
  let restFallbackTimer = null;
  // null = not yet known. Distinguished from false so the button never claims
  // "contact is live" before the server has actually said so.
  let haltState = { halted: null, reason: null };
  let haltInFlight = false;
  const devices = {};
  const errorFilters = { severity: 'ALL', status: 'ALL' };
  const expandedDevices = new Set();

  // ── HTML escaping ──
  // wall_view.js carries its own copy on purpose: the two files are
  // independent IIFEs with no shared global surface between them.
  const ESC_CHARS = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(value) {
    if (value == null) return '';
    return String(value).replace(/[&<>"']/g, (ch) => ESC_CHARS[ch]);
  }

  // ── Connection banner ──
  function showBanner(text) {
    const el = document.getElementById('conn-banner');
    if (!el) return;
    el.textContent = text;
    el.classList.add('visible');
  }

  function hideBanner() {
    const el = document.getElementById('conn-banner');
    if (el) el.classList.remove('visible');
  }

  // ── Emergency stop ──
  // One switch that stops every packet this app sends to every device. The
  // monitor once held the controller's control channel busy and locked the
  // operator out of Bitfocus Companion during a show; the fix for that class
  // of problem has to be reachable in one click from anywhere in the UI, and
  // its state has to be obvious without reading anything.
  //
  // Rules encoded below:
  //   * Halting never asks for confirmation. Nobody hesitating over a modal is
  //     helped by it, and halting is always the safe direction.
  //   * Resuming always asks. That is the direction that puts traffic back on
  //     a live show network.
  //   * The server is the authority. Every path here renders what the response
  //     said, never what was requested — a failed POST must not leave the
  //     button claiming a state the backend never entered.
  function applyHaltState(halted, reason) {
    haltState = { halted, reason: reason || null };

    const btn = document.getElementById('halt-btn');
    const label = document.getElementById('halt-btn-label');
    const banner = document.getElementById('halt-banner');
    const bannerText = document.getElementById('halt-banner-text');

    // `=== true` on purpose: an unknown state (null) is not "running".
    const isHalted = halted === true;
    const unknown = halted == null;

    if (btn) {
      btn.classList.toggle('is-halted', isHalted);
      btn.classList.toggle('is-unknown', unknown);
      btn.setAttribute('aria-pressed', isHalted ? 'true' : 'false');
      btn.title = isHalted
        ? 'Device contact is halted. Click to resume sending to devices.'
        : unknown
          ? 'Contact state unknown — the monitor service has not answered yet.'
          : 'Stop all contact with every device immediately';
    }
    if (label) {
      label.textContent = isHalted ? 'CONTACT HALTED — RESUME'
        : unknown ? 'STOP CONTACT' : 'STOP CONTACT';
    }
    if (banner) banner.classList.toggle('visible', isHalted);
    if (bannerText) {
      bannerText.textContent = isHalted
        ? (haltState.reason
          ? `No packets are being sent to any device — ${haltState.reason}. Everything below is frozen at the last reading.`
          : 'No packets are being sent to any device. Everything below is frozen at the last reading.')
        : '';
    }

    // Body-level flag so the whole page can read as halted (and so wall_view.js
    // — a separate IIFE with no shared globals — can see it without duplicating
    // the fetch). Paired with an event for the same reason `nsm:tabchange`
    // exists: a DOM signal is the two files' only common ground.
    document.body.classList.toggle('contact-halted', isHalted);
    document.dispatchEvent(new CustomEvent('nsm:halt', {
      detail: { halted: isHalted, unknown, reason: haltState.reason },
    }));
  }

  function readHaltResponse(data) {
    if (!data || typeof data !== 'object') return;
    applyHaltState(data.halted === true, typeof data.reason === 'string' ? data.reason : null);
  }

  // State on load, not just after a click: an operator arriving at an already
  // halted app must see that immediately rather than reading frozen numbers.
  function fetchHaltState() {
    if (document.hidden) return;
    fetch('/api/halt')
      .then((r) => (r.ok ? r.json() : null))
      .then(readHaltResponse)
      // A failed check leaves the last known state alone. Guessing "running"
      // here would paint a halted app as live.
      .catch(() => {});
  }

  function setHalt(halted, reason) {
    if (haltInFlight) return;
    haltInFlight = true;
    const btn = document.getElementById('halt-btn');
    if (btn) btn.classList.add('is-busy');

    fetch('/api/halt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ halted, reason }),
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status))))
      .then((data) => {
        readHaltResponse(data);
        renderAll();
      })
      .catch((err) => {
        alert((halted ? 'Failed to halt device contact: ' : 'Failed to resume device contact: ')
          + err + '\n\nThe monitor service may be unreachable. If devices must stop being '
          + 'contacted right now, stop the monitor service itself.');
        // Re-read rather than assume: the request may have landed before the
        // response was lost.
        fetchHaltState();
      })
      .finally(() => {
        haltInFlight = false;
        if (btn) btn.classList.remove('is-busy');
      });
  }

  function toggleHalt() {
    if (haltState.halted === true) {
      resumeContact();
      return;
    }
    // No confirm on the way down — see the note above.
    setHalt(true, 'Stopped from the dashboard');
  }

  function resumeContact() {
    if (!confirm('Resume contact with all devices?\n\n'
      + 'The monitor will start sending to the controller again. Do not do this '
      + 'while the wall is being driven from another control surface that the '
      + 'monitor is interfering with.')) return;
    setHalt(false, null);
  }

  // ── SocketIO ──
  function initSocket() {
    if (typeof io === 'undefined') {
      // The socket.io client is loaded from a CDN (see the note in
      // index.html); LED walls frequently run on isolated show networks with
      // no internet, where that script never arrives. Degrade to REST polling
      // instead of leaving the whole dashboard dead.
      showBanner('Realtime updates unavailable — refreshing over HTTP instead.');
      startRestFallback();
      return;
    }

    socket = io();

    socket.on('connect', () => {
      document.getElementById('topbar-info').textContent = 'Connected';
      document.getElementById('pulse-dot').classList.remove('offline');
      hideBanner();
    });

    socket.on('disconnect', () => {
      document.getElementById('topbar-info').textContent = 'Disconnected';
      document.getElementById('pulse-dot').classList.add('offline');
      showBanner('Lost connection to the monitor service — data below may be stale.');
    });

    socket.on('full_state', (data) => {
      if (data.devices) {
        data.devices.forEach((d) => { devices[d.device_id] = d; });
      }
      if (data.settings) {
        appSettings = data.settings;
        populateSettings();
      }
      // `if (data.errors)` also accepted a non-array and, more importantly,
      // left the count claiming zero when the key was absent entirely.
      if (Array.isArray(data.errors)) {
        alerts = data.errors;
        alertsLoaded = true;
      }
      renderAll();
    });

    socket.on('device_update', (data) => {
      if (data.device_id && data.state) {
        devices[data.device_id] = data.state;
        renderAll();
      }
    });

    // Per-card read progress. app.js owns the socket; wall_view.js renders
    // the bar. Re-emitted on the document so the two files keep sharing no
    // globals — same arrangement as the halt state.
    socket.on('read_progress', (info) => {
      document.dispatchEvent(new CustomEvent('nsm:read-progress',
                                             { detail: info }));
    });

    socket.on('alert', (entry) => {
      // Insert at beginning (newest first)
      alerts.unshift(entry);
      if (alerts.length > MAX_ALERTS) alerts.length = MAX_ALERTS;
      renderErrors();
      updateErrorBadge();
    });

    socket.on('error_resolved', (data) => {
      const entry = alerts.find((a) => a.id === data.id);
      if (entry) {
        entry.resolved = true;
        entry.resolved_at = new Date().toISOString();
        renderErrors();
        updateErrorBadge();
      }
    });
  }

  // Offline fallback: the same state the socket would have pushed, pulled from
  // the REST endpoints that already exist. Paused while the tab is hidden.
  function startRestFallback() {
    if (restFallbackTimer) return;

    const pull = () => {
      if (document.hidden) return;
      fetch('/api/devices')
        .then((r) => r.json())
        .then((list) => {
          if (Array.isArray(list)) list.forEach((d) => { devices[d.device_id] = d; });
          return fetch('/api/errors?limit=' + MAX_ALERTS).then((r) => r.json());
        })
        .then((list) => {
          if (Array.isArray(list)) { alerts = list; alertsLoaded = true; }
          renderAll();
        })
        // A swallowed failure must not leave a zero looking like an answer:
        // `alertsLoaded` stays false and the count renders as unknown.
        .catch(() => { /* keep the last known state on screen */ });
    };

    fetch('/api/settings')
      .then((r) => r.json())
      .then((s) => { appSettings = s || {}; populateSettings(); })
      .catch(() => {});

    pull();
    restFallbackTimer = setInterval(pull, REST_FALLBACK_MS);
  }

  // ── Tabs ──
  function initTabs() {
    const tabs = document.getElementById('tabs');
    if (!tabs) return;
    tabs.addEventListener('click', (evt) => {
      const tab = evt.target.closest('.tab');
      if (!tab || !tabs.contains(tab)) return;
      activateTab(tab.dataset.tab);
    });
    activateTab(document.querySelector('.tab.active')?.dataset.tab || 'dashboard');
  }

  function activateTab(name) {
    document.querySelectorAll('.tab').forEach((t) => {
      t.classList.toggle('active', t.dataset.tab === name);
    });
    document.querySelectorAll('.tab-content').forEach((c) => {
      c.classList.toggle('active', c.id === 'tab-' + name);
    });

    // The Wall View carries its own stats bar. Showing the dashboard grid
    // above it duplicated the same figures under different names.
    const grid = document.getElementById('stats-grid');
    if (grid) grid.classList.toggle('hidden', name === 'wall');

    // Charts can only measure themselves once their tab is displayed.
    if (name === 'dashboard') redrawExpandedCharts();

    document.dispatchEvent(new CustomEvent('nsm:tabchange', { detail: { tab: name } }));
  }

  function initErrorFilters() {
    [['severity-filter', 'severity'], ['status-filter', 'status']].forEach(([id, key]) => {
      const group = document.getElementById(id);
      if (!group) return;
      group.addEventListener('click', (evt) => {
        const pill = evt.target.closest('.pill');
        if (!pill || !group.contains(pill)) return;
        group.querySelectorAll('.pill').forEach((p) => p.classList.toggle('active', p === pill));
        errorFilters[key] = pill.dataset.val;
        renderErrors();
      });
    });
  }

  function initAddDeviceTypePills() {
    const filter = document.getElementById('add-type-filter');
    if (!filter) return;
    filter.addEventListener('click', (evt) => {
      const pill = evt.target.closest('.pill');
      if (!pill || !filter.contains(pill)) return;
      filter.querySelectorAll('.pill').forEach((p) => p.classList.toggle('active', p === pill));
      if (pill.dataset.port) document.getElementById('add-port').value = pill.dataset.port;
    });
  }

  // ── Render Everything ──
  function renderAll() {
    renderStats();
    renderDevices();
    renderErrors();
    renderSettingsDevices();
    updateTopbar();
    updateErrorBadge();
  }

  function updateTopbar() {
    const devList = Object.values(devices);
    const online = devList.filter((d) => d.connected).length;
    const total = devList.length;
    const info = document.getElementById('topbar-info');
    info.textContent = total > 0
      ? `${online}/${total} device${total !== 1 ? 's' : ''} online`
      : 'No devices configured';
  }

  function updateErrorBadge() {
    const activeCount = alerts.filter((a) => !a.resolved).length;
    const badge = document.getElementById('error-badge');
    const countEl = document.getElementById('active-error-count');
    if (activeCount > 0) {
      badge.textContent = activeCount;
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }
    // A count of zero is only news once a list has arrived. Until then say so
    // — but never hide a non-zero count behind "unknown", because an error
    // that did arrive is a fact regardless of whether the rest of the log did.
    if (countEl) {
      countEl.textContent = (alertsLoaded || activeCount > 0)
        ? `${activeCount} active`
        : '— active';
      countEl.title = (alertsLoaded || activeCount > 0)
        ? ''
        : 'No error list has been received yet, so the number of active errors '
          + 'is unknown — not zero.';
    }
  }

  // ── Stats Bar ──
  function renderStats() {
    const grid = document.getElementById('stats-grid');
    if (!grid) return;
    const devList = Object.values(devices);

    if (devList.length === 0) {
      grid.innerHTML = statCard('Status', '—', 'color-muted');
      return;
    }

    const online = devList.filter((d) => d.connected).length;
    const total = devList.length;
    // null until some device actually reports a count. Starting at 0 made
  // "no device has told us anything" render as a confident "Cards Online: 0"
  // on a fully lit wall.
  let cardsOnline = null;
    let tempSum = 0;
    let tempCount = 0;
    let peakTemp = null;
    let brightSum = 0;
    let brightCount = 0;
    // How many cards the temperature aggregates were actually computed over.
    // The server publishes this pair alongside them precisely because a mean
    // over 22 of 286 cards is not the wall's temperature — see
    // `_update_aggregates` in device_manager.py. The Wall View has carried a
    // card-data-age badge for a while; this bar carried nothing at all, so its
    // "Avg Temp 39.7°" read as the whole wall with no way to tell otherwise.
    let covRead = 0;
    let covTotal = 0;
    let covKnown = false;

    devList.forEach((d) => {
      const lm = d.live_monitoring || {};
      if (lm.coverage_read != null && lm.coverage_total != null) {
        covRead += lm.coverage_read;
        covTotal += lm.coverage_total;
        covKnown = true;
      }
      // `!= null` throughout: a genuine 0 (0 °C, 0 % blackout brightness,
      // 0 cards online) is information, not a missing reading.
      if (lm.card_count != null) cardsOnline = (cardsOnline || 0) + lm.card_count;
      if (lm.temperature_c != null) { tempSum += lm.temperature_c; tempCount++; }
      // A true per-card peak. The server already computes it; taking the max
      // over per-device *averages* (the old behaviour) hid single hot cards.
      const devPeak = lm.temperature_max_c != null ? lm.temperature_max_c : lm.temperature_c;
      if (devPeak != null && (peakTemp == null || devPeak > peakTemp)) peakTemp = devPeak;
      if (d.brightness_pct != null) { brightSum += d.brightness_pct; brightCount++; }
    });

    const avgTemp = tempCount > 0 ? tempSum / tempCount : null;
    const avgBright = brightCount > 0 ? brightSum / brightCount : null;
    const activeErrors = alerts.filter((a) => !a.resolved).length;

    // Only a partial read needs saying. Full coverage, or no coverage figures
    // at all, leaves the tile as it was rather than adding noise.
    const covNote = covKnown && covTotal > 0 && covRead < covTotal
      ? `${covRead} of ${covTotal} cards read` : null;
    const covTitle = covNote
      ? `This is an average over ${covRead} of ${covTotal} cards — the rest `
        + 'were never read, or did not answer, and are not in it.'
      : '';

    grid.innerHTML = [
      statCard('Devices', `${online}/${total}`, online === total ? 'color-success' : 'color-warning'),
      statCard('Cards Online', cardsOnline != null ? String(cardsOnline) : '—',
             cardsOnline != null ? 'color-primary' : 'color-muted'),
      statCard('Avg Temp', avgTemp != null ? avgTemp.toFixed(1) + '°' : '—', tempClass(avgTemp),
             covNote, covTitle),
      statCard('Peak Temp', peakTemp != null ? peakTemp.toFixed(1) + '°' : '—', tempClass(peakTemp),
             covNote, covNote
               ? `The hottest of ${covRead} of ${covTotal} cards. A hotter card `
                 + 'among the unread ones would not appear here.'
               : ''),
      statCard('Avg Brightness', avgBright != null ? avgBright.toFixed(0) + '%' : '—', 'color-warning'),
      // Muted "—" until an error list has actually arrived: a green zero that
      // no server ever sent is the false all-clear this dashboard exists to
      // avoid. A count that IS known still reads green at zero.
      alertsLoaded || activeErrors > 0
        ? statCard('Active Errors', String(activeErrors),
                   activeErrors > 0 ? 'color-danger' : 'color-success')
        : statCard('Active Errors', '—', 'color-muted', null,
                   'No error list has been received from the monitor service yet, '
                   + 'so the number of active errors is unknown — not zero.'),
    ].join('');
  }

  function statCard(label, value, cls, note, title) {
    return `<div class="stat-card"${title ? ` title="${esc(title)}"` : ''}>
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value ${cls || ''}">${esc(value)}</div>
      ${note ? `<div class="stat-note">${esc(note)}</div>` : ''}
    </div>`;
  }

  // ── Device Panels ──
  function renderDevices() {
    const container = document.getElementById('devices-container');
    const devList = Object.values(devices);

    if (devList.length === 0) {
      container.innerHTML = `<div class="section empty-state">
        <div class="empty-state-icon">⊕</div>
        <div class="empty-state-title">No devices configured</div>
        <div class="empty-state-body">
          Add your first NovaStar controller to begin monitoring.<br>
          Or enable <strong>Simulation Mode</strong> in Settings to explore the UI without hardware.
        </div>
        <button class="btn btn-primary" style="margin-top:14px;" data-action="add-device-show">+ Add Device</button>
      </div>`;
      return;
    }

    container.innerHTML = devList.map(renderDevicePanel).join('');

    // Restore expanded state and draw charts
    container.querySelectorAll('.device-panel').forEach((panel) => {
      if (!expandedDevices.has(panel.dataset.deviceId)) return;
      setExpanded(panel, true);
    });
    redrawExpandedCharts();
  }

  function renderDevicePanel(dev) {
    const lm = dev.live_monitoring || {};
    const si = dev.system_info || {};
    const di = dev.device_info || {};
    const cards = dev.receiving_cards || [];
    const connected = dev.connected;
    const id = esc(dev.device_id);
    // No unknown arm here meant every state that is not PRIMARY/BACKUP —
    // including UNKNOWN, which a healthy H-series chain reports — rendered
    // in danger red. A fault colour taken from an unrecognised value.
    const linkCls = lm.link_status === 'PRIMARY' ? 'success'
      : lm.link_status === 'BACKUP' ? 'backup'
        : lm.link_status === 'DISCONNECTED' ? 'danger'
          : 'muted';

    return `
      <div class="device-panel ${connected ? '' : 'offline'}" data-device-id="${id}">
        <div class="device-header" data-action="device-toggle" data-device-id="${id}">
          <div class="status-dot" style="background:var(--${connected ? 'success' : 'danger'});width:10px;height:10px;border-radius:50%;flex-shrink:0;"></div>
          <div style="flex:1">
            <div class="device-name">${esc(dev.name || dev.ip)}</div>
            <div class="device-meta">${esc(dev.ip)} • Port ${esc(dev.port != null ? dev.port : 5200)} • FW ${esc(dev.firmware_version || '—')}</div>
          </div>
          ${healthHeaderBadges(dev)}
          ${lm.card_count != null ? `<span class="badge badge-info">${esc(lm.card_count)} cards</span>` : ''}
          ${lm.link_status ? `<span class="badge badge-${linkCls}">${esc(lm.link_status)}</span>` : ''}
          ${dev.brightness_pct != null ? `<span style="font-size:12px;color:var(--text-muted);">☀ ${esc(dev.brightness_pct)}%</span>` : ''}
          <button class="btn btn-sm btn-danger" data-action="device-remove" data-device-id="${id}" title="Remove device">✕</button>
          <span class="expand-arrow">▾</span>
        </div>
        <div class="device-body">
          <div class="two-col">
            <div>
              ${renderHealthSection(dev)}
              <div class="section" style="margin-bottom:10px;">
                <div class="section-title">📡 Live Monitoring</div>
                <div class="info-grid">
                  ${infoItem('Temperature', lm.temperature_c != null ? lm.temperature_c.toFixed(1) + '°C' : '—', tempClass(lm.temperature_c))}
                  ${infoItem('Peak Card Temp', lm.temperature_max_c != null ? lm.temperature_max_c.toFixed(1) + '°C' : '—', tempClass(lm.temperature_max_c))}
                  ${infoItem('Voltage', lm.voltage_v != null ? lm.voltage_v + 'V' : '—', 'color-cyan')}
                  ${infoItem('Link', lm.link_status || '—', 'color-' + linkCls)}
                  ${infoItem('Card Count', lm.card_count != null ? lm.card_count : '—')}
                  ${infoItem('RC Firmware', lm.firmware || '—')}
                  ${infoItem('MAC Address', lm.mac_address || '—')}
                  ${infoItem('Brightness', dev.brightness_pct != null
                    ? `${dev.brightness_pct}% (${dev.brightness != null ? dev.brightness : '?'}/255)`
                    : '—')}
                  ${infoItem('Gamma', dev.gamma != null ? dev.gamma : '—')}
                </div>
              </div>
              <div class="section" style="margin-bottom:10px;">
                <div class="section-title">🖥 Device Info</div>
                <div class="info-grid">
                  ${infoItem('Model Code', di.model_code || si.device_type || '—')}
                  ${infoItem('Serial', di.serial || '—')}
                  ${infoItem('HW Revision', di.hw_revision || '—')}
                  ${infoItem('Build Date', si.build_date || '—')}
                  ${infoItem('Ethernet Ports', si.ethernet_ports != null ? si.ethernet_ports : '—')}
                  ${infoItem('Input Count', si.input_count != null ? si.input_count : '—')}
                  ${infoItem('Port 2 (Redundancy)', dev.port2_active ? '● Active' : '○ Standby')}
                  ${infoItem('Controller Time', dev.datetime || '—')}
                </div>
              </div>
            </div>
            <div>
              <div class="section" style="margin-bottom:10px;">
                <div class="section-title">🌡 Temperature History</div>
                <div class="chart-container"><canvas data-chart="temp"></canvas></div>
              </div>
              <div class="section" style="margin-bottom:10px;">
                <div class="section-title">⚡ Voltage History</div>
                <div class="chart-container"><canvas data-chart="volt"></canvas></div>
              </div>
            </div>
          </div>
          ${cards.length > 0 ? `
          <div class="section">
            <div class="section-title">📦 Receiving Cards <span class="badge badge-info">${cards.length} total</span>
              ${cardsReadBadge(dev)}
            </div>
            <p style="font-size:11px;color:var(--text-muted);margin:-2px 0 8px;">
              Per-card temperature and voltage are read on demand, not on the poll cycle.
              Use the Wall View to request a fresh read.
            </p>
            <div class="card-grid">
              ${cards.map((c) => `
                <div class="card-tile ${cardTileClass(c)}" title="${esc(cardTileTitle(c))}">
                  <div class="card-id">${esc(cardLabel(c))}</div>
                  <div class="card-temp ${tempClass(c.temperature_c)}">${c.temperature_c != null ? esc(c.temperature_c.toFixed(1)) + '°' : '—'}</div>
                  <div class="card-status">
                    <span class="status-dot" style="background:var(--${cardDotColour(c)})"></span>
                    ${cardStateLabel(c)}
                  </div>
                  ${bitErrorText(c) != null
                    ? `<div class="card-status ${bitErrorClass(c)}" style="margin-top:2px;">${esc(bitErrorText(c))}</div>`
                    : ''}
                </div>
              `).join('')}
            </div>
          </div>` : ''}
        </div>
      </div>
    `;
  }

  function panelFor(deviceId) {
    return Array.from(document.querySelectorAll('.device-panel'))
      .find((p) => p.dataset.deviceId === deviceId) || null;
  }

  function setExpanded(panel, expanded) {
    panel.querySelector('.device-body')?.classList.toggle('expanded', expanded);
    panel.querySelector('.expand-arrow')?.classList.toggle('expanded', expanded);
    panel.querySelector('.device-header')?.classList.toggle('expanded', expanded);
  }

  function redrawExpandedCharts() {
    requestAnimationFrame(() => {
      expandedDevices.forEach((id) => {
        const panel = panelFor(id);
        if (panel) drawDeviceCharts(panel, devices[id]);
      });
    });
  }

  function drawDeviceCharts(panel, dev) {
    if (!dev || !dev.history) return;
    const h = dev.history;
    drawChart(panel.querySelector('[data-chart="temp"]'), h.timestamps, h.temperature, '#22d3ee', '°C', 40, 80);
    // 3.5-4.8 V, not 4.5-5.5. Receiving cards run around 4.2 V — the old
    // window was chosen when a wrong formula (raw * 0.03 instead of the
    // documented (raw & 0x7F) * 0.1) made them read ~5.1 V. drawChart expands
    // to fit so nothing was clipped, but real data sat squashed into the
    // bottom of the plot, which is where a slow sag would be least visible.
    drawChart(panel.querySelector('[data-chart="volt"]'), h.timestamps, h.voltage, '#fbbf24', 'V', 3.5, 4.8);
  }

  // ── Error Log ──
  function renderErrors() {
    const log = document.getElementById('error-log');

    let filtered = alerts;
    if (errorFilters.severity !== 'ALL') {
      filtered = filtered.filter((a) => a.severity === errorFilters.severity);
    }
    if (errorFilters.status === 'ACTIVE') {
      filtered = filtered.filter((a) => !a.resolved);
    } else if (errorFilters.status === 'RESOLVED') {
      filtered = filtered.filter((a) => a.resolved);
    }

    if (filtered.length === 0) {
      log.innerHTML = '<p class="color-muted" style="padding:20px;text-align:center;">No matching errors</p>';
      return;
    }

    log.innerHTML = filtered.map((a) => {
      const sev = a.severity === 'CRITICAL' ? 'danger'
        : a.severity === 'WARNING' ? 'warning'
          : 'primary';
      const ts = a.timestamp ? new Date(a.timestamp).toLocaleString() : '?';
      const id = esc(a.id);

      const scope = [a.device || '?', a.cabinet, a.port != null ? 'Port ' + a.port : null]
        .filter(Boolean).map(esc).join(' • ');

      return `
        <div class="error-entry${a.resolved ? ' error-resolved' : ''}">
          <span class="error-severity sev-${sev}">${esc(a.severity)}</span>
          <div style="flex:1">
            <div class="error-device">${scope}</div>
            <div class="error-msg">${esc(a.message)}</div>
            ${a.resolved ? `<div style="font-size:10px;color:var(--success);margin-top:2px;">Resolved ${esc(a.resolved_at ? new Date(a.resolved_at).toLocaleString() : '')}</div>` : ''}
            ${!a.resolved ? `<div class="error-actions">
              <button class="btn btn-sm" data-action="error-resolve" data-error-id="${id}">Resolve</button>
              ${!a.acknowledged
                ? `<button class="btn btn-sm" data-action="error-ack" data-error-id="${id}">Acknowledge</button>`
                : '<span style="font-size:10px;color:var(--text-muted);">Acknowledged</span>'}
            </div>` : ''}
          </div>
          <span class="error-time">${esc(ts)}</span>
        </div>
      `;
    }).join('');
  }

  function resolveError(id) {
    if (!Number.isFinite(id)) return;
    fetch(`/api/errors/${id}/resolve`, { method: 'POST' })
      .then((r) => r.json())
      .then(() => {
        const entry = alerts.find((a) => a.id === id);
        if (entry) { entry.resolved = true; entry.resolved_at = new Date().toISOString(); }
        renderErrors();
        updateErrorBadge();
      });
  }

  function acknowledgeError(id) {
    if (!Number.isFinite(id)) return;
    fetch(`/api/errors/${id}/acknowledge`, { method: 'POST' })
      .then((r) => r.json())
      .then(() => {
        const entry = alerts.find((a) => a.id === id);
        if (entry) entry.acknowledged = true;
        renderErrors();
      });
  }

  function clearResolved() {
    if (!confirm('Remove all resolved errors from the log?')) return;
    fetch('/api/errors/clear-resolved', { method: 'POST' })
      .then((r) => r.json())
      .then(() => {
        alerts = alerts.filter((a) => !a.resolved);
        renderErrors();
        updateErrorBadge();
      });
  }

  // ── Settings ──
  function populateSettings() {
    const s = appSettings;
    const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
    setVal('set-temp-warn', s.temp_warning != null ? s.temp_warning : DEFAULT_TEMP_WARNING);
    setVal('set-temp-crit', s.temp_critical != null ? s.temp_critical : DEFAULT_TEMP_CRITICAL);
    setVal('set-volt-min', s.voltage_min != null ? s.voltage_min : DEFAULT_VOLTAGE_MIN);
    setVal('set-poll-int', s.poll_interval != null ? s.poll_interval : DEFAULT_POLL_INTERVAL);
  }

  function loadVersion() {
    fetch('/api/version').then((r) => r.json()).then((d) => {
      const el = document.getElementById('app-version');
      if (el) el.textContent = d.version || '—';
    }).catch(() => {});
  }

  // `parseFloat(x) || fallback` turned a typed 0 into the fallback, and 0 is a
  // legal value for every one of these (the server clamps into its own range).
  // Only a genuinely unparseable field falls back.
  function fieldNumber(id, fallback) {
    const el = document.getElementById(id);
    const value = parseFloat(el ? el.value : '');
    return Number.isFinite(value) ? value : fallback;
  }

  function saveSettings() {
    const data = {
      temp_warning: fieldNumber('set-temp-warn', DEFAULT_TEMP_WARNING),
      temp_critical: fieldNumber('set-temp-crit', DEFAULT_TEMP_CRITICAL),
      voltage_min: fieldNumber('set-volt-min', DEFAULT_VOLTAGE_MIN),
      poll_interval: fieldNumber('set-poll-int', DEFAULT_POLL_INTERVAL),
    };

    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    })
      .then((r) => r.json())
      .then((resp) => {
        // A rejected save still resolves as JSON — reporting "Saved" for it
        // would leave the operator believing a threshold took effect.
        if (resp && resp.error) { alert('Failed to save: ' + resp.error); return; }
        // The server clamps values into its own valid ranges and echoes the
        // saved settings back, so trust that over what was typed — otherwise
        // the form keeps showing a number the server never accepted.
        appSettings = (resp && resp.settings) ? resp.settings : { ...appSettings, ...data };
        populateSettings();
        const msg = document.getElementById('settings-saved');
        msg.style.display = 'inline';
        setTimeout(() => { msg.style.display = 'none'; }, 2000);
        renderAll();  // thresholds changed — recolour temperatures
      })
      .catch((err) => alert('Failed to save: ' + err));
  }

  // ── Device Management ──
  function toggleDevice(deviceId) {
    if (deviceId == null) return;
    const panel = panelFor(deviceId);
    if (!panel) return;

    const expanded = !expandedDevices.has(deviceId);
    if (expanded) expandedDevices.add(deviceId);
    else expandedDevices.delete(deviceId);

    setExpanded(panel, expanded);
    if (expanded) redrawExpandedCharts();
  }

  function showAddDevice() {
    document.getElementById('add-device-modal').classList.remove('hidden');
    document.getElementById('add-ip').focus();
  }

  function hideAddDevice() {
    document.getElementById('add-device-modal').classList.add('hidden');
  }

  function addDevice() {
    const name = document.getElementById('add-name').value.trim();
    const ip = document.getElementById('add-ip').value.trim();
    const port = parseInt(document.getElementById('add-port').value, 10) || 5203;
    const type = document.querySelector('#add-type-filter .pill.active')?.dataset.type;

    if (!ip) { alert('IP address is required'); return; }

    // Sensible default name based on selected type
    const defaultName = type === 'h_series' ? `H-series ${ip}`
      : type === 'vx1000' ? `VX1000 ${ip}`
        : `NovaStar ${ip}`;

    fetch('/api/devices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name || defaultName, ip, port }),
    })
      .then((r) => r.json())
      .then((data) => {
        if (data.error) { alert(data.error); return; }
        hideAddDevice();
        document.getElementById('add-name').value = '';
        document.getElementById('add-ip').value = '';
      })
      .catch((err) => alert('Failed to add device: ' + err));
  }

  function removeDevice(deviceId) {
    if (deviceId == null) return;
    if (!confirm('Remove this device from monitoring?')) return;
    fetch(`/api/devices/${encodeURIComponent(deviceId)}`, { method: 'DELETE' })
      .then((r) => r.json())
      .then(() => {
        delete devices[deviceId];
        expandedDevices.delete(deviceId);
        renderAll();
      })
      .catch((err) => alert('Failed: ' + err));
  }

  // ── Settings: configured devices list ──
  function renderSettingsDevices() {
    const list = document.getElementById('settings-devices-list');
    const count = document.getElementById('settings-device-count');
    const devList = Object.values(devices);
    if (count) count.textContent = String(devList.length);
    if (!list) return;

    if (devList.length === 0) {
      list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);text-align:center;padding:18px 0;">No devices configured.</p>';
      return;
    }

    list.innerHTML = devList.map((d) => {
      const status = d.connected
        ? '<span class="badge badge-success">connected</span>'
        : '<span class="badge badge-danger">offline</span>';
      const lastPoll = d.last_poll
        ? `<span style="font-size:11px;color:var(--text-muted);">last: ${esc(d.last_poll)}</span>`
        : '';
      return `<div style="display:flex;align-items:center;gap:12px;padding:10px 12px;border:1px solid var(--border-subtle);border-radius:8px;margin-bottom:6px;background:var(--bg-base);">
        <div style="flex:1;">
          <div style="font-weight:600;font-size:13px;">${esc(d.name || d.device_id)}</div>
          <div style="font-size:11px;color:var(--text-muted);">${esc(d.ip)}:${esc(d.port != null ? d.port : '?')} · ${esc(d.device_type || 'unknown')}</div>
        </div>
        ${status}
        ${lastPoll}
        <button class="btn btn-sm btn-danger" data-action="device-remove" data-device-id="${esc(d.device_id)}">Remove</button>
      </div>`;
    }).join('');
  }

  // ── Chart Drawing ──
  // history.timestamps / .temperature / .voltage are index-aligned: the server
  // writes exactly one entry per series per poll cycle, using an explicit null
  // where that reading was missing. So everything here is positional —
  // compacting the values out (the old behaviour) would slide every later
  // sample onto the wrong timestamp.
  //
  // A null is a hole in the monitoring record, and the line breaks across it.
  // Joining two samples either side of a gap would draw a straight segment
  // implying a measurement nobody took.
  function drawChart(canvas, labels, data, color, unit, yMin, yMax) {
    if (!canvas) return;

    const rect = canvas.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;  // tab still hidden

    // null = no reading at this index. Non-numbers (undefined from a short
    // array, NaN from a bad parse) are holes too, not zeroes.
    const points = (Array.isArray(data) ? data : [])
      .map((v) => (typeof v === 'number' && Number.isFinite(v) ? v : null));
    const stamps = Array.isArray(labels) ? labels : [];

    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const w = rect.width;
    const h = rect.height;
    const pad = { top: 8, right: 8, bottom: 18, left: 36 };
    const plotW = w - pad.left - pad.right;
    const plotH = h - pad.top - pad.bottom;

    ctx.clearRect(0, 0, w, h);
    if (plotW <= 0 || plotH <= 0) return;

    const readings = points.filter((v) => v !== null);
    if (readings.length === 0) {
      // All-null series (or nothing recorded yet). Say so — a blank panel
      // reads as "chart broken", which is a different problem.
      ctx.fillStyle = '#5a6478';
      ctx.font = '10px Inter, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('No readings recorded', w / 2, h / 2);
      return;
    }

    const dMin = Math.min(...readings);
    const dMax = Math.max(...readings);
    const yLo = yMin != null ? Math.min(yMin, dMin - 1) : dMin - 1;
    const yHi = yMax != null ? Math.max(yMax, dMax + 1) : dMax + 1;
    const ySpan = yHi - yLo || 1;

    // Grid
    ctx.strokeStyle = '#1d2230';
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
      const y = pad.top + (plotH / 4) * i;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(w - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = '#5a6478';
      ctx.font = '9px Inter, sans-serif';
      ctx.textAlign = 'right';
      ctx.fillText((yHi - ySpan * (i / 4)).toFixed(1), pad.left - 4, y + 3);
    }

    // x is the sample's index in the full series, gaps included, so leading
    // and trailing nulls leave their space empty instead of stretching the
    // remaining points across the whole plot.
    const lastIdx = points.length - 1;
    const xAt = (i) => (lastIdx < 1 ? pad.left + plotW / 2
      : pad.left + (i / lastIdx) * plotW);
    const yAt = (v) => pad.top + (1 - (v - yLo) / ySpan) * plotH;
    const baseY = pad.top + plotH;

    // Runs of consecutive indices that actually have readings. Each run is
    // drawn on its own; the gaps between them stay blank.
    const runs = [];
    let run = null;
    points.forEach((v, i) => {
      if (v === null) { run = null; return; }
      if (!run) { run = []; runs.push(run); }
      run.push(i);
    });

    runs.forEach((idxs) => {
      if (idxs.length === 1) {
        // A lone sample between gaps has no line to be part of — mark the
        // point itself so a single reading is still visible.
        const i = idxs[0];
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(xAt(i), yAt(points[i]), 2, 0, Math.PI * 2);
        ctx.fill();
        return;
      }

      ctx.beginPath();
      idxs.forEach((i, k) => {
        const x = xAt(i);
        const y = yAt(points[i]);
        if (k === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.stroke();

      // Fill under this run only — the gap gets no fill either.
      ctx.lineTo(xAt(idxs[idxs.length - 1]), baseY);
      ctx.lineTo(xAt(idxs[0]), baseY);
      ctx.closePath();
      ctx.fillStyle = color + '15';
      ctx.fill();
    });

    // Current value label — anchored to the most recent *reading*, which is
    // not necessarily the most recent sample when the series ends in nulls.
    const lastRun = runs[runs.length - 1];
    const valIdx = lastRun[lastRun.length - 1];
    const lastVal = points[valIdx];
    const lx = xAt(valIdx);
    const rightHalf = lx > pad.left + plotW / 2;
    ctx.fillStyle = color;
    ctx.font = 'bold 11px Inter, sans-serif';
    ctx.textAlign = rightHalf ? 'right' : 'left';
    ctx.fillText(lastVal.toFixed(1) + unit, lx, Math.max(pad.top + 9, yAt(lastVal) - 5));

    // Time labels — indexed against the same positions as the samples, so
    // each lands under the reading it actually describes.
    if (points.length > 1 && stamps.length > 0) {
      ctx.fillStyle = '#5a6478';
      ctx.font = '8px Inter, sans-serif';
      ctx.textAlign = 'center';
      const count = Math.min(4, points.length);
      for (let i = 0; i < count; i++) {
        const idx = Math.round(i * lastIdx / (count - 1));
        const label = stamps[idx];
        if (label == null) continue;
        ctx.fillText(String(label), xAt(idx), h - 4);
      }
    }
  }

  // ── Helpers ──
  // Colour classes follow the operator's configured alert thresholds so
  // "Peak Temp" actually turns red at the number set in Settings.
  function tempClass(t) {
    if (t == null) return 'color-muted';
    // `|| default` on a numeric where 0 is legal: the server accepts and
    // echoes back a threshold of 0, the settings form shows it, and nothing
    // turned red until 75 because this line silently replaced it.
    const critRaw = Number(appSettings.temp_critical);
    const warnRaw = Number(appSettings.temp_warning);
    const crit = Number.isFinite(critRaw) ? critRaw : DEFAULT_TEMP_CRITICAL;
    const warn = Number.isFinite(warnRaw) ? warnRaw : DEFAULT_TEMP_WARNING;
    if (t >= crit) return 'color-danger';
    if (t >= warn) return 'color-warning';
    if (t >= warn - 10) return 'color-cyan';
    return 'color-success';
  }

  // Per-card bit-error counter (H-series only — the JSON/R0155 path never
  // reports one, hence the null-means-"no such reading" handling). 0xFFFF is
  // the counter's ceiling, flagged by the server as bit_errors_saturated: that
  // is serious signal corruption, not a literal 65535 errors.
  // `bit_error_present === false` means the card did not answer the read;
  // the count bytes are then the previous card's and decode to a plausible 0.
  // Reporting that as "BER 0" in green is a false all-clear on the one signal
  // that reveals a break a backup sender is masking.
  function bitErrorMeasured(card) {
    return card.bit_errors != null && card.bit_error_present !== false;
  }

  function bitErrorText(card) {
    if (!bitErrorMeasured(card)) return null;
    return card.bit_errors_saturated ? 'BER MAX' : 'BER ' + card.bit_errors;
  }

  function bitErrorClass(card) {
    if (!bitErrorMeasured(card)) return 'color-muted';
    if (card.bit_errors_saturated) return 'color-danger';
    if (card.bit_errors > 0) return 'color-warning';
    return 'color-success';
  }

  // The only identifier the tile shows, so it cannot be allowed to be blank.
  //
  // Cards the server created from a per-card read carry a label it wrote; a
  // card that came from the enumeration inventory carries addressing only —
  // slot / port / card_id / card_number / user_slot / opt / port_on_opt and no
  // `label` at all (see the snapshot loader in device_manager.py). Those
  // rendered as an anonymous tile with an empty top line, which turns the grid
  // into a wall of unnameable squares exactly when the operator needs to walk
  // to one of them.
  //
  // The derived form matches what the server writes when it does make a label
  // (`f"P{port + 1}C{card_id + 1:02d}"`) and what the Wall View shows, so one
  // card is never called two different things in two tabs. The +1s are the
  // 0-based wire addressing converted to the 1-based numbering printed on the
  // hardware.
  function cardLabel(c) {
    if (c == null) return 'Card';
    if (typeof c.label === 'string' && c.label.trim() !== '') return c.label;
    const pad = (n) => String(n).padStart(2, '0');
    if (Number.isFinite(c.port) && Number.isFinite(c.card_id)) {
      return `P${c.port + 1}C${pad(c.card_id + 1)}`;
    }
    if (Number.isFinite(c.card_id)) return `C${pad(c.card_id + 1)}`;
    // The R0155/demo path indexes cards instead of addressing them.
    if (Number.isFinite(c.index)) return `C${pad(c.index + 1)}`;
    // Last resort: whatever addressing exists, spelled out. An ugly identifier
    // still points at one physical card; a blank tile points at nothing.
    const parts = [];
    if (Number.isFinite(c.user_slot)) parts.push(`S${c.user_slot}`);
    else if (Number.isFinite(c.slot)) parts.push(`S${c.slot}`);
    else if (Number.isFinite(c.card_number)) parts.push(`Card ${c.card_number}`);
    if (Number.isFinite(c.port)) parts.push(`P${c.port}`);
    return parts.length ? parts.join(' · ') : 'Card';
  }

  // Three states, not two. `online` is null when the card was never read or
  // did not answer — which on this controller is usually the request budget,
  // not a panel going away. Rendering that as a red "Offline" tile is what
  // reported 250 dead panels on a fully lit wall.
  function cardStateLabel(c) {
    if (c.online === true) return 'Online';
    if (c.online === false) return 'Offline';
    return 'No reading';
  }

  function cardTileClass(c) {
    if (c.online === true) return 'active';
    if (c.online === false) return 'offline';
    return 'unknown';
  }

  function cardDotColour(c) {
    if (c.online === true) return 'success';
    if (c.online === false) return 'danger';
    return 'text-muted';
  }

  // The tile only has room for a couple of numbers; the rest — chain index,
  // voltage, bit-error detail — rides along in the tooltip.
  function cardTileTitle(c) {
    const parts = [cardLabel(c)];
    if (c.port != null) parts.push('port ' + c.port);
    // Chain is the 0-based daisy-chain index and is *not* the port number.
    if (c.chain != null) parts.push('chain ' + c.chain);
    parts.push(cardStateLabel(c).toLowerCase());
    if (c.temperature_c != null) parts.push(c.temperature_c.toFixed(1) + '°C');
    if (c.voltage_v != null) parts.push(c.voltage_v + 'V');
    if (c.bit_errors != null) {
      parts.push(c.bit_errors_saturated
        ? 'bit errors: saturated (0xFFFF — serious signal corruption)'
        : 'bit errors: ' + c.bit_errors);
    }
    if (c.present === false) parts.push('card did not answer the bit-error read');
    // When this card's numbers were actually taken. They are on-demand reads
    // now, so the tile carries its own age rather than inheriting the panel's.
    parts.push(readAgeInfo(c.read_at != null ? c.read_at : null).text);
    return parts.join(' · ');
  }

  // ── Per-card reading age ──
  // Per-card temperature/voltage stopped being polled on a timer, so every
  // per-card number on screen is of some age. The backend stamps `read_at` on
  // each card and `cards_read_at` on the device.
  //
  // The stamp format is the server's business and has been both "HH:MM:SS" and
  // a full ISO timestamp, so both are accepted. An unparseable stamp is shown
  // verbatim with no age claimed — inventing "moments ago" for a string this
  // code did not understand is the exact failure this is here to prevent.
  const CLOCK_ONLY = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/;

  function parseReadStamp(value) {
    if (value == null || value === '') return null;
    const text = String(value);
    const iso = Date.parse(text);
    if (Number.isFinite(iso)) return new Date(iso);

    const m = CLOCK_ONLY.exec(text.trim());
    if (!m) return null;
    const now = new Date();
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate(),
      Number(m[1]), Number(m[2]), Number(m[3] || 0));
    // A wall-clock-only stamp that lands in the future is yesterday's, not
    // tomorrow's — a small clock skew is tolerated before assuming that.
    if (d.getTime() > now.getTime() + 60000) d.setDate(d.getDate() - 1);
    return d;
  }

  function ageText(date, now) {
    const secs = Math.max(0, Math.round((now - date.getTime()) / 1000));
    if (secs < 45) return 'moments ago';
    const mins = Math.round(secs / 60);
    if (mins < 90) return `${Math.max(1, mins)}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 36) return `${hours}h ago`;
    return `${Math.round(hours / 24)}d ago`;
  }

  // { text, cls, title } describing how old a per-card read is.
  function readAgeInfo(stamp) {
    if (stamp == null || stamp === '') {
      return {
        text: 'never read',
        cls: 'badge-warning',
        title: 'Per-card readings have not been taken this session. Any per-card '
          + 'value shown comes from the stored snapshot.',
      };
    }
    const date = parseReadStamp(stamp);
    if (!date) {
      return {
        text: `read ${stamp}`,
        cls: 'badge-warning',
        title: `Reading time reported as "${stamp}" — age could not be determined.`,
      };
    }
    const secs = Math.max(0, (Date.now() - date.getTime()) / 1000);
    // Two minutes: long enough that a read finishing while you look at it still
    // counts as fresh, short enough that it cannot cover a set change.
    const cls = secs <= 120 ? 'badge-success' : 'badge-warning';
    return {
      text: `read ${ageText(date, Date.now())}`,
      cls,
      title: `Per-card readings taken at ${date.toLocaleString()}.`
        + (cls === 'badge-warning'
          ? ' These are not live values — refresh from the Wall View for current readings.'
          : ''),
    };
  }

  function cardsReadBadge(dev) {
    const info = readAgeInfo(dev && dev.cards_read_at);
    return `<span class="badge ${info.cls}" title="${esc(info.title)}">cards ${esc(info.text)}</span>`;
  }

  // ── SNMP device health ──
  // Routine health comes from SNMP now: read-only, so it cannot collide with
  // whatever control surface is driving the wall. It reports the device level
  // — model, firmware, temperature status, every fan and every PSU — and none
  // of that used to be on screen.
  //
  // The backend for this is being written alongside this file, so everything
  // below probes for the section instead of assuming one shape, and every
  // missing field degrades to "unknown". A wrong-but-confident reading here is
  // worse than no reading.
  const HEALTH_KEYS = ['snmp_health', 'snmp', 'health', 'device_health'];

  function looksLikeHealth(obj) {
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return false;
    return ['fans', 'psus', 'model', 'firmware', 'temperature_status',
      'temperature_ok', 'failed_fans', 'failed_psus', 'fan_count', 'psu_count']
      .some((k) => k in obj);
  }

  function snmpHealth(dev) {
    if (!dev || typeof dev !== 'object') return null;
    for (const key of HEALTH_KEYS) {
      const value = dev[key];
      if (looksLikeHealth(value)) return value;
      // A wrapper: { snmp: { health: {...}, screens: {...} } }
      if (value && typeof value === 'object') {
        for (const inner of ['health', 'device', 'device_health']) {
          if (looksLikeHealth(value[inner])) return value[inner];
        }
      }
    }
    return null;
  }

  // Everywhere the SNMP data might be hiding, most specific first. Counts for
  // screens / cards / ports may sit beside the health block rather than in it.
  function healthRoots(dev, health) {
    const roots = [];
    if (health) roots.push(health);
    HEALTH_KEYS.forEach((k) => {
      if (dev && dev[k] && typeof dev[k] === 'object') roots.push(dev[k]);
    });
    if (dev) roots.push(dev);
    return roots;
  }

  function atPath(root, path) {
    let cur = root;
    for (const part of path.split('.')) {
      if (cur == null || typeof cur !== 'object') return undefined;
      cur = cur[part];
    }
    return cur;
  }

  function pickPath(roots, paths) {
    for (const root of roots) {
      for (const path of paths) {
        const value = atPath(root, path);
        if (value != null && value !== '') return value;
      }
    }
    return null;
  }

  // SNMP values arrive as numbers or as the strings the agent sent. Both are
  // acceptable; anything else is not a count.
  function numOrNull(value) {
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value === 'string' && value.trim() !== '') {
      const n = Number(value);
      return Number.isFinite(n) ? n : null;
    }
    return null;
  }

  function countText(roots, paths) {
    const n = numOrNull(pickPath(roots, paths));
    return n != null ? String(n) : '—';
  }

  // One unit (fan or PSU). `ok` is the field to trust; `status` is the raw
  // number behind it. speed_raw / voltage_raw read 0 on a perfectly healthy
  // wall — they carry no unit and are not measurements, so they are never
  // rendered as RPM or volts and never colour anything.
  function unitState(unit) {
    if (!unit || typeof unit !== 'object') return 'unknown';
    if (unit.ok === true) return 'ok';
    if (unit.ok === false) return 'bad';
    // An `ok` that is present and null is the backend REFUSING to judge this
    // value — not a field it forgot to send. snmp_client.parse_psus sets
    // exactly that whenever it cannot read the supply's `iSignal`, which is
    // the ONLY field on `.1.17` with a documented meaning (NovaStar R&D, by
    // email: "0: not connected to power, 1: connected to power"). Its sibling
    // `status` remains undocumented and is displayed, never judged.
    //
    // Falling through to the raw-status rule overrode that abstention and
    // painted "1 of 4 FAILED — psu 2" in danger red on the one value the
    // backend deliberately declined to call a fault — on a wall whose supplies
    // were all reporting iSignal 1, i.e. perfectly healthy. The fallback below
    // is still reachable for a payload shape that carries no `ok` at all.
    if ('ok' in unit) return 'unknown';
    const status = numOrNull(unit.status);
    if (status == null) return 'unknown';
    // Only correct for the `Normal: 0` fields — fans (`.1.16`) are. It is NOT
    // correct for a PSU, whose verdict comes from `iSignal` and not from
    // `status` at all; a PSU only reaches here in a payload with no `ok` key,
    // which the current backend never emits. Anything richer than "0 is the
    // healthy value" has to come from the backend's own `ok`.
    return status === 0 ? 'ok' : 'bad';
  }

  function unitId(unit, index) {
    const id = numOrNull(unit && (unit.fan_id != null ? unit.fan_id : unit.power_id));
    return id != null ? id : index;
  }

  function unitTitle(unit, kind, id) {
    const state = unitState(unit);
    const parts = [`${kind} ${id}`];
    parts.push(state === 'ok' ? 'reporting OK'
      : state === 'bad' ? 'FAULT — device reports this unit not OK'
        : 'status not reported');
    const status = numOrNull(unit && unit.status);
    if (status != null) parts.push(`status ${status}`);
    // Deliberately labelled as a raw field with no unit: it reads 0 on every
    // fan and every supply of a healthy wall, so presenting it as RPM or volts
    // would invent a fault that is not there.
    const raw = unit && (unit.speed_raw != null ? unit.speed_raw : unit.voltage_raw);
    if (raw != null) {
      const name = unit.speed_raw != null ? 'speed_raw' : 'voltage_raw';
      parts.push(`${name} ${raw} (raw, no unit — not a measurement)`);
    }
    if (unit && unit.i_signal != null) parts.push(`iSignal ${unit.i_signal}`);
    return parts.join(' · ');
  }

  function unitStrip(units, kind, prefix, declaredCount) {
    const list = Array.isArray(units) ? units : [];
    const declared = numOrNull(declaredCount);

    if (list.length === 0) {
      const note = declared != null
        ? `${declared} ${kind}s reported by count, but no per-${kind} status was returned.`
        : `No per-${kind} status reported.`;
      return `<div class="unit-row">
        <div class="unit-row-label">${esc(kind === 'fan' ? 'Fans' : 'PSUs')}</div>
        <div class="unit-note color-muted">${esc(note)}</div>
      </div>`;
    }

    const failed = [];
    let unknown = 0;
    const chips = list.map((unit, i) => {
      const id = unitId(unit, i);
      const state = unitState(unit);
      if (state === 'bad') failed.push(id);
      if (state === 'unknown') unknown++;
      return `<span class="unit-chip unit-${state}" title="${esc(unitTitle(unit, kind, id))}">${esc(prefix + id)}</span>`;
    }).join('');

    // The verdict comes before the chips: with 10 fans and 4 PSUs, scanning a
    // strip of near-identical squares for the one that changed colour is
    // exactly the hunting this is supposed to remove.
    let summary;
    let summaryClass;
    if (failed.length > 0) {
      summary = `${failed.length} of ${list.length} FAILED — ${kind} ${failed.join(', ')}`;
      summaryClass = 'color-danger';
    } else if (unknown === list.length) {
      summary = `${list.length} present · status not reported`;
      summaryClass = 'color-muted';
    } else if (unknown > 0) {
      summary = `${list.length - unknown} of ${list.length} OK · ${unknown} not reporting`;
      summaryClass = 'color-warning';
    } else {
      summary = `all ${list.length} OK`;
      summaryClass = 'color-success';
    }
    // A count OID that disagrees with the array is worth saying out loud
    // rather than quietly trusting one of the two.
    const mismatch = declared != null && declared !== list.length
      ? ` <span class="color-warning">(device reports ${esc(declared)})</span>`
      : '';

    return `<div class="unit-row">
      <div class="unit-row-label">${esc(kind === 'fan' ? 'Fans' : 'PSUs')}</div>
      <div class="unit-row-body">
        <div class="unit-summary ${summaryClass}">${esc(summary)}${mismatch}</div>
        <div class="unit-strip">${chips}</div>
      </div>
    </div>`;
  }

  // Fans/PSUs the device flags as failed, header-badge sized. Reads the
  // explicit failed_* lists when present and otherwise derives them, so a
  // backend that drops those keys still shows the fault.
  function failedUnits(health, listKey, failedKey) {
    if (!health) return [];
    const explicit = health[failedKey];
    if (Array.isArray(explicit)) return explicit;
    const list = Array.isArray(health[listKey]) ? health[listKey] : [];
    return list.map((u, i) => (unitState(u) === 'bad' ? unitId(u, i) : null))
      .filter((v) => v !== null);
  }

  function tempStatusInfo(health, roots) {
    const ok = health ? health.temperature_ok : null;
    if (ok === true) return { text: 'Normal', cls: 'color-success' };
    if (ok === false) return { text: 'FAULT', cls: 'color-danger' };
    const status = numOrNull(pickPath(roots, ['temperature_status']));
    if (status != null) {
      return status === 0
        ? { text: 'Normal', cls: 'color-success' }
        : { text: `FAULT (status ${status})`, cls: 'color-danger' };
    }
    return { text: 'unknown', cls: 'color-muted' };
  }

  // Badges for the collapsed device header — a failed fan or supply has to be
  // visible without expanding anything.
  function healthHeaderBadges(dev) {
    const health = snmpHealth(dev);
    if (!health) return '';
    const roots = healthRoots(dev, health);
    const badges = [];
    const temp = tempStatusInfo(health, roots);
    if (temp.cls === 'color-danger') {
      badges.push('<span class="badge badge-danger">TEMP FAULT</span>');
    }
    const fans = failedUnits(health, 'fans', 'failed_fans');
    if (fans.length > 0) {
      badges.push(`<span class="badge badge-danger">${esc(fans.length)} FAN${fans.length > 1 ? 'S' : ''} FAILED</span>`);
    }
    const psus = failedUnits(health, 'psus', 'failed_psus');
    if (psus.length > 0) {
      badges.push(`<span class="badge badge-danger">${esc(psus.length)} PSU${psus.length > 1 ? 'S' : ''} FAILED</span>`);
    }
    return badges.join('');
  }

  function renderHealthSection(dev) {
    const health = snmpHealth(dev);
    if (!health) {
      return `<div class="section" style="margin-bottom:10px;">
        <div class="section-title">🩺 SNMP Health</div>
        <p style="font-size:12px;color:var(--text-muted);">
          No SNMP health reported for this device yet. Routine health is read over SNMP,
          which is read-only and cannot collide with control traffic; it appears here once
          the first read completes.
        </p>
      </div>`;
    }

    const roots = healthRoots(dev, health);
    const temp = tempStatusInfo(health, roots);
    const cpu = numOrNull(pickPath(roots, ['cpu_status', 'summary.cpuStatus']));
    const cpuInfo = cpu == null ? { text: 'unknown', cls: 'color-muted' }
      : cpu === 0 ? { text: 'Normal', cls: 'color-success' }
        : { text: `FAULT (status ${cpu})`, cls: 'color-danger' };

    const model = pickPath(roots, ['model', 'summary.model', 'device_info.model_code']);
    const firmware = pickPath(roots, ['firmware', 'summary.firmware']);
    const serial = pickPath(roots, ['serial_number', 'summary.sn']);
    const deviceTime = pickPath(roots, ['device_time', 'summary.time']);

    const screens = countText(roots, ['screen_count', 'screens.screen_count',
      'screens.count', 'screen.screen_count']);
    const outputCards = countText(roots, ['output_card_count', 'output.card_count',
      'card_count', 'outputs.card_count']);
    const ports = countText(roots, ['port_count', 'output.port_count',
      'ports.port_count', 'output_port_count']);
    const inputCards = countText(roots, ['input_card_count', 'input.card_count',
      'inputs.card_count']);

    return `<div class="section" style="margin-bottom:10px;">
      <div class="section-title">🩺 SNMP Health <span class="badge badge-info">read-only</span></div>
      <div class="info-grid">
        ${infoItem('Model', model || '—')}
        ${infoItem('Firmware', firmware || '—')}
        ${infoItem('Serial', serial || '—')}
        ${infoItem('Temperature', temp.text, temp.cls)}
        ${infoItem('CPU', cpuInfo.text, cpuInfo.cls)}
        ${infoItem('Screens', screens)}
        ${infoItem('Output Cards', outputCards)}
        ${infoItem('Output Ports', ports)}
        ${infoItem('Input Cards', inputCards)}
        ${infoItem('Device Time', deviceTime || '—')}
      </div>
      <div class="unit-block">
        ${unitStrip(health.fans, 'fan', 'F', health.fan_count)}
        ${unitStrip(health.psus, 'psu', 'P', health.psu_count)}
      </div>
      <p class="unit-footnote">
        Status fields only. NovaStar confirm that fan speed and PSU voltage are not provided
        over SNMP at all, so those registers read 0 on every unit of a healthy wall — they
        carry no unit and are not measurements, and are never shown as RPM or volts or
        allowed to raise a fault here. PSU state comes from the controller's
        <code>iSignal</code> field: 1 connected to power, 0 not connected.
      </p>
    </div>`;
  }

  function infoItem(label, value, colorClass) {
    return `<div class="info-item">
      <div class="info-label">${esc(label)}</div>
      <div class="info-value ${colorClass || ''}">${esc(value)}</div>
    </div>`;
  }

  // ── Simulation Mode ──
  function toggleSimulation(enable) {
    fetch('/api/demo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enable }),
    })
      .then((r) => r.json())
      .then((data) => {
        updateDemoStatus(data.active);
        if (!data.active) {
          // Remove demo device from local state
          delete devices['demo-vx1000'];
          renderAll();
        }
      })
      .catch((err) => {
        alert('Failed to toggle simulation: ' + err);
        // Revert checkbox
        const toggle = document.getElementById('demo-toggle');
        if (toggle) toggle.checked = !enable;
      });
  }

  function updateDemoStatus(active) {
    const toggle = document.getElementById('demo-toggle');
    const status = document.getElementById('demo-status');
    if (toggle) toggle.checked = active;
    if (status) {
      status.textContent = active ? 'Active — Demo VX1000' : 'Disabled';
      status.style.color = active ? 'var(--success)' : 'var(--text-muted)';
    }
  }

  function checkDemoStatus() {
    fetch('/api/demo').then((r) => r.json()).then((d) => updateDemoStatus(d.active)).catch(() => {});
  }

  // ── Delegated actions ──
  // Replaces the inline onclick="fn('${dev.device_id}')" handlers, which
  // executed hardware/user-supplied strings as JavaScript on every render.
  const ACTIONS = {
    'add-device-show': showAddDevice,
    'add-device-hide': hideAddDevice,
    'add-device-submit': addDevice,
    'clear-resolved': clearResolved,
    'save-settings': saveSettings,
    'device-toggle': (el) => toggleDevice(el.dataset.deviceId),
    'device-remove': (el) => removeDevice(el.dataset.deviceId),
    'error-resolve': (el) => resolveError(Number(el.dataset.errorId)),
    'error-ack': (el) => acknowledgeError(Number(el.dataset.errorId)),
    'halt-toggle': toggleHalt,
    'halt-resume': resumeContact,
  };

  function initActions() {
    document.addEventListener('click', (evt) => {
      const el = evt.target.closest('[data-action]');
      if (!el) return;
      const handler = ACTIONS[el.dataset.action];
      if (!handler) return;
      evt.preventDefault();
      handler(el);
    });

    const demoToggle = document.getElementById('demo-toggle');
    if (demoToggle) {
      demoToggle.addEventListener('change', () => toggleSimulation(demoToggle.checked));
    }
  }

  // ── Init ──
  function init() {
    initTabs();
    initErrorFilters();
    initAddDeviceTypePills();
    initActions();
    // Before the socket: if contact is already halted, say so on the first
    // paint rather than after the first update arrives.
    applyHaltState(haltState.halted, haltState.reason);
    fetchHaltState();
    setInterval(fetchHaltState, HALT_POLL_MS);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) fetchHaltState();
    });
    initSocket();
    populateSettings();
    loadVersion();
    checkDemoStatus();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
