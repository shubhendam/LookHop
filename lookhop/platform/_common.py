"""Small helpers shared by the non-Windows backends (macOS, Linux)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def tk_message_box(text: str, title: str, error: bool = False) -> None:
    """Cross-platform message box (Tk ships with Python on every OS)."""
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    (messagebox.showerror if error else messagebox.showinfo)(title, text)
    root.destroy()


def open_file(path: Path) -> None:
    """Open a file with the default app (`open` on macOS, `xdg-open` elsewhere)."""
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)])
