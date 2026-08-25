/* NovaStar Monitor — Wall View
 *
 * Renders the wall as the controller actually reports it:
 * sender card → OPT → port → chain of receiving cards. Each card is one small
 * cell, coloured by the selected mode (temperature / power / online).
 *
 * Data source: /api/wall_live. The CONTROLLER is the authority on what the
 * wall is — name, canvas, mosaic, outputs, sender slots all come from
 * `topology`, re-derived from R0405 on every poll. The stored enumeration
 * snapshot supplies only the receiving-card inventory, and only when the
 * server has confirmed it describes the wall that is currently configured;
 * when it doesn't, the server withholds it and this file says so instead of
 * drawing a wall that no longer exists.
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

  // How old a per-card reading may be before the wall stops presenting its
  // colours as current. Two minutes: long enough that a read finishing while
  // the operator watches still counts as fresh, short enough that it cannot
  // span a set change.
  const CARDS_FRESH_MS = 120000;

  // ── State ──
  let mode = readStoredMode();
  let initialized = false;
  let inFlight = false;
  let pollTimer = null;
  // Signature of the currently rendered topology. `null` = nothing rendered
  // yet; an empty card list has its own signature, so "no inventory" still
  // draws its explanation on the first pass instead of matching the initial
  // value and rendering nothing at all.
  let structureKey = null;
  let cellEls = new Map();    // "slot-port-card_id" → cell element
  let senderSlots = new Map();  // sender key → slot id, for sender_links
  let lastCards = [];         // merged snapshot+live cards from the last fetch
  // Per-card reads are on demand now, so the view has to carry their age.
  let cardsReadStamp = null;  // raw stamp string, or null if never read
  // How much of what is drawn that stamp actually accounts for. A read covers
  // whatever chain was asked for; the remaining cells keep the stored
  // enumeration's colours, and every aggregate below has to say so.
  let cardsCoverage = { read: 0, total: 0 };
  let lastLive = null;        // the last /api/wall_live payload
  let refreshBusy = false;
  let deviceId = null;        // device to send refresh requests to
  // Mirrors the global emergency stop. app.js owns it and announces it on
  // `nsm:halt`; this file only reads it, to refuse sending and to say why.
  let contactHalted = false;

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
    // `isNaN('')` is false and `'' < 20` is true, so an empty string used to
    // sail past this guard and paint a confident cool-blue "< 20 °C" cell for
    // a card that reported no temperature at all. Same for `isNaN([])`,
    // `isNaN(null)` and `isNaN(false)` — coercion says 0. Only a real finite
    // number is a temperature; everything else is "we do not know".
    if (typeof c !== 'number' || !Number.isFinite(c)) return C_NO_DATA;
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
  // `present` is the WRONG key here and always was. The enumeration writes
  // `present: true` meaning "the binary walk proved this card exists", which
  // is the opposite polarity from the "did this read answer?" question this
  // guard is asking — and the runtime read writes its answer to
  // `bit_error_present`, a different key entirely. So both arms missed, and a
  // card that never answered kept its placeholder `bit_errors: 0` and painted
  // solid green. On the one signal that reveals a break a backup is masking,
  // that is the worst possible failure direction.
  function bitErrorMeasured(card) {
    if (card.bit_errors == null) return false;
    if (card.bit_error_present === false) return false;
    if (card.reading === 'no_answer' || card.reading === 'absent') return false;
    return true;
  }

  function bitErrorState(card) {
    if (!bitErrorMeasured(card)) return 'unknown';
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
        return 'no bit-error reading for this card';
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
    // Age of *this* card's numbers. Cards refresh a chain at a time now, so
    // neighbouring cells can legitimately be hours apart.
    parts.push(cardReadLabel(card));
    return parts.join(' · ');
  }

  // Per-card read age, worded so it can never be mistaken for a live reading.
  function cardReadLabel(card) {
    const stamp = card && card.read_at != null ? card.read_at : null;
    if (stamp == null) return 'not read this session';
    const date = parseReadStamp(stamp);
    if (!date) return `read ${stamp}`;
    return `read ${ageText(date, Date.now())} (${date.toLocaleTimeString()})`;
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
      if (live && live.available) {
        render(live);
      } else {
        showUnavailable(unavailableText(live), live);
      }
    } catch (_) {
      showUnavailable('Could not reach the monitor service.', null);
    } finally {
      inFlight = false;
    }
  }

  // Why there is nothing to draw, in the operator's terms. "No wall data" is
  // true of a fresh install and of a wall that was rebuilt this morning, and
  // those need different actions.
  function unavailableText(live) {
    if (!live) return 'Could not reach the monitor service.';
    if (snapshotStatus(live) === 'mismatch') {
      return 'The stored enumeration is for a different wall, and the controller '
        + 'is not reporting one. Nothing here would be current.';
    }
    return 'No wall data yet — the controller has not reported a screen layout, '
      + 'and no enumeration snapshot exists.';
  }

  function snapshotStatus(live) {
    return (live && live.snapshot_status && live.snapshot_status.status) || null;
  }

  function tick() {
    // A ~400 KB payload and ~1400 cells are not worth rebuilding for a tab
    // nobody is looking at, or a window that is in the background.
    if (document.hidden || !wallTabActive()) return;
    refresh();
  }

  // ── Render ──
  //
  // The card inventory is whichever of the two sources the SERVER allowed:
  // a snapshot it confirmed belongs to this wall, or the controller's own
  // per-card readings. A snapshot the server withheld never reaches here, so
  // there is no path by which cards from a different wall get drawn.
  function collectCards(live) {
    const liveCards = Array.isArray(live.receiving_cards) ? live.receiving_cards : [];
    const snapCards = live.snapshot && Array.isArray(live.snapshot.cards)
      ? live.snapshot.cards : [];

    const liveMap = new Map();
    for (const lc of liveCards) {
      liveMap.set(`${lc.slot}-${lc.port}-${lc.card_id}`, lc);
    }

    if (snapCards.length) {
      return snapCards.map((c) => {
        const key = `${c.slot}-${c.port}-${c.card_id}`;
        return Object.assign({ _key: key }, c, liveMap.get(key) || {});
      });
    }
    // No usable inventory on disk — draw exactly what the controller reported
    // and nothing more.
    return liveCards.map((c) => Object.assign(
      { _key: `${c.slot}-${c.port}-${c.card_id}` }, c));
  }

  function render(live) {
    lastLive = live;
    const cards = collectCards(live);
    lastCards = cards;

    // The signature has to include WHERE the cards came from, not just their
    // addresses: a device read and a snapshot can produce the identical key
    // set while carrying different sender/OPT labelling, and keying on
    // addresses alone left the previous source's tree on screen.
    const sig = `${live.cards_source}|${snapshotStatus(live)}|`
      + (cards.length ? cards.map((c) => c._key).join(',') : 'empty');
    if (sig !== structureKey) {
      if (cards.length) buildTree(cards);
      else showNoInventory(live);
      structureKey = sig;
    }
    paintCells();

    // Freshness comes from the server, which is the only side that knows
    // whether a device is actually connected. `receiving_cards.length > 0`
    // used to stand in for that and would call a stale snapshot "Live".
    const isLive = live.live === true;
    updateHeader(live, isLive);
    // Age of the per-card readings, which is a separate claim from `isLive`.
    updateCardsAge(live, cards);
    updateStats(live, cards, isLive);
    renderLegend();
    noteDeviceId(live);
  }

  // The wall exists and the controller described it, but nobody has walked
  // the chains — so there are no cells to draw. Says which tool builds them.
  function showNoInventory(live) {
    const wrap = document.getElementById('wall-canvas-wrap');
    if (!wrap) return;
    const lead = snapshotStatus(live) === 'mismatch'
      ? 'No per-card map for this wall. The stored enumeration belongs to a '
        + 'different wall and has been withheld.'
      : 'No per-card map for this wall yet.';
    wrap.innerHTML = `<div class="wall-empty">${esc(lead)} `
      + `${esc('The controller reports the wall’s geometry but not how many '
        + 'receiving cards are attached, so the chains have to be walked:')}`
      + `<br><code>${esc(enumerateHint(live))}</code></div>`;
    cellEls = new Map();
  }

  function enumerateHint(live) {
    return (live && live.enumerate_hint)
      || 'python3 src/enumerate_wall.py <controller-ip> --yes-contact-hardware';
  }

  function showUnavailable(message, live) {
    lastLive = live || null;
    const wrap = document.getElementById('wall-canvas-wrap');
    if (wrap) {
      wrap.innerHTML = `<div class="wall-empty">${esc(message)}`
        + (live ? `<br><code>${esc(enumerateHint(live))}</code>` : '')
        + '</div>';
    }
    structureKey = null;
    cellEls = new Map();
    lastCards = [];
    cardsReadStamp = null;
    cardsCoverage = { read: 0, total: 0 };

    const nameEl = document.getElementById('wall-name');
    if (nameEl) nameEl.textContent = 'Wall unavailable';
    const countEl = document.getElementById('wall-card-count');
    if (countEl) countEl.textContent = 'panels: unknown';
    const ageEl = document.getElementById('wall-cards-age');
    if (ageEl) {
      ageEl.className = 'badge';
      ageEl.textContent = 'card data: —';
      ageEl.title = '';
    }
    const stats = document.getElementById('wall-stats');
    if (stats) stats.innerHTML = '';
    const legend = document.getElementById('wall-legend');
    if (legend) legend.innerHTML = '';
    renderNotices();
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

  // ── Per-card reading age ──
  // The Live/Snapshot badge above describes the *topology* and the device
  // connection. It says nothing about the per-card temperature, voltage and
  // bit-error numbers the cells are actually coloured by: those are no longer
  // polled on a timer, so a connected device can sit in front of hour-old cell
  // colours. That is the bug this app already shipped once, in the other
  // direction, and it gets its own badge rather than a footnote.
  //
  // Stamps have been both "HH:MM:SS" and full ISO, so both parse. Anything
  // else is shown verbatim with no age claimed.
  const CLOCK_ONLY = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/;

  function parseReadStamp(value) {
    if (value == null || value === '') return null;
    const direct = parseStamp(value);
    if (direct) return direct;
    const m = CLOCK_ONLY.exec(String(value).trim());
    if (!m) return null;
    const now = new Date();
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate(),
      Number(m[1]), Number(m[2]), Number(m[3] || 0));
    // A clock-only stamp in the future is yesterday's, allowing for skew.
    if (d.getTime() > now.getTime() + 60000) d.setDate(d.getDate() - 1);
    return d;
  }

  // What is actually behind the cells on screen: { stamp, read, total }.
  //
  // The device-level `cards_read_at` is consulted LAST, not first. Any read
  // stamps it — including a single 22-card chain on a 286-card wall — so
  // returning it up front reported the whole wall as read moments ago, painted
  // the badge green and suppressed the "Not live" notice while 264 cells were
  // still coloured from the stored enumeration. That enumeration is not blank
  // data either: `wall_live_snapshot.json` carries `online`, `temp_c`,
  // `voltage_v` and `primary_power_ok` per card, so an unread cell shows a
  // confident colour from whenever the wall was last walked. The per-card
  // `read_at` fallback that used to sit below the early return was written for
  // exactly this case and never ran.
  //
  // Two numbers come out, answering two different questions:
  //   read/total — how many of the drawn cells have a reading behind them at
  //     all. `panels.read` is the server's own count of cards it has published
  //     readings for and is the authority when present; counting stamps is the
  //     fallback for payloads that predate the field.
  //   stamp — the OLDEST of those stamps, not the newest. Cards refresh a
  //     chain at a time, so a reading from an hour ago sits on screen beside
  //     one taken a moment ago, and the newer of the two describes only its
  //     own slice. "No fresher than" is the claim this view can actually make.
  function cardsReadState(live, cards) {
    const list = Array.isArray(cards) ? cards : [];
    let stamped = 0;
    let oldest = null;
    let oldestMs = Infinity;
    let unparsed = null;
    for (const card of list) {
      if (!card || card.read_at == null || card.read_at === '') continue;
      stamped++;
      const d = parseReadStamp(card.read_at);
      // A stamp this code cannot read is still evidence that something was
      // read, and is not evidence of when — so it takes precedence over a
      // parseable one and the badge then declines to claim an age at all.
      if (!d) { if (unparsed == null) unparsed = card.read_at; continue; }
      if (d.getTime() < oldestMs) { oldestMs = d.getTime(); oldest = card.read_at; }
    }

    const declared = live && live.panels && typeof live.panels.read === 'number'
      ? live.panels.read : null;
    const read = declared != null
      ? Math.max(0, Math.min(declared, list.length))
      : stamped;

    if (stamped > 0) {
      return {
        stamp: unparsed != null ? unparsed : oldest,
        read, total: list.length,
      };
    }
    // Nothing on any card. The device-level stamp is then the only evidence a
    // read ever happened — and `read` above still says how much of the wall it
    // actually covered, so a stamp with nothing behind it cannot pass as one.
    const device = live && (live.cards_read_at
      || (live.device && live.device.cards_read_at));
    return { stamp: device || null, read, total: list.length };
  }

  // The qualifier every partial aggregate has to carry. null when the readings
  // cover everything drawn, so a full read reads clean.
  function coverageText(cov) {
    if (!cov || !cov.total || cov.read >= cov.total) return null;
    return `${cov.read} of ${cov.total} cards read`;
  }

  // { text, cls, title, stale, age, coverage } — the single description of
  // per-card age used by the badge, the notice and the cell tooltips.
  function cardsAgeInfo() {
    const cov = cardsCoverage;
    const covText = coverageText(cov);
    // A payload has arrived and it carried no card inventory at all. "Never
    // read" would be misleading — there is nothing to read from until the
    // wall has been enumerated.
    if (lastLive && !lastCards.length) {
      return {
        text: 'card data: none',
        cls: 'badge-warning',
        stale: false,
        age: null,
        coverage: null,
        title: 'There is no per-card map for this wall, so there are no '
          + 'temperature, voltage or bit-error readings to age. Enumerate the '
          + 'wall to build one.',
      };
    }
    if (cardsReadStamp == null) {
      return {
        text: 'card data: never read',
        cls: 'badge-warning',
        stale: true,
        age: null,
        coverage: covText,
        title: 'No per-card reading has been taken this session. Cell colours come '
          + 'from the stored enumeration snapshot, which may be days old. '
          + 'Use "Refresh all cards", or the ⟳ on a single chain.',
      };
    }
    // A read happened, but none of the cells on screen came out of it. The
    // stamp is real and describes nothing the operator is looking at, which is
    // the exact confusion this badge exists to prevent.
    if (cov.read === 0) {
      return {
        text: cov.total ? `card data: none of ${cov.total} read` : 'card data: never read',
        cls: 'badge-warning',
        stale: true,
        age: null,
        coverage: covText,
        title: 'A per-card read has been taken, but none of the cells on screen '
          + 'have a reading behind them — their colours come from the stored '
          + 'enumeration snapshot. Use "Refresh all cards", or the ⟳ on a '
          + 'single chain.',
      };
    }
    const date = parseReadStamp(cardsReadStamp);
    if (!date) {
      return {
        text: `card data: read ${cardsReadStamp}`,
        cls: 'badge-warning',
        stale: true,
        age: null,
        coverage: covText,
        title: `Reading time reported as "${cardsReadStamp}" — its age could not be `
          + 'determined, so these colours are not presented as current.',
      };
    }
    const age = ageText(date, Date.now());
    const old = (Date.now() - date.getTime()) > CARDS_FRESH_MS;
    // Partial coverage is stale whatever the clock says. A reading taken one
    // minute ago over 22 of 286 cells does not make the other 264 current, and
    // a green badge over them is the false all-clear.
    const stale = old || covText != null;
    return {
      text: `card data: ${age}`
        + (covText != null ? ` · ${cov.read}/${cov.total}` : ''),
      cls: stale ? 'badge-warning' : 'badge-success',
      stale,
      age,
      coverage: covText,
      title: 'The OLDEST per-card reading behind these cells was taken at '
        + `${date.toLocaleString()} — cards refresh a chain at a time, so `
        + 'neighbouring cells can legitimately be hours apart.'
        + (covText != null
          ? ` Only ${covText}; the other ${cov.total - cov.read} cells are `
            + 'coloured from the stored enumeration snapshot, not from a reading.'
          : '')
        + (old ? ' These are not live values — refresh to update them.' : ''),
    };
  }

  function updateCardsAge(live, cards) {
    const state = cardsReadState(live, cards);
    cardsReadStamp = state.stamp;
    cardsCoverage = { read: state.read, total: state.total };
    renderCardsAge();
  }

  function renderCardsAge() {
    const info = cardsAgeInfo();

    const badge = document.getElementById('wall-cards-age');
    if (badge) {
      badge.className = `badge ${info.cls}`;
      badge.textContent = info.text;
      badge.title = info.title;
    }
    renderNotices();
  }

  // The per-card-age notice. Suppressed when there are no cells on screen —
  // "these cell colours are old" is meaningless with nothing drawn.
  function cardsAgeNoticeHtml() {
    if (!lastCards.length) return null;
    const info = cardsAgeInfo();
    if (!info.stale) return null;
    // "No fresher than" rather than "from a reading taken": the age is the
    // oldest stamp on screen, and the coverage line says how much of the wall
    // any reading covers at all.
    const lead = info.age == null
      ? 'These cell colours come from the stored enumeration — no per-card reading is behind them.'
      : `These cell colours are no fresher than a reading taken ${esc(info.age)}, and are not live values.`;
    const partial = info.coverage
      ? ` Only ${esc(info.coverage)} — the rest are the stored enumeration’s colours.`
      : '';
    const tail = contactHalted
      ? ' Device contact is halted, so nothing can be read until it is resumed.'
      : ' Per-card temperature and voltage are read on demand only — refresh the wall, or a single chain, for current readings.';
    return `<strong>Not live.</strong> ${lead}${partial}${esc(tail)}`;
  }

  // ── Stale snapshot ──
  // The loudest thing this view can say. A snapshot enumerated on a wall that
  // has since been reconfigured is not "old data about this wall" — it is
  // data about a different wall, and the server withholds it entirely. Both
  // wall names are printed so the operator can see the actual disagreement
  // rather than being told to trust a verdict.
  function snapshotMismatchHtml(live) {
    if (snapshotStatus(live) !== 'mismatch') return null;
    const ss = live.snapshot_status || {};
    const snapName = (ss.snapshot && ss.snapshot.screen_name) || 'an unnamed wall';
    const liveName = (ss.live && ss.live.screen_name) || 'the wall now configured';
    const reasons = Array.isArray(ss.reasons) ? ss.reasons : [];
    const detail = reasons.length
      ? `<ul style="margin:4px 0 4px 18px;">${reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>`
      : '';
    return `<strong>The stored enumeration is for a different wall.</strong> `
      + `It was captured on <strong>${esc(snapName)}</strong>; the controller is `
      + `now driving <strong>${esc(liveName)}</strong>. `
      + esc('Its card map is not shown at all, because none of it describes this wall.')
      + detail
      + esc('Re-enumerate this wall to rebuild the card map: ')
      + `<code>${esc(enumerateHint(live))}</code>`;
  }

  // Snapshot present but uncheckable (controller silent). Not proof it is
  // wrong, so its cards are still drawn — but never as confirmed.
  function snapshotUnverifiedHtml(live) {
    if (snapshotStatus(live) !== 'unverified') return null;
    if (!lastCards.length) return null;
    const f = freshness(live);
    return `<strong>Unconfirmed wall.</strong> `
      + esc('The controller has not reported its screen layout, so this stored '
        + 'card map could not be checked against the wall that is currently '
        + 'configured. It is shown as history, not as the wall’s state — ')
      + esc(f.text) + (f.full ? esc(` (${f.full})`) : '') + esc('.');
  }

  // ── Data breaks ──
  // The reason this app exists. A break does not necessarily take panels
  // dark: on a wall with backup sender cards the chain splits at the break,
  // the primary feeds up to it and the backup feeds the rest, and every panel
  // still answers. "All cards online" is not evidence of a healthy wall —
  // this is, and it is the loudest thing on the page after a stale snapshot.
  function chainBreaksHtml(live) {
    const breaks = (live && live.chain_breaks) || [];
    if (!breaks.length) return null;
    const rows = breaks.map((b) => {
      const where = b.at_head
        ? 'at or before the first panel'
        : `at panel ${b.break_panel}`;
      const how = b.signature === 'no_answer'
        ? `${b.affected} panel${b.affected === 1 ? '' : 's'} stopped answering`
        : `${b.affected} panel${b.affected === 1 ? '' : 's'} reporting bit errors`;
      return `<li>Sender card ${esc(b.card_number)}, port ${esc(b.port + 1)} — `
        + `<strong>${esc(where)}</strong>. ${esc(how)}; `
        + `${esc(b.clean_before)} clean before it.</li>`;
    }).join('');
    const n = breaks.length;
    return `<strong>Suspected data break${n === 1 ? '' : 's'}.</strong> `
      + `<ul style="margin:4px 0 4px 18px;">${rows}</ul>`
      + `The first affected panel is where the signal path broke — check the `
      + `cable into it. Panels after a break may still be lit by a backup `
      + `sender card.`;
  }

  function renderNotices() {
    const note = document.getElementById('wall-stale-note');
    if (!note) return;
    const blocks = [
      snapshotMismatchHtml(lastLive),
      chainBreaksHtml(lastLive),
      snapshotUnverifiedHtml(lastLive),
      cardsAgeNoticeHtml(),
    ].filter(Boolean);
    if (!blocks.length) {
      note.classList.remove('visible');
      note.textContent = '';
      return;
    }
    note.classList.add('visible');
    note.innerHTML = blocks.map((b) => `<div>${b}</div>`).join('');
  }

  // ── Header ──
  // The wall's identity comes from the controller. The snapshot is only a
  // fallback for the name when the controller has said nothing, and it is
  // labelled as such — never silently substituted.
  function wallIdentity(live) {
    const t = live.topology;
    if (t && t.screen_name) return { name: t.screen_name, source: 'device' };
    if (live.snapshot && live.snapshot.screen_name) {
      return { name: live.snapshot.screen_name, source: 'snapshot' };
    }
    return { name: t ? 'Unnamed screen' : 'Wall', source: null };
  }

  function updateHeader(live, isLive) {
    const nameEl = document.getElementById('wall-name');
    if (nameEl) {
      const ident = wallIdentity(live);
      // `||` binds looser than `+`, so the old expression appended the badge
      // to the fallback string only and the badge never appeared.
      let badge;
      let trailer = '';
      if (live.topology) {
        const pollNote = live.last_poll ? ` — last poll ${live.last_poll}` : '';
        const what = isLive
          ? 'Name, canvas and outputs read from the controller; per-card readings are live'
          : 'Name, canvas and outputs read from the controller. Per-card readings are '
            + 'a separate claim — see the card-data badge';
        badge = `<span class="badge badge-success" style="margin-left:6px;" `
          + `title="${esc(what + pollNote)}">Live topology</span>`;
      } else {
        const f = freshness(live);
        const why = live.device_connected
          ? 'Device is connected but has not reported a screen layout'
          : 'No device connected';
        const title = f.full ? `${why}. ${f.text} (${f.full}).` : `${why}. ${f.text}.`;
        badge = `<span class="badge badge-warning" style="margin-left:6px;" title="${esc(title)}">Not reported by controller</span>`;
        // The age is spelled out in the header, not just the tooltip: a wall
        // of temperatures nobody measured today must not read as current.
        trailer = ` <span style="font-size:11px;color:var(--text-muted);">${esc(f.text)}${f.full ? esc(' · ' + f.full) : ''}</span>`;
      }
      if (ident.source === 'snapshot') {
        trailer += ` <span style="font-size:11px;color:var(--text-muted);">${esc('(name from stored snapshot — unconfirmed)')}</span>`;
      }
      nameEl.innerHTML = esc(ident.name) + ' ' + badge + trailer;
    }
    const countEl = document.getElementById('wall-card-count');
    if (countEl) {
      const p = live.panels || {};
      const parts = [];
      const noun = p.count === 1 ? 'panel' : 'panels';
      if (p.known && typeof p.count === 'number') {
        parts.push(`${p.count} ${noun}`);
      } else if (typeof p.count === 'number') {
        parts.push(`${p.count} ${noun} (unconfirmed)`);
      } else {
        parts.push('panels: unknown');
      }
      // Two different numbers, both true, so show both. `slot_count` is how
      // many sender cards the controller has (4 on this H15: two primary,
      // two backup). `populated_sender_cards` is how many are carrying panels
      // right now — the backups answer nothing until they take over, so they
      // are correctly absent from the inventory. Showing only the first hides
      // a failover; showing only the second looks like half the hardware
      // vanished.
      const t = live.topology;
      const installed = t && typeof t.slot_count === 'number' && t.slot_count > 0
        ? t.slot_count : null;
      const carrying = typeof p.populated_sender_cards === 'number'
        ? p.populated_sender_cards : null;
      if (installed != null) {
        let text = `${installed} sender card${installed === 1 ? '' : 's'}`;
        if (carrying != null && carrying !== installed) {
          text += ` (${carrying} carrying panels)`;
        }
        parts.push(text);
      } else if (carrying != null && carrying > 0) {
        parts.push(`${carrying} sender card${carrying === 1 ? '' : 's'} carrying panels`);
      }
      countEl.textContent = parts.join(' · ');
      countEl.title = p.reason || '';
    }
  }

  // Full structural rebuild. Only runs when the topology itself changes —
  // routine refreshes just recolour the existing cells.
  function buildTree(cards) {
    const wrap = document.getElementById('wall-canvas-wrap');
    if (!wrap) return;
    senderSlots = new Map();

    // The controller's own per-card readings carry slot/port/card_id but not
    // the snapshot's card_number/opt bookkeeping, so both groupings have to
    // survive a null: an inventory read straight off the device must not
    // collapse into one "Card undefined" heap.
    const senderKey = (c) => (c.card_number != null ? c.card_number
      : (c.slot != null ? `slot ${c.slot}` : 'unknown'));
    const tree = new Map();  // sender → Map(optNum → Map(port → cards[]))
    const labels = new Map();
    for (const c of cards) {
      const sk = senderKey(c);
      if (!tree.has(sk)) {
        tree.set(sk, new Map());
        labels.set(sk, c.card_number != null ? `Card ${c.card_number}`
          : (c.slot != null ? `Slot ${c.slot}` : 'Unknown sender'));
      }
      // sender_links is keyed by slot, and a sender may be keyed here by card
      // number, so remember the mapping while we have both in hand.
      if (c.slot != null && !senderSlots.has(sk)) senderSlots.set(sk, c.slot);
      const opts = tree.get(sk);
      const ok = c.opt != null ? c.opt : null;
      if (!opts.has(ok)) opts.set(ok, new Map());
      const ports = opts.get(ok);
      if (!ports.has(c.port)) ports.set(c.port, []);
      ports.get(c.port).push(c);
    }

    // Numbers first and in order, then anything else alphabetically — a mixed
    // key set is a data shape, not an error.
    const mixedSort = (a, b) => {
      const an = typeof a === 'number';
      const bn = typeof b === 'number';
      if (an && bn) return a - b;
      if (an !== bn) return an ? -1 : 1;
      return String(a).localeCompare(String(b));
    };
    const numeric = (a, b) => mixedSort(a, b);
    const senderNums = [...tree.keys()].sort(mixedSort);

    let html = '<div class="device-tree">';
    for (const sn of senderNums) {
      const opts = tree.get(sn);
      const optNums = [...opts.keys()].sort(numeric);
      let senderTotal = 0;
      optNums.forEach((o) => opts.get(o).forEach((chain) => { senderTotal += chain.length; }));

      html += `<div class="sender-card">
        <div class="sender-card-head">
          <span class="sender-card-kicker">Sender Card</span>
          <span class="sender-card-title">${esc(labels.get(sn))}</span>
          <span class="badge badge-info">${senderTotal} cards</span>
        </div>
        <div class="opt-columns">
          ${optNums.map((o) => renderOptColumn(o, opts.get(o), senderMedium(sn))).join('')}
        </div>
      </div>`;
    }
    html += '</div>';

    wrap.innerHTML = html;

    cellEls = new Map();
    wrap.querySelectorAll('.dt-cell').forEach((el) => cellEls.set(el.dataset.k, el));
    // The per-chain buttons were just recreated, so re-apply whatever the
    // emergency stop / in-flight state says about them.
    setRefreshBusy(refreshBusy);
  }

  // The controller reports, per sender card, two OPT link states and sixteen
  // Ethernet ones (R0100 `lightstatus` / `linkstatus`). A card patched
  // straight out of its Ethernet ports has no OPT link up at all, and calling
  // its chains "OPT 1" is simply wrong — on the reference wall, card 1 runs
  // fibre and card 2 runs copper, and the map labelled both as OPT.
  //
  // `medium` is null when the controller has not reported it, and then the
  // column says so rather than guessing.
  function senderMedium(senderKeyValue) {
    const links = (lastLive && lastLive.sender_links) || null;
    if (!links) return null;
    const slot = senderSlots.get(senderKeyValue);
    if (slot == null) return null;
    const entry = links[String(slot)];
    return entry ? entry.medium : null;
  }

  function optColumnLabel(optNum, medium) {
    if (medium === 'ethernet') {
      return optNum != null ? `Ethernet (group ${optNum})` : 'Ethernet';
    }
    if (medium === 'opt') {
      return optNum != null ? `OPT ${optNum}` : 'OPT';
    }
    // Unknown medium: name the group without claiming which cable it is.
    return optNum != null ? `Port group ${optNum}` : 'Ports';
  }

  function renderOptColumn(optNum, ports, medium) {
    const portNums = [...ports.keys()].sort((a, b) => a - b);
    const total = portNums.reduce((sum, p) => sum + ports.get(p).length, 0);

    let html = `<div class="opt-column" data-medium="${esc(medium || 'unknown')}">
      <div class="opt-column-head">
        <span class="opt-column-label">${esc(optColumnLabel(optNum, medium))}</span>
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
    // One chain is tens of cards, so this is the cheap read: it asks the
    // controller for exactly the cards on this port and nothing else.
    // `slot` / `port` are the addressing the API takes — `port_on_opt` is the
    // operator-facing label and is not interchangeable with them.
    const slot = chain[0] && chain[0].slot != null ? chain[0].slot : null;
    const addr = chain[0] && chain[0].port != null ? chain[0].port : port;
    const refreshBtn = slot != null && addr != null
      ? `<button type="button" class="chain-refresh" data-wall-action="refresh-chain"
          data-slot="${esc(slot)}" data-port="${esc(addr)}"
          title="Read the ${chain.length} cards on slot ${esc(slot)} port ${esc(addr)} now">⟳</button>`
      : '';
    return `<div class="port-row">
      <span class="port-row-label">Port ${esc(portOnOpt)}</span>
      <span class="badge port-row-count">${chain.length}</span>
      ${refreshBtn}
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
  //
  // Geometry stats come from the controller; card stats come from whatever
  // inventory was allowed through. Panel count is the one that has to be
  // three-valued: the processor reports how big the canvas is, never how many
  // receiving cards hang off it, so "unknown" is the truthful answer whenever
  // no enumeration for THIS wall exists.
  function panelStat(live) {
    const p = live.panels || {};
    if (p.known && typeof p.count === 'number') {
      const how = p.source === 'device_read'
        ? 'Read from the controller this session.'
        : 'From the enumeration snapshot, confirmed to describe this wall.';
      return { label: 'Panels', value: String(p.count), cls: '', title: how };
    }
    if (typeof p.count === 'number') {
      return {
        label: 'Panels', value: `${p.count}?`, cls: 'color-warning',
        title: (p.reason || 'This count could not be confirmed against the controller.'),
      };
    }
    return {
      label: 'Panels', value: 'unknown', cls: 'color-muted',
      title: (p.reason || 'No enumeration exists for the wall that is currently '
        + 'configured.') + ' ' + enumerateHint(live),
    };
  }

  // Explicitly capacity, never "panels detected": it is canvas ÷ panel pitch.
  function capacityStat(live) {
    const cap = live.panels && live.panels.capacity;
    if (!cap || typeof cap.panels !== 'number') return null;
    const pitch = cap.panel ? `${cap.panel.width}x${cap.panel.height}px panels` : 'the configured panel size';
    return {
      label: 'Capacity (geometry)', value: String(cap.panels), cls: 'color-muted',
      title: `The canvas would tile with ${cap.columns} x ${cap.rows} ${pitch}. `
        + 'This is what the geometry allows, NOT a count of panels that are '
        + 'attached, cabled or powered.',
    };
  }

  // "Active outputs" is the number of independently driven regions of the
  // canvas. On this wall R0405 lists 16 output connections, but they are four
  // sender cards each covering the same four 960-wide columns — so the answer
  // is 4, and 16 is reported beside it as connections, never as ports in use.
  function outputStats(live) {
    const t = live.topology;
    if (!t) return [];
    const out = [];
    const active = typeof t.active_outputs === 'number' ? t.active_outputs : null;
    const total = typeof t.outputs_total === 'number' ? t.outputs_total : null;
    let title = 'Distinct regions of the canvas driven by the sender cards.';
    if (total != null) title += ` The controller lists ${total} output connection${total === 1 ? '' : 's'}.`;
    if (t.redundant) {
      title += ` Each of the ${t.redundancy_factor} sender cards covers the same `
        + 'regions, so this is redundancy — not that many separate outputs.';
    }
    if (t.card_online_known === false) {
      title += ' The controller reports no online flag for these cards, so which '
        + 'sender card is actually driving cannot be determined here.';
    }
    out.push({
      label: 'Active outputs', value: active != null ? String(active) : '—',
      cls: '', title,
    });
    if (total != null && active != null && total !== active) {
      out.push({
        label: 'Output connections', value: String(total), cls: 'color-muted',
        title: `${total} physical outputs across ${t.slot_count} sender card`
          + `${t.slot_count === 1 ? '' : 's'}`
          + (t.redundant ? ', driving the same regions in parallel.' : '.'),
      });
    }
    if (t.canvas) {
      const mosaic = t.mosaic ? ` Mosaic ${t.mosaic.row} x ${t.mosaic.column}.` : '';
      out.push({
        label: 'Canvas', value: `${t.canvas.width}x${t.canvas.height}`,
        cls: '', title: `As reported by the controller.${mosaic}`,
      });
    }
    if (Array.isArray(t.sender_slots) && t.sender_slots.length) {
      out.push({
        label: 'Sender slots', value: t.sender_slots.join(', '), cls: '',
        title: 'Slot ids the controller reports driving this screen, primary '
          + 'and backup alike. A backup answers nothing over R0155 and has no '
          + 'cards in the inventory until it takes over.',
      });
    }
    return out;
  }

  function updateStats(live, cards, hasLive) {
    const wrap = document.getElementById('wall-stats');
    if (!wrap) return;

    // Strict `=== true`: a card the controller did not report on is not
    // evidence that it is up. Erring toward "fewer online" keeps a silent
    // dropout visible instead of padding the healthy count.
    const online = cards.filter((c) => c.online === true).length;
    const offline = cards.filter((c) => c.online === false).length;
    // The residual. Strict ===true/===false is right, but leaving the rest
    // unrendered meant "Offline 0" read as an all-clear over a wall where 250
    // cards' state was simply unknown.
    const unread = cards.length - online - offline;
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

    // How much of the wall the numbers above are actually backed by. Set by
    // updateCardsAge(), which render() calls immediately before this.
    //
    // Every aggregate here is computed over the whole inventory, but on a wall
    // where one 22-card chain has been read and the other 264 cells still
    // carry the stored enumeration's values, "Avg temp 39.7 °C" is not the
    // wall's average and "Supply faults 0" is not an all-clear — it is 22
    // measurements and 264 recollections, presented identically. The qualifier
    // rides on the stat itself rather than living in a tooltip nobody opens.
    const cov = cardsCoverage;
    const covNote = coverageText(cov);
    const covTitle = covNote
      ? ` Computed over all ${cov.total} cards, but only ${covNote} this session — `
        + `the other ${cov.total - cov.read} contribute the stored enumeration’s `
        + 'numbers, which are not measurements taken now.'
      : '';

    // Without live data every "online/offline" figure would be a guess — say
    // so rather than printing a reassuring zero.
    const cells = [panelStat(live), capacityStat(live)]
      .filter(Boolean)
      .concat(outputStats(live))
      .concat([
        { label: 'Online', value: hasLive ? String(online) : '—', cls: hasLive && online > 0 ? 'color-success' : 'color-muted' },
        { label: 'Offline', value: hasLive ? String(offline) : '—', cls: hasLive && offline > 0 ? 'color-danger' : 'color-muted' },

        { label: 'No reading', value: String(unread),
          cls: unread > 0 ? 'color-warning' : 'color-muted',
          title: 'Cards whose state is unknown — never read, or the '
            + 'controller did not answer. Not a fault, and not an all-clear.' },
        // Temperatures survive without live data because the cells show them
        // too — but they are the SAME historical numbers, so they are muted
        // and say so rather than sitting there looking like a measurement.
        { label: 'Avg temp', value: avgT != null ? `${avgT.toFixed(1)}°C` : '—',
          cls: !hasLive ? 'color-muted' : (avgT != null && avgT > 60 ? 'color-warning' : ''),
          note: covNote,
          title: (hasLive ? '' : 'From the stored card map — not a current reading.')
            + covTitle },
        { label: 'Max temp', value: maxT != null ? `${maxT.toFixed(1)}°C` : '—',
          cls: !hasLive ? 'color-muted' : (maxT != null && maxT > 70 ? 'color-danger' : ''),
          note: covNote,
          title: (hasLive ? '' : 'From the stored card map — not a current reading.')
            + covTitle
            + (covNote ? ' The hottest card on the wall may simply not have been read.' : '') },
        // A found fault is a found fault whatever the coverage; a zero is only
        // an all-clear over cards that were actually asked. Both get the
        // qualifier, and the zero says out loud what it does not cover.
        { label: 'Supply faults', value: hasLive ? String(faults) : '—',
          cls: hasLive && faults > 0 ? 'color-danger' : 'color-muted',
          note: covNote,
          title: (covNote && faults === 0
            ? `Zero here is not an all-clear: ${covNote}, so the supplies of the `
              + `other ${cov.total - cov.read} cards are simply unknown.`
            : 'Cards reporting exactly one of their two supplies bad.')
            + covTitle },
        { label: 'Cards w/ bit errors', value: hasLive ? berValue : '—', cls: hasLive ? berClass : 'color-muted' },
      ]);

    wrap.innerHTML = cells.map((c) => `
      <div class="stat-card" style="padding:8px 10px;" title="${esc(c.title || '')}">
        <div class="stat-label">${esc(c.label)}</div>
        <div class="stat-value ${esc(c.cls || '')}" style="font-size:18px;">${esc(c.value)}</div>
        ${c.note ? `<div class="stat-note">${esc(c.note)}</div>` : ''}
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

  // ── On-demand per-card refresh ──
  // The automatic ~1374-card sweep is gone: it was one of the behaviours that
  // took the operator's control surface away mid-show. Per-card readings are
  // now requested explicitly, either one chain at a time (cheap) or for the
  // whole wall (rate limited by the server).
  //
  // The three server verdicts mean three different things and are never
  // collapsed into "it failed":
  //   ok      — cards were read; how many is in the response.
  //   refused — the rate limiter said not yet. Nothing is wrong; wait.
  //   halted  — the emergency stop is engaged. Nothing was sent, and the fix
  //             is to resume contact, not to retry.
  function noteDeviceId(live) {
    const fromDevice = live && live.device
      && (live.device.device_id || live.device.id);
    const id = fromDevice || (live && live.device_id) || null;
    if (id) deviceId = String(id);
  }

  // Fallback when /api/wall_live doesn't name the device: ask the device list.
  // Prefers the connected H-series, which is the one the wall view renders.
  async function ensureDeviceId() {
    if (deviceId) return deviceId;
    try {
      const res = await fetch('/api/devices');
      const list = res.ok ? await res.json() : null;
      if (Array.isArray(list) && list.length) {
        const pick = list.find((d) => d && d.device_type === 'h_series' && d.connected)
          || list.find((d) => d && d.connected)
          || list[0];
        if (pick && pick.device_id != null) deviceId = String(pick.device_id);
      }
    } catch (_) { /* leave deviceId null — the caller reports it */ }
    return deviceId;
  }

  function setRefreshStatus(text, cls) {
    const el = document.getElementById('wall-refresh-status');
    if (!el) return;
    el.className = 'wall-refresh-status ' + (cls || '');
    el.textContent = text || '';
  }

  function setRefreshBusy(busy) {
    refreshBusy = busy;
    ['wall-refresh-all', 'wall-read-bit-errors', 'wall-zero-bit-errors']
      .forEach((id) => {
        const b = document.getElementById(id);
        if (b) b.disabled = busy || contactHalted;
      });
    document.querySelectorAll('.chain-refresh').forEach((b) => {
      b.disabled = busy || contactHalted;
    });
  }

  function describeResult(data, label) {
    const status = data && typeof data.status === 'string' ? data.status : null;
    const reason = data && typeof data.reason === 'string' && data.reason
      ? ` — ${data.reason}` : '';

    if (status === 'busy') {
      return {
        text: `${label}: a read is already running on this device — `
          + 'wait for it to finish. Starting a second would spend the '
          + "controller's request budget twice as fast and corrupt both.",
        cls: 'color-warning',
      };
    }

    if (status === 'ok') {
      const count = typeof data.cards === 'number' ? data.cards : null;
      if (count === 0) {
        return {
          text: `${label}: the read completed but no cards answered${reason}`,
          cls: 'color-warning',
        };
      }
      return {
        text: `${label}: read ${count != null ? count : 'the'} card${count === 1 ? '' : 's'}`
          + ` at ${new Date().toLocaleTimeString()}`,
        cls: 'color-success',
      };
    }
    if (status === 'refused') {
      // Not a failure — saying "failed" here teaches the operator to hammer
      // the button, which is exactly what the rate limit exists to stop.
      return {
        text: `${label}: not yet — a full read was taken recently${reason}. `
          + 'Nothing is wrong; wait a few minutes and try again, or refresh a single chain.',
        cls: 'color-warning',
      };
    }
    if (status === 'halted') {
      return {
        text: `${label}: nothing was sent — device contact is halted${reason}. `
          + 'Resume contact from the top bar; retrying will not help.',
        cls: 'color-danger',
      };
    }
    return {
      text: `${label}: unexpected reply from the monitor service`
        + (status ? ` (status "${status}")` : '') + reason,
      cls: 'color-warning',
    };
  }

  async function requestRefresh(body, label, endpoint) {
    if (refreshBusy) return;

    if (contactHalted) {
      // Refuse locally too. The server would answer "halted" anyway, but this
      // way the emergency stop visibly means "this app sends nothing".
      setRefreshStatus(`${label}: device contact is halted — nothing was sent. `
        + 'Resume contact from the top bar first.', 'color-danger');
      return;
    }

    setRefreshBusy(true);
    setRefreshStatus(`${label}: reading…`, 'color-muted');

    const id = await ensureDeviceId();
    if (!id) {
      setRefreshBusy(false);
      setRefreshStatus('No connected device to read from — per-card readings need a live controller.',
        'color-warning');
      return;
    }

    try {
      const path = endpoint || 'refresh_cards';
      const res = await fetch(`/api/devices/${encodeURIComponent(id)}/${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!res.ok && (!data || !data.status)) {
        setRefreshStatus(`${label}: the monitor service answered HTTP ${res.status}.`,
          'color-danger');
      } else {
        const info = describeResult(data, label);
        setRefreshStatus(info.text, info.cls);
      }
    } catch (err) {
      setRefreshStatus(`${label}: could not reach the monitor service (${err}).`, 'color-danger');
    } finally {
      setRefreshBusy(false);
      // Pull the new readings (and their stamps) straight back, so the cells
      // and the age badge reflect what was just read.
      refresh();
    }
  }

  // Zeroing is deliberately the LOCAL baseline, not the device write. The
  // controller's counter is cumulative and clearing it discards the only
  // evidence of an intermittent link for whoever looks next — that is an
  // explicit decision, not something a button in a dashboard should do
  // quietly. The device-side clear exists at /bit_errors/clear and requires
  // {"confirm": true}.
  async function zeroBitErrors() {
    if (refreshBusy) return;
    const id = await ensureDeviceId();
    if (!id) {
      setRefreshStatus('No connected device.', 'color-warning');
      return;
    }
    setRefreshBusy(true);
    try {
      const res = await fetch(
        `/api/devices/${encodeURIComponent(id)}/bit_errors/baseline`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ zero: true }),
        });
      const data = await res.json().catch(() => null);
      if (data && data.status === 'ok') {
        setRefreshStatus(
          `Counters zeroed for ${data.cards} card${data.cards === 1 ? '' : 's'} `
          + '— in this dashboard only. The controller\'s own counters are '
          + 'untouched.', 'color-muted');
      } else {
        setRefreshStatus('Could not zero the counters.', 'color-warning');
      }
    } catch (err) {
      setRefreshStatus(`Could not zero the counters: ${err}`, 'color-danger');
    } finally {
      setRefreshBusy(false);
      refresh();
    }
  }

  // ── Read progress ──
  // A whole-wall pass is 286 sequential per-card reads and pauses ~45 s
  // partway while the controller recovers its request budget. The pause is
  // the part that matters: a bar that stops dead for three quarters of a
  // minute reads as a crash, and killing the pass mid-show wastes the budget
  // it already spent.
  let progressHideTimer = null;

  function renderReadProgress(info) {
    const box = document.getElementById('read-progress');
    if (!box || !info) return;
    const total = Number(info.total) || 0;
    const done = Number(info.done) || 0;
    const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

    box.hidden = false;
    box.classList.toggle('is-resting', info.phase === 'resting');
    box.classList.toggle('is-done', info.phase === 'done');
    box.classList.toggle('is-halted', info.phase === 'halted');

    const label = document.getElementById('read-progress-label');
    const count = document.getElementById('read-progress-count');
    const bar = document.getElementById('read-progress-bar');
    const note = document.getElementById('read-progress-note');
    if (label) label.textContent = `Reading ${info.label || 'cards'}`;
    if (count) count.textContent = total ? `${done} / ${total}` : '';
    if (bar) bar.style.width = `${info.phase === 'done' ? 100 : pct}%`;

    if (note) {
      if (info.phase === 'resting') {
        const secs = Number(info.rest_seconds) || 0;
        note.textContent = `Pausing ${secs ? Math.round(secs) + 's' : ''} — the `
          + 'controller stops answering after a few hundred reads and needs '
          + 'quiet to recover. This is normal; leave it running.';
      } else if (info.phase === 'halted') {
        note.textContent = 'Stopped — device contact was halted.';
      } else if (info.phase === 'done') {
        note.textContent = 'Finished.';
      } else {
        note.textContent = '';
      }
    }

    clearTimeout(progressHideTimer);
    if (info.phase === 'done' || info.phase === 'halted') {
      progressHideTimer = setTimeout(() => { box.hidden = true; }, 6000);
    }
  }

  function bindReadProgress() {
    // app.js owns the socket; it re-emits on the document so the two files
    // keep sharing no globals.
    document.addEventListener('nsm:read-progress', (evt) => {
      renderReadProgress(evt.detail);
      // A flush means the server just published a partial result. Pull it
      // immediately instead of waiting out the poll interval — the whole
      // point of flushing is that the wall fills in as the read proceeds,
      // and a five-second lag on every batch loses that.
      if (evt.detail && evt.detail.phase === 'flush') refresh();
    });
  }

  function bindRefreshControls() {
    // Delegated, like everything else here — the chain buttons are rebuilt on
    // every structural change and carry their address in data-* attributes.
    document.addEventListener('click', (evt) => {
      const el = evt.target.closest('[data-wall-action]');
      if (!el) return;
      evt.preventDefault();
      if (el.dataset.wallAction === 'refresh-all') {
        // Binary, not R0155. R0155 answers ~150 cards then goes quiet, which
        // left most of the wall with no readings; the binary register covers
        // every card and returns link status too.
        requestRefresh({}, 'Whole wall', 'live_readings');
        return;
      }
      if (el.dataset.wallAction === 'refresh-chain') {
        const slot = Number(el.dataset.slot);
        const port = Number(el.dataset.port);
        if (!Number.isFinite(slot) || !Number.isFinite(port)) return;
        requestRefresh({ slot, port }, `Slot ${slot} port ${port}`,
                       'live_readings');
        return;
      }
      // Bit errors are a separate read: binary-only, no R0155 equivalent, and
      // the only signal that shows a break the backup is hiding.
      if (el.dataset.wallAction === 'read-bit-errors') {
        requestRefresh({}, 'Bit errors', 'bit_errors');
        return;
      }
      if (el.dataset.wallAction === 'zero-bit-errors') {
        zeroBitErrors();
      }
    });
  }

  // app.js owns the emergency stop and announces changes on this event; the
  // two files share no globals, so the DOM event is the whole interface.
  function bindHaltState() {
    document.addEventListener('nsm:halt', (evt) => {
      contactHalted = !!(evt.detail && evt.detail.halted);
      setRefreshBusy(refreshBusy);
      if (contactHalted) {
        setRefreshStatus('Device contact is halted — per-card reads are unavailable '
          + 'until contact is resumed.', 'color-danger');
      } else {
        setRefreshStatus('', '');
      }
      // The stale notice's wording depends on whether a refresh is possible.
      // Re-render only — the halt says nothing about when cards were read.
      renderCardsAge();
    });
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
    bindRefreshControls();
    bindReadProgress();
    bindHaltState();
    renderLegend();
    // Say "never read" before the first fetch rather than an empty badge —
    // an unlabelled wall of colours is the thing being avoided here.
    renderCardsAge();

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
