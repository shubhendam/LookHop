"""Shared placeholder pieces for backends that aren't written yet.

A backend that isn't ready sets SUPPORTED = False; the app then shows a
friendly "not supported yet - contributions welcome" message and exits.
"""
from __future__ import annotations

import threading

from lookhop.platform._common import open_file, tk_message_box  # noqa: F401 (re-exported)

REPO_URL = "https://github.com/shubhendam/LookHop"


class NotPortedYet(NotImplementedError):
    """Raised by stubbed OS functions."""
    def __init__(self, what: str, os_name: str):
        """Build the 'not implemented for <OS> yet' message."""
        super().__init__(f"{what} is not implemented for {os_name} yet. "
                         f"See CONTRIBUTING.md ({REPO_URL}) - help wanted!")


class NoHotkey(threading.Thread):
    """Stand-in for GlobalHotkey: starts, reports failure, does nothing."""

    def __init__(self, spec: str, callback):
        """Accept the same arguments as GlobalHotkey but do nothing."""
        super().__init__(daemon=True)
        self.ok = False
        self.ready = threading.Event()
        self.ready.set()

    def run(self) -> None:
        """No-op thread body."""
        pass

    def stop(self) -> None:
        """No-op."""
        pass
