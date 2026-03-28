"""
NovaStar Monitor — macOS System Tray Launcher
Uses rumps for macOS-native menu bar integration if available,
falls back to pystray otherwise.
"""
import sys
import os
import threading
import webbrowser
import time

if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    APP_DIR = os.path.dirname(sys.executable)
    if '.app/Contents/MacOS' in APP_DIR:
        APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(APP_DIR)))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BASE_DIR

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from launcher_settings import load_settings, save_settings, get_network_interfaces


def start_flask_server(settings):
    host = settings.get('interface', '127.0.0.1')
    port = int(settings.get('port', 8050))
    from app import app, socketio, log_event
    log_event('launcher_start', {
        'platform': 'macos',
        'host': host,
        'port': port,
        'log_dir': os.environ.get('_NSM_LOG_DIR', 'unknown'),
    })
    print(f'[NovaStar Monitor] Server starting on {host}:{port}')
    socketio.run(app, host=host, port=port, debug=False,
                 allow_unsafe_werkzeug=True)


def get_display_url(settings):
    host = settings.get('interface', '127.0.0.1')
    port = settings.get('port', 8050)
    display_host = host if host != '0.0.0.0' else '127.0.0.1'
    return f'http://{display_host}:{port}'


def main():
    settings = load_settings()

    server_thread = threading.Thread(target=start_flask_server, args=(settings,), daemon=True)
    server_thread.start()
    time.sleep(1.5)

    if settings.get('auto_open_browser', True):
        webbrowser.open(get_display_url(settings))

    # Try rumps (native macOS), fall back to pystray
    try:
        import rumps
        import json
        import urllib.request

        class NovaStarMonitorApp(rumps.App):
            def __init__(self):
                super().__init__('NovaStar Monitor', title='N\u2605')
                self._sim_item = rumps.MenuItem('Simulation Mode')

            @rumps.clicked('Open Dashboard')
            def open_dashboard(self, _):
                webbrowser.open(get_display_url(settings))

            @rumps.clicked('Simulation Mode')
            def toggle_simulation(self, sender):
                enable = not sender.state
                port = settings.get('port', 8050)
                url = f'http://127.0.0.1:{port}/api/demo'
                data = json.dumps({'enable': enable}).encode()
                req = urllib.request.Request(url, data=data,
                                             headers={'Content-Type': 'application/json'})
                try:
                    resp = urllib.request.urlopen(req, timeout=5)
                    result = json.loads(resp.read())
                    sender.state = result.get('active', False)
                except Exception:
                    pass

            @rumps.clicked('Quit')
            def quit_app(self, _):
                rumps.quit_application()

        app = NovaStarMonitorApp()
        app.run()

    except ImportError:
        # Fall back to pystray
        from launcher_pc import run_tray
        run_tray(settings)


if __name__ == '__main__':
    main()
