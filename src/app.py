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
from datetime import datetime

# Support PyInstaller bundle
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, 'templates'),
            static_folder=os.path.join(BASE_DIR, 'static'))
app.config['SECRET_KEY'] = 'novastar-monitor-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Settings file location
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(APP_DIR, 'novastar_settings.json')

# Import device manager
from device_manager import DeviceManager

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
        print(f"[NovaStar Monitor] Failed to save settings: {e}")


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

    if temp and temp >= settings.get('temp_critical', 75):
        socketio.emit('alert', {
            'severity': 'CRITICAL',
            'device': state.get('name', device_id),
            'message': f"Temperature {temp:.1f}°C exceeds critical threshold",
            'timestamp': datetime.now().isoformat(),
        })
    elif temp and temp >= settings.get('temp_warning', 60):
        socketio.emit('alert', {
            'severity': 'WARNING',
            'device': state.get('name', device_id),
            'message': f"Temperature {temp:.1f}°C exceeds warning threshold",
            'timestamp': datetime.now().isoformat(),
        })

    if voltage and voltage < settings.get('voltage_min', 4.7):
        socketio.emit('alert', {
            'severity': 'WARNING',
            'device': state.get('name', device_id),
            'message': f"Voltage {voltage:.2f}V below minimum threshold",
            'timestamp': datetime.now().isoformat(),
        })


def on_device_error(device_id, error_info):
    """Called by device manager on errors."""
    socketio.emit('alert', {
        'severity': 'CRITICAL',
        'device': device_id,
        'message': f"Connection error: {error_info.get('error', 'unknown')}",
        'timestamp': datetime.now().isoformat(),
    })


manager.set_callbacks(on_update=on_device_update, on_error=on_device_error)


@socketio.on('connect')
def handle_connect():
    """Send current state to newly connected client."""
    all_states = manager.get_all_states()
    emit('full_state', {
        'devices': all_states,
        'settings': load_settings(),
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


# ── Startup ───────────────────────────────────────────────

def init_devices():
    """Load saved devices and start polling."""
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
            print(f"[NovaStar Monitor] Failed to add device {dev_conf.get('ip')}: {e}")

    manager.poll_interval = settings.get('poll_interval', 2.0)
    manager.start()
    print(f"[NovaStar Monitor] Started monitoring {len(manager.devices)} device(s)")


# Initialize on import (but not on reload)
_initialized = False
if not _initialized:
    init_devices()
    _initialized = True


if __name__ == '__main__':
    print("[NovaStar Monitor] Starting server on http://127.0.0.1:8050")
    socketio.run(app, host='127.0.0.1', port=8050, debug=True,
                 allow_unsafe_werkzeug=True)
