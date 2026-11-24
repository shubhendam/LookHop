"""Settings, paths and calibration-profile locations.

Everything personal (settings, calibration, logs) lives in a per-user folder:
  Windows  %APPDATA%\\LookHop
  macOS    ~/Library/Application Support/LookHop
  Linux    $XDG_CONFIG_HOME/lookhop  (default ~/.config/lookhop)
so nothing personal ever sits in the source tree or next to the .exe.
Missing keys in config.json are filled with defaults and unknown keys are
ignored, so the file can be edited by hand.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_NAME = "LookHop"
FROZEN = getattr(sys, "frozen", False)          # running from the PyInstaller build
PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parent                # repo root when running from source
# Read-only files shipped with the app (the face model is bundled in builds).
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))


def _user_data_dir() -> Path:
    """Per-user folder for settings, calibration and logs (differs per OS)."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME.lower()


DATA_DIR = _user_data_dir()
MODELS_DIR = DATA_DIR / "models"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "lookhop.log"


def self_command(*args: str) -> list[str]:
    """Command line that starts this app again (used for calibration and autostart)."""
    if FROZEN:
        return [sys.executable, *args]
    return [sys.executable, "-m", "lookhop", *args]


def migrate_legacy_files() -> None:
    """Early source versions kept config.json and data/ in the project folder."""
    if FROZEN or CONFIG_PATH.exists():
        return
    copies = [(SOURCE_ROOT / "config.json", CONFIG_PATH)]
    copies += [(p, DATA_DIR / p.name) for p in (SOURCE_ROOT / "data").glob("calibration*.json")]
    for src, dst in copies:
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            logging.getLogger(__name__).info("Moved %s to %s", src.name, dst)

MODES = ("mouse_move", "instant")
WARP_TARGETS = ("center", "last")
CONFIG_VERSION = 3
# v1 shipped slower timing defaults; files still holding them get the new ones.
_V1_DEFAULTS = {"fps": 8.0, "idle_fps": 3.0, "laptop_dwell_ms": 250,
                "monitor_dwell_ms": 350, "no_face_to_monitor_ms": 600}
# v2 defaulted to mouse-move jumps landing at the centre; v3 defaults are
# instant jumps landing where the cursor last was.
_V2_DEFAULTS = {"mode": ("mouse_move", "instant"), "warp_to": ("center", "last")}
# Settings the control panel's "Reset tuning" restores.
TUNING_KEYS = ("fps", "idle_fps", "laptop_enter_prob", "laptop_exit_prob", "laptop_dwell_ms",
               "monitor_dwell_ms", "no_face_to_monitor_ms", "move_start_gap_ms",
               "quick_jump_prob", "typing_guard_ms", "iris_weight", "iris_weight_glasses")

log = logging.getLogger(__name__)


@dataclass
class Config:
    """All user settings, saved as config.json; unknown keys are ignored, missing ones default."""
    # "instant": jump as soon as your gaze settles on the other screen.
    # "mouse_move": jump only when you start moving the mouse while looking there.
    mode: str = "instant"
    # Glasses make iris tracking less reliable, so head pose gets more say.
    # Each setting has its own calibration profile.
    glasses: bool = False
    hotkey: str = "ctrl+alt+g"
    # Which display is the laptop: "left", "right", "primary", "builtin" (the computer's own
    # display, macOS), or a device name like "\\\\.\\DISPLAY1". The largest other display is
    # the monitor. On macOS the default is the built-in display, since the physical layout
    # (which side the monitor sits on) says nothing about which one is the laptop.
    laptop_display: str = "builtin" if sys.platform == "darwin" else "left"
    # Where the cursor lands: the "last" place it was on that screen, or the
    # "center" of the screen.
    warp_to: str = "last"
    # After a jump, bring the window that was on top on that screen to the
    # front (like Alt+Tab, but per screen), so you don't have to click it.
    focus_window: bool = True
    # instant mode: hold a jump back until you've stopped typing for
    # typing_guard_ms, so a glance mid-sentence doesn't steal your keystrokes.
    typing_guard: bool = True
    typing_guard_ms: int = 600

    camera_index: int = 0
    # Camera by name; set by the picker shown before calibration when there are several cameras
    # ("" = not chosen yet: the built-in one is used). Wins over camera_index where cameras can be listed.
    camera_name: str = ""
    camera_width: int = 640
    camera_height: int = 480
    fps: float = 15.0         # analysis rate while a face is visible
    idle_fps: float = 5.0     # analysis rate while no face is visible

    # The monitor is "home". Switching to the laptop needs high confidence,
    # switching back needs much less (hysteresis).
    laptop_enter_prob: float = 0.70
    laptop_exit_prob: float = 0.35
    laptop_dwell_ms: int = 150        # must look at laptop this long
    monitor_dwell_ms: int = 200       # must look at monitor this long
    no_face_to_monitor_ms: int = 500  # face lost this long => monitor
    # mouse_move mode: the mouse must have been still for this long before a
    # movement counts as "reaching for the mouse". Stops jumps while you are
    # mid-drag and merely glance at the other screen.
    move_start_gap_ms: int = 150
    # Quick jump: when you reach for the mouse and the current frame is
    # already this confident about the other screen, skip the hold time.
    quick_jump: bool = True
    quick_jump_prob: float = 0.85

    iris_weight: float = 0.5          # iris vote weight relative to head pose
    iris_weight_glasses: float = 0.2
    calibration_rounds: int = 2
    show_notifications: bool = True
    config_version: int = 3

    @property
    def profile_path(self) -> Path:
        """Calibration file for the current glasses setting and the camera in use."""
        from lookhop.cameras import camera_key  # lazy: cameras imports this module
        return self.profile_path_for(camera_key(self))

    def profile_path_for(self, camera: str) -> Path:
        """Calibration file for the glasses setting and the camera id `camera`.

        Each camera has its own profile (it sees you differently). With no camera id
        (cameras can't be listed on this OS) the original names are used.
        """
        parts = ["calibration"] + (["glasses"] if self.glasses else []) + ([camera] if camera else [])
        return DATA_DIR / ("_".join(parts) + ".json")

    @property
    def effective_iris_weight(self) -> float:
        """Iris weight in use: head pose gets more say when wearing glasses."""
        return self.iris_weight_glasses if self.glasses else self.iris_weight

    def save(self) -> None:
        """Write the settings to config.json."""
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def update_from(self, other: "Config") -> None:
        """Copy every setting from `other` into this object."""
        for f in fields(self):
            setattr(self, f.name, getattr(other, f.name))


def _coerce(value, default):
    """Convert a value from JSON to the type of its default (bool/int/float/str)."""
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    return type(default)(value)


def _validate(cfg: Config) -> None:
    """Replace out-of-range or unknown values with the defaults."""
    d = Config()
    if cfg.mode not in MODES:
        log.warning("Unknown mode %r, using %r", cfg.mode, d.mode)
        cfg.mode = d.mode
    if cfg.warp_to not in WARP_TARGETS:
        cfg.warp_to = d.warp_to
    cfg.fps = min(max(cfg.fps, 1.0), 30.0)
    cfg.idle_fps = min(max(cfg.idle_fps, 0.5), cfg.fps)
    cfg.laptop_exit_prob = min(cfg.laptop_exit_prob, cfg.laptop_enter_prob)
    cfg.quick_jump_prob = min(max(cfg.quick_jump_prob, 0.5), 0.999)
    cfg.calibration_rounds = min(max(cfg.calibration_rounds, 1), 5)


def load_config() -> Config:
    """Read config.json (creating the data folder), apply migrations and validation."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_files()
    cfg = Config()
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:  # keep running with defaults on a broken file
            log.warning("Could not read %s (%s); using defaults", CONFIG_PATH, e)
            raw = {}
        defaults = Config()
        names = {f.name for f in fields(Config)}
        for key, value in raw.items():
            if key not in names:
                log.warning("Ignoring unknown setting %r", key)
                continue
            try:
                setattr(cfg, key, _coerce(value, getattr(defaults, key)))
            except (TypeError, ValueError):
                log.warning("Bad value for %r: %r", key, value)
        if int(raw.get("config_version", 1)) < 2:
            for key, old in _V1_DEFAULTS.items():
                if raw.get(key) == old:
                    setattr(cfg, key, getattr(defaults, key))
            log.info("Updated timing settings to the faster v2 defaults")
        if int(raw.get("config_version", 1)) < 3:
            for key, (old, new) in _V2_DEFAULTS.items():
                if raw.get(key, old) == old:
                    setattr(cfg, key, new)
            log.info("Updated jump mode / landing spot to the v3 defaults")
    cfg.config_version = CONFIG_VERSION
    _validate(cfg)
    cfg.save()  # write back so new settings show up in the file
    return cfg
