"""System tray icon + app controller (pause, glasses, hotkey, calibration, panel)."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem as Item

from lookhop.cameras import requested_missing
from lookhop.config import (APP_NAME, CONFIG_PATH, LOG_PATH, SOURCE_ROOT, Config, load_config,
                            self_command)
from lookhop.engine import LAPTOP, MONITOR, Engine
from lookhop.platform import GlobalHotkey, open_file, parse_hotkey

log = logging.getLogger(__name__)

COLORS = {
    LAPTOP: (77, 163, 255),
    MONITOR: (76, 209, 122),
    "paused": (140, 140, 150),
    "warn": (255, 181, 71),
    "error": (235, 87, 87),
}


def make_icon(color: tuple[int, int, int], look: str | None) -> Image.Image:
    """An eye whose pupil points at the screen you're looking at (laptop = left)."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((3, 14, 61, 50), fill=(250, 250, 250, 255), outline=color + (255,), width=5)
    cx = {LAPTOP: 22, MONITOR: 42}.get(look, 32)
    d.ellipse((cx - 10, 22, cx + 10, 42), fill=color + (255,))
    d.ellipse((cx - 4, 28, cx + 4, 36), fill=(20, 20, 25, 255))
    return img


def hotkey_label(spec: str) -> str:
    """Human-readable hotkey; on macOS Alt is the Option key and Win is Command."""
    parts = [p.strip().title() for p in spec.split("+")]
    if sys.platform == "darwin":
        names = {"Alt": "Option", "Win": "Cmd", "Ctrl": "Ctrl"}
        parts = [names.get(p, p) for p in parts]
    return "+".join(parts)


class TrayApp:
    """Tray / menu-bar icon plus the app controller (pause, glasses, hotkey, calibration, panel)."""
    def __init__(self, cfg: Config, engine: Engine):
        """Start the hotkey, create the control panel and the tray icon."""
        from lookhop.ui import ControlPanel

        self.cfg = cfg
        self.engine = engine
        self.hotkey: GlobalHotkey | None = None
        self.hotkey_label: str | None = None
        self._hotkey_lock = threading.Lock()
        self._calibrating = False
        self._icons: dict = {}
        self._last_key = None

        ok, msg = self._start_hotkey(cfg.hotkey)
        if not ok:
            log.error("Hotkey %s: %s", cfg.hotkey, msg)

        self.panel = ControlPanel(cfg, engine, self)
        if sys.platform == "darwin":
            self.panel.prepare_main_thread()  # Tk first: it owns the NSApplication subclass
            # Menu-bar app: no Dock icon (the packaged .app also sets LSUIElement).
            from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory)
        self.icon = pystray.Icon(APP_NAME, self._icon_image(), APP_NAME, menu=self._menu())
        engine.on_change = self.refresh
        engine.on_camera_change = self.camera_changed

    # ---------- appearance ----------
    def _icon_key(self):
        """Cache key describing which icon the current engine state needs."""
        e = self.engine
        if e.paused:
            return "paused", None
        if e.status in ("no_model", "error"):
            return "error", None
        if e.status != "running" or not e.face:
            return "warn", e.state if e.status == "running" else None
        return e.state, e.state

    def _icon_image(self):
        """The icon image for the current state (cached)."""
        key = self._icon_key()
        if key not in self._icons:
            self._icons[key] = make_icon(COLORS[key[0]], key[1])
        return self._icons[key]

    def icon_image(self) -> Image.Image:
        """The app icon shown in the control panel's title bar."""
        return make_icon(COLORS[MONITOR], MONITOR)

    def status_text(self) -> str:
        """One-line status for the menu and tooltip."""
        e = self.engine
        if self._calibrating:
            return "Calibrating…"
        if e.paused:
            return "Paused"
        return {
            "no_model": f"Not calibrated ({e.camera_label})" if e.camera_label else "Not calibrated",
            "no_camera": "Camera unavailable (in use?)",
            "error": "Face tracker error - see log",
            "starting": "Starting…",
        }.get(e.status, f"Looking at {e.state.capitalize()}" + ("" if e.face else " (no face)"))

    def refresh(self) -> None:
        """Update the icon, tooltip and menu if anything changed (any thread)."""
        key = (self._icon_key(), self.status_text())
        if key == self._last_key:
            return
        self._last_key = key
        if sys.platform == "darwin":  # AppKit objects may only be touched on the main thread
            self.panel.call_soon(self._apply_refresh)
        else:
            self._apply_refresh()

    def _apply_refresh(self) -> None:
        """Do the actual icon/menu update (on the main thread on macOS)."""
        try:
            self.icon.icon = self._icon_image()
            self.icon.title = f"{APP_NAME} - {self.status_text()}"
            self.icon.update_menu()
        except Exception:
            log.debug("Tray refresh failed", exc_info=True)

    def notify(self, message: str) -> None:
        """Show a desktop notification if enabled."""
        if not self.cfg.show_notifications:
            return
        if sys.platform == "darwin":  # LookHop's own notifications, not Script Editor's
            from lookhop.platform.macos.notify import send
            if send(message, APP_NAME):
                return
        try:
            self.icon.notify(message, APP_NAME)
        except Exception:
            log.debug("Notification failed", exc_info=True)

    # ---------- menu ----------
    def _menu(self) -> Menu:
        """Build the tray menu."""
        return Menu(
            Item(lambda _i: self.status_text(), None, enabled=False),
            Menu.SEPARATOR,
            Item("Open control panel", self.open_panel, default=True),
            Item(lambda _i: ("Resume" if self.engine.paused else "Pause")
                 + (f"   ({self.hotkey_label})" if self.hotkey_label else ""),
                 self.toggle_pause),
            Item("Recalibrate…", self.recalibrate, enabled=lambda _i: not self._calibrating),
            Menu.SEPARATOR,
            Item("Quit", self._quit),
        )

    # ---------- controller API (also used by the control panel) ----------
    def open_panel(self):
        """Show the control panel."""
        self.panel.show()

    def toggle_pause(self):
        """Pause or resume tracking."""
        self.engine.toggle_pause()
        self.refresh()

    def on_hotkey(self):
        """Hotkey handler: toggle pause and say so."""
        self.toggle_pause()
        self.notify("Paused" if self.engine.paused else "Running")

    def set_glasses(self, on: bool):
        """Switch glasses mode and load (or start calibrating) that profile."""
        if on == self.cfg.glasses:
            return
        self.cfg.glasses = on
        self.cfg.save()
        self.engine.reload_model()
        log.info("Glasses -> %s", on)
        if self.engine.model is None:
            self.notify("No calibration for this glasses setting yet - starting calibration.")
            self.recalibrate()
        else:
            self.notify("Glasses " + ("on" if on else "off") + " - using that calibration.")
        self.refresh()

    def camera_changed(self, name: str, calibrated: bool, missing: str) -> None:
        """The camera in use changed: tell the user, and point to Recalibrate if it has no profile."""
        lead = f"{missing} isn't connected. " if missing else ""
        if calibrated:
            self.notify(f"{lead}Using {name}.")
        else:
            self.notify(f"{lead}{name} hasn't been calibrated yet. "
                        "Choose Recalibrate… in the menu to calibrate it.")
        self.refresh()

    def set_camera(self, name: str) -> None:
        """Switch to the camera called `name` ('' = built-in): use its calibration, or calibrate."""
        self.cfg.save()
        self.engine.reload_model()
        self.engine.restart_camera()
        if self.engine.model is None:
            self.notify(f"{self.engine.camera_label} hasn't been calibrated yet - starting calibration.")
            self.recalibrate()
        else:
            self.notify(f"Using {self.engine.camera_label} with its saved calibration.")
        self.refresh()

    def _start_hotkey(self, spec: str) -> tuple[bool, str]:
        """(Re)register the global hotkey; returns (ok, message)."""
        with self._hotkey_lock:
            if self.hotkey is not None:
                self.hotkey.stop()
                self.hotkey.join(timeout=1)
            self.hotkey, self.hotkey_label = None, None
            try:
                hk = GlobalHotkey(spec, self.on_hotkey)
            except ValueError as e:
                return False, str(e)
            hk.start()
            hk.ready.wait(2)
            if not hk.ok:
                return False, ("Already used by another app, or LookHop lacks the Accessibility "
                               "permission" if sys.platform == "darwin" else
                               "Already used by another app")
            self.hotkey, self.hotkey_label = hk, hotkey_label(spec)
            return True, ""

    def set_hotkey(self, spec: str) -> tuple[bool, str]:
        """Validate and apply a new hotkey, keeping the old one if it fails."""
        try:
            parse_hotkey(spec)
        except ValueError as e:
            return False, str(e)
        ok, msg = self._start_hotkey(spec)
        if ok:
            self.cfg.hotkey = spec
            self.cfg.save()
        else:
            self._start_hotkey(self.cfg.hotkey)  # put the old one back
        try:
            self.icon.update_menu()
        except Exception:
            pass
        return ok, msg

    def open_config(self):
        """Open config.json in the default app."""
        open_file(CONFIG_PATH)

    def open_log(self):
        """Open the log file in the default app."""
        open_file(LOG_PATH)

    def recalibrate(self):
        """Run calibration in a separate process."""
        if self._calibrating:
            return
        self._calibrating = True
        self.refresh()
        threading.Thread(target=self._run_calibration, daemon=True).start()

    def _run_calibration(self):
        """Release the camera, run the calibration process, then reload the model."""
        try:
            self.cfg.save()  # the calibration process reads config.json
            if not self.engine.release_camera():
                log.warning("Camera did not close in time; calibration may fail to open it")
            # Separate process: Tk wants its own main thread (the tray or the panel owns ours).
            rc = subprocess.run(self_command("calibrate"), cwd=str(SOURCE_ROOT)).returncode
            chosen = load_config().camera_name  # the calibration process may have asked which camera
            if chosen != self.cfg.camera_name:
                self.cfg.camera_name = chosen
            self.engine.reload_model()
            self.notify("Calibration saved." if rc == 0 else "Calibration cancelled.")
        except Exception:
            log.exception("Calibration failed")
        finally:
            self.engine.resume_camera()
            self._calibrating = False
            self.refresh()

    def _quit(self):
        """Quit the app: stop the hotkey, the panel and the tray."""
        if self.hotkey is not None:
            self.hotkey.stop()
        if sys.platform == "darwin":
            self.icon.visible = False  # remove the menu-bar item; Tk's loop is what ends
            self.panel.shutdown()
            return
        self.panel.shutdown()
        self.icon.stop()

    def run(self) -> None:
        """Run the tray (blocks until Quit); on macOS Tk's loop drives it."""
        def setup(icon):
            icon.visible = True
            self.refresh()
            self.notify("Running. Click the tray icon for the control panel.")
            missing = requested_missing(self.cfg)
            if missing:  # the chosen camera isn't plugged in: a stand-in is being used
                self.camera_changed(self.engine.camera_label, self.engine.model is not None, missing)
        if sys.platform == "darwin":
            # Cocoa wants the main thread for both the menu-bar item and Tk, so Tk runs
            # the event loop here and pystray rides on it (run_detached).
            self.icon.run_detached(setup=lambda icon: self.panel.call_soon(lambda: setup(icon)))
            self.panel.run_mainloop()
            return
        self.icon.run(setup=setup)
