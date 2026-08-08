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
  // Mirrors DEFAULT_POLL_INTERVAL in app.py — the server owns this number.
  // The settings input has min="3", so a lower default here would render a
  // value the field itself rejects.
  const DEFAULT_POLL_INTERVAL = 10;

  // ── State ──
  let socket = null;
  let alerts = [];
  let appSettings = {};
  let restFallbackTimer = null;
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
      if (data.errors) {
        alerts = data.errors;
      }
      renderAll();
    });

    socket.on('device_update', (data) => {
      if (data.device_id && data.state) {
        devices[data.device_id] = data.state;
        renderAll();
      }
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
          if (Array.isArray(list)) alerts = list;
          renderAll();
        })
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
    if (countEl) countEl.textContent = `${activeCount} active`;
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
    let cardsOnline = 0;
    let tempSum = 0;
    let tempCount = 0;
    let peakTemp = null;
    let brightSum = 0;
    let brightCount = 0;

    devList.forEach((d) => {
      const lm = d.live_monitoring || {};
      // `!= null` throughout: a genuine 0 (0 °C, 0 % blackout brightness,
      // 0 cards online) is information, not a missing reading.
      if (lm.card_count != null) cardsOnline += lm.card_count;
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

    grid.innerHTML = [
      statCard('Devices', `${online}/${total}`, online === total ? 'color-success' : 'color-warning'),
      statCard('Cards Online', String(cardsOnline), 'color-primary'),
      statCard('Avg Temp', avgTemp != null ? avgTemp.toFixed(1) + '°' : '—', tempClass(avgTemp)),
      statCard('Peak Temp', peakTemp != null ? peakTemp.toFixed(1) + '°' : '—', tempClass(peakTemp)),
      statCard('Avg Brightness', avgBright != null ? avgBright.toFixed(0) + '%' : '—', 'color-warning'),
      statCard('Active Errors', String(activeErrors), activeErrors > 0 ? 'color-danger' : 'color-success'),
    ].join('');
  }

  function statCard(label, value, cls) {
    return `<div class="stat-card">
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value ${cls || ''}">${esc(value)}</div>
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
    const linkCls = lm.link_status === 'PRIMARY' ? 'success'
      : lm.link_status === 'BACKUP' ? 'backup'
        : 'danger';

    return `
      <div class="device-panel ${connected ? '' : 'offline'}" data-device-id="${id}">
        <div class="device-header" data-action="device-toggle" data-device-id="${id}">
          <div class="status-dot" style="background:var(--${connected ? 'success' : 'danger'});width:10px;height:10px;border-radius:50%;flex-shrink:0;"></div>
          <div style="flex:1">
            <div class="device-name">${esc(dev.name || dev.ip)}</div>
            <div class="device-meta">${esc(dev.ip)} • Port ${esc(dev.port != null ? dev.port : 5200)} • FW ${esc(dev.firmware_version || '—')}</div>
          </div>
          ${lm.card_count != null ? `<span class="badge badge-info">${esc(lm.card_count)} cards</span>` : ''}
          ${lm.link_status ? `<span class="badge badge-${linkCls}">${esc(lm.link_status)}</span>` : ''}
          ${dev.brightness_pct != null ? `<span style="font-size:12px;color:var(--text-muted);">☀ ${esc(dev.brightness_pct)}%</span>` : ''}
          <button class="btn btn-sm btn-danger" data-action="device-remove" data-device-id="${id}" title="Remove device">✕</button>
          <span class="expand-arrow">▾</span>
        </div>
        <div class="device-body">
          <div class="two-col">
            <div>
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
            <div class="section-title">📦 Receiving Cards <span class="badge badge-info">${cards.length} total</span></div>
            <div class="card-grid">
              ${cards.map((c) => `
                <div class="card-tile ${c.online ? 'active' : 'offline'}" title="${esc(cardTileTitle(c))}">
                  <div class="card-id">${esc(c.label)}</div>
                  <div class="card-temp ${tempClass(c.temperature_c)}">${c.temperature_c != null ? esc(c.temperature_c.toFixed(1)) + '°' : '—'}</div>
                  <div class="card-status">
                    <span class="status-dot" style="background:var(--${c.online ? 'success' : 'danger'})"></span>
                    ${c.online ? 'Online' : 'Offline'}
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
    drawChart(panel.querySelector('[data-chart="volt"]'), h.timestamps, h.voltage, '#fbbf24', 'V', 4.5, 5.5);
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
    setVal('set-temp-warn', s.temp_warning != null ? s.temp_warning : 60);
    setVal('set-temp-crit', s.temp_critical != null ? s.temp_critical : 75);
    setVal('set-volt-min', s.voltage_min != null ? s.voltage_min : 4.7);
    setVal('set-poll-int', s.poll_interval != null ? s.poll_interval : DEFAULT_POLL_INTERVAL);
  }

  function loadVersion() {
    fetch('/api/version').then((r) => r.json()).then((d) => {
      const el = document.getElementById('app-version');
      if (el) el.textContent = d.version || '—';
    }).catch(() => {});
  }

  function saveSettings() {
    const data = {
      temp_warning: parseFloat(document.getElementById('set-temp-warn').value) || 60,
      temp_critical: parseFloat(document.getElementById('set-temp-crit').value) || 75,
      voltage_min: parseFloat(document.getElementById('set-volt-min').value) || 4.7,
      poll_interval: parseFloat(document.getElementById('set-poll-int').value)
        || DEFAULT_POLL_INTERVAL,
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
    const crit = Number(appSettings.temp_critical) || 75;
    const warn = Number(appSettings.temp_warning) || 60;
    if (t >= crit) return 'color-danger';
    if (t >= warn) return 'color-warning';
    if (t >= warn - 10) return 'color-cyan';
    return 'color-success';
  }

  // Per-card bit-error counter (H-series only — the JSON/R0155 path never
  // reports one, hence the null-means-"no such reading" handling). 0xFFFF is
  // the counter's ceiling, flagged by the server as bit_errors_saturated: that
  // is serious signal corruption, not a literal 65535 errors.
  function bitErrorText(card) {
    if (card.bit_errors == null) return null;
    return card.bit_errors_saturated ? 'BER MAX' : 'BER ' + card.bit_errors;
  }

  function bitErrorClass(card) {
    if (card.bit_errors == null) return 'color-muted';
    if (card.bit_errors_saturated) return 'color-danger';
    if (card.bit_errors > 0) return 'color-warning';
    return 'color-success';
  }

  // The tile only has room for a couple of numbers; the rest — chain index,
  // voltage, bit-error detail — rides along in the tooltip.
  function cardTileTitle(c) {
    const parts = [c.label || 'Card'];
    if (c.port != null) parts.push('port ' + c.port);
    // Chain is the 0-based daisy-chain index and is *not* the port number.
    if (c.chain != null) parts.push('chain ' + c.chain);
    parts.push(c.online ? 'online' : 'offline');
    if (c.temperature_c != null) parts.push(c.temperature_c.toFixed(1) + '°C');
    if (c.voltage_v != null) parts.push(c.voltage_v + 'V');
    if (c.bit_errors != null) {
      parts.push(c.bit_errors_saturated
        ? 'bit errors: saturated (0xFFFF — serious signal corruption)'
        : 'bit errors: ' + c.bit_errors);
    }
    if (c.present === false) parts.push('card did not answer the bit-error read');
    return parts.join(' · ');
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
