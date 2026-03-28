# -*- mode: python ; coding: utf-8 -*-
# NovaStar Monitor — PyInstaller Build Spec

import platform

block_cipher = None
system = platform.system()

# Choose launcher based on platform
if system == 'Darwin':
    entry_script = 'launcher_mac.py'
else:
    entry_script = 'launcher_pc.py'

a = Analysis(
    [entry_script],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        ('VERSION.txt', '.'),
    ],
    hiddenimports=[
        'flask',
        'flask_socketio',
        'engineio.async_drivers.threading',
        'pystray',
        'PIL',
        'app',
        'device_manager',
        'novastar_protocol',
        'launcher_settings',
        'demo_device',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NovaStar Monitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,  # TODO: Add icon file
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NovaStar Monitor',
)

if system == 'Darwin':
    app = BUNDLE(
        coll,
        name='NovaStar Monitor.app',
        icon=None,  # TODO: Add .icns file
        bundle_identifier='com.novastar.monitor',
        info_plist={
            'CFBundleName': 'NovaStar Monitor',
            'CFBundleDisplayName': 'NovaStar Monitor',
            'CFBundleVersion': '0.3.0',
            'CFBundleShortVersionString': '0.3.0',
            'LSUIElement': True,  # Hide from Dock (menu bar app)
        },
    )
