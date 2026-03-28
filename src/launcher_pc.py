"""
NovaStar Monitor — Windows/Linux System Tray Launcher
Runs the Flask/SocketIO server in the background and provides a system tray icon.
"""
import sys
import os
import threading
import webbrowser
import time

# Resolve paths for PyInstaller bundle
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    APP_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BASE_DIR

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from launcher_settings import (
    load_settings, save_settings, get_network_interfaces, set_run_at_login
)


def start_flask_server(settings):
    """Import and run the Flask app in a background thread."""
    host = settings.get('interface', '127.0.0.1')
    port = int(settings.get('port', 8050))

    from app import app, socketio, log_event
    log_event('launcher_start', {
        'platform': 'pc',
        'host': host,
        'port': port,
        'log_dir': os.environ.get('_NSM_LOG_DIR', 'unknown'),
    })
    print(f'[NovaStar Monitor] Server starting on {host}:{port}')
    socketio.run(app, host=host, port=port, debug=False,
                 allow_unsafe_werkzeug=True)


def get_display_url(settings):
    """Build the display URL from settings."""
    host = settings.get('interface', '127.0.0.1')
    port = settings.get('port', 8050)
    display_host = host if host != '0.0.0.0' else '127.0.0.1'
    return f'http://{display_host}:{port}'


def create_tray_icon_image():
    """Create the NovaStar Monitor tray icon programmatically."""
    from PIL import Image, ImageDraw

    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Blue-cyan gradient background circle
    draw.ellipse([4, 4, 60, 60], fill='#1e40af', outline='#3b82f6', width=2)

    # "N" letter in white
    draw.text((20, 14), 'N', fill='#ffffff')

    # Small green status dot (bottom-right)
    draw.ellipse([44, 44, 56, 56], fill='#10b981', outline='#064e3b', width=1)

    return img


def run_tray(settings):
    """Set up the system tray icon using pystray."""
    import pystray
    from pystray import MenuItem, Menu

    def open_browser(icon, item):
        webbrowser.open(get_display_url(settings))

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)

    # Network interface submenu
    interfaces = get_network_interfaces()

    def make_iface_callback(ip):
        def callback(icon, item):
            settings['interface'] = ip
            save_settings(settings)
        return callback

    def iface_checked(ip):
        def check(item):
            return settings.get('interface', '127.0.0.1') == ip
        return check

    iface_items = []
    for ip, label in interfaces:
        iface_items.append(
            MenuItem(label, make_iface_callback(ip), checked=iface_checked(ip))
        )

    host = settings.get('interface', '127.0.0.1')
    port = settings.get('port', 8050)
    display_host = host if host != '0.0.0.0' else '127.0.0.1'

    def toggle_run_at_login(icon, item):
        enabled = not settings.get('run_at_login', False)
        settings['run_at_login'] = enabled
        save_settings(settings)
        set_run_at_login(enabled)

    def run_at_login_checked(item):
        return settings.get('run_at_login', False)

    def toggle_auto_browser(icon, item):
        settings['auto_open_browser'] = not settings.get('auto_open_browser', True)
        save_settings(settings)

    def auto_browser_checked(item):
        return settings.get('auto_open_browser', True)

    icon = pystray.Icon(
        name='NovaStar Monitor',
        icon=create_tray_icon_image(),
        title='NovaStar Monitor',
        menu=Menu(
            MenuItem('Open Dashboard', open_browser, default=True),
            Menu.SEPARATOR,
            MenuItem('Network Interface', Menu(*iface_items)),
            MenuItem(f'Port: {port}', None, enabled=False),
            Menu.SEPARATOR,
            MenuItem(f'Running on {display_host}:{port}', None, enabled=False),
            Menu.SEPARATOR,
            MenuItem('Open Browser on Launch', toggle_auto_browser,
                     checked=auto_browser_checked),
            MenuItem('Run at Login', toggle_run_at_login,
                     checked=run_at_login_checked),
            Menu.SEPARATOR,
            MenuItem('Quit NovaStar Monitor', quit_app),
        )
    )

    icon.run()


def main():
    settings = load_settings()

    # Start Flask in background daemon thread
    server_thread = threading.Thread(target=start_flask_server, args=(settings,), daemon=True)
    server_thread.start()

    # Give the server a moment to start
    time.sleep(1.5)

    # Auto-open browser
    if settings.get('auto_open_browser', True) and not settings.get('start_minimized', False):
        webbrowser.open(get_display_url(settings))

    # Run the system tray (blocks main thread)
    run_tray(settings)


if __name__ == '__main__':
    main()
