"""
NovaStar Monitor — Launcher Settings
Handles settings persistence, network interface detection, and run-at-login.
Mirrors LED Raster Designer launcher_settings.py pattern.
"""

import json
import os
import sys
import socket
import platform

if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(APP_DIR, 'settings.json')

DEFAULT_SETTINGS = {
    'interface': '127.0.0.1',
    'port': 8050,
    'start_minimized': False,
    'run_at_login': False,
    'auto_open_browser': True,
}


def load_settings():
    """Load settings from JSON file, merging with defaults."""
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                saved = json.load(f)
                return {**DEFAULT_SETTINGS, **saved}
    except Exception:
        pass
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    """Save settings to JSON file."""
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print(f'[NovaStar Monitor] Failed to save settings: {e}')


def get_network_interfaces():
    """
    Get list of available network interfaces as (ip, label) tuples.
    Returns at minimum localhost and 0.0.0.0 (all interfaces).
    """
    interfaces = [
        ('127.0.0.1', '127.0.0.1 (Localhost)'),
        ('0.0.0.0', '0.0.0.0 (All Interfaces)'),
    ]

    try:
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, socket.AF_INET)
        seen = set()
        for addr in addrs:
            ip = addr[4][0]
            if ip not in seen and ip != '127.0.0.1':
                seen.add(ip)
                interfaces.append((ip, f'{ip} ({hostname})'))
    except Exception:
        pass

    # Try platform-specific methods for more complete list
    try:
        if platform.system() == 'Windows':
            import subprocess
            result = subprocess.run(['ipconfig'], capture_output=True, text=True, timeout=5)
            for line in result.stdout.split('\n'):
                line = line.strip()
                if 'IPv4' in line and ':' in line:
                    ip = line.split(':')[-1].strip()
                    if ip and ip not in [i[0] for i in interfaces]:
                        interfaces.append((ip, ip))
        else:
            import subprocess
            result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
            for ip in result.stdout.strip().split():
                if ip and ip not in [i[0] for i in interfaces]:
                    interfaces.append((ip, ip))
    except Exception:
        pass

    return interfaces


def set_run_at_login(enabled):
    """Configure the app to run at system login."""
    system = platform.system()

    if system == 'Windows':
        _set_run_at_login_windows(enabled)
    elif system == 'Darwin':
        _set_run_at_login_macos(enabled)
    elif system == 'Linux':
        _set_run_at_login_linux(enabled)


def _set_run_at_login_windows(enabled):
    """Add/remove from Windows registry Run key."""
    try:
        import winreg
        key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'
        app_name = 'NovaStar Monitor'

        if getattr(sys, 'frozen', False):
            exe_path = sys.executable
        else:
            exe_path = f'"{sys.executable}" "{os.path.abspath(__file__)}"'

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, exe_path)
        else:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f'[NovaStar Monitor] Failed to set run at login: {e}')


def _set_run_at_login_macos(enabled):
    """Add/remove macOS Launch Agent plist."""
    try:
        plist_dir = os.path.expanduser('~/Library/LaunchAgents')
        plist_path = os.path.join(plist_dir, 'com.novastar.monitor.plist')

        if enabled:
            os.makedirs(plist_dir, exist_ok=True)
            if getattr(sys, 'frozen', False):
                program = sys.executable
            else:
                program = sys.executable
                args = os.path.abspath(__file__)

            plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.novastar.monitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>{program}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>"""
            with open(plist_path, 'w') as f:
                f.write(plist_content)
        else:
            if os.path.exists(plist_path):
                os.remove(plist_path)
    except Exception as e:
        print(f'[NovaStar Monitor] Failed to set run at login: {e}')


def _set_run_at_login_linux(enabled):
    """Add/remove XDG autostart desktop entry."""
    try:
        autostart_dir = os.path.expanduser('~/.config/autostart')
        desktop_path = os.path.join(autostart_dir, 'novastar-monitor.desktop')

        if enabled:
            os.makedirs(autostart_dir, exist_ok=True)
            if getattr(sys, 'frozen', False):
                exe = sys.executable
            else:
                exe = f'{sys.executable} {os.path.abspath(__file__)}'

            content = f"""[Desktop Entry]
Type=Application
Name=NovaStar Monitor
Exec={exe}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
"""
            with open(desktop_path, 'w') as f:
                f.write(content)
        else:
            if os.path.exists(desktop_path):
                os.remove(desktop_path)
    except Exception as e:
        print(f'[NovaStar Monitor] Failed to set run at login: {e}')
