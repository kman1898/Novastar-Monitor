/* NovaStar Monitor — Wall View
 *
 * Renders the wall as the controller actually reports it:
 * sender card → OPT → port → chain of receiving cards. Each card is one small
 * cell, coloured by the selected mode (temperature / power / online).
 *
 * Data source: /api/wall_live — the enumeration snapshot supplies the
 * topology, the polled device state overlays current per-card readings.
 *
 * The previous hand-authored SVG wall (pillars + serpentine chains, driven by
 * /api/wall_rendered) has been removed: it was unreachable behind the live
 * path and its card lookup keyed on a field the server never emitted.
 *
 * Classic (non-module) script, scoped in an IIFE — app.js occupies the same
 * global namespace and the two files used to overwrite each other's helpers.
 */
(function () {
  'use strict';

  const POLL_MS = 5000;
  const MODE_KEY = 'nsm.wallMode';
  const MODES = ['temperature', 'power', 'online', 'bit_errors'];
  const MODE_LABELS = {
    temperature: 'Temp',
    power: 'Power',
    online: 'Online',
    bit_errors: 'Bit Errors',
  };

  // ── Palette ──
  const C_NO_DATA = '#374151';  // grey — nothing reported for this card
  const C_OK = '#16a34a';
  const C_WARN = '#ca8a04';
  const C_BAD = '#dc2626';
  // Saturated bit-error counter gets its own colour rather than a deeper red:
  // "the counter pinned at its ceiling" is a different statement from "a few
  // errors", and it must not be mistakable for the hot end of any other scale.
  const C_BER_SAT = '#d946ef';
  const RING_SAT = '0 0 0 2px var(--warning)';

  // Temperature heatmap, NovaLCT MonitorSite-style.
  // Below ~30 °C cool blue, 30–45 °C green (normal), then yellow → orange → red.
  const TEMP_BANDS = [
    [20, '#1e3a8a', '< 20°C'],
    [30, '#0ea5e9', '20–30°C'],
    [35, '#22c55e', '30–35°C'],
    [45, '#16a34a', '35–45°C'],
    [55, '#ca8a04', '45–55°C'],
    [65, '#ea580c', '55–65°C'],
    [Infinity, '#dc2626', '> 65°C'],
  ];

  // ── State ──
  let mode = readStoredMode();
  let initialized = false;
  let inFlight = false;
  let pollTimer = null;
  let structureKey = '';      // signature of the currently rendered topology
  let cellEls = new Map();    // "slot-port-card_id" → cell element
  let lastCards = [];         // merged snapshot+live cards from the last fetch

  // ── HTML escaping ──
  // Screen names, and anything else that came off the hardware, are untrusted.
  // app.js keeps its own copy — the two files share no globals by design.
  const ESC_CHARS = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(value) {
    if (value == null) return '';
    return String(value).replace(/[&<>"']/g, (ch) => ESC_CHARS[ch]);
  }

  function readStoredMode() {
    try {
      const stored = localStorage.getItem(MODE_KEY);
      if (MODES.indexOf(stored) !== -1) return stored;
    } catch (_) { /* private browsing / storage disabled */ }
    return 'temperature';
  }

  // ── Colour resolution ──
  function tempFill(c) {
    if (c == null || isNaN(c)) return C_NO_DATA;
    for (const [limit, color] of TEMP_BANDS) {
      if (c < limit) return color;
    }
    return C_BAD;
  }

  // A card flags a supply fault when exactly one of its two supplies reports
  // bad. Both flagged at once is usually an R0155 artifact from a transient
  // timeout rather than a real double-supply failure — shown as a warning.
  function powerState(card) {
    const p = card.primary_power_ok;
    const b = card.backup_power_ok;
    if (p == null && b == null) return 'unknown';
    if (p === false && b === false) return 'suspect';
    if (p === false || b === false) return 'fault';
    return 'ok';
  }

  function powerFill(card) {
    switch (powerState(card)) {
      case 'fault': return C_BAD;
      case 'suspect': return C_WARN;
      case 'ok': return C_OK;
      default: return C_NO_DATA;
    }
  }

  // Per-card bit-error counter — the only continuous data-integrity signal the
  // controller exposes. `bit_errors == null` means the card was never asked
  // (the JSON/R0155 path doesn't read this register), which is not the same as
  // "zero errors". `present === false` means it was asked and didn't answer.
  //
  // BER_ALERT mirrors BIT_ERROR_WARNING in app.py. Keep them equal: a red cell
  // that raises no alert (or an alert with no red cell) teaches the operator to
  // distrust one of the two. The counter is cumulative since card power-on, so
  // single-digit totals are ordinary cable noise on a long chain.
  const BER_ALERT = 100;
  function bitErrorState(card) {
    if (card.bit_errors == null || card.present === false) return 'unknown';
    if (card.bit_errors_saturated) return 'saturated';
    if (card.bit_errors >= BER_ALERT) return 'bad';
    if (card.bit_errors > 0) return 'some';
    return 'clean';
  }

  function bitErrorFill(card) {
    switch (bitErrorState(card)) {
      case 'saturated': return C_BER_SAT;
      case 'bad': return C_BAD;
      case 'some': return C_WARN;
      case 'clean': return C_OK;
      default: return C_NO_DATA;
    }
  }

  function fillForCard(card) {
    // An offline card has no trustworthy reading; colouring it by its stale
    // temperature would paint a dead card "healthy green". It gets the no-data
    // grey plus a red ring (.is-offline) in every mode except Online.
    if (mode === 'online') {
      if (card.online === false) return C_BAD;
      if (card.online === true) return C_OK;
      return C_NO_DATA;
    }
    if (card.online === false) return C_NO_DATA;
    if (mode === 'power') return powerFill(card);
    if (mode === 'bit_errors') return bitErrorFill(card);
    return tempFill(card.temp_c);
  }

  // Colour alone can't separate "counter pinned at 0xFFFF" from "lots of
  // errors", so a saturated card also gets a ring. Inline, because the cell
  // classes live in a stylesheet this file doesn't own.
  function ringForCard(card) {
    if (mode !== 'bit_errors') return '';
    if (card.online === false) return '';
    return bitErrorState(card) === 'saturated' ? RING_SAT : '';
  }

  function bitErrorLabel(card) {
    switch (bitErrorState(card)) {
      case 'saturated': return 'bit errors SATURATED (0xFFFF — signal corruption)';
      case 'unknown':
        return card.present === false ? 'no answer to bit-error read' : null;
      default: return `bit errors ${card.bit_errors}`;
    }
  }

  function cardTitle(card) {
    // `chain` is the 0-based daisy-chain index reported by the binary poll
    // path; `port` is the port number. They are different numbers and only
    // one of the two poll paths supplies the former.
    const ident = [];
    if (card.slot != null) ident.push(`slot ${card.slot}`);
    if (card.port != null) ident.push(`port ${card.port}`);
    if (card.chain != null) ident.push(`chain ${card.chain}`);
    if (card.card_id != null) ident.push(`card ${card.card_id}`);

    const parts = [
      ident.join(' · ') || String(card.label || 'card'),
      card.temp_c != null ? `${card.temp_c.toFixed(1)}°C` : 'no temp',
      card.voltage_v != null ? `${card.voltage_v.toFixed(2)}V` : 'no voltage',
    ];
    const bits = bitErrorLabel(card);
    if (bits) parts.push(bits);
    if (card.online === false) parts.push('OFFLINE');
    const power = powerState(card);
    if (power === 'fault') parts.push('supply fault');
    else if (power === 'suspect') parts.push('both supplies flagged');
    return parts.join(' · ');
  }

  // ── Data fetch ──
  function wallTabActive() {
    return !!document.getElementById('tab-wall')?.classList.contains('active');
  }

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    try {
      const res = await fetch('/api/wall_live');
      const live = res.ok ? await res.json() : null;
      if (live && live.available && live.snapshot && Array.isArray(live.snapshot.cards)) {
        render(live);
      } else {
        showUnavailable('No wall data yet — run the enumeration to build a snapshot.');
      }
    } catch (_) {
      showUnavailable('Could not reach the monitor service.');
    } finally {
      inFlight = false;
    }
  }

  function tick() {
    // A ~400 KB payload and ~1400 cells are not worth rebuilding for a tab
    // nobody is looking at, or a window that is in the background.
    if (document.hidden || !wallTabActive()) return;
    refresh();
  }

  // ── Render ──
  function render(live) {
    const snap = live.snapshot;
    const liveCards = Array.isArray(live.receiving_cards) ? live.receiving_cards : [];

    const liveMap = new Map();
    for (const lc of liveCards) {
      liveMap.set(`${lc.slot}-${lc.port}-${lc.card_id}`, lc);
    }

    const cards = snap.cards.map((c) => {
      const key = `${c.slot}-${c.port}-${c.card_id}`;
      return Object.assign({ _key: key }, c, liveMap.get(key) || {});
    });
    lastCards = cards;

    const sig = cards.map((c) => c._key).join(',');
    if (sig !== structureKey) {
      buildTree(cards);
      structureKey = sig;
    }
    paintCells();

    // Freshness comes from the server, which is the only side that knows
    // whether a device is actually connected. `receiving_cards.length > 0`
    // used to stand in for that and would call a stale snapshot "Live".
    const isLive = live.live === true;
    updateHeader(snap, live, isLive);
    updateStats(snap, cards, isLive);
    renderLegend();
  }

  function showUnavailable(message) {
    const wrap = document.getElementById('wall-canvas-wrap');
    if (wrap) {
      wrap.innerHTML = `<div class="wall-empty">${esc(message)}</div>`;
    }
    structureKey = '';
    cellEls = new Map();
    lastCards = [];

    const nameEl = document.getElementById('wall-name');
    if (nameEl) nameEl.textContent = 'Wall unavailable';
    const countEl = document.getElementById('wall-card-count');
    if (countEl) countEl.textContent = '— cards';
    const stats = document.getElementById('wall-stats');
    if (stats) stats.innerHTML = '';
    const legend = document.getElementById('wall-legend');
    if (legend) legend.innerHTML = '';
  }

  // ── Freshness ──
  function parseStamp(value) {
    if (!value) return null;
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? new Date(ms) : null;
  }

  // Relative age, because "3h ago" answers the operator's actual question —
  // am I looking at now, or at last Tuesday? — faster than a wall-clock time.
  function ageText(date, now) {
    const secs = Math.max(0, Math.round((now - date.getTime()) / 1000));
    if (secs < 45) return 'moments ago';
    const mins = Math.round(secs / 60);
    if (mins < 90) return `${Math.max(1, mins)}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 36) return `${hours}h ago`;
    return `${Math.round(hours / 24)}d ago`;
  }

  // Prefer the capture time written into the snapshot; fall back to the
  // snapshot file's mtime, which is only an upper bound on how old the data is.
  function freshness(live) {
    const captured = parseStamp(live.captured_at);
    const date = captured || parseStamp(live.snapshot_mtime);
    if (!date) return { text: 'capture time unknown', full: '' };
    const now = Date.now();
    const verb = captured ? 'captured' : 'snapshot file written';
    return {
      text: `${verb} ${ageText(date, now)}`,
      full: date.toLocaleString(),
    };
  }

  function updateHeader(snap, live, isLive) {
    const nameEl = document.getElementById('wall-name');
    if (nameEl) {
      // `||` binds looser than `+`, so the old expression appended the badge
      // to the fallback string only and the badge never appeared.
      let badge;
      let trailer = '';
      if (isLive) {
        const pollNote = live.last_poll ? ` — last poll ${live.last_poll}` : '';
        badge = `<span class="badge badge-success" style="margin-left:6px;" title="${esc('Device connected and reporting cards' + pollNote)}">Live</span>`;
      } else {
        const f = freshness(live);
        const why = live.device_connected
          ? 'Device is connected but reporting no cards — showing the stored snapshot'
          : 'No device connected — showing the stored snapshot';
        const title = f.full ? `${why}. ${f.text} (${f.full}).` : `${why}. ${f.text}.`;
        badge = `<span class="badge badge-warning" style="margin-left:6px;" title="${esc(title)}">Snapshot</span>`;
        // The age is spelled out in the header, not just the tooltip: a wall
        // of temperatures nobody measured today must not read as current.
        trailer = ` <span style="font-size:11px;color:var(--text-muted);">${esc(f.text)}${f.full ? esc(' · ' + f.full) : ''}</span>`;
      }
      nameEl.innerHTML = esc(snap.screen_name || 'Live wall') + ' ' + badge + trailer;
    }
    const countEl = document.getElementById('wall-card-count');
    if (countEl) {
      const senders = new Set(snap.cards.map((c) => c.card_number)).size;
      countEl.textContent = `${snap.cards.length} cards · ${senders} sender cards`;
    }
  }

  // Full structural rebuild. Only runs when the topology itself changes —
  // routine refreshes just recolour the existing cells.
  function buildTree(cards) {
    const wrap = document.getElementById('wall-canvas-wrap');
    if (!wrap) return;

    const tree = new Map();  // card_number → Map(optNum → Map(port → cards[]))
    for (const c of cards) {
      if (!tree.has(c.card_number)) tree.set(c.card_number, new Map());
      const opts = tree.get(c.card_number);
      if (!opts.has(c.opt)) opts.set(c.opt, new Map());
      const ports = opts.get(c.opt);
      if (!ports.has(c.port)) ports.set(c.port, []);
      ports.get(c.port).push(c);
    }

    const numeric = (a, b) => a - b;
    const senderNums = [...tree.keys()].sort(numeric);

    let html = '<div class="device-tree">';
    for (const sn of senderNums) {
      const opts = tree.get(sn);
      const optNums = [...opts.keys()].sort(numeric);
      let senderTotal = 0;
      optNums.forEach((o) => opts.get(o).forEach((chain) => { senderTotal += chain.length; }));

      html += `<div class="sender-card">
        <div class="sender-card-head">
          <span class="sender-card-kicker">Sender Card</span>
          <span class="sender-card-title">Card ${esc(sn)}</span>
          <span class="badge badge-info">${senderTotal} cards</span>
        </div>
        <div class="opt-columns">
          ${optNums.map((o) => renderOptColumn(o, opts.get(o))).join('')}
        </div>
      </div>`;
    }
    html += '</div>';

    wrap.innerHTML = html;

    cellEls = new Map();
    wrap.querySelectorAll('.dt-cell').forEach((el) => cellEls.set(el.dataset.k, el));
  }

  function renderOptColumn(optNum, ports) {
    const portNums = [...ports.keys()].sort((a, b) => a - b);
    const total = portNums.reduce((sum, p) => sum + ports.get(p).length, 0);

    let html = `<div class="opt-column">
      <div class="opt-column-head">
        <span class="opt-column-label">OPT ${esc(optNum)}</span>
        <span class="badge badge-info" style="font-size:10px;">${total} cards · ${portNums.length} ports</span>
      </div>`;

    if (!portNums.length) {
      html += '<div class="opt-column-empty">No ports active</div>';
    } else {
      html += '<div class="port-list">';
      for (const p of portNums) html += renderPortChain(p, ports.get(p));
      html += '</div>';
    }
    return html + '</div>';
  }

  function renderPortChain(port, chain) {
    const portOnOpt = chain[0] && chain[0].port_on_opt != null ? chain[0].port_on_opt : port;
    const cells = chain
      .map((c) => `<span class="dt-cell" data-k="${esc(c._key)}"></span>`)
      .join('');
    return `<div class="port-row">
      <span class="port-row-label">Port ${esc(portOnOpt)}</span>
      <span class="badge port-row-count">${chain.length}</span>
      <span class="port-chain">${cells}</span>
    </div>`;
  }

  // Diff-update: touch only the cells whose appearance actually changed.
  function paintCells() {
    for (const card of lastCards) {
      const el = cellEls.get(card._key);
      if (!el) continue;
      const fill = fillForCard(card);
      if (el.dataset.fill !== fill) {
        el.dataset.fill = fill;
        el.style.background = fill;
      }
      const ring = ringForCard(card);
      if (el.dataset.ring !== ring) {
        el.dataset.ring = ring;
        // Empty string clears it, letting .is-offline's ring apply again.
        el.style.boxShadow = ring;
      }
      const offline = card.online === false;
      el.classList.toggle('is-offline', offline);
      const title = cardTitle(card);
      if (el.getAttribute('title') !== title) el.setAttribute('title', title);
    }
  }

  // ── Stats ──
  function updateStats(snap, cards, hasLive) {
    const wrap = document.getElementById('wall-stats');
    if (!wrap) return;

    const total = snap.cards.length;
    // Strict `=== true`: a card the controller did not report on is not
    // evidence that it is up. Erring toward "fewer online" keeps a silent
    // dropout visible instead of padding the healthy count.
    const online = cards.filter((c) => c.online === true).length;
    const offline = cards.filter((c) => c.online === false).length;
    const temps = cards.filter((c) => c.online !== false && c.temp_c != null).map((c) => c.temp_c);
    const avgT = temps.length ? temps.reduce((a, b) => a + b, 0) / temps.length : null;
    const maxT = temps.length ? Math.max(...temps) : null;
    const faults = cards.filter((c) => c.online !== false && powerState(c) === 'fault').length;

    // Bit errors are only meaningful for cards that were actually asked, so
    // the denominator is the measured set, not the whole wall.
    const measured = cards.filter((c) => bitErrorState(c) !== 'unknown');
    const dirty = measured.filter((c) => bitErrorState(c) !== 'clean').length;
    const saturated = measured.filter((c) => bitErrorState(c) === 'saturated').length;
    const berValue = measured.length === 0 ? '—'
      : saturated > 0 ? `${dirty} (${saturated} max)` : String(dirty);
    const berClass = measured.length === 0 ? 'color-muted'
      : saturated > 0 ? 'color-danger'
        : dirty > 0 ? 'color-warning' : 'color-success';

    // Without live data every "online/offline" figure would be a guess — say
    // so rather than printing a reassuring zero.
    const cells = [
      ['Total cards', String(total), ''],
      ['Online', hasLive ? String(online) : '—', hasLive && online > 0 ? 'color-success' : 'color-muted'],
      ['Offline', hasLive ? String(offline) : '—', hasLive && offline > 0 ? 'color-danger' : 'color-muted'],
      ['Avg temp', avgT != null ? `${avgT.toFixed(1)}°C` : '—', avgT != null && avgT > 60 ? 'color-warning' : ''],
      ['Max temp', maxT != null ? `${maxT.toFixed(1)}°C` : '—', maxT != null && maxT > 70 ? 'color-danger' : ''],
      ['Supply faults', hasLive ? String(faults) : '—', hasLive && faults > 0 ? 'color-danger' : 'color-muted'],
      ['Cards w/ bit errors', hasLive ? berValue : '—', hasLive ? berClass : 'color-muted'],
    ];

    wrap.innerHTML = cells.map(([label, value, cls]) => `
      <div class="stat-card" style="padding:8px 10px;">
        <div class="stat-label">${esc(label)}</div>
        <div class="stat-value ${cls}" style="font-size:18px;">${esc(value)}</div>
      </div>
    `).join('');
  }

  // ── Legend ──
  function swatch(label, color, extraClass, extraStyle) {
    const style = `background:${color}` + (extraStyle ? ';' + extraStyle : '');
    return `<span class="legend-item"><span class="legend-swatch ${extraClass || ''}" style="${style}"></span>${esc(label)}</span>`;
  }

  function renderLegend() {
    const el = document.getElementById('wall-legend');
    if (!el) return;

    let items;
    if (mode === 'temperature') {
      items = ['<span>Temp:</span>']
        .concat(TEMP_BANDS.map(([, color, label]) => swatch(label, color)))
        .concat([swatch('Offline', C_NO_DATA, 'is-offline')]);
    } else if (mode === 'power') {
      items = [
        '<span>Power:</span>',
        swatch('Both supplies OK', C_OK),
        swatch('One supply down', C_BAD),
        swatch('Both flagged (suspect reading)', C_WARN),
        swatch('No data', C_NO_DATA),
        swatch('Offline', C_NO_DATA, 'is-offline'),
      ];
    } else if (mode === 'bit_errors') {
      items = [
        '<span>Bit errors:</span>',
        swatch('None', C_OK),
        swatch('1–99', C_WARN),
        swatch('100+', C_BAD),
        swatch('Saturated (0xFFFF)', C_BER_SAT, '', `box-shadow:${RING_SAT}`),
        swatch('Not measured', C_NO_DATA),
        swatch('Offline', C_NO_DATA, 'is-offline'),
      ];
    } else {
      items = [
        '<span>Online:</span>',
        swatch('Online', C_OK),
        swatch('Offline', C_BAD, 'is-offline'),
        swatch('No data', C_NO_DATA),
      ];
    }
    el.innerHTML = items.join('');
  }

  // ── Mode pills ──
  // The template ships the original three pills; any mode added here appends
  // its own so the two lists can't drift apart.
  function ensureModePills(filter) {
    MODES.forEach((m) => {
      if (filter.querySelector(`.pill[data-val="${m}"]`)) return;
      const btn = document.createElement('button');
      btn.className = 'pill';
      btn.dataset.val = m;
      btn.textContent = MODE_LABELS[m] || m;
      filter.appendChild(btn);
    });
  }

  function bindModeFilter() {
    const filter = document.getElementById('wall-mode-filter');
    if (!filter) return;
    ensureModePills(filter);
    filter.querySelectorAll('.pill').forEach((p) => {
      p.classList.toggle('active', p.dataset.val === mode);
    });
    filter.addEventListener('click', (evt) => {
      const pill = evt.target.closest('.pill');
      if (!pill || !filter.contains(pill)) return;
      filter.querySelectorAll('.pill').forEach((p) => p.classList.toggle('active', p === pill));
      mode = pill.dataset.val;
      try { localStorage.setItem(MODE_KEY, mode); } catch (_) { /* no-op */ }
      paintCells();
      renderLegend();
    });
  }

  // ── Init ──
  function init() {
    if (initialized) return;
    initialized = true;

    bindModeFilter();
    renderLegend();

    // Nothing is fetched until the wall is actually on screen.
    document.addEventListener('nsm:tabchange', (evt) => {
      if (evt.detail && evt.detail.tab === 'wall') refresh();
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && wallTabActive()) refresh();
    });

    pollTimer = setInterval(tick, POLL_MS);
    window.addEventListener('beforeunload', () => clearInterval(pollTimer));

    if (wallTabActive()) refresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
