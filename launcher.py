"""Ledger desktop launcher.

Starts the web server quietly in the background, opens it in Brave as a proper app
window (no tabs, no address bar), and leaves a small icon by the clock so there is a
way to reopen or quit it.

Everything here is local. Nothing contacts the internet.
"""
import os
import sys
import time
import socket
import threading
import subprocess

import paths


def log(msg):
    """Write to ledger-log.txt beside the exe. Without this a windowed app fails
    silently and there is no way to tell why."""
    try:
        paths.ensure_dirs()
        with open(os.path.join(paths.DATA_DIR, "ledger-log.txt"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass

# Listen on every network interface so a phone on the same wifi can reach Ledger.
# The window on this PC still opens on 127.0.0.1.
BIND_HOST = "0.0.0.0"
HOST = "127.0.0.1"
PORT = 5000
URL = f"http://{HOST}:{PORT}"

# Where Brave usually lives on Windows. Checked in order.
BRAVE_PATHS = [
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
]


def find_brave():
    for p in BRAVE_PATHS:
        if p and os.path.exists(p):
            return p
    # Fall back to the registry, in case Brave was installed somewhere unusual.
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                k = winreg.OpenKey(
                    root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe")
                val = winreg.QueryValue(k, None)
                if val and os.path.exists(val):
                    return val
            except OSError:
                continue
    except ImportError:
        pass
    return None


def port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, PORT)) == 0


def wait_until_up(timeout=25):
    start = time.time()
    while time.time() - start < timeout:
        if port_in_use():
            return True
        time.sleep(0.25)
    return False


def open_window():
    """Open Ledger in Brave as an app window, and return the process so we can tell
    when the user closes it.

    The dedicated --user-data-dir matters: without it Brave hands the window to an
    already-running Brave and our process exits immediately, so we would have no way
    to know when the window was closed. Its own profile keeps it a real, separate
    process - and keeps Ledger isolated from normal browsing.
    """
    brave = find_brave()
    profile = os.path.join(paths.DATA_DIR, "browser-profile")
    if brave:
        try:
            proc = subprocess.Popen(
                [brave, f"--app={URL}", f"--user-data-dir={profile}",
                 "--no-first-run", "--no-default-browser-check"],
                close_fds=True)
            log(f"opened Brave (pid {proc.pid})")
            return proc
        except OSError as e:
            log(f"Brave failed to launch: {e}")
    log("Brave not found - using default browser")
    import webbrowser
    webbrowser.open(URL)
    return None


def serve():
    """Run the app on a real production server, quietly."""
    import app as ledger
    ledger.dbm.init_db()

    # Record which database is actually open. If Ledger keeps asking to set up a new
    # company, this line says why - it will be pointing at an empty or different file.
    try:
        import sqlite3
        info = ledger.db_info()
        n = sqlite3.connect(paths.DB_PATH).execute("SELECT COUNT(*) FROM users").fetchone()[0]
        log(f"database: {info['path']}")
        log(f"  exists={info['exists']} size={info['size_kb']}KB users={n}")
        if n == 0:
            log("  NO USERS -> setup screen will appear. Wrong or empty database file.")
        log(f"phone access: http://{ledger.local_ip()}:{PORT}")
    except Exception as e:
        log(f"could not inspect database: {e}")

    # A snapshot every time Ledger starts, keeping the last 20. Cheap insurance.
    try:
        b = ledger.make_backup("startup")
        log(f"auto-backup: {os.path.basename(b)}")
    except Exception as e:
        log(f"auto-backup failed: {e}")
    try:
        from waitress import serve as waitress_serve
        waitress_serve(ledger.app, host=BIND_HOST, port=PORT, threads=8, _quiet=True)
    except ImportError:
        # Waitress missing - fall back so the app still runs.
        ledger.app.run(host=BIND_HOST, port=PORT, debug=False, use_reloader=False)


def run_tray():
    """Small icon by the clock: Open Ledger / Quit. Without it there'd be no way to stop
    a server that has no window."""
    try:
        import pystray
        from PIL import Image
    except ImportError as e:
        log(f"tray unavailable: {e}")
        return False

    icon_file = os.path.join(paths.BUNDLE_DIR, "static", "icon.png")
    if os.path.exists(icon_file):
        image = Image.open(icon_file)
    else:
        image = Image.new("RGB", (64, 64), (47, 92, 63))

    def on_open(icon, item):
        open_window()

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open Ledger", on_open, default=True),
        pystray.MenuItem("Quit Ledger", on_quit),
    )
    log("tray icon starting")
    try:
        pystray.Icon("Ledger", image, "Ledger ERP", menu).run()
    except Exception as e:
        log(f"tray failed to start: {e}")
        return False
    return True


def main():
    log("--- Ledger starting ---")

    # Already running? Just bring the window back instead of starting a second copy
    # and colliding on the port.
    if port_in_use():
        log("already running - reopening window only")
        open_window()
        return

    threading.Thread(target=serve, daemon=True).start()

    if not wait_until_up():
        log("server did not start")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, "Ledger could not start its server on port 5000.\n\n"
                   "Something else may be using that port. Restart the PC and try again.",
                "Ledger", 0x10)
        except Exception:
            print("Server failed to start on port 5000.")
        return

    proc = open_window()

    # Closing the Ledger window quits the whole app - the natural thing to expect,
    # and it means there is always a way out even if the tray icon is missing.
    if proc is not None:
        def watch():
            proc.wait()
            log("window closed - shutting down")
            time.sleep(0.4)
            os._exit(0)
        threading.Thread(target=watch, daemon=True).start()

    # Tray icon is a bonus on top. If it is unavailable, idle instead - the window
    # watcher above still handles quitting.
    if not run_tray():
        log("running without tray - waiting for window to close")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
