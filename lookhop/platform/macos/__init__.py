"""macOS backend (complete). Quartz / AppKit / ApplicationServices via pyobjc.

Permissions the user must grant: Camera, and Accessibility (raising other apps'
windows and the global hotkey). See permissions.py.
"""
from __future__ import annotations

import fcntl
from pathlib import Path

from lookhop.config import APP_NAME, DATA_DIR
from lookhop.platform._common import open_file
from lookhop.platform.macos import permissions
from lookhop.platform.macos.autostart import autostart_enabled, set_autostart
from lookhop.platform.macos.display import (get_cursor, last_input_tick, list_screens,
                                            make_dpi_aware, message_box, mouse_button_down,
                                            set_cursor)
from lookhop.platform.macos.focus import focus_screen
from lookhop.platform.macos.hotkey import GlobalHotkey, parse_hotkey

NAME = "macOS"
SUPPORTED = True


def acquire_single_instance(app_name: str):
    """Returns a handle to keep for the process lifetime, or None if already running."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(Path(DATA_DIR) / f"{app_name.lower()}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    return lock


def check_permissions(need_accessibility: bool = True) -> bool:
    """Ask for Camera (and optionally Accessibility); False if something is missing."""
    return permissions.check_permissions(message_box, need_accessibility)


__all__ = ["NAME", "SUPPORTED", "acquire_single_instance", "autostart_enabled", "check_permissions",
           "focus_screen", "get_cursor", "GlobalHotkey", "last_input_tick", "list_screens",
           "make_dpi_aware", "message_box", "mouse_button_down", "open_file", "parse_hotkey",
           "set_autostart", "set_cursor"]
