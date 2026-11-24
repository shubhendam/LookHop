"""Runtime core: camera -> gaze state (laptop/monitor) -> cursor jumps.

Gaze state machine (monitor is "home"):
  - MONITOR -> LAPTOP only when p(laptop) stays >= laptop_enter_prob for laptop_dwell_ms.
  - LAPTOP -> MONITOR when p(laptop) stays <= laptop_exit_prob for monitor_dwell_ms,
    or when no face is seen for no_face_to_monitor_ms (looking away from the
    camera almost always means looking at the monitor).

Each gaze change "arms" at most one jump, so the app never fights you: if you
drag the cursor back by hand, it stays put until your gaze changes again.
  - mouse_move mode: the jump fires when you start moving the mouse
    (after it was still for move_start_gap_ms) and the cursor is on the other screen.
    Quick jump: if the gaze change isn't confirmed yet but the latest frame is
    already very confident, reaching for the mouse confirms it immediately.
  - instant mode: the jump fires as soon as the gaze change is confirmed,
    but waits while you're typing (typing guard) so keystrokes never land in
    the wrong window.
After a jump, the window on top of that screen is brought to the front
(focus_window), so you can type/scroll there without clicking first.
"""
from __future__ import annotations

import logging
import threading
import time

import cv2

from lookhop.cameras import active_camera_name, camera_key, requested_missing
from lookhop.config import Config
from lookhop.model import GazeModel
from lookhop.platform import (focus_screen, get_cursor, last_input_tick, list_screens,
                              mouse_button_down, resolve_roles, screen_at, set_cursor)
from lookhop.tracker import Camera, FaceFeatures, camera_index

log = logging.getLogger(__name__)

LAPTOP = "laptop"
MONITOR = "monitor"
SMOOTHING = 0.65           # weight of the newest frame in p(laptop)
QUICK_NO_FACE_MS = 150     # quick jump: face gone this long counts as "monitor"
FRESH_FRAME_S = 0.3        # quick jump: only trust a frame this recent
PREVIEW_WIDTH = 320


class Engine:
    """Gaze engine: a vision thread (camera -> face -> gaze) plus a mouse thread that jumps the cursor."""
    def __init__(self, cfg: Config):
        """Create the engine and load the calibration model if there is one."""
        self.cfg = cfg
        self.on_change = lambda: None  # set by the tray
        # Called (from the vision thread) when the camera in use changes: (name, calibrated, missing)
        self.on_camera_change = lambda name, calibrated, missing: None
        self.camera_label = ""      # name of the camera the loaded profile belongs to
        self._model_camera_key = None

        self.model: GazeModel | None = None
        self.state = MONITOR
        self.p_laptop = 0.0        # latest raw frame
        self.p_smooth = 0.0        # smoothed, drives the state machine
        self.face = False
        self.status = "starting"   # starting | running | paused | no_model | no_camera | error
        self.paused = False

        # live stats + preview for the control panel
        self.fps_actual = 0.0
        self.proc_ms = 0.0
        self.ui_preview = False
        self.preview_frame = None  # RGB numpy array, mirrored
        self.preview_seq = 0
        self.last_jump: tuple[float, str] | None = None

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._hold = threading.Event()          # someone else (calibration) needs the camera
        self._restart_cam = threading.Event()
        self._camera_free = threading.Event()
        self._camera_free.set()

        self._armed: str | None = None
        self._pending: str | None = None
        self._pending_since = 0.0
        self._no_face_since: float | None = None
        self._p_time = 0.0
        self._last_key_time = 0.0   # last keyboard/click/wheel input (not mouse movement)
        self._jumped_at = 0.0
        self._last_pos: dict[str, tuple[int, int]] = {}
        self._threads: list[threading.Thread] = []

        self.reload_model()

    # ---------- lifecycle ----------
    def start(self) -> None:
        """Start the vision and mouse threads."""
        for target, name in ((self._vision_loop, "vision"), (self._mouse_loop, "mouse")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        """Stop both threads and wait briefly for them."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)

    def reload_model(self) -> None:
        """(Re)load the calibration profile for the current glasses setting and camera."""
        key = camera_key(self.cfg)
        path = self.cfg.profile_path_for(key)
        self.camera_label = active_camera_name(self.cfg)
        model = None
        if path.exists():
            try:
                model = GazeModel.load(path, self.cfg.effective_iris_weight)
                log.info("Loaded calibration %s (iris weight %.2f)", path.name, model.iris_weight)
            except Exception:
                log.exception("Could not load %s", path)
        with self._lock:
            self.model = model
            self._model_camera_key = key
            self._reset_gaze()
        self._set_status(self._idle_status())

    def check_camera_change(self) -> None:
        """If the camera in use changed (plugged / unplugged / re-chosen), switch profile.

        Loads that camera's calibration (or none), restarts the capture and tells the tray
        so the user can be told - and asked to calibrate when the camera has no profile yet.
        """
        if self._hold.is_set() or camera_key(self.cfg) == self._model_camera_key:
            return
        log.info("Camera changed: now %r", active_camera_name(self.cfg))
        self.reload_model()
        self._restart_cam.set()
        try:
            self.on_camera_change(self.camera_label, self.model is not None,
                                  requested_missing(self.cfg))
        except Exception:
            log.exception("on_camera_change failed")

    def apply_iris_weight(self) -> None:
        """Push the configured iris weight into the loaded model."""
        with self._lock:
            if self.model is not None:
                self.model.iris_weight = self.cfg.effective_iris_weight

    def restart_camera(self) -> None:
        """Ask the vision thread to close and reopen the camera (e.g. after switching cameras)."""
        self._restart_cam.set()

    def release_camera(self, timeout: float = 5.0) -> bool:
        """Close the camera so another process (calibration) can use it."""
        self._hold.set()
        return self._camera_free.wait(timeout)

    def resume_camera(self) -> None:
        """Let the vision thread use the camera again after release_camera()."""
        self._hold.clear()

    def set_paused(self, paused: bool) -> None:
        """Pause or resume tracking (the camera is released while paused)."""
        with self._lock:
            self.paused = paused
            self._reset_gaze()
        log.info("Paused" if paused else "Resumed")
        self._set_status(self._idle_status())

    def toggle_pause(self) -> None:
        """Flip the paused state."""
        self.set_paused(not self.paused)

    # ---------- helpers ----------
    def _idle_status(self) -> str:
        """Status to show when nothing is running: paused, no_model, or starting."""
        if self.paused:
            return "paused"
        if self.model is None:
            return "no_model"
        return "running" if self.status == "running" else "starting"

    def _set_status(self, status: str) -> None:
        """Change the status and tell the tray if it changed."""
        if status != self.status:
            self.status = status
            self._notify()

    def _notify(self) -> None:
        """Call on_change() (the tray refresh), logging any error."""
        try:
            self.on_change()
        except Exception:
            log.exception("on_change failed")

    def _typing(self, now: float) -> bool:
        """True while the typing guard says a key was pressed recently."""
        return (self.cfg.typing_guard
                and (now - self._last_key_time) * 1000 < self.cfg.typing_guard_ms)

    def _reset_gaze(self) -> None:
        """Forget the gaze state and go back to 'monitor'."""
        changed = self.state != MONITOR or self.face
        self.state = MONITOR
        self.face = False
        self._armed = None
        self._pending = None
        self._no_face_since = None
        self.p_smooth = 0.0
        self.p_laptop = 0.0
        if changed:
            self._notify()

    # ---------- vision ----------
    def _vision_loop(self) -> None:
        """Camera thread: grab frames, track the face and update the gaze state."""
        try:
            tracker = FaceFeatures()
        except Exception:
            log.exception("Could not start the face tracker")
            self._set_status("error")
            return

        cam: Camera | None = None
        next_retry = 0.0
        read_failures = 0
        last_t0 = 0.0
        next_cam_check = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= next_cam_check:  # watch for cameras coming and going
                next_cam_check = time.monotonic() + 3.0
                self.check_camera_change()
            if self._restart_cam.is_set():
                self._restart_cam.clear()
                if cam is not None:
                    cam.close()
                    cam = None
                    self._camera_free.set()
                next_retry = 0.0

            if self.paused or self._hold.is_set() or self.model is None:
                if cam is not None:
                    cam.close()
                    cam = None
                self.preview_frame = None
                self.fps_actual = 0.0
                self._camera_free.set()
                self._stop.wait(0.2)
                continue

            if cam is None:
                if time.monotonic() < next_retry:
                    self._stop.wait(0.2)
                    continue
                self._camera_free.clear()
                if self._hold.is_set():  # calibration asked for the camera meanwhile
                    self._camera_free.set()
                    continue
                cam = Camera(camera_index(self.cfg), self.cfg.camera_width,
                             self.cfg.camera_height, fps=30)
                if not cam.open():
                    cam = None
                    self._camera_free.set()
                    log.warning("Camera unavailable (in use by another app?) - retrying in 5 s")
                    self._set_status("no_camera")
                    next_retry = time.monotonic() + 5
                    continue
                read_failures = 0
                last_t0 = 0.0
                self._set_status("running")

            t0 = time.monotonic()
            frame = cam.read()
            if frame is None:
                read_failures += 1
                if read_failures >= 5:
                    cam.close()
                    cam = None
                    self._camera_free.set()
                    self._set_status("no_camera")
                    next_retry = time.monotonic() + 2
                else:
                    self._stop.wait(0.1)
                continue
            read_failures = 0

            t_proc = time.monotonic()
            feats = tracker.process(frame)
            now = time.monotonic()
            proc = (now - t_proc) * 1000
            self.proc_ms = proc if self.proc_ms == 0 else 0.85 * self.proc_ms + 0.15 * proc
            if last_t0:
                inst = 1.0 / max(t0 - last_t0, 1e-3)
                self.fps_actual = inst if self.fps_actual == 0 else 0.85 * self.fps_actual + 0.15 * inst
            last_t0 = t0

            self._update_gaze(feats, now)
            if self.ui_preview:
                self._make_preview(frame, tracker.last_points)
            else:
                self.preview_frame = None

            fps = self.cfg.fps if (feats is not None or self.ui_preview) else self.cfg.idle_fps
            self._stop.wait(max(0.0, 1.0 / fps - (time.monotonic() - t0)))

        if cam is not None:
            cam.close()
        tracker.close()
        self._camera_free.set()

    def _update_gaze(self, feats, now: float) -> None:
        """Turn one frame's features into a laptop/monitor decision with hold times."""
        cfg = self.cfg
        with self._lock:
            model = self.model
            if model is None:
                return
            face = feats is not None
            face_changed = face != self.face
            self.face = face

            if not face:
                if self._no_face_since is None:
                    self._no_face_since = now
                if (self.state == LAPTOP
                        and (now - self._no_face_since) * 1000 >= cfg.no_face_to_monitor_ms):
                    self._transition(MONITOR, "face lost")
                elif face_changed:
                    self._notify()
                return

            self._no_face_since = None
            p = model.p_laptop(feats)
            self.p_laptop = p
            self._p_time = now
            self.p_smooth = SMOOTHING * p + (1 - SMOOTHING) * self.p_smooth

            if self.state == MONITOR:
                wanted = LAPTOP if self.p_smooth >= cfg.laptop_enter_prob else MONITOR
            else:
                wanted = MONITOR if self.p_smooth <= cfg.laptop_exit_prob else LAPTOP

            if wanted == self.state:
                self._pending = None
                if face_changed:
                    self._notify()
                return
            if self._pending != wanted:
                self._pending = wanted
                self._pending_since = now
            dwell = cfg.laptop_dwell_ms if wanted == LAPTOP else cfg.monitor_dwell_ms
            if (now - self._pending_since) * 1000 >= dwell:
                self._transition(wanted, f"p={self.p_smooth:.2f}")
            elif face_changed:
                self._notify()

    def _transition(self, new_state: str, reason: str) -> None:
        """Switch gaze state and arm a jump to the new screen."""
        self.state = new_state
        self._pending = None
        self._armed = new_state
        log.debug("Gaze -> %s (%s)", new_state, reason)
        if self.cfg.mode == "instant":
            self._try_warp()
        self._notify()

    # ---------- mouse ----------
    def _mouse_loop(self) -> None:
        """Mouse thread: watch the cursor/keyboard and fire armed jumps at the right moment."""
        last = get_cursor()
        last_move = 0.0
        last_tick = last_input_tick()
        screens = list_screens()
        screens_at = time.monotonic()
        while not self._stop.wait(0.008):
            pos = get_cursor()
            if pos is None:
                continue
            now = time.monotonic()

            # Input that isn't cursor movement = keyboard, click or wheel.
            # (Ignore the synthetic input focus.py sends right after a jump.)
            tick = last_input_tick()
            if tick != last_tick:
                last_tick = tick
                if pos == last and now - self._jumped_at > 0.25:
                    self._last_key_time = now

            if self.paused or self.model is None:
                last = pos
                continue
            if now - screens_at > 2.0:  # displays can be plugged in/out
                screens, screens_at = list_screens(), now

            # instant mode: a jump held back by typing / a held button fires as
            # soon as that's over, provided the gaze still points there.
            if self._armed is not None and self.cfg.mode == "instant":
                if self._try_warp():
                    last = get_cursor() or pos
                    continue

            if pos == last:
                continue
            gap_ms = (now - last_move) * 1000
            last_move = now

            s = screen_at(screens, *pos)
            if s is not None:
                self._last_pos[s.name] = pos

            jumped = False
            if self.cfg.mode == "mouse_move" and gap_ms >= self.cfg.move_start_gap_ms:
                if self._armed is not None:
                    jumped = self._try_warp()
                elif self.cfg.quick_jump:
                    jumped = self._quick_jump(screens, pos, now)
            elif self._armed is not None and self.cfg.mode == "mouse_move":
                self._disarm_if_on_target(screens, pos)
            if jumped:
                pos = get_cursor() or pos
            last = pos

    def _target_screen(self, role: str, screens):
        """Screen to jump to for `role`, or None with only one display."""
        laptop, monitor = resolve_roles(screens, self.cfg.laptop_display)
        if monitor is None:
            return None
        return laptop if role == LAPTOP else monitor

    def _disarm_if_on_target(self, screens, pos) -> None:
        """Cancel a pending jump when the cursor is already on the target screen."""
        with self._lock:
            if self._armed is None:
                return
            target = self._target_screen(self._armed, screens)
            if target is None or target.contains(*pos):
                self._armed = None

    def _jump(self, target, role: str) -> None:
        """Move the cursor to `target` (centre or last position) and focus the app there."""
        dest = target.center
        if self.cfg.warp_to == "last":
            remembered = self._last_pos.get(target.name)
            if remembered and target.contains(*remembered):
                dest = remembered
        set_cursor(*dest)
        self._jumped_at = time.monotonic()
        self.last_jump = (self._jumped_at, role)
        focused = False
        if self.cfg.focus_window:
            try:
                focused = focus_screen(target)
            except Exception:
                log.exception("Window focus failed")
        log.info("Cursor -> %s %s%s", role, dest, " (+focus)" if focused else "")

    def _try_warp(self) -> bool:
        """Jump now if one is armed and allowed (not mid-drag, not while typing)."""
        with self._lock:
            role = self._armed
            if role is None or self.paused:
                return False
            if mouse_button_down():  # never yank the cursor mid-drag
                return False
            if self.cfg.mode == "instant" and self._typing(time.monotonic()):
                return False  # stay armed; the mouse loop retries when typing stops
            self._armed = None
            target = self._target_screen(role, list_screens())
            pos = get_cursor()
            if target is None or pos is None or target.contains(*pos):
                return False
            self._jump(target, role)
            return True

    def _quick_jump(self, screens, pos, now: float) -> bool:
        """Reaching for the mouse confirms a gaze change that is still inside its hold time."""
        cfg = self.cfg
        with self._lock:
            if self.model is None or self.paused:
                return False
            if self.face and now - self._p_time <= FRESH_FRAME_S:
                if self.p_laptop >= cfg.quick_jump_prob:
                    role = LAPTOP
                elif self.p_laptop <= 1 - cfg.quick_jump_prob:
                    role = MONITOR
                else:
                    return False
            elif (not self.face and self._no_face_since is not None
                  and (now - self._no_face_since) * 1000 >= QUICK_NO_FACE_MS):
                role = MONITOR
            else:
                return False
            if role == self.state:
                return False  # gaze already settled: never undo a manual cursor move
            target = self._target_screen(role, screens)
            if target is None or target.contains(*pos) or mouse_button_down():
                return False
            self.state = role
            self._pending = None
            self._armed = None
            self._jump(target, role)
            self._notify()
            return True

    # ---------- preview for the control panel ----------
    def _make_preview(self, frame, pts) -> None:
        """Draw the camera frame with landmarks for the control panel's live view."""
        h, w = frame.shape[:2]
        scale = PREVIEW_WIDTH / w
        small = cv2.resize(frame, (PREVIEW_WIDTH, int(h * scale)), interpolation=cv2.INTER_AREA)
        if pts is not None:
            for i in (33, 133, 362, 263):
                cv2.circle(small, (int(pts[i][0] * scale), int(pts[i][1] * scale)), 2, (200, 200, 200), -1)
            for i in (468, 473):
                cv2.circle(small, (int(pts[i][0] * scale), int(pts[i][1] * scale)), 3, (0, 230, 255), -1)
        small = cv2.flip(small, 1)  # mirror, like a selfie view
        self.preview_frame = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        self.preview_seq += 1
