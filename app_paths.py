"""Installation paths and writable per-user data for the standalone build."""
import os
from pathlib import Path
import sys


VERSION = "0.9.4"
APP_NAME = "Agora"
DISPLAY_NAME = "Agora \u00b7 \u8bae\u5ead"
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable if FROZEN else __file__).resolve().parent


def data_directory():
    override = os.environ.get("CONDUCTOR_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if FROZEN:
        if (APP_DIR / "conductor.db").is_file():
            return APP_DIR
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return local / "CodexConductor"
    return APP_DIR


def resource_directory():
    return Path(getattr(sys, "_MEIPASS", APP_DIR))


DATA_DIR = data_directory()
DEFAULT_WORKSPACE = DATA_DIR / "workspace" if FROZEN else APP_DIR.parent.parent / "work"
