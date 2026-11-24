"""Bring the top app window on a screen to the front - Cmd+Tab, but per screen.

"Top window on that screen" = the first normal window, front-to-back, whose
largest part is on that screen. That's the app you were last using there.
Needs the Accessibility permission to raise windows of other apps.
"""
from __future__ import annotations

import logging
import os

import Quartz
from AppKit import NSRunningApplication, NSWorkspace
from ApplicationServices import (AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
                                 AXUIElementPerformAction, AXUIElementSetAttributeValue,
                                 AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType)

from lookhop.platform.base import Screen
from lookhop.platform.macos.display import list_screens

log = logging.getLogger(__name__)

# System chrome that is never "the app on that screen".
SKIP_OWNERS = {"Window Server", "Dock", "Control Center", "Notification Center",
               "SystemUIServer", "Spotlight", "loginwindow"}
MIN_SIZE = 80  # ignore tiny helper windows


def _normal_windows() -> list[dict]:
    """On-screen, ordinary app windows, front-most first.

    LookHop's own windows (the control panel) are left out: focusing runs on a background
    thread, and raising a Tk window from there crashes inside AppKit (Tk is main-thread only).
    """
    opts = (Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements)
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        b = w.get(Quartz.kCGWindowBounds)
        if (w.get(Quartz.kCGWindowOwnerPID) == os.getpid()
                or w.get(Quartz.kCGWindowLayer, 1) != 0 or w.get(Quartz.kCGWindowAlpha, 1) <= 0
                or w.get(Quartz.kCGWindowOwnerName) in SKIP_OWNERS or not b
                or b["Width"] < MIN_SIZE or b["Height"] < MIN_SIZE):
            continue
        out.append(w)
    return out


def _overlap(b, s: Screen) -> float:
    """Area of the window rectangle `b` that lies on screen `s`."""
    w = min(b["X"] + b["Width"], s.right) - max(b["X"], s.left)
    h = min(b["Y"] + b["Height"], s.bottom) - max(b["Y"], s.top)
    return max(w, 0) * max(h, 0)


def _on_screen(w: dict, screen: Screen, screens: list[Screen]) -> bool:
    """True when the largest part of the window is on `screen`."""
    b = w[Quartz.kCGWindowBounds]
    best = max(screens, key=lambda s: _overlap(b, s), default=screen)
    return _overlap(b, screen) > 0 and best.name == screen.name


def _ax_point_size(ax_win):
    """Position and size of an Accessibility window element, or (None, None)."""
    def get(attr, kind):
        err, val = AXUIElementCopyAttributeValue(ax_win, attr, None)
        if err or val is None:
            return None
        ok, out = AXValueGetValue(val, kind, None)
        return out if ok else None
    return get("AXPosition", kAXValueCGPointType), get("AXSize", kAXValueCGSizeType)


def _raise_window(pid: int, bounds) -> bool:
    """Make the app frontmost via Accessibility and raise the window matching `bounds`."""
    app = AXUIElementCreateApplication(pid)
    if AXUIElementSetAttributeValue(app, "AXFrontmost", True) != 0:
        return False
    err, windows = AXUIElementCopyAttributeValue(app, "AXWindows", None)
    for win in (windows or []) if not err else []:
        pos, size = _ax_point_size(win)
        if pos is None or size is None:
            continue
        if (abs(pos.x - bounds["X"]) < 3 and abs(pos.y - bounds["Y"]) < 3
                and abs(size.width - bounds["Width"]) < 3
                and abs(size.height - bounds["Height"]) < 3):
            AXUIElementPerformAction(win, "AXRaise")
            break
    return True


def _activate(w: dict) -> bool:
    """Bring the app owning window `w` to the front (Accessibility first, then NSRunningApplication)."""
    pid = int(w[Quartz.kCGWindowOwnerPID])
    try:
        if _raise_window(pid, w[Quartz.kCGWindowBounds]):
            return True
    except Exception:  # noqa: BLE001
        log.debug("Accessibility raise failed", exc_info=True)
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return bool(app and app.activateWithOptions_(1 << 1))  # NSApplicationActivateIgnoringOtherApps


def focus_screen(screen: Screen) -> bool:
    """Activate the top window on `screen`, unless the active window is already there."""
    windows = _normal_windows()
    if not windows:
        return False
    screens = list_screens()
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    front_pid = int(front.processIdentifier()) if front else -1
    for w in windows:
        if _on_screen(w, screen, screens):
            if int(w[Quartz.kCGWindowOwnerPID]) == front_pid:
                return False  # the active app is already the top app here
            ok = _activate(w)
            if not ok:
                log.debug("Could not activate window on %s", screen.name)
            return ok
    return False
