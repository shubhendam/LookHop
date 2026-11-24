"""macOS: display layout, cursor, input state (Quartz).

Quartz global coordinates are points with the origin at the top-left of the
main display - the same space for display bounds and the cursor, which is what
the rest of LookHop needs. (NSScreen uses a bottom-left origin; we only use it
to look up friendly display names.)
"""
from __future__ import annotations

import threading
import time

import Quartz

from lookhop.platform.base import Screen
from lookhop.platform._common import tk_message_box

_SRC = Quartz.kCGEventSourceStateCombinedSessionState
_ANY_INPUT = Quartz.kCGAnyInputEventType
_BUTTONS = (Quartz.kCGMouseButtonLeft, Quartz.kCGMouseButtonRight, Quartz.kCGMouseButtonCenter)


def make_dpi_aware() -> None:
    """Nothing to do: Quartz and Tk both work in points."""


def _display_names() -> dict[int, str]:
    """Map CGDirectDisplayID -> friendly name (NSScreen.localizedName)."""
    names: dict[int, str] = {}
    try:
        from AppKit import NSScreen
        for s in NSScreen.screens():
            num = int(s.deviceDescription()["NSScreenNumber"])
            names[num] = str(s.localizedName())
    except Exception:  # noqa: BLE001 - names are cosmetic; fall back to ids
        pass
    return names


def list_screens() -> list[Screen]:
    """All active displays in top-left-origin global coordinates, same space as the cursor."""
    err, ids, count = Quartz.CGGetActiveDisplayList(32, None, None)
    if err != 0 or not count:
        return []
    names = _display_names()
    main = Quartz.CGMainDisplayID()
    seen: dict[str, int] = {}
    screens: list[Screen] = []
    for did in ids[:count]:
        r = Quartz.CGDisplayBounds(did)
        base = names.get(int(did), f"Display {did}")
        seen[base] = seen.get(base, 0) + 1
        name = base if seen[base] == 1 else f"{base} ({seen[base]})"  # two identical monitors
        left, top = int(r.origin.x), int(r.origin.y)
        screens.append(Screen(name, left, top, left + int(r.size.width), top + int(r.size.height),
                              int(did) == int(main), bool(Quartz.CGDisplayIsBuiltin(did))))
    return screens


def get_cursor() -> tuple[int, int] | None:
    """Current cursor position, or None if it can't be read."""
    ev = Quartz.CGEventCreate(None)
    if ev is None:
        return None
    p = Quartz.CGEventGetLocation(ev)
    return int(p.x), int(p.y)


def set_cursor(x: int, y: int) -> None:
    """Move the cursor to (x, y) without generating mouse events."""
    Quartz.CGWarpMouseCursorPosition((float(x), float(y)))
    # A warp briefly freezes the cursor unless mouse and cursor are re-associated.
    Quartz.CGAssociateMouseAndMouseCursorPosition(True)


def mouse_button_down() -> bool:
    """True while any mouse button is held (never jump mid-drag)."""
    return any(Quartz.CGEventSourceButtonState(_SRC, b) for b in _BUTTONS)


_tick_lock = threading.Lock()
_tick = 0
_prev_idle = 0.0
_prev_at = 0.0


def last_input_tick() -> int:
    """A counter that increases whenever any keyboard/mouse input happens.

    macOS only reports "seconds since the last input", so we bump a counter
    whenever that idle time is smaller than it should have grown to by now.
    """
    global _tick, _prev_idle, _prev_at
    with _tick_lock:
        now = time.monotonic()
        idle = Quartz.CGEventSourceSecondsSinceLastEventType(_SRC, _ANY_INPUT)
        if _prev_at and idle < _prev_idle + (now - _prev_at) - 0.002:
            _tick += 1
        _prev_idle, _prev_at = idle, now
        return _tick


def message_box(text: str, title: str, error: bool = False) -> None:
    """Show an info or error dialog (Tk)."""
    tk_message_box(text, title, error)
