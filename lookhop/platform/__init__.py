"""Everything OS-specific lives here, one folder per OS:

    platform/windows/   complete
    platform/macos/     complete
    platform/linux/     not implemented yet - help wanted

Each backend exposes the same names (see __all__ below). The rest of LookHop
only imports from `lookhop.platform`, so porting means filling in one folder.

Backend contract
  NAME, SUPPORTED                 display name; False shows "not supported yet"
  make_dpi_aware()                use physical pixels (call before any window exists)
  list_screens() -> [Screen]      all displays, in one virtual-desktop coordinate space
  get_cursor() -> (x, y) | None   None if it can't be read right now (e.g. lock screen)
  set_cursor(x, y)
  mouse_button_down() -> bool     any mouse button held (never jump mid-drag)
  last_input_tick() -> int        changes whenever any keyboard/mouse input happens
  focus_screen(Screen) -> bool    bring the top app on that screen to the front
  GlobalHotkey(spec, callback)    thread: .start() .stop(), .ok after .ready is set
  parse_hotkey(spec)              raises ValueError for a bad spec like "ctrl+alt+g"
  autostart_enabled() / set_autostart(bool)
  acquire_single_instance(name)   handle to keep, or None when already running
  message_box(text, title, error=False)
  open_file(path)
  check_permissions(need_accessibility=True) -> bool
                                  optional: ask for OS privacy permissions (macOS); default True
"""
from __future__ import annotations

import sys

from lookhop.platform.base import Screen, resolve_roles, screen_at, ui_font

if sys.platform == "win32":
    from lookhop.platform.windows import *  # noqa: F401,F403
    from lookhop.platform.windows import __all__ as _backend_all
elif sys.platform == "darwin":
    from lookhop.platform.macos import *  # noqa: F401,F403
    from lookhop.platform.macos import __all__ as _backend_all
else:
    from lookhop.platform.linux import *  # noqa: F401,F403
    from lookhop.platform.linux import __all__ as _backend_all

if "check_permissions" not in globals():  # only macOS needs one
    def check_permissions(need_accessibility: bool = True) -> bool:
        """Default: nothing to ask for (only macOS has privacy permissions)."""
        return True

__all__ = ["Screen", "resolve_roles", "screen_at", "ui_font", "check_permissions",
           *(n for n in _backend_all if n != "check_permissions")]
