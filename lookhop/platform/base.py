"""OS-independent display helpers shared by every platform backend."""
from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Screen:
    """One display: name plus its rectangle in the shared virtual-desktop coordinate space."""
    name: str
    left: int
    top: int
    right: int
    bottom: int
    primary: bool
    builtin: bool = False  # the computer's own display (known on macOS)

    @property
    def width(self) -> int:
        """Width in pixels (points on macOS)."""
        return self.right - self.left

    @property
    def height(self) -> int:
        """Height in pixels (points on macOS)."""
        return self.bottom - self.top

    @property
    def area(self) -> int:
        """Width x height, used to pick the larger display as the monitor."""
        return self.width * self.height

    @property
    def center(self) -> tuple[int, int]:
        """Centre point of the display."""
        return self.left + self.width // 2, self.top + self.height // 2

    def contains(self, x: int, y: int) -> bool:
        """True if the point (x, y) is on this display."""
        return self.left <= x < self.right and self.top <= y < self.bottom


def resolve_roles(screens: list[Screen], laptop_display: str) -> tuple[Screen | None, Screen | None]:
    """Return (laptop, monitor). monitor is None when only one display is connected."""
    if not screens:
        return None, None
    key = laptop_display.strip().lower()
    if key == "right":
        laptop = max(screens, key=lambda s: s.right)
    elif key == "primary":
        laptop = next((s for s in screens if s.primary), screens[0])
    elif key == "builtin":  # the computer's own display; falls back to the leftmost if unknown
        laptop = next((s for s in screens if s.builtin), min(screens, key=lambda s: s.left))
    elif key == "left":
        laptop = min(screens, key=lambda s: s.left)
    else:
        laptop = next((s for s in screens if s.name.lower() == key),
                      min(screens, key=lambda s: s.left))
    others = [s for s in screens if s != laptop]
    monitor = max(others, key=lambda s: s.area) if others else None
    return laptop, monitor


def screen_at(screens: list[Screen], x: int, y: int) -> Screen | None:
    """The display containing (x, y), or None."""
    return next((s for s in screens if s.contains(x, y)), None)


def ui_font(size: int, bold: bool = False) -> tuple:
    """Tk font tuple using the system UI font of this OS."""
    if sys.platform == "win32":
        return ("Segoe UI Semibold" if bold else "Segoe UI", size)
    family = ".AppleSystemUIFont" if sys.platform == "darwin" else "TkDefaultFont"
    return (family, size, "bold") if bold else (family, size)
