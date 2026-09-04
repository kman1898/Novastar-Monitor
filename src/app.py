"""
NovaStar Monitor — Flask + SocketIO Backend
Following the LED Raster Designer app pattern.
"""

from flask import Flask, render_template, request, jsonify, url_for
from flask_socketio import SocketIO, emit
from werkzeug.exceptions import HTTPException
import copy
import ipaddress
import json
import os
import sys
import tempfile
import time
import threading
import logging
import traceback
from datetime import datetime

# Support PyInstaller bundle
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# App directory (writable, for logs/settings)
if getattr(sys, 'frozen', False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Logging Setup ─────────────────────────────────────────
LOG_DIR_PATH = os.path.join(_APP_DIR, 'logs')
LOG_FILE_PATH = os.path.join(LOG_DIR_PATH, 'novastar_monitor.log')
LOG_MAX_BYTES = 20 * 1024 * 1024   # 20 MB max file size
LOG_BACKUPS = 2                     # Keep 2 backup rotations
os.environ['_NSM_LOG_DIR'] = LOG_DIR_PATH
os.makedirs(LOG_DIR_PATH, exist_ok=True)
print(f'[NovaStar Monitor] Log directory: {LOG_DIR_PATH}')


def rotate_logs():
    """Rotate log file if it exceeds LOG_MAX_BYTES."""
    # Note: only `logger` may be used for failures in this file's logging
    # helpers — calling log_event() here would recurse.
    try:
        if os.path.exists(LOG_FILE_PATH) and os.path.getsize(LOG_FILE_PATH) > LOG_MAX_BYTES:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup = os.path.join(LOG_DIR_PATH, f'novastar_monitor_{ts}.log')
            os.rename(LOG_FILE_PATH, backup)
            prune_log_files()
    except Exception:
        logger.exception('Failed to rotate log file %s', LOG_FILE_PATH)


def prune_log_files():
    """Keep only LOG_BACKUPS most recent backup log files."""
    try:
        backups = sorted(
            [f for f in os.listdir(LOG_DIR_PATH)
             if f.startswith('novastar_monitor_') and f.endswith('.log')],
            reverse=True
        )
        for old in backups[LOG_BACKUPS:]:
            os.remove(os.path.join(LOG_DIR_PATH, old))
    except Exception:
        logger.exception('Failed to prune old log files in %s', LOG_DIR_PATH)


def log_event(action, details=None, source='server'):
    """Write a structured JSON event to the log file."""
    rotate_logs()
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    payload = {
        'timestamp': ts,
        'source': source,
        'action': action,
        'details': details,
    }
    try:
        with open(LOG_FILE_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except Exception:
        logger.exception('Failed to write log event %r', action)


logger = logging.getLogger('novastar_monitor')
logger.setLevel(logging.DEBUG)

# Console handler
_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter('[NovaStar Monitor] %(levelname)s — %(message)s'))
logger.addHandler(_ch)


app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, 'templates'),
            static_folder=os.path.join(BASE_DIR, 'static'))
app.config['SECRET_KEY'] = 'novastar-monitor-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

APP_DIR = _APP_DIR
SETTINGS_FILE = os.path.join(APP_DIR, 'novastar_settings.json')

# ── Request / Error Logging Hooks ─────────────────────────

@app.before_request
def _log_request():
    """Log incoming HTTP requests (skip static assets and log endpoint)."""
    if request.path.startswith('/static') or request.path == '/api/log':
        return
    if request.path == '/':
        log_event('http_request', {
            'method': request.method,
            'path': request.path,
            'remote_addr': request.remote_addr,
        })


@app.errorhandler(Exception)
def _handle_error(e):
    """Log unhandled exceptions.

    HTTP errors raised on purpose by Flask (404, 405, 400 …) are *not*
    internal errors — pass them through untouched, otherwise a mistyped URL
    comes back as `500 Internal server error` and every client sees a
    server fault where there is none.
    """
    if isinstance(e, HTTPException):
        return e

    logger.exception('Unhandled exception: %s', e)
    log_event('unhandled_error', {
        'error': str(e),
        'type': type(e).__name__,
        'path': request.path,
        'method': request.method,
        'traceback': traceback.format_exc(),
    })
    return jsonify({'error': 'Internal server error'}), 500


@app.after_request
def _no_store_html(response):
    """Never let a browser cache the page shell.

    The shell carries the versioned asset URLs, so caching it defeats the
    cache-busting entirely: the browser reuses yesterday's HTML, requests
    yesterday's `app.js?v=...`, and the operator sees a dashboard that has not
    changed no matter how hard they reload. Flask sent NO cache headers for
    the page, which leaves the browser free to cache heuristically — Safari
    does.

    Static assets keep their own caching; they are safe to cache precisely
    because their URL changes when the file does.
    """
    if response.mimetype == 'text/html':
        response.headers['Cache-Control'] = 'no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.template_global()
def asset_url(filename):
    """Static asset URL with a cache-buster derived from the file's mtime.

    The version used to be a hand-edited literal (`?v=20260808b`). It was
    changed when somebody remembered, which meant every edit after that point
    shipped under a URL browsers had already cached — the operator reloaded a
    rebuilt dashboard and got the previous one, with no way to tell. An mtime
    stamp cannot be forgotten.

    Falls back to no version if the file is missing rather than raising: a
    templating error would take the whole page down over a cosmetic concern.
    """
    path = os.path.join(app.static_folder, filename)
    try:
        stamp = int(os.path.getmtime(path))
    except OSError:
        return url_for('static', filename=filename)
    return f"{url_for('static', filename=filename)}?v={stamp}"


@app.route('/api/log', methods=['POST'])
def api_client_log():
    """Accept log events from the browser client."""
    data = request.get_json(silent=True) or {}
    log_event(data.get('action', 'client_event'), data.get('details'), source='client')
    return jsonify({'status': 'ok'})


# Import device manager
from device_manager import (
    DeviceManager, DEFAULT_POLL_INTERVAL, FULL_SWEEP_MIN_INTERVAL,
    wall_topology,
)

# Demo mode flag — set via --demo CLI arg or /api/demo endpoint
DEMO_MODE = '--demo' in sys.argv

# DEFAULT_POLL_INTERVAL (10s) is defined in device_manager and imported above —
# the manager, DEFAULT_SETTINGS and the init fallback all read that one
# constant. It lives there rather than here because device_manager cannot
# import app (app imports it), so the manager's own signature default has to be
# the authority; they used to disagree.
MIN_POLL_INTERVAL = 1.0      # below this we'd be hammering the controller
MAX_POLL_INTERVAL = 3600.0   # an hour between polls is already barely monitoring

# Global device manager instance.
manager = DeviceManager(poll_interval=DEFAULT_POLL_INTERVAL)

# ── Settings ──────────────────────────────────────────────

# The low-voltage floor, in volts.
#
# This was 4.7, and 4.7 flags every healthy card on this hardware. The
# receiving cards on the operator's wall report 4.10–4.40 V on the centi
# firmware (h_series_json.decode_volt_centi, calibrated against a 1374-card
# capture), and NovaStar's own H-series SNMP spec gives 4.6 V as its worked
# example of a normal reading. A threshold above the entire normal range is
# not a threshold; it is a guarantee of a false alarm per card per cycle, and
# the alert history in src/error_log.json is exactly that — "Voltage 4.19V
# below minimum threshold of 4.7V" across 15 cards on 3 chains.
#
# 3.8 V sits ~0.3 V (7%) below the bottom of the observed healthy band, which
# is a real sag rather than normal spread. The older byte-schema fleet reads
# the same band once its voltage byte is decoded the way NovaStar documents it
# — lower 7 bits, units of 0.1 V, §4.3.4 / §5.4.2 — putting the 1374-card
# capture's raw 165–173 at 3.7–4.5 V rather than the 4.95–5.19 V this comment
# used to claim. That higher figure came from `raw * 0.03`, a formula this
# project invented while trying to explain away exactly the false alarms the
# 4.7 V floor was producing; the two schemas agree at ~4.2 V once the byte is
# masked. Note the bottom of that byte-schema band (3.7 V) is just under this
# floor, so a card down there will alert — see the note in
# h_series_json.decode_voltage_byte.
#
# Anything under VOLTAGE_DEAD_V (0.5 V) is escalated to CRITICAL separately, so
# this floor's whole job is to catch the middle case: a rail on its way down
# but not yet gone.
DEFAULT_VOLTAGE_MIN = 3.8

# The old default, still sitting in every install's novastar_settings.json.
# See _migrate_settings().
LEGACY_VOLTAGE_MIN = 4.7

DEFAULT_SETTINGS = {
    "devices": [],
    "poll_interval": DEFAULT_POLL_INTERVAL,
    "temp_warning": 60.0,
    "temp_critical": 75.0,
    "voltage_min": DEFAULT_VOLTAGE_MIN,
}

# Settings are read on every device update (i.e. once per card-poll cycle per
# device), so they live in memory. Disk is only touched on first read and on
# write.
_settings_cache = None
_settings_lock = threading.RLock()


def _atomic_write_json(path, data):
    """Write JSON to `path` atomically (tempfile + os.replace).

    Truncate-then-write loses the whole file if the process dies mid-write or
    if two threads interleave — for the alert history that means losing every
    recorded fault. os.replace() is atomic on POSIX and Windows, so a reader
    either sees the old file or the new one, never a half-written one.
    """
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.tmp-',
                                    suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # best-effort cleanup; the original error is re-raised below
        raise


def _migrate_settings(settings):
    """Correct persisted values that a changed default cannot reach.

    A default only applies to a key the settings file does not have, and every
    existing install wrote `voltage_min: 4.7` to disk the first time anything
    was saved. Lowering DEFAULT_SETTINGS therefore fixes nobody who is already
    running the app — including the operator whose wall prompted the fix.

    Only the exact old default is rewritten. A 4.7 in a settings file cannot
    be told apart from an operator who typed 4.7 on purpose, but on this
    hardware 4.7 alarms on every healthy card either way, so leaving it is not
    the conservative option — it is the one that keeps a broken alarm broken.
    Any other value is an operator's choice and is left alone. In place, in
    memory: this does not write to disk, so an install that is deliberately
    running an old threshold gets it back by editing the file, not by hunting
    for whatever rewrote it.
    """
    if settings.get('voltage_min') == LEGACY_VOLTAGE_MIN:
        settings['voltage_min'] = DEFAULT_VOLTAGE_MIN
        logger.warning(
            'voltage_min was the old %.1f V default, which flags every '
            'healthy card (they run 4.1-4.4 V) — using %.1f V instead',
            LEGACY_VOLTAGE_MIN, DEFAULT_VOLTAGE_MIN)
    return settings


def _read_settings_file():
    """Read settings from disk, merged over the defaults.

    Always deep-copies the defaults: DEFAULT_SETTINGS['devices'] is a mutable
    list, and a shallow copy would let a caller's `settings['devices'].append`
    permanently edit the module-level defaults.
    """
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                return _migrate_settings(
                    {**copy.deepcopy(DEFAULT_SETTINGS), **json.load(f)})
    except Exception:
        logger.exception('Failed to read settings from %s — using defaults',
                         SETTINGS_FILE)
    return copy.deepcopy(DEFAULT_SETTINGS)


def load_settings():
    """Return the current settings (cached in memory after the first read)."""
    global _settings_cache
    with _settings_lock:
        if _settings_cache is None:
            _settings_cache = _read_settings_file()
        return copy.deepcopy(_settings_cache)


def reload_settings():
    """Force a re-read from disk (used at startup and by tests)."""
    global _settings_cache
    with _settings_lock:
        _settings_cache = _read_settings_file()
        return copy.deepcopy(_settings_cache)


def save_settings(settings):
    """Persist settings atomically and refresh the in-memory cache."""
    global _settings_cache
    with _settings_lock:
        try:
            _atomic_write_json(SETTINGS_FILE, settings)
        except Exception:
            logger.exception('Failed to save settings to %s', SETTINGS_FILE)
            return False
        _settings_cache = copy.deepcopy(settings)
        return True


def _coerce_number(value, default=None):
    """Return `value` as a float, or `default` if it isn't numeric."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def apply_poll_interval(settings):
    """Push the configured poll interval onto the running DeviceManager.

    Clamped, because the UI is a free-text number field and a 0.0s interval
    would spin the poll threads flat out against live hardware.
    """
    interval = _coerce_number(settings.get('poll_interval'),
                              DEFAULT_POLL_INTERVAL)
    interval = max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, interval))
    manager.poll_interval = interval
    return interval


# ── SocketIO Events ──────────────────────────────────────

# ── Alerting ──────────────────────────────────────────────
#
# Thresholds are evaluated PER RECEIVING CARD, never on the device-level
# average. `live_monitoring.temperature_c` is the mean across every card on
# the wall (~1374 of them here) — one card at 95 °C moves that mean by about
# 0.04 °C, so an average can never cross a 75 °C threshold and the app could
# not detect the one condition it exists to detect.
#
# Four per-card signals are evaluated, all through the same grouping and
# dedupe machinery: temperature and voltage (thresholded readings), power
# supply status (booleans, no threshold) and the bit-error counter.

# One breach re-alerts at most this often, keyed per card+metric+severity.
ALERT_COOLDOWN_SEC = 60.0
# Device-level rollups are a periodic summary line, not an incident.
DEVICE_ALERT_COOLDOWN_SEC = 300.0
# Breaching cards on one chain (slot+port) before they collapse into a single
# chain-level alert. 40 cards on one data run going hot together is ONE event
# — a failed run or a dead power leg — not 40 unrelated events.
CHAIN_ALERT_THRESHOLD = 5
# Distinct chains breaching before the whole thing collapses to one
# device-level alert (wall-wide event: HVAC out, feed lost, etc.).
DEVICE_ROLLUP_CHAIN_THRESHOLD = 3
# Hard ceiling on alerts written per poll cycle, applied after grouping.
MAX_ALERTS_PER_CYCLE = 20
# A card reporting under this is not "a bit low", it has no supply at all.
# Escalated to CRITICAL regardless of the configured voltage_min.
VOLTAGE_DEAD_V = 0.5
# Per-card bit-error counter (binary path, register 0x4A010002). The count is
# cumulative since the card powered up, and single-digit totals show up on a
# long chain as ordinary cable noise — a threshold near zero would alert on a
# perfectly healthy wall. 100 sits well clear of that background and far below
# the counter's 0xFFFF ceiling, so it fires on a run that is actually
# degrading. Saturation is handled separately and is always CRITICAL.
BIT_ERROR_WARNING = 100

# Dedupe state: key -> monotonic timestamp of the last alert for that key.
# O(1) per card. The old implementation rescanned the tail of the error log,
# which silently stopped deduping the moment a burst was longer than the
# window it scanned (20 entries) — exactly when dedupe matters most.
_alert_seen = {}
_alert_seen_lock = threading.Lock()
_ALERT_SEEN_MAX = 5000


def _alert_due(key, cooldown=ALERT_COOLDOWN_SEC):
    """True if `key` hasn't alerted within `cooldown` seconds (and marks it)."""
    now = time.monotonic()
    with _alert_seen_lock:
        last = _alert_seen.get(key)
        if last is not None and (now - last) < cooldown:
            return False
        _alert_seen[key] = now
        if len(_alert_seen) > _ALERT_SEEN_MAX:
            # Evict anything well past any cooldown window we use.
            stale_before = now - (DEVICE_ALERT_COOLDOWN_SEC * 2)
            for k in [k for k, ts in _alert_seen.items() if ts < stale_before]:
                del _alert_seen[k]
        return True


def _card_identity(card):
    """Return (key, label, chain, chain_label, port) for a receiving card.

    H-series cards are addressed by (slot, port, card_id) — the same triple
    the Wall View tooltip shows, so an alert can be matched to a cell on
    screen. VX1000 cards only carry a chain index.
    """
    slot = card.get('slot')
    port = card.get('port')
    card_id = card.get('card_id')

    if slot is not None and card_id is not None:
        key = f's{slot}p{port}c{card_id}'
        label = card.get('label') or f'Slot {slot} · Port {port} · Card {card_id}'
        chain = f's{slot}p{port}'
        chain_label = f'slot {slot} port {port}'
    else:
        index = card.get('index')
        key = f'p{port}i{index}'
        label = card.get('label') or f'Card {index}'
        chain = f'p{port}'
        chain_label = f'port {port}' if port is not None else 'chain'
    return key, label, chain, chain_label, port


def _thresholds(settings):
    """Pull the numeric alert thresholds out of settings, with fallbacks."""
    return (
        _coerce_number(settings.get('temp_warning'),
                       DEFAULT_SETTINGS['temp_warning']),
        _coerce_number(settings.get('temp_critical'),
                       DEFAULT_SETTINGS['temp_critical']),
        _coerce_number(settings.get('voltage_min'),
                       DEFAULT_SETTINGS['voltage_min']),
    )


def _card_breaches(cards, settings):
    """Evaluate every receiving card against the thresholds.

    Every comparison is guarded with `is not None`, never truthiness: 0.0 V
    (dead power supply) and 0.0 °C are falsy, and they are precisely the
    readings that must page someone.

    That cuts both ways, which is why a card that is not reporting is dropped
    before any comparison happens. On the H-series centi firmware a card the
    controller cannot reach still ANSWERS R0155 — with `workStatus: 1` and
    temp/volt/brightness all 0. Those zeros are placeholders, and a wall where
    hundreds of cards are absent would otherwise raise hundreds of CRITICAL
    "supply appears dead" alerts on first contact. `h_series_json` already
    strips such a card's readings to None and marks it offline; both flags are
    re-checked here so no future producer can slip a placeholder through.
    """
    temp_warn, temp_crit, volt_min = _thresholds(settings)
    breaches = []

    for card in cards or []:
        if card.get('online') is False or card.get('reporting') is False:
            continue
        key, label, chain, chain_label, port = _card_identity(card)
        base = {'key': key, 'label': label, 'chain': chain,
                'chain_label': chain_label, 'port': port}

        # temperature_c is the canonical field; temp_c is the alias the
        # H-series card refresh also writes.
        temp = card.get('temperature_c')
        if temp is None:
            temp = card.get('temp_c')
        temp = _coerce_number(temp)
        if temp is not None:
            if temp >= temp_crit:
                breaches.append({**base, 'metric': 'Temperature',
                                 'severity': 'CRITICAL', 'value': temp,
                                 'worst_is_high': True,
                                 'text': f'Temperature {temp:.1f}°C exceeds '
                                         f'critical threshold of {temp_crit}°C'})
            elif temp >= temp_warn:
                breaches.append({**base, 'metric': 'Temperature',
                                 'severity': 'WARNING', 'value': temp,
                                 'worst_is_high': True,
                                 'text': f'Temperature {temp:.1f}°C exceeds '
                                         f'warning threshold of {temp_warn}°C'})

        volt = _coerce_number(card.get('voltage_v'))
        if volt is not None and volt < volt_min:
            dead = volt < VOLTAGE_DEAD_V
            breaches.append({**base, 'metric': 'Voltage',
                             'severity': 'CRITICAL' if dead else 'WARNING',
                             'value': volt, 'worst_is_high': False,
                             'text': (f'Voltage {volt:.2f}V — supply appears dead'
                                      if dead else
                                      f'Voltage {volt:.2f}V below minimum '
                                      f'threshold of {volt_min}V')})

        # Power supplies — the other "a panel is failing" signal, alongside
        # temperature and voltage. Severity mirrors wall_view.js powerState()
        # exactly, and for the same reason:
        #   · exactly one supply flagged  → a real hardware failure. The card
        #     is still lit but now running unprotected on its remaining feed,
        #     so the next failure takes the panel dark. CRITICAL.
        #   · BOTH flagged at once        → almost always an R0155 decode
        #     artifact from a transient timeout, not a genuine double failure.
        #     The Wall View paints that amber "suspect", and this is a WARNING
        #     for the same reason — nobody should be paged at 3am for a
        #     dropped packet.
        # Neither reported (both None) → nothing to say. That case matters: a
        # non-reporting card fills every field with placeholders, and reading
        # a placeholder flag as a verdict on a supply is the same trap as
        # reading its 0 V as a dead rail. The parser hands such a card
        # None/None precisely so it lands here.
        #
        # ⚠ NOTHING BELOW FIRES ON LIVE DATA TODAY, and that is deliberate.
        # NovaStar documents the flags as 0 = Fault / 1 = Normal, but 21 of 36
        # cards on the operator's lit wall report 0 on BOTH supplies, so
        # `_power_status` refuses to call 0 a fault and returns only True or
        # None — never False. These branches are reachable only from an older
        # snapshot, and the snapshot loader now clears those too
        # (`_drop_stale_power_flags`).
        #
        # They stay, unmodified, because the day NovaStar explains what 0
        # means on a card that is plainly running, this is the policy that
        # should apply — and the tests below pin that policy so it cannot rot
        # while it is dormant. Do not read their passing as evidence that
        # per-card power alerting is currently live; it is not.
        #
        # These are booleans, not readings, so `value` stays None; `_worst`
        # handles a group with nothing rankable in it.
        primary_ok = card.get('primary_power_ok')
        backup_ok = card.get('backup_power_ok')
        if primary_ok is False and backup_ok is False:
            breaches.append({**base, 'metric': 'Power supply',
                             'severity': 'WARNING', 'value': None,
                             'worst_is_high': True,
                             'text': 'both power supplies flagged — usually a '
                                     'transient read rather than a double '
                                     'failure; confirm before dispatching'})
        elif primary_ok is False or backup_ok is False:
            which = 'Primary' if primary_ok is False else 'Backup'
            breaches.append({**base, 'metric': 'Power supply',
                             'severity': 'CRITICAL', 'value': None,
                             'worst_is_high': True,
                             'text': f'{which} power supply failed — card is '
                                     f'running unprotected on the other feed'})

        # Per-card bit-error counter — the only continuous data-integrity
        # signal we have, and binary-path only (no JSON UDP equivalent).
        # `bit_errors_saturated` means the counter pinned at 0xFFFF, which is
        # not a count but "more corruption than this register can express", so
        # it is judged on its own rather than against the warning threshold.
        saturated = card.get('bit_errors_saturated') is True
        bit_errors = _coerce_number(card.get('bit_errors'))
        if saturated:
            breaches.append({**base, 'metric': 'Bit errors',
                             'severity': 'CRITICAL', 'value': bit_errors,
                             'worst_is_high': True,
                             'text': 'bit-error counter saturated (0xFFFF) — '
                                     'serious signal corruption on this run'})
        elif bit_errors is not None and bit_errors >= BIT_ERROR_WARNING:
            breaches.append({**base, 'metric': 'Bit errors',
                             'severity': 'WARNING', 'value': bit_errors,
                             'worst_is_high': True,
                             'text': f'{int(bit_errors)} bit errors — above the '
                                     f'{BIT_ERROR_WARNING} expected on a healthy '
                                     f'run'})

    return breaches


def _snmp_breaches(snmp):
    """Evaluate the SNMP health block, in the same shape as card breaches.

    Routine per-card polling is gone (it was one of the two behaviours behind
    the outage), so the signals that arrive on every cycle are now the ones
    SNMP can read without claiming the controller role: chassis fans, chassis
    power supplies, the device's own temperature verdict, and the output
    card's slot status. Output port link state used to be on that list and no
    longer is — see the note where the alert was. They go through `_emit_card_alerts`
    with the card breaches rather than down a path of their own, so one
    physical fault still produces one alert and the dedupe, the tiering and
    the per-cycle cap all apply to them too.

    NOT EVALUATED, EVER: fan `speed_raw` and PSU `voltage_raw`. Neither is
    implemented over SNMP — NovaStar R&D, by email: "The device's fan speed and
    power supply voltage are not currently provided by the SNMP protocol. If
    you require this data, it needs to be customized." They read 0 on every fan
    and every supply because nothing is behind them, so there is no decode to
    find and no firmware to wait for — see the note in
    snmp_client.parse_fans. This codebase has twice shipped an alarm that fired
    on healthy hardware because the number behind it was never pinned down: the
    workStatus placeholder zeros, and the 4.7 V voltage floor set above the
    band these cards actually report. (This note used to name "the masked
    voltage formula" as the second case. It was not a case at all — the masked
    form is the vendor-documented one, per NovaStar's H Series control protocol
    §4.3.4 / §5.4.2; the alarms came from the threshold.) Only `status` / `ok`
    fields decide anything here.

    Takes no thresholds: every signal below is a boolean the device itself
    reports, not a reading to compare against a number.
    """
    breaches = []
    # `available` is false when this cycle's SNMP read produced nothing. The
    # readings left in the block are then the last good ones, and alerting off
    # stale values would report a fault that may have been fixed — or, worse,
    # keep reporting one after the device stopped answering at all.
    if not isinstance(snmp, dict) or not snmp.get('available'):
        return breaches

    chassis = {'chain': 'chassis', 'chain_label': 'the controller chassis',
               'port': None}

    # The device's own verdict on its temperature. It is a status field, not a
    # reading, so the configured temp_warning / temp_critical thresholds have
    # nothing to compare against and are not consulted.
    if snmp.get('temperature_ok') is False:
        breaches.append({
            **chassis, 'key': 'chassis-temp', 'label': 'Controller chassis',
            'unit': 'sensors', 'metric': 'Chassis temperature',
            'severity': 'CRITICAL', 'value': None, 'worst_is_high': True,
            'text': f'controller reports a temperature fault '
                    f'(status {snmp.get("temperature_status")}) — check the '
                    f'chassis airflow before the splicer throttles'})

    # Fans. One dead fan on a ten-fan chassis is a hardware fault that leads
    # to a thermal problem over minutes, not a dark wall right now — WARNING,
    # with the chassis temperature above as the CRITICAL that follows if it
    # actually gets hot. Several failing at once collapses to one chain-level
    # alert, which is right: that is a fan tray, not ten coincidences.
    for fan in snmp.get('fans') or []:
        if fan.get('ok') is not False:
            continue
        fan_id = fan.get('fan_id')
        breaches.append({
            **chassis, 'key': f'chassis-fan{fan_id}',
            'label': 'Fan' if fan_id is None else f'Fan {fan_id}',
            'unit': 'fans', 'metric': 'Chassis fan', 'severity': 'WARNING',
            'value': None, 'worst_is_high': True,
            'text': f'reported failed by the controller '
                    f'(status {fan.get("status")}) — fan speed is not reported '
                    f'over SNMP at all, so it says nothing either way'})

    # Power supplies, judged exactly like the per-card ones: a supply that has
    # dropped off a redundant pair means the splicer is still up but now
    # running unprotected, and the next failure takes the whole wall dark.
    #
    # `ok` is the `iSignal` flag, and only that. NovaStar R&D, by email, on
    # `.1.17`: "Regarding the device power status, please use the iSignal
    # field. Meaning: Power status (0: not connected to power, 1: connected to
    # power)." So False here is the device's own documented claim that a supply
    # is not connected to power — a real state, worth a CRITICAL mid-show.
    #
    # This loop was unreachable until that answer arrived: PSU `ok` was derived
    # from the `status` key and never came back False, because nobody could say
    # what `status` meant. It still cannot — R&D did not document it — so
    # `status` is quoted in the text as raw evidence and is not what fired the
    # alert. Do not reinstate a rule that reads it.
    # Only supplies this process WATCHED drop from connected to not connected.
    # `ok is False` on its own is not alertable: an empty PSU bay reports
    # exactly the same iSignal 0 as a dead supply, so alerting on the state
    # would raise a CRITICAL every cycle for the life of the show about a bay
    # that never had a supply in it. See `_psu_transitions` in device_manager.
    dropped = set(snmp.get('dropped_psus') or [])
    for psu in snmp.get('psus') or []:
        if psu.get('power_id') not in dropped:
            continue
        psu_id = psu.get('power_id')
        breaches.append({
            **chassis, 'key': f'chassis-psu{psu_id}',
            'label': 'PSU' if psu_id is None else f'PSU {psu_id}',
            'unit': 'supplies', 'metric': 'Chassis power supply',
            'severity': 'CRITICAL', 'value': None, 'worst_is_high': True,
            'text': f'controller reports this supply as not connected to power '
                    f'(iSignal {psu.get("i_signal")}, raw status '
                    f'{psu.get("status")}) — the splicer is running on its '
                    f'remaining supplies'})

    output = snmp.get('output') or {}

    # `slot_ok` is False only for the card-slot status's documented ABNORMAL
    # value, which is 0 — the inverse of every `Normal: 0` field above it in
    # this function. device_manager._snmp_slot_ok owns that polarity and also
    # withholds the verdict entirely when the `.30` subtree is answering with
    # stubs, so False here is a real claim rather than an artefact.
    if output.get('slot_ok') is False:
        breaches.append({
            **chassis, 'key': 'output-card', 'label': 'Output card',
            'unit': 'cards', 'metric': 'Output card', 'severity': 'CRITICAL',
            'value': None, 'worst_is_high': True,
            'text': f'reports a fault (slot status '
                    f'{output.get("slot_status")}; the healthy value is 1)'})

    # There is deliberately NO output-port link alert here. A CRITICAL
    # "Output port N: link lost" used to be raised from `output['ports_down']`;
    # both the key and the alert are gone. The `.30.5.x` OIDs it rested on are
    # a FIELD table for one port (link status, backup working, backup link) and
    # not a per-port link array, so `{1: 0, 3: 0, 4: 0}` from our H15 was never
    # "three ports down" — it was one primary link plus an idle backup, which
    # is what a healthy wall reports. device_manager._apply_snmp_health carries
    # the full account. The raw fields are still published under
    # `output['port']` for display; nothing may derive a verdict from them,
    # because which port they describe is selected by a `.30.4` SET the
    # read-only client never issues.
    #
    # Output-chain faults are covered by the per-card R0155 path, which knows
    # which chain it is talking about — see `chain_breaks`.

    return breaches


def _worst(items):
    """Pick the most severe breach in a group (hottest / lowest voltage).

    Not every metric is a reading — a power-supply fault is a boolean
    condition and carries `value: None`, which can't be ranked. A group with
    nothing numeric in it is represented by its first breach instead of
    raising on the comparison.
    """
    scored = [b for b in items if isinstance(b.get('value'), (int, float))]
    if not scored:
        return items[0]
    if scored[0].get('worst_is_high'):
        return max(scored, key=lambda b: b['value'])
    return min(scored, key=lambda b: b['value'])


def _emit_card_alerts(device_name, breaches):
    """Log card breaches, collapsed by chain / device and deduped per key.

    Three tiers, so one physical fault produces one line rather than 1374:
      · >= DEVICE_ROLLUP_CHAIN_THRESHOLD chains affected → one device alert
      · >= CHAIN_ALERT_THRESHOLD cards on one chain      → one chain alert
      · otherwise                                        → one alert per card

    Also used for the SNMP chassis breaches, which are counted in fans and
    supplies rather than cards — hence `unit`, read per group because a group
    is one (severity, metric) pair and so is all of one kind. It defaults to
    'cards' so every card breach reads exactly as it did before.
    """
    emitted = 0
    suppressed = 0

    by_kind = {}
    for b in breaches:
        by_kind.setdefault((b['severity'], b['metric']), []).append(b)

    for (severity, metric), group in sorted(by_kind.items()):
        unit = group[0].get('unit') or 'cards'
        chains = {}
        for b in group:
            chains.setdefault(b['chain'], []).append(b)

        # Wall-wide event
        if len(chains) >= DEVICE_ROLLUP_CHAIN_THRESHOLD:
            worst = _worst(group)
            if _alert_due(f'{device_name}|wall|{metric}|{severity}'):
                add_error(severity, device_name,
                          f'{metric} alert on {len(group)} {unit} '
                          f'across {len(chains)} chains — worst {worst["label"]}: '
                          f'{worst["text"]}',
                          cabinet=f'{len(group)} {unit} / {len(chains)} chains',
                          value=worst['value'])
                emitted += 1
            continue

        for chain_key, items in sorted(chains.items()):
            worst = _worst(items)

            # Chain-level event
            if len(items) >= CHAIN_ALERT_THRESHOLD:
                if _alert_due(f'{device_name}|chain:{chain_key}|{metric}|{severity}'):
                    add_error(severity, device_name,
                              f'{metric} alert on {len(items)} {unit} '
                              f'on {worst["chain_label"]} — worst '
                              f'{worst["label"]}: {worst["text"]}',
                              cabinet=f'{len(items)} {unit} on {worst["chain_label"]}',
                              port=worst['port'], value=worst['value'])
                    emitted += 1
                continue

            # Individual cards
            for b in items:
                if emitted >= MAX_ALERTS_PER_CYCLE:
                    suppressed += 1
                    continue
                if _alert_due(f'{device_name}|{b["key"]}|{metric}|{severity}'):
                    add_error(severity, device_name,
                              f'{b["label"]}: {b["text"]}',
                              cabinet=b['label'], port=b['port'],
                              value=b['value'])
                    emitted += 1

    if suppressed:
        # Deliberately not written to the alert log — flooding it is the very
        # thing the cap exists to prevent.
        logger.warning('%s: suppressed %d further card alerts this cycle '
                       '(cap %d)', device_name, suppressed, MAX_ALERTS_PER_CYCLE)
    return emitted


def _emit_device_rollup(device_name, live_monitoring, settings):
    """Device-level rollup, based on the MAX card reading — never the mean.

    device_manager already computes `temperature_max_c` and nothing read it.
    `voltage_min_v` is used if the manager provides it; the mean voltage is
    useless for detecting a single failed supply, so it is never used here.

    Both figures come from `_update_aggregates`, which only ever sees cards
    that are actually reporting — a non-reporting card's placeholder 0 can
    reach neither the max nor the min, which is what keeps this rollup from
    firing a wall-wide "supply appears dead" on a wall that is merely partly
    unreachable.
    """
    temp_warn, temp_crit, volt_min = _thresholds(settings)
    lm = live_monitoring or {}

    temp_max = _coerce_number(lm.get('temperature_max_c'))
    if temp_max is not None:
        severity = None
        if temp_max >= temp_crit:
            severity, limit = 'CRITICAL', temp_crit
        elif temp_max >= temp_warn:
            severity, limit = 'WARNING', temp_warn
        if severity and _alert_due(f'{device_name}|device|TemperatureMax|{severity}',
                                   DEVICE_ALERT_COOLDOWN_SEC):
            add_error(severity, device_name,
                      f'Hottest card {temp_max:.1f}°C across '
                      f'{lm.get("card_count", "?")} cards exceeds threshold '
                      f'of {limit}°C',
                      value=temp_max)

    volt_low = _coerce_number(lm.get('voltage_min_v'))
    if volt_low is not None and volt_low < volt_min:
        severity = 'CRITICAL' if volt_low < VOLTAGE_DEAD_V else 'WARNING'
        if _alert_due(f'{device_name}|device|VoltageMin|{severity}',
                      DEVICE_ALERT_COOLDOWN_SEC):
            add_error(severity, device_name,
                      f'Lowest card voltage {volt_low:.2f}V below minimum '
                      f'threshold of {volt_min}V',
                      value=volt_low)


def _chain_break_breaches(breaks):
    """Turn detected data breaks into breaches, in the shared breach shape.

    This is the highest-confidence fault the app can report: the cards were
    enumerated as present, and a contiguous run of them from a known point to
    the end of the chain has either stopped answering or started reporting bit
    errors. Until now it rendered only on the Wall View, so it was seen only
    if somebody happened to have that tab open.

    Deliberately NOT gated on card-reading freshness: a break is derived from
    the bit-error read that produced it, and that read is the evidence.

    One breach per chain, so the existing wall/chain/card tiering collapses a
    wall-wide failure into a single alert rather than one per broken run.
    """
    out = []
    for b in (breaks or []):
        if not isinstance(b, dict):
            continue
        card = b.get('card_number')
        port = b.get('port')
        panel = b.get('break_panel')
        affected = b.get('affected')
        port_label = port + 1 if isinstance(port, int) else port
        where = ('at or before the first panel' if b.get('at_head')
                 else f'at panel {panel}')
        if b.get('signature') == 'no_answer':
            detail = (f'{affected} panels stopped answering — the enumeration '
                      f'recorded them as present')
        else:
            detail = (f'{affected} panels are reporting bit errors, '
                      f'everything before them is clean')
        chain_label = f'card {card} port {port_label}'
        out.append({
            'key': f'break:{card}:{port}',
            'label': f'Card {card} · Port {port_label}',
            'chain': f'{card}:{port}',
            'chain_label': chain_label,
            'port': port,
            'metric': 'Data break',
            'severity': 'CRITICAL',
            'value': affected,
            'worst_is_high': True,
            'unit': 'chains',
            'text': (f'Data break {where} on {chain_label}: {detail}. '
                     f'Panels after a break may still be lit by a backup '
                     f'sender card, so the wall can look fine.'),
        })
    return out


def evaluate_alerts(device_id, state):
    """Run the full alert evaluation for one polled device state.

    Card breaches and SNMP chassis breaches are emitted in ONE call, not two,
    so MAX_ALERTS_PER_CYCLE stays an honest per-cycle cap rather than a cap
    per source. Per-card readings only exist here when somebody has asked for
    them on demand; SNMP is what arrives every cycle.
    """
    settings = load_settings()
    device_name = state.get('name', device_id)

    # Per-card readings are taken on demand, so `receiving_cards` can hold
    # values from hours ago. Alerting on them every cycle meant a card that
    # read 76 C during a sweep at 14:00 raised a fresh CRITICAL every minute
    # for the rest of the day — after it had been fixed, powered down or
    # unplugged, with nothing to say the reading was old. An alert nobody can
    # act on is worse than no alert: it trains an operator to ignore the log.
    breaches = []
    if state.get('cards_fresh'):
        breaches = _card_breaches(state.get('receiving_cards'), settings)
    breaches += _snmp_breaches(state.get('snmp'))
    breaches += _chain_break_breaches(state.get('chain_breaks'))
    _emit_card_alerts(device_name, breaches)
    _emit_device_rollup(device_name, state.get('live_monitoring'), settings)


def on_device_update(device_id, state):
    """Called by device manager when a device is polled."""
    socketio.emit('device_update', {
        'device_id': device_id,
        'state': state,
        'timestamp': datetime.now().isoformat(),
    })

    try:
        evaluate_alerts(device_id, state)
    except Exception:
        # A bad reading must never kill the poll thread's callback.
        logger.exception('Alert evaluation failed for %s', device_id)


def on_device_error(device_id, error_info):
    """Called by device manager on errors."""
    device_name = device_id
    dev = manager.devices.get(device_id)
    if dev:
        device_name = dev.state.get('name', device_id)
    if _alert_due(f'{device_id}|connection|CRITICAL'):
        add_error('CRITICAL', device_name,
                  f"Connection error: {error_info.get('error', 'unknown')}")


manager.set_callbacks(on_update=on_device_update, on_error=on_device_error)


@socketio.on('connect')
def handle_connect():
    """Send current state and error log to newly connected client."""
    all_states = manager.get_all_states()
    with _error_lock:
        recent_errors = _error_log[-100:][::-1]
    emit('full_state', {
        'devices': all_states,
        'settings': load_settings(),
        'errors': recent_errors,
        'timestamp': datetime.now().isoformat(),
    })


@socketio.on('request_state')
def handle_request_state():
    """Client requests a full state refresh."""
    all_states = manager.get_all_states()
    emit('full_state', {
        'devices': all_states,
        'settings': load_settings(),
        'timestamp': datetime.now().isoformat(),
    })


# ── HTTP Routes ───────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/devices', methods=['GET'])
def api_list_devices():
    return jsonify(manager.get_all_states())


@app.route('/api/devices', methods=['POST'])
def api_add_device():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'ip' not in data:
        return jsonify({"error": "IP address required"}), 400

    # Validate before anything is derived from it: the raw value used to flow
    # straight into the device id and into persisted settings.
    try:
        ip = str(ipaddress.ip_address(str(data['ip']).strip()))
    except ValueError:
        return jsonify({"error": f"Invalid IP address: {data['ip']!r}"}), 400

    try:
        port = int(data.get('port', 5200))
    except (TypeError, ValueError):
        return jsonify({"error": f"Invalid port: {data.get('port')!r}"}), 400
    if not 1 <= port <= 65535:
        return jsonify({"error": "Port must be between 1 and 65535"}), 400

    name = str(data.get('name') or f"NovaStar {ip}")[:100]
    # ':' appears in IPv6 literals and is not id-safe.
    device_id = f"dev-{ip.replace('.', '-').replace(':', '-')}"

    # Check if already exists
    if device_id in manager.devices:
        return jsonify({"error": "Device already exists"}), 409

    manager.add_device(device_id, name, ip, port)

    # Save to settings
    settings = load_settings()
    settings.setdefault('devices', []).append({
        'id': device_id, 'name': name, 'ip': ip, 'port': port,
    })
    save_settings(settings)

    return jsonify({"status": "added", "device_id": device_id})


@app.route('/api/devices/<device_id>', methods=['DELETE'])
def api_remove_device(device_id):
    if device_id not in manager.devices:
        return jsonify({"error": "Device not found"}), 404

    manager.remove_device(device_id)

    settings = load_settings()
    settings['devices'] = [d for d in settings.get('devices', [])
                           if d.get('id') != device_id]
    save_settings(settings)

    return jsonify({"status": "removed"})


@app.route('/api/devices/<device_id>/state', methods=['GET'])
def api_device_state(device_id):
    state = manager.get_state(device_id)
    if not state:
        return jsonify({"error": "Device not found"}), 404
    return jsonify(state)


# ── Emergency stop & on-demand card reads ─────────────────
#
# `/api/halt` is the control the operator did not have during the outage. The
# only way to stop this app touching the wall mid-show was to kill it, which
# also took away the dashboard, the alert history and the log. The flag it
# flips lives in device_manager and is process-wide, so it covers every
# transport — JSON UDP, the binary path, and SNMP.
#
# The reason string is kept here rather than in device_manager because it is
# purely an operator-facing note ("show in progress"); the stop itself needs
# nothing but a boolean, and nothing about honouring it may depend on this.

_halt_reason = None
_halt_reason_lock = threading.Lock()
MAX_HALT_REASON = 200


def _halt_payload():
    """The `/api/halt` body. `halted` always comes from device_manager.

    The authority is the module-level flag, never a copy kept here: a halt
    engaged from the console or from a future scheduler must still read back
    as halted through the API.
    """
    with _halt_reason_lock:
        return {'halted': manager.is_halted(), 'reason': _halt_reason}


@app.route('/api/halt', methods=['GET'])
def api_halt_status():
    return jsonify(_halt_payload())


@app.route('/api/halt', methods=['POST'])
def api_halt_set():
    """Engage or release the global stop.

    Deliberately does nothing except set a flag (and record a note), so it
    completes while a poll is in flight: reads already on the wire finish on
    their own timeouts rather than being aborted, because yanking a socket out
    from under a poll thread produces a hang, not a stop.
    """
    global _halt_reason
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400

    halted = data.get('halted')
    # Strictly a boolean: an emergency stop is not somewhere to be generous
    # about what "1" or "false" might have meant.
    if not isinstance(halted, bool):
        return jsonify({"error": "halted must be true or false"}), 400

    reason = data.get('reason')
    reason = str(reason).strip()[:MAX_HALT_REASON] if reason is not None else ''

    if halted:
        manager.halt(reason or None)
        with _halt_reason_lock:
            _halt_reason = reason or None
    else:
        manager.resume()
        with _halt_reason_lock:
            _halt_reason = None

    payload = _halt_payload()
    log_event('device_contact_halted' if halted else 'device_contact_resumed',
              {'reason': payload['reason']})
    socketio.emit('halt_changed', payload)
    return jsonify(payload)


@app.route('/api/devices/<device_id>/refresh_cards', methods=['POST'])
def api_refresh_cards(device_id):
    """Read receiving cards on demand — one chain, or the whole inventory.

    Per-card reads are no longer routine (the ~1374-card sweep every cycle is
    one of the two behaviours implicated in the outage), so this is the only
    way per-card readings are ever taken. Empty body = every chain, which is
    rate-limited in device_manager; `{"slot": N, "port": N}` = one chain,
    which is tens of cards and is not.

    Three outcomes, and the caller has to be able to tell them apart:
      · "ok"      the read ran; `cards` is how many came back
      · "refused" the rate limiter declined it — distinct from a read that
                  ran and found nothing, which is "ok" with cards 0
      · "halted"  the global stop is engaged
    """
    dev = manager.devices.get(device_id)
    if dev is None:
        return jsonify({"error": "Device not found"}), 404

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400

    slot, port = data.get('slot'), data.get('port')
    one_chain = slot is not None or port is not None
    if one_chain and (slot is None or port is None):
        return jsonify({"error": "slot and port must be given together"}), 400
    if one_chain:
        try:
            slot, port = int(slot), int(port)
        except (TypeError, ValueError):
            return jsonify({"error": "slot and port must be integers"}), 400

    refresh_chain = getattr(dev, 'refresh_chain', None)
    refresh_all = getattr(dev, 'refresh_all_cards', None)
    if not callable(refresh_chain) or not callable(refresh_all):
        return jsonify({"error": "Device has no per-card reads"}), 400

    # Checked before dispatching, because refresh_all_cards() returns None for
    # both "halted" and "rate-limited" and the frontend has to distinguish
    # them: one is the operator's own doing, the other is a wait.
    if manager.is_halted():
        return jsonify({"status": "halted", "cards": 0,
                        "reason": _halt_payload()['reason']
                        or 'device contact is halted'})

    if one_chain:
        cards = refresh_chain(slot, port)
    else:
        cards = refresh_all()

    if cards is None:
        # A halt engaged between the check above and the call lands here too,
        # so re-read the flag rather than reporting the operator's own stop as
        # a rate limit.
        if manager.is_halted():
            return jsonify({"status": "halted", "cards": 0,
                            "reason": _halt_payload()['reason']
                            or 'device contact is halted'})
        return jsonify({
            "status": "refused", "cards": 0,
            "reason": f'a full sweep runs at most once every '
                      f'{int(FULL_SWEEP_MIN_INTERVAL)}s — it is the traffic '
                      f'pattern that cost the operator control of the wall'})

    log_event('cards_refreshed', {'device_id': device_id, 'slot': slot,
                                  'port': port, 'cards': len(cards)})
    # Push the fresh readings out the same way a poll cycle would, so the
    # dashboard updates and the new values are alert-evaluated. Cards read on
    # demand are the only per-card data there is now; they must not be
    # collected and then not looked at.
    state = manager.get_state(device_id)
    if state is not None:
        on_device_update(device_id, state)
    return jsonify({"status": "ok", "cards": len(cards), "reason": None})


def _read_progress(device_id):
    """Emit per-card read progress to the dashboard as it happens.

    A whole-wall pass is 286 sequential reads with a 45 s pause partway. With
    no progress it is indistinguishable from a hang, and an operator who kills
    it mid-show loses the pass AND has spent the controller's request budget
    for nothing.
    """
    def emit(info):
        socketio.emit('read_progress', dict(info, device_id=device_id))
        # On a flush the device has just published a partial result. Push it
        # out the same way a poll cycle would, so the wall fills in as the
        # read proceeds instead of staying blank for minutes and then jumping.
        # Alerts are evaluated on it too — a break found at card 40 of 286 is
        # worth knowing about before the remaining 246 are read.
        if info.get('phase') == 'flush':
            state = manager.get_state(device_id)
            if state is not None:
                on_device_update(device_id, state)
    return emit


@app.route('/api/devices/<device_id>/live_readings', methods=['POST'])
def api_refresh_live_readings(device_id):
    """Read temperature/voltage/link for every card, over the binary path.

    The whole-wall read that actually covers the whole wall. R0155 answers
    about 150 cards before the controller stops talking, so the JSON route
    left ~87% of a 286-panel wall with no readings at all and the dashboard
    averaging the rest as if it were the wall.

    Read-only.
    """
    dev = manager.devices.get(device_id)
    if dev is None:
        return jsonify({"error": "Device not found"}), 404
    read = getattr(dev, 'refresh_live_readings', None)
    if not callable(read):
        return jsonify({"error": "Device has no binary per-card reads"}), 400

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    slot, port = data.get('slot'), data.get('port')
    one_chain = slot is not None or port is not None
    if one_chain and (slot is None or port is None):
        return jsonify({"error": "slot and port must be given together"}), 400

    cards = None
    if one_chain:
        try:
            slot, port = int(slot), int(port)
        except (TypeError, ValueError):
            return jsonify({"error": "slot and port must be integers"}), 400
        known = getattr(dev, 'known_cards', None)
        inventory = known() if callable(known) else []
        cards = [c for c in inventory
                 if c.get('slot') == slot and c.get('port') == port]

    if manager.is_halted():
        return jsonify({"status": "halted", "cards": 0,
                        "reason": _halt_payload()['reason']
                        or 'device contact is halted'})

    results = read(cards, progress=_read_progress(device_id))
    if results == "busy":
        # Two concurrent passes would interleave on one TCP connection, spend
        # the controller's request budget twice as fast, and report progress
        # over each other — which is what made the bar jump around.
        return jsonify({"status": "busy", "cards": 0,
                        "reason": 'a per-card read is already running on this '
                                  'device — wait for it to finish'}), 409
    if results is None:
        return jsonify({"status": "halted", "cards": 0,
                        "reason": _halt_payload()['reason']
                        or 'device contact is halted'})

    answered = sum(1 for r in results if r.get('present'))
    log_event('live_readings', {'device_id': device_id, 'slot': slot,
                                'port': port, 'cards': answered})
    state = manager.get_state(device_id)
    if state is not None:
        on_device_update(device_id, state)
    return jsonify({"status": "ok", "cards": answered, "reason": None})


@app.route('/api/devices/<device_id>/bit_errors', methods=['POST'])
def api_refresh_bit_errors(device_id):
    """Read per-card bit-error counters on demand.

    Binary-only — R0155 carries no bit errors — so this is the one reading
    that cannot come from the JSON path. Same shape as `/refresh_cards`:
    empty body reads the whole known inventory (capped), `{"slot": N,
    "port": N}` reads one chain.

    Read-only. Nothing here writes to the controller.
    """
    dev = manager.devices.get(device_id)
    if dev is None:
        return jsonify({"error": "Device not found"}), 404

    read = getattr(dev, 'refresh_bit_errors', None)
    if not callable(read):
        return jsonify({"error": "Device has no bit-error reads"}), 400

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400

    slot, port = data.get('slot'), data.get('port')
    one_chain = slot is not None or port is not None
    if one_chain and (slot is None or port is None):
        return jsonify({"error": "slot and port must be given together"}), 400

    cards = None
    if one_chain:
        try:
            slot, port = int(slot), int(port)
        except (TypeError, ValueError):
            return jsonify({"error": "slot and port must be integers"}), 400
        known = getattr(dev, 'known_cards', None)
        inventory = known() if callable(known) else []
        cards = [c for c in inventory
                 if c.get('slot') == slot and c.get('port') == port]

    if manager.is_halted():
        return jsonify({"status": "halted", "cards": 0,
                        "reason": _halt_payload()['reason']
                        or 'device contact is halted'})

    results = read(cards, progress=_read_progress(device_id))
    if results == "busy":
        # Two concurrent passes would interleave on one TCP connection, spend
        # the controller's request budget twice as fast, and report progress
        # over each other — which is what made the bar jump around.
        return jsonify({"status": "busy", "cards": 0,
                        "reason": 'a per-card read is already running on this '
                                  'device — wait for it to finish'}), 409
    if results is None:
        return jsonify({"status": "halted", "cards": 0,
                        "reason": _halt_payload()['reason']
                        or 'device contact is halted'})

    log_event('bit_errors_read', {'device_id': device_id, 'slot': slot,
                                  'port': port, 'cards': len(results)})
    state = manager.get_state(device_id)
    if state is not None:
        on_device_update(device_id, state)
    return jsonify({"status": "ok", "cards": len(results), "reason": None})


@app.route('/api/devices/<device_id>/bit_errors/baseline', methods=['POST'])
def api_bit_error_baseline(device_id):
    """Zero the displayed bit-error counters, or restore the raw ones.

    `{"zero": true}`  — record the current counters as the new zero point.
    `{"zero": false}` — forget it and show the controller's own cumulative
                        totals again.

    This does NOT write to the controller. The device-side counter is
    cumulative and the command NovaLCT uses to reset it has not been captured,
    so "clear" here means "start counting from now" in this app only. Say so
    in the UI: an operator who believes the hardware counter was reset, when
    it was not, will misread the next engineer's readings.
    """
    dev = manager.devices.get(device_id)
    if dev is None:
        return jsonify({"error": "Device not found"}), 404

    setter = getattr(dev, 'set_bit_error_baseline', None)
    clearer = getattr(dev, 'clear_bit_error_baseline', None)
    if not callable(setter) or not callable(clearer):
        return jsonify({"error": "Device has no bit-error baseline"}), 400

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    zero = data.get('zero', True)
    if not isinstance(zero, bool):
        return jsonify({"error": "zero must be a boolean"}), 400

    if zero:
        count = setter()
    else:
        clearer()
        count = 0

    log_event('bit_error_baseline', {'device_id': device_id, 'zero': zero,
                                     'cards': count})
    state = manager.get_state(device_id)
    if state is not None:
        on_device_update(device_id, state)
    return jsonify({"status": "ok", "zero": zero, "cards": count,
                    "device_counter_reset": False})


@app.route('/api/devices/<device_id>/bit_errors/clear', methods=['POST'])
def api_clear_device_bit_errors(device_id):
    """Clear the controller's own bit-error counters. THIS WRITES TO THE DEVICE.

    The only write this application makes. It sends the frame NovaLCT sends
    when you click "clear", reproduced byte-for-byte from a capture, and it is
    broadcast — every card on every chain of every sender card.

    Requires `{"confirm": true}` in the body. Not because a typo is likely,
    but because this is the one endpoint whose effect cannot be undone: the
    counter is cumulative and clearing it discards a number that may be the
    only evidence of an intermittent link, for whoever looks next — not just
    for this dashboard.

    `/bit_errors/baseline` is the non-destructive alternative: it zeroes the
    display here and leaves the hardware counter intact.
    """
    dev = manager.devices.get(device_id)
    if dev is None:
        return jsonify({"error": "Device not found"}), 404

    clear = getattr(dev, 'clear_device_bit_errors', None)
    if not callable(clear):
        return jsonify({"error": "Device cannot clear bit errors"}), 400

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    if data.get('confirm') is not True:
        return jsonify({
            "status": "refused",
            "reason": 'clearing the controller\'s bit-error counters is a '
                      'device write and cannot be undone — send '
                      '{"confirm": true} to proceed, or use '
                      '/bit_errors/baseline to zero the display only',
        }), 400

    if manager.is_halted():
        return jsonify({"status": "halted",
                        "reason": _halt_payload()['reason']
                        or 'device contact is halted'})

    if not clear():
        if manager.is_halted():
            return jsonify({"status": "halted",
                            "reason": _halt_payload()['reason']
                            or 'device contact is halted'})
        return jsonify({"status": "failed",
                        "reason": 'the controller did not accept the write'})

    log_event('bit_errors_cleared', {'device_id': device_id,
                                     'device_write': True})
    state = manager.get_state(device_id)
    if state is not None:
        on_device_update(device_id, state)
    return jsonify({"status": "ok", "device_counter_reset": True,
                    "reason": None})


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    return jsonify(load_settings())


# Numeric settings and the range each is allowed to take.
_SETTINGS_RANGES = {
    'poll_interval': (MIN_POLL_INTERVAL, MAX_POLL_INTERVAL),
    'temp_warning': (0.0, 200.0),
    'temp_critical': (0.0, 200.0),
    'voltage_min': (0.0, 60.0),
}


@app.route('/api/settings', methods=['POST'])
def api_update_settings():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400

    updates = {}
    for key, value in data.items():
        if key in _SETTINGS_RANGES:
            number = _coerce_number(value)
            if number is None:
                return jsonify({"error": f"{key} must be a number"}), 400
            low, high = _SETTINGS_RANGES[key]
            # Clamp rather than reject: the UI is a free-text number field and
            # an operator typing 0 should get the safe minimum, not an error.
            updates[key] = max(low, min(high, number))
        elif key == 'devices':
            if not isinstance(value, list):
                return jsonify({"error": "devices must be a list"}), 400
            updates[key] = value
        else:
            updates[key] = value

    settings = load_settings()
    settings.update(updates)
    if not save_settings(settings):
        return jsonify({"error": "Failed to save settings"}), 500

    # Apply live — this used to only take effect after a restart.
    interval = apply_poll_interval(settings)
    log_event('settings_saved', {'poll_interval': interval})
    return jsonify({"status": "saved", "settings": settings})


@app.route('/api/demo', methods=['GET'])
def api_demo_status():
    """Check if simulation mode is active."""
    from demo_device import DemoDevice
    active = DemoDevice.DEVICE_ID in manager.devices
    return jsonify({"active": active})


@app.route('/api/demo', methods=['POST'])
def api_demo_toggle():
    """Enable or disable simulation mode."""
    from demo_device import DemoDevice
    data = request.get_json(silent=True) or {}
    enable = data.get('enable', True)

    if enable:
        if DemoDevice.DEVICE_ID in manager.devices:
            return jsonify({"active": True, "status": "already_active"})
        demo = DemoDevice()
        manager.devices[demo.device_id] = demo
        if manager._running:
            manager._start_device_thread(demo.device_id)
        log_event('simulation_enabled')
        logger.info('Simulation mode enabled')
        return jsonify({"active": True, "status": "enabled"})
    else:
        if DemoDevice.DEVICE_ID not in manager.devices:
            return jsonify({"active": False, "status": "already_inactive"})
        manager.devices[DemoDevice.DEVICE_ID].disconnect()
        del manager.devices[DemoDevice.DEVICE_ID]
        log_event('simulation_disabled')
        logger.info('Simulation mode disabled')
        return jsonify({"active": False, "status": "disabled"})


@app.route('/api/version', methods=['GET'])
def api_version():
    version_file = os.path.join(BASE_DIR, 'VERSION.txt')
    version = "0.1.0"
    try:
        with open(version_file) as f:
            version = f.read().strip()
    except Exception:
        logger.exception('Failed to read VERSION.txt at %s', version_file)
    return jsonify({"version": version})


# ── Wall Topology / Layout ────────────────────────────────
#
# `/api/wall_config`, `/api/wall_layout` and `/api/wall_rendered` serve the
# operator-authored PHYSICAL wall map (which pillar a chain hangs on), which
# the controller can never report — it only knows logical (slot, port, card)
# topology. No frontend calls them yet; the drag-drop editor that will is not
# built. Retained on purpose — see the module docstring in wall_config.py
# before deleting them as dead.
# `/api/wall_live` below is the separate, live enumeration-driven path that
# the Wall View actually renders today.

import wall_config as wc


@app.route('/api/wall_config', methods=['GET'])
def api_wall_config():
    """Return the authoritative wall topology (pillars + ports + card counts)."""
    try:
        config = wc.load_config(APP_DIR, BASE_DIR)
        return jsonify(config)
    except Exception as e:
        logger.error('Failed to load wall config: %s', e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/wall_layout', methods=['GET'])
def api_wall_layout_get():
    """Return the user's saved layout overrides (display positions per pillar)."""
    try:
        layout = wc.load_layout(APP_DIR, BASE_DIR)
        return jsonify(layout)
    except Exception as e:
        logger.error('Failed to load wall layout: %s', e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/wall_layout', methods=['POST'])
def api_wall_layout_save():
    """Save layout overrides (sent by drag-drop UI in dashboard)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'JSON object required'}), 400

    config = wc.load_config(APP_DIR, BASE_DIR)
    errors = wc.validate_layout(data, config)
    if errors:
        return jsonify({'error': 'Invalid layout', 'details': errors}), 400

    try:
        wc.save_layout(APP_DIR, data)
        log_event('wall_layout_saved', {'layout_name': data.get('layout_name')})
        return jsonify({'status': 'saved'})
    except Exception as e:
        logger.error('Failed to save wall layout: %s', e)
        return jsonify({'error': str(e)}), 500


WALL_SNAPSHOT_FILE = os.path.join(BASE_DIR, 'wall_live_snapshot.json')

# The snapshot is ~400 KB of JSON and the Wall View polls every 5s. Re-reading
# and re-parsing it per request is pure waste — cache on (mtime, size).
_snapshot_cache = {}
_snapshot_cache_lock = threading.Lock()


def _drop_stale_power_flags(data):
    """Clear `False` receiving-card supply flags out of a loaded snapshot.

    No decoder in this project can produce `False` for `primary_power_ok` or
    `backup_power_ok` — `h_series_json._power_status` returns only True or
    None. Any `False` in a snapshot was therefore written by an older parser
    working from the inverted polarity (it read `0` as healthy and non-zero as
    failed; NovaStar has since documented 0 = Fault, 1 = Normal).

    That mattered visibly: `enumerate_wall.attach_readings` merges only
    non-None readings, so a stale `False` survives every subsequent read, and
    the Wall View paints those cards red for a supply fault that no live
    reading claims. Fifteen cards on the stored 286-panel snapshot were doing
    exactly that.

    Cleared to None — unknown — rather than to True. The card's real supply
    state is whatever the next read says; until then nothing is claimed.
    Mutates in place, before the snapshot is cached.
    """
    if not isinstance(data, dict):
        return
    for card in (data.get('cards') or []):
        if not isinstance(card, dict):
            continue
        for key in ('primary_power_ok', 'backup_power_ok'):
            if card.get(key) is False:
                card[key] = None


def load_wall_snapshot(path=None):
    """Return (snapshot, mtime_iso) for the enumeration snapshot.

    Cached on the file's (mtime, size); a re-enumeration rewrites the file and
    invalidates the cache automatically. Returns (None, None) if absent or
    unreadable.
    """
    path = path or WALL_SNAPSHOT_FILE
    try:
        stat = os.stat(path)
    except OSError:
        return None, None

    stamp = (stat.st_mtime, stat.st_size)
    with _snapshot_cache_lock:
        cached = _snapshot_cache.get(path)
        if cached and cached['stamp'] == stamp:
            return cached['data'], cached['mtime_iso']

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        logger.exception('Failed to load wall live snapshot from %s', path)
        return None, None

    _drop_stale_power_flags(data)

    mtime_iso = datetime.fromtimestamp(stat.st_mtime).isoformat()
    with _snapshot_cache_lock:
        _snapshot_cache[path] = {'stamp': stamp, 'data': data,
                                 'mtime_iso': mtime_iso}
    return data, mtime_iso


# How the enumeration snapshot is regenerated. Quoted to the operator
# verbatim whenever the stored snapshot is stale or missing, because "run the
# enumeration" is not an instruction anybody can act on. The flag is mandatory
# by design — see the Safety section of enumerate_wall's module docstring.
ENUMERATE_HINT = 'python3 src/enumerate_wall.py <controller-ip> --yes-contact-hardware'


def _norm_name(value):
    """Screen name normalised for comparison. None when there is no name."""
    if value is None:
        return None
    text = str(value).strip()
    return text.upper() if text else None


def _snapshot_identity(snapshot):
    """The three facts about a snapshot that identify which wall it describes."""
    snapshot = snapshot or {}
    size = snapshot.get('screen_size')
    canvas = None
    if isinstance(size, dict):
        w, h = size.get('width'), size.get('height')
        if isinstance(w, int) and isinstance(h, int):
            canvas = {'width': w, 'height': h}
    slots = sorted({sc.get('slot') for sc in (snapshot.get('sender_cards') or [])
                    if isinstance(sc, dict) and isinstance(sc.get('slot'), int)})
    return {
        'screen_name': snapshot.get('screen_name'),
        'canvas': canvas,
        'sender_slots': slots,
    }


def compare_snapshot_to_topology(snapshot, topology):
    """Decide whether a stored snapshot describes the wall now configured.

    Returns a verdict dict with a three-valued `status`:

      no_snapshot  nothing on disk.
      unverified   there is a snapshot but the device has not reported its
                   topology, so nothing can be checked. Absence of live data
                   is NOT evidence the snapshot is wrong, so it is still
                   shown — labelled as unverified, never as live.
      match        every check that could be evaluated agreed.
      mismatch     at least one check disagreed. The snapshot describes a
                   DIFFERENT wall and its cards must not be presented as the
                   state of this one.

    Three discriminators, in descending order of how obvious they are to an
    operator: the screen name, the canvas size, and the set of sender slots.
    Any one of them disagreeing is disqualifying — a wall rebuilt onto the
    same screen name with different sender cards is still a different wall,
    and the card inventory would be wrong in exactly the way that produced
    this bug. Each check is skipped when either side is silent, so a snapshot
    written before a field existed degrades to "unverified", not "stale".
    """
    if not snapshot:
        return {'status': 'no_snapshot', 'checks': {}, 'reasons': [],
                'snapshot': _snapshot_identity(None), 'live': None}

    ident = _snapshot_identity(snapshot)
    if not topology:
        return {'status': 'unverified', 'checks': {}, 'snapshot': ident,
                'live': None,
                'reasons': ['The controller has not reported its screen layout, '
                            'so the stored snapshot could not be checked against '
                            'the wall that is currently configured.']}

    live_ident = {
        'screen_name': topology.get('screen_name'),
        'canvas': topology.get('canvas'),
        'sender_slots': topology.get('sender_slots') or [],
    }

    checks, reasons = {}, []

    snap_name, live_name = _norm_name(ident['screen_name']), _norm_name(live_ident['screen_name'])
    if snap_name and live_name:
        checks['screen_name'] = 'match' if snap_name == live_name else 'mismatch'
        if checks['screen_name'] == 'mismatch':
            reasons.append(
                f"The stored snapshot was enumerated on \"{ident['screen_name']}\", "
                f"but the controller is currently configured as "
                f"\"{live_ident['screen_name']}\".")
    else:
        checks['screen_name'] = 'unknown'

    if ident['canvas'] and live_ident['canvas']:
        same = (ident['canvas']['width'] == live_ident['canvas']['width']
                and ident['canvas']['height'] == live_ident['canvas']['height'])
        checks['canvas'] = 'match' if same else 'mismatch'
        if not same:
            reasons.append(
                f"Canvas is {live_ident['canvas']['width']}x"
                f"{live_ident['canvas']['height']} on the controller, but the "
                f"snapshot describes {ident['canvas']['width']}x"
                f"{ident['canvas']['height']}.")
    else:
        checks['canvas'] = 'unknown'

    if ident['sender_slots'] and live_ident['sender_slots']:
        # Subset, not equality. R0405 lists every sender card, including the
        # backups — on the H15 that is 20, 22, 28 and 30, two primary and two
        # backup. A backup answers nothing over R0155 and has no cards behind
        # it until it takes over, so an enumeration can only ever record the
        # slots it actually found cards on. Requiring equality rejected a
        # snapshot taken minutes earlier from this very controller.
        #
        # The safety property survives: a wall rebuilt onto different sender
        # cards has slots the current topology does not list, which still
        # fails. What no longer fails is a correct snapshot that simply omits
        # a slot with nothing on it.
        extra = sorted(set(ident['sender_slots'])
                       - set(live_ident['sender_slots']))
        checks['sender_slots'] = 'mismatch' if extra else 'match'
        if extra:
            reasons.append(
                'The snapshot has cards on sender slot(s) '
                f"{', '.join(str(s) for s in extra)}, which the controller "
                'does not report at all. It reports '
                f"{', '.join(str(s) for s in sorted(live_ident['sender_slots']))}.")
    else:
        checks['sender_slots'] = 'unknown'

    verdicts = set(checks.values())
    if 'mismatch' in verdicts:
        status = 'mismatch'
    elif 'match' in verdicts:
        status = 'match'
    else:
        # A snapshot nothing could be checked against. Same treatment as no
        # live topology at all: shown, but never asserted to be this wall.
        status = 'unverified'
        reasons.append('None of the snapshot\'s identifying fields could be '
                       'compared with what the controller reports.')

    return {'status': status, 'checks': checks, 'reasons': reasons,
            'snapshot': ident, 'live': live_ident}


def panel_capacity(canvas, panel):
    """Theoretical panel capacity of a canvas at a given panel pitch, or None.

    This is a division, not a measurement: it says how many panels of this
    pitch would TILE the canvas, and says nothing about how many are hung,
    cabled or powered. Everything that renders it has to label it as capacity
    — presenting it as "panels detected" would be the same class of lie this
    endpoint exists to stop telling.
    """
    if not isinstance(canvas, dict) or not isinstance(panel, dict):
        return None
    cw, ch = canvas.get('width'), canvas.get('height')
    pw, ph = panel.get('width'), panel.get('height')
    for value in (cw, ch, pw, ph):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return None
    return {'columns': cw // pw, 'rows': ch // ph,
            'panels': (cw // pw) * (ch // ph),
            'panel': {'width': pw, 'height': ph}}


def _configured_panel_size():
    """Panel pitch from the operator-authored wall config, or None."""
    try:
        panel = wc.load_config(APP_DIR, BASE_DIR).get('panel')
    except Exception:
        logger.exception('Could not read panel size from wall config')
        return None
    return panel if isinstance(panel, dict) else None


@app.route('/api/wall_live', methods=['GET'])
def api_wall_live():
    """Return the device-derived wall topology + per-card monitoring.

    Two sources, with a strict hierarchy between them:

    * The CONTROLLER is the authority on what the wall is — screen name,
      canvas, mosaic, outputs, sender slots. Re-derived from `screen_outputs`
      (R0405) on every request, never defaulted from disk.
    * The enumeration snapshot (`src/wall_live_snapshot.json`) may only supply
      what the controller cannot report: the receiving-card inventory. And it
      may only do so once it has been checked against the live topology — a
      snapshot enumerated on a wall that has since been reconfigured describes
      a different wall, and is withheld entirely rather than dimmed or
      badged. See `compare_snapshot_to_topology`.

    Panel count is therefore three-valued, and `panels` carries its own
    provenance: a number only when a matching enumeration exists, otherwise
    `known: false` with the reason. The controller reports geometry, not how
    many receiving cards are attached, so "unknown" is the honest answer and
    the geometric `capacity` alongside it is explicitly not a panel count.
    """
    snapshot, snapshot_mtime = load_wall_snapshot()

    devices = manager.get_all_states()
    h = next((d for d in devices if d.get('device_type') == 'h_series'
              and d.get('connected')), None)

    topology = wall_topology(h) if h else None
    verdict = compare_snapshot_to_topology(snapshot, topology)
    snapshot_usable = verdict['status'] in ('match', 'unverified')

    if not topology and not snapshot_usable:
        return jsonify({
            'available': False, 'live': False, 'device_connected': bool(h),
            'topology': None,
            'snapshot_status': verdict,
            'snapshot_mtime': snapshot_mtime,
            'enumerate_hint': ENUMERATE_HINT,
            'reason': ('stored snapshot is for a different wall'
                       if verdict['status'] == 'mismatch'
                       else 'no snapshot, no live device'),
        }), 200

    cards = h.get('receiving_cards', []) if h else []
    snapshot_cards = (snapshot or {}).get('cards') if snapshot_usable else None
    snapshot_cards = snapshot_cards if isinstance(snapshot_cards, list) else None

    # Where the cells on screen actually come from, stated once. `live` is a
    # claim about the CARDS, not about the connection: a connected controller
    # sitting in front of snapshot cards is not live data, and saying it is
    # was the original bug.
    if cards:
        cards_source = 'device'
    elif snapshot_cards:
        cards_source = 'snapshot'
    else:
        cards_source = None

    payload = {
        'available': True,
        'live': cards_source == 'device',
        'device_connected': bool(h),
        'topology': topology,
        'topology_source': 'device' if topology else None,
        'snapshot_status': verdict,
        'enumerate_hint': ENUMERATE_HINT,
        # When enumeration captured the snapshot. Older snapshots predate the
        # field, so the file's mtime is the fallback upper bound.
        'captured_at': (snapshot or {}).get('captured_at') if snapshot_usable else None,
        'snapshot_mtime': snapshot_mtime,
        'last_poll': h.get('last_poll') if h else None,
        'cards_source': cards_source,
        'panels': _panel_report(topology, verdict, snapshot_cards, cards),
    }
    if snapshot_usable and snapshot:
        payload['snapshot'] = snapshot
    if h:
        payload['device'] = {
            'device_id': h.get('device_id'),
            'name': h.get('name'),
            'ip': h.get('ip'),
            'connected': h.get('connected'),
            'last_poll': h.get('last_poll'),
            'model_id': h.get('device_info', {}).get('model_id'),
            'proto_version': h.get('firmware_version'),
            'cards_read_at': h.get('cards_read_at'),
        }
        payload['screen_outputs'] = h.get('screen_outputs', {})
        payload['receiving_cards'] = cards
        # Per sender card: fibre or copper, and which of its ports are up.
        # The wall map needs this to stop labelling an Ethernet-patched card's
        # chains "OPT n". Keys are stringified for JSON.
        payload['sender_links'] = {str(k): v
                                   for k, v in (h.get('sender_links') or {}).items()}
        # Suspected data breaks, derived from the bit-error pattern and from
        # inventory cards that stopped answering. Empty until a bit-error read
        # has run — the counter is binary-only and on demand.
        payload['chain_breaks'] = h.get('chain_breaks') or []
        payload['bit_errors_read_at'] = h.get('bit_errors_read_at')
        snmp = h.get('snmp') or {}
        # SNMP counts screens and output cards independently of R0405. Passed
        # through as corroboration, never merged into the R0405 numbers.
        payload['snmp_summary'] = {
            'available': snmp.get('available'),
            'screen_count': (snmp.get('screens') or {}).get('screen_count'),
            'output_card_count': (snmp.get('output') or {}).get('card_count'),
            'port_count': (snmp.get('output') or {}).get('port_count'),
        }
    return jsonify(payload)


def _populated_sender_cards(cards):
    """How many sender cards currently have receiving cards behind them.

    This is NOT a correction to the controller's slot count — that count is
    right. On the H15 R0405 reports slots 20, 22, 28 and 30 and all four are
    sender cards: two primary and two backup. The backups answer nothing over
    R0155 and nothing to a per-card binary read, because nothing is running
    behind them until they take over.

    So the two numbers mean different things and the UI shows both: how many
    sender cards are installed (from the controller) and how many are carrying
    panels right now (from the inventory). A drop in the second without a
    change in the first is a failover or a dead fibre, which is exactly the
    kind of thing this dashboard exists to show.

    None when there is no inventory to count from.
    """
    if not cards:
        return None
    slots = {c.get('slot') for c in cards if c.get('slot') is not None}
    if slots:
        return len(slots)
    numbers = {c.get('card_number') for c in cards
               if c.get('card_number') is not None}
    return len(numbers) or None


def _panel_report(topology, verdict, snapshot_cards, live_cards):
    """How many receiving cards (panels) the wall has — or that we don't know.

    The processor reports geometry; it does not report how many receiving
    cards are hanging off each port. That takes an enumeration walk. So the
    only sources of a real number are a live per-card read or a snapshot that
    has been confirmed to describe THIS wall. Everything else is `known:
    false` plus a reason and the command that fixes it.
    """
    capacity = panel_capacity((topology or {}).get('canvas'),
                              _configured_panel_size())
    report = {'known': False, 'count': None, 'source': None,
              'capacity': capacity, 'enumerate_hint': ENUMERATE_HINT,
              'populated_sender_cards': None}

    # How many panels the wall HAS is an inventory question, and the verified
    # enumeration is the authority on it. A live read answers a different
    # question — how many were read just now — and reading one 22-card chain
    # must not restate a 286-panel wall as 22 panels.
    if verdict['status'] == 'match' and snapshot_cards:
        report.update({'known': True, 'count': len(snapshot_cards),
                       'source': 'enumeration',
                       'read': len(live_cards or []),
                       'populated_sender_cards': _populated_sender_cards(
                           snapshot_cards)})
        return report

    if live_cards:
        # No verified inventory, so the only count available is what answered.
        # Flagged as a floor: cards nobody read are not in it.
        report.update({'known': True, 'count': len(live_cards),
                       'source': 'device_read',
                       'read': len(live_cards),
                       'is_lower_bound': True,
                       'populated_sender_cards': _populated_sender_cards(
                           live_cards)})
        return report

    if verdict['status'] == 'mismatch':
        report['reason'] = (
            'The stored enumeration is for a different wall, so the number of '
            'panels on this one is unknown until it is re-enumerated.')
    elif verdict['status'] == 'no_snapshot':
        report['reason'] = ('No enumeration has been run, so the number of '
                            'panels is unknown.')
    elif verdict['status'] == 'unverified' and snapshot_cards:
        # A snapshot that could not be checked. Its count is offered, but as
        # unverified — it is not promoted to `known`.
        report.update({'count': len(snapshot_cards), 'source': 'unverified_snapshot',
                       'reason': ('The stored enumeration could not be checked '
                                  'against the controller, so this count is '
                                  'unconfirmed.')})
    else:
        report['reason'] = ('No per-card enumeration exists for the wall that '
                            'is currently configured.')
    return report


@app.route('/api/wall_rendered', methods=['GET'])
def api_wall_rendered():
    """Combined view: config + layout merged into one pillar list with display_position."""
    try:
        config = wc.load_config(APP_DIR, BASE_DIR)
        layout = wc.load_layout(APP_DIR, BASE_DIR)
        return jsonify(wc.render_wall(config, layout))
    except Exception as e:
        logger.error('Failed to render wall: %s', e)
        return jsonify({'error': str(e)}), 500


# ── Error Log Persistence ─────────────────────────────────

ERROR_LOG_FILE = os.path.join(APP_DIR, 'error_log.json')
MAX_ERROR_LOG = 500
_error_log = []
# The log is appended from poll threads and rebound from request threads —
# every read and write of _error_log / _next_error_id goes through this lock.
_error_lock = threading.RLock()
# Monotonic id counter. `len(_error_log) + 1` collided as soon as
# clear-resolved shrank the list (or the 500-entry cap dropped old rows), and
# resolve/acknowledge then hit whichever alert happened to share the id.
_next_error_id = 1


def _seed_error_id_locked():
    """Seed the id counter above every id already in the log."""
    global _next_error_id
    highest = 0
    for entry in _error_log:
        try:
            highest = max(highest, int(entry.get('id', 0)))
        except (TypeError, ValueError):
            continue
    _next_error_id = highest + 1


def load_error_log():
    with _error_lock:
        try:
            if os.path.exists(ERROR_LOG_FILE):
                with open(ERROR_LOG_FILE, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                # In place — poll threads hold a reference to this list.
                _error_log[:] = loaded if isinstance(loaded, list) else []
        except Exception:
            # Never silently discard the fault history: say so loudly and keep
            # the unreadable file aside so it can be recovered by hand.
            logger.exception('Failed to load error log from %s', ERROR_LOG_FILE)
            _error_log[:] = []
            try:
                os.replace(ERROR_LOG_FILE, ERROR_LOG_FILE + '.corrupt')
                logger.error('Unreadable error log preserved as %s.corrupt',
                             ERROR_LOG_FILE)
            except OSError:
                logger.exception('Could not preserve unreadable error log')
        _seed_error_id_locked()
        return _error_log


def save_error_log():
    """Persist the log atomically. Caller may or may not hold _error_lock."""
    with _error_lock:
        snapshot = _error_log[-MAX_ERROR_LOG:]
    try:
        _atomic_write_json(ERROR_LOG_FILE, snapshot)
        return True
    except Exception:
        logger.exception('Failed to save error log to %s', ERROR_LOG_FILE)
        return False


def add_error(severity, device, message, cabinet=None, port=None, value=None):
    """Add an error to the persistent log and broadcast via SocketIO."""
    global _next_error_id
    with _error_lock:
        entry = {
            'id': _next_error_id,
            'timestamp': datetime.now().isoformat(),
            'severity': severity,
            'device': device,
            'message': message,
            'cabinet': cabinet,
            'port': port,
            'value': value,
            'resolved': False,
            'resolved_at': None,
            'acknowledged': False,
        }
        _next_error_id += 1
        _error_log.append(entry)
    save_error_log()
    socketio.emit('alert', entry)
    log_event('alert', {'severity': severity, 'device': device, 'message': message})
    return entry


@app.route('/api/errors', methods=['GET'])
def api_list_errors():
    """Get error log with optional filters."""
    severity = request.args.get('severity')
    device = request.args.get('device')
    resolved = request.args.get('resolved')
    try:
        limit = int(request.args.get('limit', 100))
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400
    limit = max(1, min(limit, MAX_ERROR_LOG))

    with _error_lock:
        filtered = list(_error_log)
    if severity and severity != 'ALL':
        filtered = [e for e in filtered if e.get('severity') == severity]
    if device and device != 'ALL':
        filtered = [e for e in filtered if e.get('device') == device]
    if resolved == 'true':
        filtered = [e for e in filtered if e.get('resolved')]
    elif resolved == 'false':
        filtered = [e for e in filtered if not e.get('resolved')]

    return jsonify(filtered[-limit:][::-1])  # Newest first


@app.route('/api/errors/<int:error_id>/resolve', methods=['POST'])
def api_resolve_error(error_id):
    with _error_lock:
        for entry in _error_log:
            if entry.get('id') == error_id:
                entry['resolved'] = True
                entry['resolved_at'] = datetime.now().isoformat()
                break
        else:
            return jsonify({"error": "Not found"}), 404
    save_error_log()
    socketio.emit('error_resolved', {'id': error_id})
    return jsonify({"status": "resolved"})


@app.route('/api/errors/<int:error_id>/acknowledge', methods=['POST'])
def api_acknowledge_error(error_id):
    with _error_lock:
        for entry in _error_log:
            if entry.get('id') == error_id:
                entry['acknowledged'] = True
                break
        else:
            return jsonify({"error": "Not found"}), 404
    save_error_log()
    return jsonify({"status": "acknowledged"})


@app.route('/api/errors/clear-resolved', methods=['POST'])
def api_clear_resolved():
    """Remove all resolved errors from the log."""
    with _error_lock:
        remaining = [e for e in _error_log if not e.get('resolved')]
        cleared = len(_error_log) - len(remaining)
        # Mutate in place rather than rebinding — the list is shared with the
        # poll threads that append to it.
        _error_log[:] = remaining
    save_error_log()
    return jsonify({"status": "cleared", "cleared": cleared})


# ── CSV Export ────────────────────────────────────────────

@app.route('/api/export/errors.csv', methods=['GET'])
def api_export_errors_csv():
    """Export error log as CSV download."""
    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Timestamp', 'Severity', 'Device', 'Cabinet', 'Port', 'Message',
                     'Value', 'Resolved', 'Resolved At'])

    with _error_lock:
        entries = list(_error_log)

    for e in reversed(entries):
        writer.writerow([
            e.get('timestamp', ''),
            e.get('severity', ''),
            e.get('device', ''),
            e.get('cabinet', ''),
            e.get('port', ''),
            e.get('message', ''),
            e.get('value', ''),
            e.get('resolved', False),
            e.get('resolved_at', ''),
        ])

    from flask import make_response
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = (
        f'attachment; filename=novastar_errors_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    )
    return response


@app.route('/api/export/monitoring.csv', methods=['GET'])
def api_export_monitoring_csv():
    """Export current monitoring snapshot as CSV."""
    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Device', 'IP', 'Connected', 'Temperature', 'Voltage',
                     'Brightness', 'Card Count', 'Link Status', 'Firmware'])

    for dev in manager.get_all_states():
        lm = dev.get('live_monitoring', {})
        writer.writerow([
            dev.get('name', ''),
            dev.get('ip', ''),
            dev.get('connected', False),
            lm.get('temperature_c', ''),
            lm.get('voltage_v', ''),
            dev.get('brightness_pct', ''),
            lm.get('card_count', ''),
            lm.get('link_status', ''),
            dev.get('firmware_version', ''),
        ])

    from flask import make_response
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = (
        f'attachment; filename=novastar_snapshot_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    )
    return response


# ── Startup ───────────────────────────────────────────────

_init_lock = threading.Lock()
_initialized = False


def init_devices():
    """Load saved devices, error log, and start polling.

    Idempotent: this is called from the server-start path (see `_run_server`
    below), not at import time. Importing `app` must never open a socket to
    real hardware — test clients, the launchers' import step and the Werkzeug
    reloader's parent process all import this module.
    """
    global _initialized
    with _init_lock:
        if _initialized:
            return False
        _initialized = True

    load_error_log()
    logger.info('Loaded %d error log entries', len(_error_log))

    settings = reload_settings()
    for dev_conf in settings.get('devices', []):
        try:
            manager.add_device(
                dev_conf['id'],
                dev_conf.get('name', dev_conf['ip']),
                dev_conf['ip'],
                dev_conf.get('port', 5200),
            )
        except Exception as e:
            logger.error('Failed to add device %s: %s', dev_conf.get('ip'), e)

    # Auto-add demo device if --demo flag is set
    if DEMO_MODE:
        from demo_device import DemoDevice
        demo = DemoDevice()
        manager.devices[demo.device_id] = demo
        logger.info('Demo mode: added simulated VX1000 device')

    apply_poll_interval(settings)
    manager.start()
    logger.info('Started monitoring %d device(s)%s', len(manager.devices),
                ' (DEMO MODE)' if DEMO_MODE else '')
    log_event('server_start', {
        'device_count': len(manager.devices),
        'poll_interval': manager.poll_interval,
        'log_dir': LOG_DIR_PATH,
        'demo_mode': DEMO_MODE,
    })
    return True


def _should_init_here(use_reloader):
    """Should THIS process own the pollers?

    With the Werkzeug reloader on, `socketio.run()` runs in two processes: the
    supervisor and the reloaded child (which has WERKZEUG_RUN_MAIN set). Only
    the child should poll — otherwise two processes hit the same controller
    with duplicate reads and duplicate heartbeats.
    """
    if not use_reloader:
        return True
    return os.environ.get('WERKZEUG_RUN_MAIN') == 'true'


# The packaged launchers do `from app import app, socketio` and then call
# socketio.run() themselves, so device init has to hang off run() to keep
# working for them without touching launcher_mac.py / launcher_pc.py.
_socketio_run = socketio.run


def _run_server(flask_app, *args, **kwargs):
    """socketio.run() wrapper that initializes devices in the serving process."""
    use_reloader = kwargs.get('use_reloader', kwargs.get('debug', False))
    if _should_init_here(use_reloader):
        init_devices()
    else:
        logger.info('Reloader supervisor process — not starting pollers')
    return _socketio_run(flask_app, *args, **kwargs)


socketio.run = _run_server


if __name__ == '__main__':
    # Port can be overridden via NSM_PORT env var (used by the editor's launch config
    # for the preview server). Defaults to 8060 in dev / 8050 in prod-bundle.
    port = int(os.environ.get('NSM_PORT', 8060))
    # Debug (and with it the Werkzeug reloader) is opt-in via NSM_DEBUG. It
    # used to be hard-coded on, which spawned a reloader child that polled the
    # live controller alongside its parent.
    debug = os.environ.get('NSM_DEBUG', '').strip().lower() in ('1', 'true', 'yes', 'on')
    logger.info('Starting server on http://127.0.0.1:%s%s', port,
                ' (debug)' if debug else '')
    socketio.run(app, host='127.0.0.1', port=port, debug=debug,
                 allow_unsafe_werkzeug=True)
