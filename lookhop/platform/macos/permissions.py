"""macOS privacy permissions: Camera and Accessibility.

Accessibility lets LookHop raise other apps' windows and watch the hotkey.
macOS attributes the permission to the app that launched LookHop: the .app
itself, or Terminal/iTerm/your IDE when running from source.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


def accessibility_trusted(prompt: bool = False) -> bool:
    """True if LookHop may control other apps; with prompt=True macOS asks the user."""
    from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))


def request_camera_access(timeout: float = 120.0) -> bool:
    """Show the camera permission prompt now (OpenCV can't from a worker thread)."""
    try:
        import AVFoundation as av
    except ImportError:
        return True  # let OpenCV try on its own
    status = av.AVCaptureDevice.authorizationStatusForMediaType_(av.AVMediaTypeVideo)
    if status == 3:   # authorized
        return True
    if status != 0:   # denied / restricted
        return False
    done, granted = threading.Event(), []

    def handler(ok):
        granted.append(bool(ok))
        done.set()

    av.AVCaptureDevice.requestAccessForMediaType_completionHandler_(av.AVMediaTypeVideo, handler)
    done.wait(timeout)
    return bool(granted and granted[0])


def check_permissions(message_box, need_accessibility: bool = True) -> bool:
    """Ask for what LookHop needs. Returns False if something is missing (we still run)."""
    ok = True
    if not request_camera_access():
        ok = False
        log.warning("Camera access denied")
        message_box("LookHop needs camera access to see where you look.\n\n"
                    "Enable it under System Settings > Privacy & Security > Camera, "
                    "then restart LookHop.", "LookHop", True)
    # Check silently first: when the permission is already granted nothing is shown at all.
    # Only if it is missing do we ask (macOS's own prompt has an "Open System Settings"
    # button, so we don't also open the pane ourselves).
    if need_accessibility and not accessibility_trusted(prompt=False):
        accessibility_trusted(prompt=True)
        ok = False
        log.warning("Accessibility permission missing - focus and hotkey won't work")
        message_box("LookHop needs the Accessibility permission to switch app focus between "
                    "screens and to listen for the pause hotkey.\n\n"
                    "Enable LookHop (or the terminal you started it from) under System "
                    "Settings > Privacy & Security > Accessibility, then restart it.",
                    "LookHop")
    return ok
