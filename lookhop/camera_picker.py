"""'Which camera?' dialog with a live preview.

Shown before calibration when the computer reports more than one camera (on
macOS an iPhone nearby appears as a camera too), so the wrong one is never
used silently. The choice is stored by name in Config.camera_name.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import cv2
from PIL import Image, ImageTk

from lookhop.config import APP_NAME, Config
from lookhop.platform import list_cameras, resolve_camera_index, ui_font
from lookhop.tracker import Camera

log = logging.getLogger(__name__)

PREVIEW_W, PREVIEW_H = 320, 240


class _Picker:
    """The 'choose your camera' window: a radio list plus a live mirrored preview."""
    def __init__(self, cfg: Config, cameras):
        """Build the window and start the preview thread."""
        self.cfg, self.cameras = cfg, cameras
        self.choice: int | None = None
        self._want = resolve_camera_index(cfg.camera_name, cfg.camera_index, cameras)
        self._latest = None
        self._failed = False
        self._stop = threading.Event()

        self.root = root = tk.Tk()
        root.title(f"{APP_NAME} - choose your camera")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._cancel)
        pad = ttk.Frame(root, padding=14)
        pad.pack()
        ttk.Label(pad, text="Which camera should LookHop use?", font=ui_font(12, True)).pack(anchor="w")
        ttk.Label(pad, text="More than one camera was found. Pick the one that sees your face.",
                  wraplength=PREVIEW_W).pack(anchor="w", pady=(2, 8))
        self.var = tk.IntVar(value=self._want)
        for i, c in enumerate(cameras):
            label = c.name + ("  (built-in)" if c.builtin else "")
            ttk.Radiobutton(pad, text=label, value=i, variable=self.var,
                            command=self._selected).pack(anchor="w", pady=1)
        self.preview = tk.Label(pad, width=PREVIEW_W, height=PREVIEW_H, bg="#16181c", fg="#9aa0aa",
                                text="Starting camera…", compound="center")
        self.preview.config(width=PREVIEW_W // 8, height=PREVIEW_H // 16)  # text units until an image arrives
        self.preview.pack(pady=10)
        buttons = ttk.Frame(pad)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Continue", command=self._ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=6)
        root.bind("<Return>", lambda _e: self._ok())
        root.bind("<Escape>", lambda _e: self._cancel())

        self._thread = threading.Thread(target=self._worker, name="camera-preview", daemon=True)
        self._thread.start()
        root.after(60, self._tick)
        root.lift()
        if sys.platform == "darwin":
            try:
                from AppKit import NSApplication
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            except Exception:  # noqa: BLE001
                pass

    def _selected(self) -> None:
        """The user picked another camera: restart the preview on it."""
        self._want = int(self.var.get())
        self._latest, self._failed = None, False
        self.preview.config(image="", text="Starting camera…", width=PREVIEW_W // 8, height=PREVIEW_H // 16)

    def _worker(self) -> None:
        """Preview thread: (re)open the wanted camera and keep the latest frame."""
        cur, cam = None, None
        while not self._stop.is_set():
            if cur != self._want:
                if cam is not None:
                    cam.close()
                cur, cam = self._want, Camera(self._want, 640, 480)
                if not cam.open():
                    cam, self._failed = None, True
            if cam is None:
                time.sleep(0.1)
                continue
            frame = cam.read()
            if frame is not None:
                self._latest = frame
        if cam is not None:
            cam.close()

    def _tick(self) -> None:
        """UI timer: paint the latest preview frame (or an error) into the window."""
        if self._failed:
            self.preview.config(image="", text="Could not open this camera")
        frame, self._latest = self._latest, None
        if frame is not None:
            rgb = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)  # mirrored, like a mirror
            img = Image.fromarray(rgb)
            img.thumbnail((PREVIEW_W, PREVIEW_H))
            self._photo = ImageTk.PhotoImage(img)
            self.preview.config(image=self._photo, text="", width=img.width, height=img.height)
        self.root.after(60, self._tick)

    def _finish(self) -> None:
        """Stop the preview thread and close the window."""
        self._stop.set()
        self._thread.join(timeout=3)
        self.root.destroy()

    def _ok(self) -> None:
        """Continue with the selected camera."""
        self.choice = int(self.var.get())
        self._finish()

    def _cancel(self) -> None:
        """Close without choosing."""
        self._finish()


def choose_camera(cfg: Config) -> bool:
    """Ask which camera to use when there is a choice. False = the user cancelled."""
    cameras = list_cameras()
    if len(cameras) < 2 or cfg.camera_name:
        return True
    picker = _Picker(cfg, cameras)
    picker.root.mainloop()
    if picker.choice is None:
        return False
    cfg.camera_name = cameras[picker.choice].name
    cfg.save()
    log.info("Camera chosen: %s", cfg.camera_name)
    return True
