"""Which camera LookHop uses, and the calibration profile that belongs to it.

Every camera sees you differently (position, lens, resolution), so each one gets
its own calibration file. Where the OS can list cameras (macOS) the active camera
is the one chosen by name in the settings; if that one is not connected, LookHop
falls back to the computer's own camera and says so. Elsewhere the camera is
just an index and the original profile file names are kept.
"""
from __future__ import annotations

import logging
import re

from lookhop.config import Config
from lookhop.platform import CameraInfo, find_camera, list_cameras, resolve_camera_index

log = logging.getLogger(__name__)


def resolve(cfg: Config, cameras: list[CameraInfo] | None = None) -> tuple[int, CameraInfo | None]:
    """The camera index to open and its info (None where cameras can't be listed)."""
    cameras = list_cameras() if cameras is None else cameras
    idx = resolve_camera_index(cfg.camera_name, cfg.camera_index, cameras)
    return idx, (cameras[idx] if cameras and 0 <= idx < len(cameras) else None)


def camera_index(cfg: Config) -> int:
    """Camera index to open (logs which camera was picked from the list)."""
    cameras = list_cameras()
    idx, info = resolve(cfg, cameras)
    if cameras:
        log.info("Cameras: %s -> using %d (%s)",
                 [f"{i}: {c.name}" for i, c in enumerate(cameras)], idx, info.name if info else "?")
    return idx


def active_camera_name(cfg: Config) -> str:
    """Name of the camera in use, or '' where cameras can't be listed."""
    info = resolve(cfg)[1]
    return info.name if info else ""


def camera_key(cfg: Config) -> str:
    """File-name-safe id of the active camera ('' where cameras can't be listed)."""
    return re.sub(r"[^a-z0-9]+", "-", active_camera_name(cfg).lower()).strip("-")


def requested_missing(cfg: Config) -> str:
    """The camera the user chose by name if it is not connected right now, else ''."""
    cameras = list_cameras()
    if cfg.camera_name and cameras and find_camera(cfg.camera_name, cameras) is None:
        return cfg.camera_name
    return ""
