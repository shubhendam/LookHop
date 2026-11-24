"""macOS notifications that belong to LookHop.

pystray's macOS notifications go through `osascript`, which makes them Script
Editor's: clicking one opens Script Editor's file picker. In the packaged app we
use the system notification centre instead, so they carry LookHop's name and
icon and clicking just activates LookHop. From source there is no app identity
(UNUserNotificationCenter would crash), so callers fall back to the old way.
"""
from __future__ import annotations

import logging
import sys
import uuid

log = logging.getLogger(__name__)

_center = None
_delegate = None


def available() -> bool:
    """True when running as the packaged .app (the only case that has an app identity)."""
    return bool(getattr(sys, "frozen", False))


def _setup():
    """Create the notification centre once: show banners even while LookHop is 'active',
    and ask macOS for permission the first time."""
    global _center, _delegate
    if _center is not None:
        return _center
    import objc
    import UserNotifications as un
    from Foundation import NSObject

    class LookHopNotificationDelegate(NSObject):
        """Lets notifications appear as banners even though LookHop is a foreground app."""

        def userNotificationCenter_willPresentNotification_withCompletionHandler_(
                self, _center, _notification, handler):
            handler(un.UNNotificationPresentationOptionBanner | un.UNNotificationPresentationOptionList)

    center = un.UNUserNotificationCenter.currentNotificationCenter()
    _delegate = LookHopNotificationDelegate.alloc().init()
    center.setDelegate_(_delegate)

    def granted(ok, error):
        if error is not None or not ok:
            log.info("Notification permission not granted (%s)", error)

    center.requestAuthorizationWithOptions_completionHandler_(
        un.UNAuthorizationOptionAlert | un.UNAuthorizationOptionSound, granted)
    _center = center
    return center


def send(message: str, title: str) -> bool:
    """Post a notification; False if it could not be sent (so the caller can fall back)."""
    if not available():
        return False
    try:
        import UserNotifications as un
        center = _setup()
        content = un.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(message)
        request = un.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            str(uuid.uuid4()), content, None)

        def done(error):
            if error is not None:
                log.info("Notification failed: %s", error)

        center.addNotificationRequest_withCompletionHandler_(request, done)
        return True
    except Exception:  # noqa: BLE001
        log.debug("UserNotifications unavailable", exc_info=True)
        return False
