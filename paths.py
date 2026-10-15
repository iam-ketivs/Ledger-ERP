"""Where things live, whether Ledger is running as plain Python or as a packed .exe.

Two different directories matter and they are NOT the same when frozen:

  BUNDLE_DIR - templates, static files and schema.sql. When packed by PyInstaller these
               are unpacked into a temporary folder (sys._MEIPASS) that is wiped when the
               app closes. Read-only. Never write here.

  DATA_DIR   - instance/erp.db and instance/secret.key. These must survive restarts, so
               they live beside the .exe itself, not inside it. This is the folder the
               user should back up.
"""
import os
import sys

def _writable(path):
    """Can we actually create files here? Program Files says yes to exists() and no to
    everything else, so ask properly rather than assuming."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


if getattr(sys, "frozen", False):
    # Running from a PyInstaller build
    BUNDLE_DIR = sys._MEIPASS
    beside_exe = os.path.dirname(sys.executable)

    # Beside the exe is right for the portable copy-a-folder version, and wrong once
    # installed into Program Files - that folder is read-only for a normal user, and
    # anything written there is wiped by the next install. So: use it when we can write
    # to it, otherwise fall back to the user's own AppData, which survives updates.
    if _writable(os.path.join(beside_exe, "instance")):
        DATA_DIR = beside_exe
    else:
        DATA_DIR = os.path.join(
            os.environ.get("APPDATA") or os.path.expanduser("~"), "Ledger")
else:
    # Running normally from source
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = BUNDLE_DIR

INSTANCE_DIR = os.path.join(DATA_DIR, "instance")
# Photos live on disk beside the database, not inside it - a few hundred images would
# bloat erp.db from a couple of hundred KB into hundreds of MB and slow every backup.
UPLOAD_DIR = os.path.join(INSTANCE_DIR, "uploads")
DB_PATH = os.path.join(INSTANCE_DIR, "erp.db")
SECRET_PATH = os.path.join(INSTANCE_DIR, "secret.key")
SCHEMA_PATH = os.path.join(BUNDLE_DIR, "schema.sql")
TEMPLATE_DIR = os.path.join(BUNDLE_DIR, "templates")
STATIC_DIR = os.path.join(BUNDLE_DIR, "static")


def ensure_dirs():
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
