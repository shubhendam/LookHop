"""System-wide hotkey via a Quartz event tap (needs Accessibility permission)."""
from __future__ import annotations

import logging
import threading

import Quartz

log = logging.getLogger(__name__)

MODIFIERS = {
    "ctrl": Quartz.kCGEventFlagMaskControl, "control": Quartz.kCGEventFlagMaskControl,
    "alt": Quartz.kCGEventFlagMaskAlternate, "option": Quartz.kCGEventFlagMaskAlternate,
    "shift": Quartz.kCGEventFlagMaskShift,
    "cmd": Quartz.kCGEventFlagMaskCommand, "command": Quartz.kCGEventFlagMaskCommand,
    "win": Quartz.kCGEventFlagMaskCommand,
}
MOD_MASK = (Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskAlternate
            | Quartz.kCGEventFlagMaskShift | Quartz.kCGEventFlagMaskCommand)

# ANSI virtual key codes (kVK_ANSI_*), independent of keyboard layout.
LETTERS = dict(zip("asdfhgzxcv", (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)))
LETTERS.update(zip("bqweryt", (11, 12, 13, 14, 15, 16, 17)))
LETTERS.update(zip("ou", (31, 32)))
LETTERS.update(zip("ipl", (34, 35, 37)))
LETTERS.update(zip("jknm", (38, 40, 45, 46)))
DIGITS = {"1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25, "0": 29}
FKEYS = {1: 122, 2: 120, 3: 99, 4: 118, 5: 96, 6: 97, 7: 98, 8: 100, 9: 101, 10: 109,
         11: 103, 12: 111, 13: 105, 14: 107, 15: 113, 16: 106, 17: 64, 18: 79, 19: 80, 20: 90}
NAMED_KEYS = {"space": 49, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
              "insert": 114}  # Insert = Help key


def parse_hotkey(spec: str) -> tuple[int, int]:
    """-> (modifier flag mask, virtual key code)"""
    mods, code = 0, None
    for part in (p.strip().lower() for p in spec.split("+") if p.strip()):
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        elif len(part) == 1 and part in LETTERS:
            code = LETTERS[part]
        elif part in DIGITS:
            code = DIGITS[part]
        elif part.startswith("f") and part[1:].isdigit() and int(part[1:]) in FKEYS:
            code = FKEYS[int(part[1:])]
        elif part in NAMED_KEYS:
            code = NAMED_KEYS[part]
        else:
            raise ValueError(f"Unknown key {part!r} in hotkey {spec!r}")
    if code is None or mods == 0:
        raise ValueError(f"Hotkey {spec!r} needs at least one modifier and one key")
    return mods, code


class GlobalHotkey(threading.Thread):
    """Global hotkey thread built on a Quartz event tap (needs Accessibility permission)."""
    def __init__(self, spec: str, callback):
        """Parse `spec` (e.g. 'ctrl+alt+g'); raises ValueError for a bad spec."""
        super().__init__(daemon=True, name="hotkey")
        self.spec = spec
        self.mods, self.code = parse_hotkey(spec)
        self.callback = callback
        self.ok = False
        self.ready = threading.Event()
        self._loop = None
        self._tap = None

    def _on_event(self, _proxy, etype, event, _refcon):
        """Event-tap callback: swallow and handle the hotkey, pass everything else through."""
        if etype in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            Quartz.CGEventTapEnable(self._tap, True)  # macOS switches slow taps off
            return event
        if etype != Quartz.kCGEventKeyDown:
            return event
        if (Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode) == self.code
                and Quartz.CGEventGetFlags(event) & MOD_MASK == self.mods):
            if not Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
                try:
                    self.callback()
                except Exception:
                    log.exception("Hotkey callback failed")
            return None  # swallow, like RegisterHotKey does
        return event

    def run(self) -> None:
        """Create the tap and run its run loop; `ok` stays False if the tap can't be created."""
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault, Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown),
            self._on_event, None)
        if self._tap is None:
            log.error("Could not register hotkey %s - grant Accessibility (and Input Monitoring) "
                      "to LookHop in System Settings > Privacy & Security", self.spec)
            self.ready.set()
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        self.ok = True
        self.ready.set()
        try:
            Quartz.CFRunLoopRun()
        finally:
            Quartz.CGEventTapEnable(self._tap, False)
            Quartz.CFRunLoopRemoveSource(self._loop, source, Quartz.kCFRunLoopCommonModes)

    def stop(self) -> None:
        """Stop the run loop so the thread exits."""
        if self._loop is not None:
            Quartz.CFRunLoopStop(self._loop)
