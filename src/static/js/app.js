/* NovaStar Monitor — Frontend Application */

// ── State ──
let socket;
let devices = {};
let alerts = [];
let appSettings = {};
let errorFilters = { severity: 'ALL', status: 'ALL' };
const MAX_ALERTS = 500;

// ── SocketIO ──
function initSocket() {
  socket = io();

  socket.on('connect', () => {
    document.getElementById('topbar-info').textContent = 'Connected';
    document.getElementById('pulse-dot').classList.remove('offline');
  });

  socket.on('disconnect', () => {
    document.getElementById('topbar-info').textContent = 'Disconnected';
    document.getElementById('pulse-dot').classList.add('offline');
  });

  socket.on('full_state', (data) => {
    if (data.devices) {
      data.devices.forEach(d => { devices[d.device_id] = d; });
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
    const entry = alerts.find(a => a.id === data.id);
    if (entry) {
      entry.resolved = true;
      entry.resolved_at = new Date().toISOString();
      renderErrors();
      updateErrorBadge();
    }
  });
}

// ── Tabs ──
function initTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
    });
  });

  // Filter pills
  document.querySelectorAll('.filter-pills').forEach(group => {
    group.querySelectorAll('.pill').forEach(pill => {
      pill.addEventListener('click', () => {
        group.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
        pill.classList.add('active');
        if (group.id === 'severity-filter') errorFilters.severity = pill.dataset.val;
        if (group.id === 'status-filter') errorFilters.status = pill.dataset.val;
        renderErrors();
      });
    });
  });
}

// ── Render Everything ──
function renderAll() {
  renderStats();
  renderDevices();
  renderErrors();
  updateTopbar();
  updateErrorBadge();
}

function updateTopbar() {
  const devList = Object.values(devices);
  const online = devList.filter(d => d.connected).length;
  const total = devList.length;
  const info = document.getElementById('topbar-info');
  info.textContent = total > 0
    ? `${online}/${total} device${total !== 1 ? 's' : ''} online`
    : 'No devices configured';
}

function updateErrorBadge() {
  const activeCount = alerts.filter(a => !a.resolved).length;
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
  const devList = Object.values(devices);
  const grid = document.getElementById('stats-grid');

  if (devList.length === 0) {
    grid.innerHTML = `<div class="stat-card"><div class="stat-label">Status</div><div class="stat-value color-muted">—</div></div>`;
    return;
  }

  const online = devList.filter(d => d.connected).length;
  const total = devList.length;
  let totalCards = 0, totalTemp = 0, tempCount = 0, maxTemp = 0;
  let totalBrightness = 0, brightCount = 0;

  devList.forEach(d => {
    const lm = d.live_monitoring || {};
    if (lm.card_count) totalCards += lm.card_count;
    if (lm.temperature_c) {
      totalTemp += lm.temperature_c;
      tempCount++;
      if (lm.temperature_c > maxTemp) maxTemp = lm.temperature_c;
    }
    if (d.brightness_pct) { totalBrightness += d.brightness_pct; brightCount++; }
  });

  const avgTemp = tempCount > 0 ? totalTemp / tempCount : 0;
  const avgBright = brightCount > 0 ? totalBrightness / brightCount : 0;
  const activeErrors = alerts.filter(a => !a.resolved).length;

  grid.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">Devices</div>
      <div class="stat-value ${online === total ? 'color-success' : 'color-warning'}">${online}/${total}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Recv Cards</div>
      <div class="stat-value color-primary">${totalCards}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Temp</div>
      <div class="stat-value ${tempColor(avgTemp)}">${avgTemp > 0 ? avgTemp.toFixed(1) + '°' : '—'}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Peak Temp</div>
      <div class="stat-value ${tempColor(maxTemp)}">${maxTemp > 0 ? maxTemp.toFixed(1) + '°' : '—'}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Brightness</div>
      <div class="stat-value color-warning">${avgBright > 0 ? avgBright.toFixed(0) + '%' : '—'}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Active Errors</div>
      <div class="stat-value ${activeErrors > 0 ? 'color-danger' : 'color-success'}">${activeErrors}</div>
    </div>
  `;
}

// ── Device Panels ──
function renderDevices() {
  const container = document.getElementById('devices-container');
  const devList = Object.values(devices);

  if (devList.length === 0) {
    container.innerHTML = `<div class="section" style="text-align:center;padding:40px;">
      <p class="color-muted">No devices configured.</p>
      <p class="color-muted" style="margin-top:8px;">Click <strong>+ Add Device</strong> to start monitoring.</p>
    </div>`;
    return;
  }

  container.innerHTML = devList.map(dev => renderDevicePanel(dev)).join('');

  // Restore expanded state and draw charts
  expandedDevices.forEach(id => {
    const body = document.getElementById('body-' + id);
    const arrow = document.getElementById('arrow-' + id);
    const header = body?.previousElementSibling;
    if (body) body.classList.add('expanded');
    if (arrow) arrow.classList.add('expanded');
    if (header) header.classList.add('expanded');
    setTimeout(() => renderChartsForDevice(id), 50);
  });
}

function renderDevicePanel(dev) {
  const lm = dev.live_monitoring || {};
  const si = dev.system_info || {};
  const di = dev.device_info || {};
  const cards = dev.receiving_cards || [];
  const connected = dev.connected;
  const linkColor = lm.link_status === 'PRIMARY' ? 'success' : lm.link_status === 'BACKUP' ? 'backup' : 'danger';

  return `
    <div class="device-panel ${connected ? '' : 'offline'}">
      <div class="device-header" onclick="toggleDevice('${dev.device_id}')">
        <div class="status-dot" style="background:var(--${connected ? 'success' : 'danger'});width:10px;height:10px;border-radius:50%;flex-shrink:0;"></div>
        <div style="flex:1">
          <div class="device-name">${dev.name || dev.ip}</div>
          <div class="device-meta">${dev.ip} • Port ${dev.port || 5200} • FW ${dev.firmware_version || '—'}</div>
        </div>
        ${lm.card_count ? `<span class="badge badge-info">${lm.card_count} cards</span>` : ''}
        ${lm.link_status ? `<span class="badge badge-${linkColor}">${lm.link_status}</span>` : ''}
        ${dev.brightness_pct ? `<span style="font-size:12px;color:var(--muted);">☀ ${dev.brightness_pct}%</span>` : ''}
        <button class="btn btn-sm btn-danger" onclick="event.stopPropagation();removeDevice('${dev.device_id}')" title="Remove device">✕</button>
        <span class="expand-arrow" id="arrow-${dev.device_id}">▾</span>
      </div>
      <div class="device-body" id="body-${dev.device_id}">
        <div class="two-col">
          <div>
            <div class="section" style="margin-bottom:10px;">
              <div class="section-title">📡 Live Monitoring</div>
              <div class="info-grid">
                ${infoItem('Temperature', lm.temperature_c != null ? lm.temperature_c.toFixed(1) + '°C' : '—', tempColor(lm.temperature_c))}
                ${infoItem('Voltage', lm.voltage_v != null ? lm.voltage_v + 'V' : '—', 'color-cyan')}
                ${infoItem('Link', lm.link_status || '—', 'color-' + linkColor)}
                ${infoItem('Card Count', lm.card_count || '—')}
                ${infoItem('RC Firmware', lm.firmware || '—')}
                ${infoItem('MAC Address', lm.mac_address || '—')}
                ${infoItem('Brightness', (dev.brightness_pct || 0) + '% (' + (dev.brightness || 0) + '/255)')}
                ${infoItem('Gamma', dev.gamma || '—')}
              </div>
            </div>
            <div class="section" style="margin-bottom:10px;">
              <div class="section-title">🖥 Device Info</div>
              <div class="info-grid">
                ${infoItem('Model Code', di.model_code || si.device_type || '—')}
                ${infoItem('Serial', di.serial || '—')}
                ${infoItem('HW Revision', di.hw_revision || '—')}
                ${infoItem('Build Date', si.build_date || '—')}
                ${infoItem('Ethernet Ports', si.ethernet_ports || '—')}
                ${infoItem('Input Count', si.input_count || '—')}
                ${infoItem('Port 2 (Redundancy)', dev.port2_active ? '● Active' : '○ Standby')}
                ${infoItem('Controller Time', dev.datetime || '—')}
              </div>
            </div>
          </div>
          <div>
            <div class="section" style="margin-bottom:10px;">
              <div class="section-title">🌡 Temperature History</div>
              <div class="chart-container"><canvas id="chart-temp-${dev.device_id}"></canvas></div>
            </div>
            <div class="section" style="margin-bottom:10px;">
              <div class="section-title">⚡ Voltage History</div>
              <div class="chart-container"><canvas id="chart-volt-${dev.device_id}"></canvas></div>
            </div>
          </div>
        </div>
        ${cards.length > 0 ? `
        <div class="section">
          <div class="section-title">📦 Receiving Cards <span class="badge badge-info">${cards.length} total</span></div>
          <div class="card-grid">
            ${cards.map(c => `
              <div class="card-tile ${c.online ? 'active' : 'offline'}">
                <div class="card-id">${c.label}</div>
                <div class="card-status">
                  <span class="status-dot" style="background:var(--${c.online ? 'success' : 'danger'})"></span>
                  ${c.online ? 'Online' : 'Offline'}
                </div>
              </div>
            `).join('')}
          </div>
        </div>` : ''}
      </div>
    </div>
  `;
}

function renderChartsForDevice(deviceId) {
  const dev = devices[deviceId];
  if (!dev || !dev.history) return;
  const h = dev.history;
  drawChart(`chart-temp-${deviceId}`, h.timestamps, h.temperature, '#06b6d4', '°C', 40, 80);
  drawChart(`chart-volt-${deviceId}`, h.timestamps, h.voltage, '#f59e0b', 'V', 4.5, 5.5);
}

// ── Error Log ──
function renderErrors() {
  const log = document.getElementById('error-log');

  let filtered = alerts;
  if (errorFilters.severity !== 'ALL') {
    filtered = filtered.filter(a => a.severity === errorFilters.severity);
  }
  if (errorFilters.status === 'ACTIVE') {
    filtered = filtered.filter(a => !a.resolved);
  } else if (errorFilters.status === 'RESOLVED') {
    filtered = filtered.filter(a => a.resolved);
  }

  if (filtered.length === 0) {
    log.innerHTML = '<p class="color-muted" style="padding:20px;text-align:center;">No matching errors</p>';
    return;
  }

  log.innerHTML = filtered.map(a => {
    const sevColor = a.severity === 'CRITICAL' ? 'danger' : a.severity === 'WARNING' ? 'warning' : 'primary';
    const ts = a.timestamp ? new Date(a.timestamp).toLocaleString() : '?';
    const resolvedClass = a.resolved ? ' error-resolved' : '';

    return `
      <div class="error-entry${resolvedClass}">
        <span class="error-severity" style="background:rgba(${sevColor === 'danger' ? '239,68,68' : sevColor === 'warning' ? '245,158,11' : '59,130,246'},0.2);color:var(--${sevColor})">${a.severity}</span>
        <div style="flex:1">
          <div class="error-device">${a.device || '?'}${a.cabinet ? ' • ' + a.cabinet : ''}${a.port ? ' • Port ' + a.port : ''}</div>
          <div class="error-msg">${a.message || ''}</div>
          ${a.resolved ? `<div style="font-size:10px;color:var(--success);margin-top:2px;">Resolved ${a.resolved_at ? new Date(a.resolved_at).toLocaleString() : ''}</div>` : ''}
          ${!a.resolved ? `<div class="error-actions">
            <button class="btn btn-sm" onclick="resolveError(${a.id})">Resolve</button>
            ${!a.acknowledged ? `<button class="btn btn-sm" onclick="acknowledgeError(${a.id})">Acknowledge</button>` : '<span style="font-size:10px;color:var(--muted);">Acknowledged</span>'}
          </div>` : ''}
        </div>
        <span class="error-time">${ts}</span>
      </div>
    `;
  }).join('');
}

function resolveError(id) {
  fetch(`/api/errors/${id}/resolve`, { method: 'POST' })
    .then(r => r.json())
    .then(() => {
      const entry = alerts.find(a => a.id === id);
      if (entry) { entry.resolved = true; entry.resolved_at = new Date().toISOString(); }
      renderErrors();
      updateErrorBadge();
    });
}

function acknowledgeError(id) {
  fetch(`/api/errors/${id}/acknowledge`, { method: 'POST' })
    .then(r => r.json())
    .then(() => {
      const entry = alerts.find(a => a.id === id);
      if (entry) entry.acknowledged = true;
      renderErrors();
    });
}

function clearResolved() {
  if (!confirm('Remove all resolved errors from the log?')) return;
  fetch('/api/errors/clear-resolved', { method: 'POST' })
    .then(r => r.json())
    .then(() => {
      alerts = alerts.filter(a => !a.resolved);
      renderErrors();
      updateErrorBadge();
    });
}

// ── Settings ──
function populateSettings() {
  const s = appSettings;
  const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  setVal('set-temp-warn', s.temp_warning || 60);
  setVal('set-temp-crit', s.temp_critical || 75);
  setVal('set-volt-min', s.voltage_min || 4.7);
  setVal('set-poll-int', s.poll_interval || 2);

  // Load version
  fetch('/api/version').then(r => r.json()).then(d => {
    const el = document.getElementById('app-version');
    if (el) el.textContent = d.version || '—';
  }).catch(() => {});
}

function saveSettings() {
  const data = {
    temp_warning: parseFloat(document.getElementById('set-temp-warn').value) || 60,
    temp_critical: parseFloat(document.getElementById('set-temp-crit').value) || 75,
    voltage_min: parseFloat(document.getElementById('set-volt-min').value) || 4.7,
    poll_interval: parseFloat(document.getElementById('set-poll-int').value) || 2,
  };

  fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  .then(r => r.json())
  .then(() => {
    appSettings = { ...appSettings, ...data };
    const msg = document.getElementById('settings-saved');
    msg.style.display = 'inline';
    setTimeout(() => { msg.style.display = 'none'; }, 2000);
  })
  .catch(err => alert('Failed to save: ' + err));
}

// ── Device Management ──
let expandedDevices = new Set();

function toggleDevice(deviceId) {
  if (expandedDevices.has(deviceId)) {
    expandedDevices.delete(deviceId);
  } else {
    expandedDevices.add(deviceId);
  }
  const body = document.getElementById('body-' + deviceId);
  const arrow = document.getElementById('arrow-' + deviceId);
  const header = body?.previousElementSibling;
  if (body) body.classList.toggle('expanded');
  if (arrow) arrow.classList.toggle('expanded');
  if (header) header.classList.toggle('expanded');

  if (expandedDevices.has(deviceId)) {
    setTimeout(() => renderChartsForDevice(deviceId), 50);
  }
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
  const port = parseInt(document.getElementById('add-port').value) || 5200;

  if (!ip) { alert('IP address is required'); return; }

  fetch('/api/devices', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name || `NovaStar ${ip}`, ip, port }),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) { alert(data.error); return; }
    hideAddDevice();
    document.getElementById('add-name').value = '';
    document.getElementById('add-ip').value = '';
  })
  .catch(err => alert('Failed to add device: ' + err));
}

function removeDevice(deviceId) {
  if (!confirm('Remove this device from monitoring?')) return;
  fetch(`/api/devices/${deviceId}`, { method: 'DELETE' })
  .then(r => r.json())
  .then(() => {
    delete devices[deviceId];
    expandedDevices.delete(deviceId);
    renderAll();
  })
  .catch(err => alert('Failed: ' + err));
}

// ── Chart Drawing ──
function drawChart(canvasId, labels, data, color, unit, yMin, yMax) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !data || data.length < 2) return;

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);

  const w = rect.width, h = rect.height;
  const pad = { top:8, right:8, bottom:18, left:36 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  const dMin = Math.min(...data), dMax = Math.max(...data);
  const yLo = yMin != null ? Math.min(yMin, dMin - 1) : dMin - 1;
  const yHi = yMax != null ? Math.max(yMax, dMax + 1) : dMax + 1;

  ctx.clearRect(0, 0, w, h);

  // Grid
  ctx.strokeStyle = '#1e293b'; ctx.lineWidth = 0.5;
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (plotH / 4) * i;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(w - pad.right, y); ctx.stroke();
    const val = yHi - (yHi - yLo) * (i / 4);
    ctx.fillStyle = '#475569'; ctx.font = '9px DM Sans'; ctx.textAlign = 'right';
    ctx.fillText(val.toFixed(1), pad.left - 4, y + 3);
  }

  // Line
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath();
  for (let i = 0; i < data.length; i++) {
    const x = pad.left + (i / (data.length - 1)) * plotW;
    const y = pad.top + (1 - (data[i] - yLo) / (yHi - yLo)) * plotH;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Fill under line
  ctx.lineTo(pad.left + plotW, pad.top + plotH);
  ctx.lineTo(pad.left, pad.top + plotH);
  ctx.closePath();
  ctx.fillStyle = color + '15'; ctx.fill();

  // Current value label
  const lastVal = data[data.length - 1];
  const lastX = pad.left + plotW;
  const lastY = pad.top + (1 - (lastVal - yLo) / (yHi - yLo)) * plotH;
  ctx.fillStyle = color; ctx.font = 'bold 11px DM Sans'; ctx.textAlign = 'right';
  ctx.fillText(lastVal.toFixed(1) + unit, lastX, lastY - 5);

  // Time labels
  if (labels && labels.length > 1) {
    ctx.fillStyle = '#475569'; ctx.font = '8px DM Sans'; ctx.textAlign = 'center';
    const count = Math.min(4, labels.length);
    for (let i = 0; i < count; i++) {
      const idx = Math.floor(i * (labels.length - 1) / (count - 1));
      const x = pad.left + (idx / (data.length - 1)) * plotW;
      ctx.fillText(labels[idx], x, h - 4);
    }
  }
}

// ── Helpers ──
function tempColor(t) {
  if (t == null || t === 0) return 'color-muted';
  if (t < 50) return 'color-success';
  if (t < 65) return 'color-cyan';
  if (t < 75) return 'color-warning';
  return 'color-danger';
}

function infoItem(label, value, colorClass) {
  return `<div class="info-item">
    <div class="info-label">${label}</div>
    <div class="info-value ${colorClass || ''}">${value}</div>
  </div>`;
}

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
  initSocket();
  initTabs();
});
