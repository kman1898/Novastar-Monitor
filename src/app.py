"""
NovaStar Monitor — Flask + SocketIO Backend
Following the LED Raster Designer app pattern.
"""

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import json
import os
import sys
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
    try:
        if os.path.exists(LOG_FILE_PATH) and os.path.getsize(LOG_FILE_PATH) > LOG_MAX_BYTES:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup = os.path.join(LOG_DIR_PATH, f'novastar_monitor_{ts}.log')
            os.rename(LOG_FILE_PATH, backup)
            prune_log_files()
    except Exception:
        pass


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
        pass


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
        pass


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
    """Log unhandled exceptions."""
    logger.error('Unhandled exception: %s', e)
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
from device_manager import DeviceManager

# Demo mode flag — set via --demo CLI arg or /api/demo endpoint
DEMO_MODE = '--demo' in sys.argv

# Global device manager instance
manager = DeviceManager(poll_interval=2.0)

# ── Settings ──────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "devices": [],
    "poll_interval": 2.0,
    "temp_warning": 60.0,
    "temp_critical": 75.0,
    "voltage_min": 4.7,
}


def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                return {**DEFAULT_SETTINGS, **json.load(f)}
    except Exception:
        pass
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        logger.error('Failed to save settings: %s', e)


# ── SocketIO Events ──────────────────────────────────────

def on_device_update(device_id, state):
    """Called by device manager when a device is polled."""
    socketio.emit('device_update', {
        'device_id': device_id,
        'state': state,
        'timestamp': datetime.now().isoformat(),
    })

    # Check alert thresholds
    settings = load_settings()
    lm = state.get('live_monitoring', {})
    temp = lm.get('temperature_c')
    voltage = lm.get('voltage_v')
    device_name = state.get('name', device_id)

    if temp and temp >= settings.get('temp_critical', 75):
        # Avoid duplicate alerts — check if same alert exists in last 60 seconds
        if not _recent_alert_exists('CRITICAL', device_name, 'Temperature'):
            add_error('CRITICAL', device_name,
                      f"Temperature {temp:.1f}°C exceeds critical threshold of {settings.get('temp_critical', 75)}°C",
                      value=temp)
    elif temp and temp >= settings.get('temp_warning', 60):
        if not _recent_alert_exists('WARNING', device_name, 'Temperature'):
            add_error('WARNING', device_name,
                      f"Temperature {temp:.1f}°C exceeds warning threshold of {settings.get('temp_warning', 60)}°C",
                      value=temp)

    if voltage and voltage < settings.get('voltage_min', 4.7):
        if not _recent_alert_exists('WARNING', device_name, 'Voltage'):
            add_error('WARNING', device_name,
                      f"Voltage {voltage:.2f}V below minimum threshold of {settings.get('voltage_min', 4.7)}V",
                      value=voltage)


def _recent_alert_exists(severity, device, keyword, seconds=60):
    """Check if a similar alert was created within the last N seconds."""
    cutoff = datetime.now().timestamp() - seconds
    for entry in reversed(_error_log[-20:]):
        try:
            entry_ts = datetime.fromisoformat(entry['timestamp']).timestamp()
            if (entry_ts > cutoff and
                entry['severity'] == severity and
                entry['device'] == device and
                keyword in entry.get('message', '')):
                return True
        except Exception:
            pass
    return False


def on_device_error(device_id, error_info):
    """Called by device manager on errors."""
    device_name = device_id
    dev = manager.devices.get(device_id)
    if dev:
        device_name = dev.state.get('name', device_id)
    if not _recent_alert_exists('CRITICAL', device_name, 'Connection'):
        add_error('CRITICAL', device_name,
                  f"Connection error: {error_info.get('error', 'unknown')}")


manager.set_callbacks(on_update=on_device_update, on_error=on_device_error)


@socketio.on('connect')
def handle_connect():
    """Send current state and error log to newly connected client."""
    all_states = manager.get_all_states()
    emit('full_state', {
        'devices': all_states,
        'settings': load_settings(),
        'errors': _error_log[-100:][::-1],
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
    data = request.get_json()
    if not data or 'ip' not in data:
        return jsonify({"error": "IP address required"}), 400

    name = data.get('name', f"NovaStar {data['ip']}")
    ip = data['ip']
    port = int(data.get('port', 5200))
    device_id = f"dev-{ip.replace('.', '-')}"

    # Check if already exists
    if device_id in manager.devices:
        return jsonify({"error": "Device already exists"}), 409

    manager.add_device(device_id, name, ip, port)

    # Save to settings
    settings = load_settings()
    settings['devices'].append({
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
    settings['devices'] = [d for d in settings['devices'] if d['id'] != device_id]
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


@app.route('/api/settings', methods=['POST'])
def api_update_settings():
    data = request.get_json()
    settings = load_settings()
    settings.update(data)
    save_settings(settings)
    return jsonify({"status": "saved"})


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
        pass
    return jsonify({"version": version})


# ── Error Log Persistence ─────────────────────────────────

ERROR_LOG_FILE = os.path.join(APP_DIR, 'error_log.json')
MAX_ERROR_LOG = 500
_error_log = []


def load_error_log():
    global _error_log
    try:
        if os.path.exists(ERROR_LOG_FILE):
            with open(ERROR_LOG_FILE, 'r') as f:
                _error_log = json.load(f)
    except Exception:
        _error_log = []
    return _error_log


def save_error_log():
    try:
        with open(ERROR_LOG_FILE, 'w') as f:
            json.dump(_error_log[-MAX_ERROR_LOG:], f, indent=2)
    except Exception as e:
        logger.error('Failed to save error log: %s', e)


def add_error(severity, device, message, cabinet=None, port=None, value=None):
    """Add an error to the persistent log and broadcast via SocketIO."""
    entry = {
        'id': len(_error_log) + 1,
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
    limit = int(request.args.get('limit', 100))

    filtered = _error_log.copy()
    if severity and severity != 'ALL':
        filtered = [e for e in filtered if e['severity'] == severity]
    if device and device != 'ALL':
        filtered = [e for e in filtered if e['device'] == device]
    if resolved == 'true':
        filtered = [e for e in filtered if e['resolved']]
    elif resolved == 'false':
        filtered = [e for e in filtered if not e['resolved']]

    return jsonify(filtered[-limit:][::-1])  # Newest first


@app.route('/api/errors/<int:error_id>/resolve', methods=['POST'])
def api_resolve_error(error_id):
    for entry in _error_log:
        if entry['id'] == error_id:
            entry['resolved'] = True
            entry['resolved_at'] = datetime.now().isoformat()
            save_error_log()
            socketio.emit('error_resolved', {'id': error_id})
            return jsonify({"status": "resolved"})
    return jsonify({"error": "Not found"}), 404


@app.route('/api/errors/<int:error_id>/acknowledge', methods=['POST'])
def api_acknowledge_error(error_id):
    for entry in _error_log:
        if entry['id'] == error_id:
            entry['acknowledged'] = True
            save_error_log()
            return jsonify({"status": "acknowledged"})
    return jsonify({"error": "Not found"}), 404


@app.route('/api/errors/clear-resolved', methods=['POST'])
def api_clear_resolved():
    """Remove all resolved errors from the log."""
    global _error_log
    _error_log = [e for e in _error_log if not e['resolved']]
    save_error_log()
    return jsonify({"status": "cleared"})


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

    for e in reversed(_error_log):
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

def init_devices():
    """Load saved devices, error log, and start polling."""
    load_error_log()
    logger.info('Loaded %d error log entries', len(_error_log))

    settings = load_settings()
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

    manager.poll_interval = settings.get('poll_interval', 2.0)
    manager.start()
    logger.info('Started monitoring %d device(s)%s', len(manager.devices),
                ' (DEMO MODE)' if DEMO_MODE else '')
    log_event('server_start', {
        'device_count': len(manager.devices),
        'poll_interval': manager.poll_interval,
        'log_dir': LOG_DIR_PATH,
        'demo_mode': DEMO_MODE,
    })


# Initialize on import (but not on reload)
_initialized = False
if not _initialized:
    init_devices()
    _initialized = True


if __name__ == '__main__':
    logger.info('Starting server on http://127.0.0.1:8050')
    socketio.run(app, host='127.0.0.1', port=8050, debug=True,
                 allow_unsafe_werkzeug=True)
