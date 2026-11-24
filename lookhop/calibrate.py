"""Fullscreen calibration: dots alternate between laptop and monitor while the
camera records head pose + iris features, then a classifier is trained and saved.

Keys: SPACE / click = start, ESC / right-click = cancel,
      after results: ENTER = keep, R = redo.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import tkinter as tk

import numpy as np

from lookhop.config import APP_NAME, Config
from lookhop.model import LAPTOP, MONITOR, GazeModel, cross_validate, make_meta
from lookhop.platform import list_screens, message_box, resolve_roles, ui_font
from lookhop.tracker import Camera, FaceFeatures, camera_index

log = logging.getLogger(__name__)

# Dot positions as fractions of the screen (centre + four inset corners).
POINTS = [(0.5, 0.5), (0.15, 0.15), (0.85, 0.15), (0.85, 0.85), (0.15, 0.85)]
SETTLE_MS = 900    # dot shrinks: time to move eyes/head
RECORD_MS = 1300   # dot solid: samples are recorded
MIN_SAMPLES = 25   # per class
GOOD_ACCURACY = 0.85

BG = "#101114"
FG = "#d8dae0"
DIM = "#6b6f7a"
ACCENT = "#4da3ff"
WARN = "#ffb547"
OK = "#4cd17a"
FONT = ui_font(0)[0]


def build_sequence(rounds: int) -> list[tuple[str, tuple[float, float]]]:
    """Alternate laptop <-> monitor so the model learns the real head turn."""
    seq = []
    order = list(range(len(POINTS)))
    for r in range(rounds):
        for i in (order if r % 2 == 0 else order[::-1]):
            seq.append(("laptop", POINTS[i]))
            seq.append(("monitor", POINTS[(i + 2) % len(POINTS)]))
    return seq


class _Sampler(threading.Thread):
    """Reads the camera as fast as it can; while recording, stores labelled features."""

    def __init__(self, cfg: Config):
        """Start with no samples; the thread is started by the calibrator."""
        super().__init__(daemon=True, name="calib-sampler")
        self.cfg = cfg
        self.error: str | None = None
        self.ready = threading.Event()
        self.face_seen_at = 0.0
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._label: tuple[int, int] | None = None
        self.samples: list[tuple[np.ndarray, int, int]] = []

    def record(self, label: int | None, group: int = -1) -> None:
        """Label incoming frames as `label` (laptop/monitor) for dot `group`; None stops recording."""
        with self._lock:
            self._label = None if label is None else (label, group)

    def clear(self) -> None:
        """Drop all samples collected so far."""
        with self._lock:
            self.samples = []

    def stop(self) -> None:
        """Ask the camera thread to finish."""
        self._halt.set()

    def run(self) -> None:
        """Camera loop: read frames, extract face features, store labelled samples."""
        cam = Camera(camera_index(self.cfg), self.cfg.camera_width, self.cfg.camera_height)
        try:
            features = FaceFeatures()
        except Exception as e:
            log.exception("Face tracker failed to start")
            self.error = f"Face tracker failed to start:\n{e}"
            self.ready.set()
            return
        if not cam.open():
            self.error = ("Could not open the camera.\n"
                          "Close apps that may be using it (Teams, Zoom, Camera) and try again.")
            self.ready.set()
            features.close()
            return
        self.ready.set()
        failures = 0
        try:
            while not self._halt.is_set():
                frame = cam.read()
                if frame is None:
                    failures += 1
                    if failures > 50:
                        self.error = "The camera stopped sending frames."
                        return
                    time.sleep(0.05)
                    continue
                failures = 0
                f = features.process(frame)
                if f is None:
                    continue
                self.face_seen_at = time.monotonic()
                with self._lock:
                    if self._label is not None:
                        self.samples.append((f, *self._label))
        finally:
            cam.close()
            features.close()


class _Calibrator:
    """Full-screen calibration: shows dots on both displays and trains the gaze model."""
    def __init__(self, cfg: Config):
        """Create one borderless window per display and start the camera sampler."""
        self.cfg = cfg
        laptop, monitor = resolve_roles(list_screens(), cfg.laptop_display)
        if monitor is None:
            raise RuntimeError("Only one display is connected. Connect the monitor and try again.")
        self.screens = {"laptop": laptop, "monitor": monitor}
        self.sequence = build_sequence(cfg.calibration_rounds)
        self.saved = False
        self.phase = "intro"  # intro | running | result | error
        self.idx = 0
        self.accuracy: dict | None = None
        self._pending_model: GazeModel | None = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.canvas: dict[str, tk.Canvas] = {}
        self.windows: dict[str, tk.Toplevel] = {}
        for role, s in self.screens.items():
            win = tk.Toplevel(self.root)
            if sys.platform == "darwin":
                # overrideredirect windows can't take keyboard focus on macOS; a borderless
                # "plain" window can.
                try:
                    win.tk.call("::tk::unsupported::MacWindowStyle", "style", win._w, "plain", "none")
                except tk.TclError:
                    win.overrideredirect(True)
            else:
                win.overrideredirect(True)
            win.geometry(f"{s.width}x{s.height}+{s.left}+{s.top}")
            win.configure(bg=BG)
            win.attributes("-topmost", True)
            if sys.platform != "darwin":  # on macOS the cursor stays visible until the dots start
                try:
                    win.configure(cursor="none")
                except tk.TclError:
                    pass
            c = tk.Canvas(win, bg=BG, highlightthickness=0, width=s.width, height=s.height)
            c.pack(fill="both", expand=True)
            win.bind("<Key>", self._on_key)
            win.bind("<Button-1>", lambda _e: self._on_action("start"))
            win.bind("<Button-3>", lambda _e: self._on_action("cancel"))
            self.windows[role] = win
            self.canvas[role] = c

        self.sampler = _Sampler(cfg)
        self.sampler.start()

    # ---------- drawing ----------
    def _size(self, role):
        """Pixel size of the display used for `role` (laptop/monitor)."""
        s = self.screens[role]
        return s.width, s.height

    def _clear(self):
        """Erase everything drawn on both displays."""
        for c in self.canvas.values():
            c.delete("all")

    def _text(self, role, y_frac, text, size=20, color=FG, tag=None):
        """Draw centered text on `role`'s display at a vertical fraction of its height."""
        w, h = self._size(role)
        self.canvas[role].create_text(w / 2, h * y_frac, text=text, fill=color, tags=tag,
                                      font=(FONT, size), justify="center", width=w * 0.8)

    # ---------- phases ----------
    def run(self) -> bool:
        """Show the intro screen and run Tk until calibration ends; True if a profile was saved."""
        if sys.platform == "darwin":  # menu-bar apps aren't frontmost by default
            try:
                from AppKit import NSApplication
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            except Exception:  # noqa: BLE001
                pass
        self.windows["laptop"].after(100, lambda: self.windows["laptop"].focus_force())
        self._show_intro()
        self.root.mainloop()
        self.sampler.stop()
        self.sampler.join(timeout=3)
        return self.saved

    def _show_intro(self):
        """Intro screen: instructions, glasses state and camera/face status."""
        self.phase = "intro"
        self._clear()
        glasses = "ON" if self.cfg.glasses else "OFF"
        body = ("Sit the way you normally do.\n\n"
                "A dot will jump between this laptop and your monitor.\n"
                "Look at each dot the way you naturally would - turn your head as usual,\n"
                "don't hold it artificially still.\n\n"
                f"Glasses mode: {glasses}   ·   about {len(self.sequence) * (SETTLE_MS + RECORD_MS) // 1000} seconds\n\n"
                "Press SPACE (or click) to start  ·  G to toggle glasses mode  ·  ESC to cancel")
        self._text("laptop", 0.30, f"{APP_NAME} calibration", size=32)
        self._text("laptop", 0.55, body, size=16)
        self._text("monitor", 0.5, "Calibration starting on the laptop screen…", size=22, color=DIM)
        self._update_face_status()

    def _update_face_status(self):
        """Refresh the 'face detected' line on the intro screen (polls every 200 ms)."""
        if self.phase != "intro":
            return
        c = self.canvas["laptop"]
        c.delete("face")
        if self.sampler.error:
            self._show_error(self.sampler.error)
            return
        if not self.sampler.ready.is_set():
            msg, color = "Starting camera…", DIM
        elif time.monotonic() - self.sampler.face_seen_at < 0.5:
            msg, color = "Face detected ✓", OK
        else:
            msg, color = "Looking for your face…", WARN
        self._text("laptop", 0.85, msg, size=18, color=color, tag="face")
        self.root.after(200, self._update_face_status)

    def _start(self):
        """Begin the dot sequence once the camera is ready."""
        if not self.sampler.ready.is_set() or self.sampler.error:
            return
        self.phase = "running"
        if sys.platform == "darwin":
            for win in self.windows.values():
                win.configure(cursor="none")
        self.sampler.clear()
        self.idx = 0
        self._next_point()

    def _next_point(self):
        """Show the next dot, wait for the gaze to settle, then record it."""
        if self.phase != "running":
            return
        if self.idx >= len(self.sequence):
            self._finish()
            return
        if self.sampler.error:
            self._show_error(self.sampler.error)
            return
        role, (fx, fy) = self.sequence[self.idx]
        self._clear()
        for r in self.canvas:
            w, h = self._size(r)
            self.canvas[r].create_text(w - 20, h - 20, anchor="se", fill=DIM, font=(FONT, 12),
                                       text=f"{self.idx + 1}/{len(self.sequence)}   ESC to cancel")
        w, h = self._size(role)
        x, y = fx * w, fy * h
        start = time.monotonic()
        dot = self.canvas[role].create_oval(0, 0, 0, 0, fill=ACCENT, outline="")
        idx = self.idx

        def settle():
            if self.phase != "running" or self.idx != idx:
                return
            t = (time.monotonic() - start) * 1000
            if t < SETTLE_MS:
                r = 10 + 40 * (1 - t / SETTLE_MS)
                self.canvas[role].coords(dot, x - r, y - r, x + r, y + r)
                self.root.after(16, settle)
                return
            self.canvas[role].coords(dot, x - 10, y - 10, x + 10, y + 10)
            self.canvas[role].create_oval(x - 3, y - 3, x + 3, y + 3, fill="white", outline="")
            self.sampler.record(LAPTOP if role == "laptop" else MONITOR, idx)
            self.root.after(RECORD_MS, done)

        def done():
            self.sampler.record(None)
            if self.phase != "running" or self.idx != idx:
                return
            self.idx += 1
            self._next_point()

        settle()

    def _finish(self):
        """Train the model from the recorded samples and show the accuracy result."""
        self.phase = "result"
        self.sampler.record(None)
        samples = list(self.sampler.samples)
        X = np.array([s[0] for s in samples]) if samples else np.zeros((0, 4))
        y = np.array([s[1] for s in samples], dtype=int)
        groups = np.array([s[2] for s in samples], dtype=int)
        n_lap, n_mon = int((y == LAPTOP).sum()), int((y == MONITOR).sum())
        self._clear()
        if n_lap < MIN_SAMPLES or n_mon < MIN_SAMPLES:
            self._text("laptop", 0.4, "Not enough face data", size=28, color=WARN)
            self._text("laptop", 0.55,
                       f"Got {n_lap} laptop and {n_mon} monitor samples (need {MIN_SAMPLES} each).\n"
                       "The camera lost your face too often - check lighting and that you're in frame.\n\n"
                       "R = try again   ·   ESC = cancel", size=16)
            return

        w_iris = self.cfg.effective_iris_weight
        acc = cross_validate(X, y, groups, w_iris)
        self.accuracy = acc
        meta = make_meta(self.cfg.glasses, n_lap, n_mon, acc)
        self._pending_model = GazeModel.train(X, y, w_iris, meta)
        log.info("Calibration accuracy: %s", acc)

        comb = acc["combined"]
        worst = min(comb["laptop"], comb["monitor"])
        good = worst >= GOOD_ACCURACY
        pct = lambda v: "n/a" if v != v else f"{v * 100:.0f}%"
        lines = (f"Laptop {pct(comb['laptop'])}   ·   Monitor {pct(comb['monitor'])}\n\n"
                 f"head only: {pct(acc['head']['laptop'])} / {pct(acc['head']['monitor'])}     "
                 f"iris only: {pct(acc['iris']['laptop'])} / {pct(acc['iris']['monitor'])}")
        self._text("laptop", 0.30, "Calibration done" if good else "Accuracy is low",
                   size=30, color=OK if good else WARN)
        self._text("laptop", 0.48, lines, size=18)
        hint = ("ENTER = save and start   ·   R = redo   ·   ESC = cancel" if good else
                "Try sitting how you normally work and turning your head naturally.\n\n"
                "R = redo   ·   ENTER = save anyway   ·   ESC = cancel")
        self._text("laptop", 0.72, hint, size=16, color=FG if good else WARN)
        self._text("monitor", 0.5, "Results are on the laptop screen", size=22, color=DIM)

    def _save(self):
        """Write the trained model to the profile file and close."""
        if self._pending_model is None:
            return
        self._pending_model.save(self.cfg.profile_path)
        log.info("Saved calibration to %s", self.cfg.profile_path)
        self.saved = True
        self.root.quit()

    def _show_error(self, msg: str):
        """Show a problem message (e.g. no camera); any key closes."""
        self.phase = "error"
        self._clear()
        self._text("laptop", 0.4, "Calibration can't run", size=28, color=WARN)
        self._text("laptop", 0.55, msg + "\n\nPress any key to close.", size=16)

    # ---------- input ----------
    def _on_key(self, event):
        """Keyboard shortcuts: Esc cancel, Space start, Enter save, R redo, G glasses mode."""
        key = event.keysym.lower()
        if key == "escape":
            self._on_action("cancel")
        elif key == "space":
            self._on_action("start")
        elif key in ("return", "kp_enter"):
            self._on_action("save")
        elif key == "r":
            self._on_action("redo")
        elif key == "g" and self.phase == "intro":  # glasses mode decides which profile is saved
            self.cfg.glasses = not self.cfg.glasses
            self.cfg.save()
            self._show_intro()
        elif self.phase == "error":
            self._on_action("cancel")

    def _on_action(self, action: str):
        """Apply a start / save / redo / cancel request if it is valid in the current phase."""
        if action == "cancel" or self.phase == "error":
            self.phase = "cancelled"
            self.root.quit()
        elif action == "start" and self.phase == "intro":
            self._start()
        elif action == "save" and self.phase == "result":
            self._save()
        elif action == "redo" and self.phase == "result":
            self._show_intro()


def run_calibration(cfg: Config) -> bool:
    """Returns True when a new calibration profile was saved."""
    from lookhop.camera_picker import choose_camera
    if not choose_camera(cfg):  # several cameras and the user cancelled the picker
        return False
    try:
        cal = _Calibrator(cfg)
    except Exception as e:
        log.exception("Calibration could not start")
        message_box(str(e), f"{APP_NAME} calibration", error=True)
        return False
    try:
        return cal.run()
    finally:
        try:
            cal.root.destroy()
        except tk.TclError:
            pass
