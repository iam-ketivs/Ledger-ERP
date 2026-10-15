# PyInstaller build recipe for Ledger.
# Build with:  pyinstaller Ledger.spec --noconfirm
#
# Produces a single Ledger.exe with no console window. Templates, static files and
# schema.sql are packed inside. The database is NOT packed - it is created next to
# the .exe on first run so it survives updates and can be backed up.

block_cipher = None

# The tray icon is optional. If pystray/Pillow could not be installed (they need a
# prebuilt package for your Python version), build without them rather than failing -
# Ledger still runs, and "Stop Ledger.bat" closes it.
hidden = ['app', 'db', 'paths', 'exports', 'waitress', 'openpyxl']
try:
    import pystray  # noqa: F401
    import PIL      # noqa: F401
    hidden += ['pystray._win32', 'PIL.Image']
    print('>>> Building WITH tray icon support')
except ImportError:
    print('>>> Building WITHOUT tray icon (pystray/Pillow not installed)')

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        ('schema.sql', '.'),
    ],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy', 'PyQt5', 'PySide2'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Ledger',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no black terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='static/icon.ico',
)
