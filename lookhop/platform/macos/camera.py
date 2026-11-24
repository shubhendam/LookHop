"""macOS: list cameras (AVFoundation), so LookHop can pick the built-in one.

An iPhone nearby shows up as a Continuity Camera and can take index 0, so the
bare index in the config isn't a stable way to name the Mac's own camera.
"""
from __future__ import annotations

from lookhop.platform.base import CameraInfo


def list_cameras() -> list[CameraInfo]:
    """Cameras in the stable order used for every camera index (see avcamera.devices)."""
    try:
        import AVFoundation as av
        from lookhop.platform.macos.avcamera import devices
        devs = devices()
    except Exception:  # noqa: BLE001
        return []
    return [CameraInfo(str(d.localizedName()),
                       str(d.deviceType()) == str(av.AVCaptureDeviceTypeBuiltInWideAngleCamera))
            for d in devs]
