"""
NovaStar Monitor — Flask + SocketIO Backend
Following the LED Raster Designer app pattern.
"""

from flask import Flask, render_template, request, jsonify
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


@app.route('/api/log', methods=['POST'])
def api_client_log():
    """Accept log events from the browser client."""
    data = request.get_json(silent=True) or {}
    log_event(data.get('action', 'client_event'), data.get('details'), source='client')
    return jsonify({'status': 'ok'})


# Import device manager
from device_manager import DeviceManager, DEFAULT_POLL_INTERVAL

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

DEFAULT_SETTINGS = {
    "devices": [],
    "poll_interval": DEFAULT_POLL_INTERVAL,
    "temp_warning": 60.0,
    "temp_critical": 75.0,
    "voltage_min": 4.7,
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


def _read_settings_file():
    """Read settings from disk, merged over the defaults.

    Always deep-copies the defaults: DEFAULT_SETTINGS['devices'] is a mutable
    list, and a shallow copy would let a caller's `settings['devices'].append`
    permanently edit the module-level defaults.
    """
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                return {**copy.deepcopy(DEFAULT_SETTINGS), **json.load(f)}
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
    """
    temp_warn, temp_crit, volt_min = _thresholds(settings)
    breaches = []

    for card in cards or []:
        if card.get('online') is False:
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
        # Neither reported (both None, e.g. the binary path) → nothing to say.
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
    """
    emitted = 0
    suppressed = 0

    by_kind = {}
    for b in breaches:
        by_kind.setdefault((b['severity'], b['metric']), []).append(b)

    for (severity, metric), group in sorted(by_kind.items()):
        chains = {}
        for b in group:
            chains.setdefault(b['chain'], []).append(b)

        # Wall-wide event
        if len(chains) >= DEVICE_ROLLUP_CHAIN_THRESHOLD:
            worst = _worst(group)
            if _alert_due(f'{device_name}|wall|{metric}|{severity}'):
                add_error(severity, device_name,
                          f'{metric} alert on {len(group)} cards '
                          f'across {len(chains)} chains — worst {worst["label"]}: '
                          f'{worst["text"]}',
                          cabinet=f'{len(group)} cards / {len(chains)} chains',
                          value=worst['value'])
                emitted += 1
            continue

        for chain_key, items in sorted(chains.items()):
            worst = _worst(items)

            # Chain-level event
            if len(items) >= CHAIN_ALERT_THRESHOLD:
                if _alert_due(f'{device_name}|chain:{chain_key}|{metric}|{severity}'):
                    add_error(severity, device_name,
                              f'{metric} alert on {len(items)} cards '
                              f'on {worst["chain_label"]} — worst '
                              f'{worst["label"]}: {worst["text"]}',
                              cabinet=f'{len(items)} cards on {worst["chain_label"]}',
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


def evaluate_alerts(device_id, state):
    """Run the full alert evaluation for one polled device state."""
    settings = load_settings()
    device_name = state.get('name', device_id)
    _emit_card_alerts(device_name,
                      _card_breaches(state.get('receiving_cards'), settings))
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

    mtime_iso = datetime.fromtimestamp(stat.st_mtime).isoformat()
    with _snapshot_cache_lock:
        _snapshot_cache[path] = {'stamp': stamp, 'data': data,
                                 'mtime_iso': mtime_iso}
    return data, mtime_iso


@app.route('/api/wall_live', methods=['GET'])
def api_wall_live():
    """Return the device-derived wall topology + per-card monitoring.

    Reads the most recent enumeration snapshot (src/wall_live_snapshot.json),
    plus current live state from device_manager. The snapshot is produced
    by the R0155 enumeration script; the live state overlays current temps
    if the polling thread has been updating cards.

    The payload states its own freshness explicitly. A snapshot file existing
    on disk says nothing about whether any hardware is currently connected —
    rendering months-old temperatures as if they were live is the worst
    failure mode this tool has.
    """
    snapshot, snapshot_mtime = load_wall_snapshot()

    devices = manager.get_all_states()
    h = next((d for d in devices if d.get('device_type') == 'h_series'
              and d.get('connected')), None)

    if not snapshot and not h:
        return jsonify({'available': False, 'live': False,
                        'device_connected': False,
                        'reason': 'no snapshot, no live device'}), 200

    cards = h.get('receiving_cards', []) if h else []
    payload = {
        'available': True,
        # `live` means: a device is connected AND it has actually reported
        # cards this session. Anything else is historical data.
        'live': bool(h) and len(cards) > 0,
        'device_connected': bool(h),
        # When enumeration captured the snapshot. The writer is adding this
        # field separately, so fall back to the file's mtime until it lands.
        'captured_at': (snapshot or {}).get('captured_at'),
        'snapshot_mtime': snapshot_mtime,
        'last_poll': h.get('last_poll') if h else None,
    }
    if snapshot:
        payload['snapshot'] = snapshot
    if h:
        payload['device'] = {
            'name': h.get('name'),
            'ip': h.get('ip'),
            'connected': h.get('connected'),
            'last_poll': h.get('last_poll'),
            'model_id': h.get('device_info', {}).get('model_id'),
            'proto_version': h.get('firmware_version'),
        }
        payload['screen_outputs'] = h.get('screen_outputs', {})
        payload['receiving_cards'] = cards
    return jsonify(payload)


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
    # Port can be overridden via NSM_PORT env var (used by .claude/launch.json
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
