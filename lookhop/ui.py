"""Control panel: live view of what the tracker sees + every setting, applied live.

Tk runs on its own thread (on Windows the tray owns the main thread). On macOS
Cocoa needs the main thread for both the tray icon and Tk, so there Tk takes the
main thread (run_mainloop) and the tray rides on its event loop. Either way all
Tk calls stay on the Tk thread; other threads talk to it through a queue.
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from lookhop.config import APP_NAME, TUNING_KEYS, Config
from lookhop.engine import LAPTOP, MONITOR, Engine
from lookhop.platform import (autostart_enabled, get_cursor, list_screens, resolve_roles,
                              set_autostart, ui_font)

log = logging.getLogger(__name__)

TICK_MS = 50
COLORS = {LAPTOP: "#4da3ff", MONITOR: "#4cd17a"}
MAP_BG = "#16181c"
SCREEN_OFF = "#2a2d33"
MUTED = "#6b6f7a"
WARN = "#c77d00"
FONT = ui_font(9)
FONT_B = ui_font(9, True)


class ControlPanel:
    """Control panel window: live view and every setting, applied immediately."""
    def __init__(self, cfg: Config, engine: Engine, controller):
        """Remember the config, engine and controller; the window is built later."""
        self.cfg = cfg
        self.engine = engine
        self.ctl = controller  # TrayApp: toggle_pause, recalibrate, set_glasses, set_hotkey, ...
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._visible = False
        self.on_main_thread = sys.platform == "darwin"

    # ---------- thread-safe API ----------
    def call_soon(self, fn) -> None:
        """Run fn() on the Tk thread (safe from any thread)."""
        self._q.put(fn)

    def show(self) -> None:
        """Show the panel (creating the Tk thread on first use outside macOS)."""
        if self.on_main_thread:
            self._q.put("show")
        elif self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="ui", daemon=True)
            self._thread.start()
        else:
            self._q.put("show")

    def shutdown(self) -> None:
        """Close the panel and stop its loop."""
        self._q.put("quit")
        if self._thread is not None:
            self._thread.join(timeout=2)

    # ---------- Tk thread ----------
    def prepare_main_thread(self) -> None:
        """macOS: create the (hidden) Tk root on the main thread. Must happen before
        anything else touches NSApplication - Tk installs its own NSApplication
        subclass and crashes if a plain one already exists."""
        self.root = tk.Tk()
        self.root.withdraw()
        self._build()

    def run_mainloop(self) -> None:
        """Run Tk on the calling (main) thread until shutdown(); starts hidden."""
        self._run(prebuilt=True)

    def _run(self, prebuilt: bool = False) -> None:
        """Build the window and run Tk's main loop."""
        try:
            if not prebuilt:
                self.root = tk.Tk()
                self._build()
                self._show()
            self.root.after(TICK_MS, self._tick)
            self.root.mainloop()
        except Exception:
            log.exception("Control panel crashed")
        finally:
            self.engine.ui_preview = False
            try:
                self.root.destroy()
            except Exception:
                pass

    def _show(self) -> None:
        """Bring the window to the front and start the live preview."""
        self._visible = True
        self.root.deiconify()
        self.root.lift()
        if sys.platform == "darwin":  # menu-bar apps aren't frontmost by default
            try:
                from AppKit import NSApplication
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            except Exception:  # noqa: BLE001
                pass
        self.root.focus_force()
        self.engine.ui_preview = self.show_cam.get()

    def _hide(self) -> None:
        """Hide the window, stop the preview and save pending changes."""
        self._visible = False
        self.engine.ui_preview = False
        self._flush_save()
        self.root.withdraw()

    def _tick(self) -> None:
        """Timer: handle queued messages and refresh the live view."""
        try:
            while True:
                msg = self._q.get_nowait()
                if callable(msg):
                    try:
                        msg()
                    except Exception:
                        log.exception("call_soon callback failed")
                elif msg == "show":
                    self._show()
                elif msg == "quit":
                    self._flush_save()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        if self._visible:
            try:
                self._update_live()
            except Exception:
                log.debug("live update failed", exc_info=True)
        self.root.after(TICK_MS, self._tick)

    # ---------- layout ----------
    def _build(self) -> None:
        """Create the window, styles and tabs."""
        root = self.root
        root.title(APP_NAME)
        root.protocol("WM_DELETE_WINDOW", self._hide)
        root.resizable(False, False)
        try:
            self._icon_photo = ImageTk.PhotoImage(self.ctl.icon_image())
            root.iconphoto(True, self._icon_photo)
        except Exception:
            pass
        style = ttk.Style(root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Head.TLabel", font=ui_font(12, True))
        style.configure("Section.TLabel", font=FONT_B)

        self._vars: dict[str, tuple[tk.Variable, callable]] = {}
        self._save_job = None

        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)

        # header
        hdr = ttk.Frame(outer)
        hdr.pack(fill="x")
        ttk.Label(hdr, text=APP_NAME, style="Head.TLabel").pack(side="left")
        self.pause_btn = ttk.Button(hdr, text="Pause", width=10, command=self._toggle_pause)
        self.pause_btn.pack(side="right")
        self.status_lbl = ttk.Label(hdr, text="", font=FONT_B)
        self.status_lbl.pack(side="right", padx=10)

        # live view
        self.map = tk.Canvas(outer, width=400, height=130, bg=MAP_BG, highlightthickness=0)
        self.map.pack(fill="x", pady=(10, 0))
        self.bar = tk.Canvas(outer, width=400, height=30, bg=MAP_BG, highlightthickness=0)
        self.bar.pack(fill="x")
        live_row = ttk.Frame(outer)
        live_row.pack(fill="x", pady=(4, 0))
        self.info_lbl = ttk.Label(live_row, text="", style="Muted.TLabel")
        self.info_lbl.pack(side="left")
        self.show_cam = tk.BooleanVar(value=False)
        ttk.Checkbutton(live_row, text="Show camera", variable=self.show_cam,
                        command=self._cam_toggled).pack(side="right")
        self.cam_lbl = ttk.Label(outer, anchor="center")
        self._cam_photo = None
        self._cam_seq = -1

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(10, 0))
        self._build_behaviour(self._tab("Behaviour"))
        self._build_tuning(self._tab("Tuning"))
        self._build_system(self._tab("System"))

        foot = ttk.Frame(outer)
        foot.pack(fill="x", pady=(10, 0))
        ttk.Button(foot, text="Reset tuning", command=self._reset_tuning).pack(side="left")
        ttk.Button(foot, text="Hide", command=self._hide).pack(side="right")
        ttk.Label(foot, text="Changes apply instantly", style="Muted.TLabel").pack(side="right", padx=10)

        self._screens = list_screens()
        self._screens_at = time.monotonic()

    def _tab(self, title: str) -> ttk.Frame:
        """Add a notebook tab and return its frame."""
        f = ttk.Frame(self.nb, padding=10)
        f.columnconfigure(1, weight=1)
        self.nb.add(f, text=title)
        return f

    def _build_behaviour(self, f: ttk.Frame) -> None:
        """Behaviour tab: mode, jump target, typing guard, glasses, recalibrate."""
        r = 0
        ttk.Label(f, text="Jump the cursor", style="Section.TLabel").grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        self.mode_var = tk.StringVar(value=self.cfg.mode)
        for value, text in (("instant", "As soon as I look at the other screen (recommended)"),
                            ("mouse_move", "When I start moving the mouse")):
            ttk.Radiobutton(f, text=text, value=value, variable=self.mode_var,
                            command=lambda: self._set("mode", self.mode_var.get())
                            ).grid(row=r, column=0, columnspan=3, sticky="w", padx=(10, 0))
            r += 1
        self._check(f, r, "typing_guard",
                    "Wait until I stop typing (instant mode), so keystrokes\n"
                    "never land in the wrong window")
        r += 1
        self._check(f, r, "quick_jump",
                    "Quick jump (mouse mode): if I'm clearly looking at the other\n"
                    "screen when I grab the mouse, jump right away")
        r += 1

        ttk.Label(f, text="Land at", style="Section.TLabel").grid(row=r, column=0, sticky="w", pady=(10, 0))
        r += 1
        self.warp_var = tk.StringVar(value=self.cfg.warp_to)
        for value, text in (("last", "Where the cursor last was on that screen"),
                            ("center", "Centre of the screen")):
            ttk.Radiobutton(f, text=text, value=value, variable=self.warp_var,
                            command=lambda: self._set("warp_to", self.warp_var.get())
                            ).grid(row=r, column=0, columnspan=3, sticky="w", padx=(10, 0))
            r += 1

        ttk.Label(f, text="Select window", style="Section.TLabel").grid(row=r, column=0, sticky="w", pady=(10, 0))
        r += 1
        self.focus_var = tk.BooleanVar(value=self.cfg.focus_window)
        for value, text in ((True, "Select the app on that screen (like Alt+Tab, no click needed)"),
                            (False, "Don't change the selected app")):
            ttk.Radiobutton(f, text=text, value=value, variable=self.focus_var,
                            command=lambda: self._set("focus_window", bool(self.focus_var.get()))
                            ).grid(row=r, column=0, columnspan=3, sticky="w", padx=(10, 0))
            r += 1
        self._vars["focus_window"] = (self.focus_var, lambda: self.focus_var.set(self.cfg.focus_window))

        ttk.Label(f, text="Calibration", style="Section.TLabel").grid(row=r, column=0, sticky="w", pady=(10, 0))
        r += 1
        self.glasses_var = tk.BooleanVar(value=self.cfg.glasses)
        ttk.Checkbutton(f, text="Wearing glasses (separate calibration, trusts the head more)",
                        variable=self.glasses_var,
                        command=lambda: self.ctl.set_glasses(self.glasses_var.get())
                        ).grid(row=r, column=0, columnspan=3, sticky="w", padx=(10, 0))
        r += 1
        row = ttk.Frame(f)
        row.grid(row=r, column=0, columnspan=3, sticky="we", padx=(10, 0), pady=(4, 0))
        ttk.Button(row, text="Recalibrate…", command=self.ctl.recalibrate).pack(side="left")
        self.calib_lbl = ttk.Label(row, text="", style="Muted.TLabel")
        self.calib_lbl.pack(side="left", padx=10)

    def _build_tuning(self, f: ttk.Frame) -> None:
        """Tuning tab: sliders for hold times and thresholds."""
        ttk.Label(f, text="Lower hold times feel snappier; higher ones mean fewer accidental jumps.",
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ms = lambda v: f"{v:.0f} ms"
        rows = [
            ("fps", "Analysis rate", 4, 30, 1, lambda v: f"{v:.0f} fps"),
            ("idle_fps", "Rate with no face", 1, 15, 1, lambda v: f"{v:.0f} fps"),
            ("laptop_dwell_ms", "Hold before laptop", 0, 800, 10, ms),
            ("monitor_dwell_ms", "Hold before monitor", 0, 800, 10, ms),
            ("no_face_to_monitor_ms", "Face lost → monitor", 100, 2000, 50, ms),
            ("move_start_gap_ms", "Mouse still before move", 0, 500, 10, ms),
            ("typing_guard_ms", "Typing pause before jump", 100, 2000, 50, ms),
            ("laptop_enter_prob", "Laptop when score ≥", 0.50, 0.99, 0.01, lambda v: f"{v:.2f}"),
            ("laptop_exit_prob", "Monitor when score ≤", 0.01, 0.50, 0.01, lambda v: f"{v:.2f}"),
            ("quick_jump_prob", "Quick jump confidence", 0.60, 0.99, 0.01, lambda v: f"{v:.2f}"),
            ("iris_weight", "Iris weight", 0.0, 1.5, 0.05, lambda v: f"{v:.2f}"),
            ("iris_weight_glasses", "Iris weight (glasses)", 0.0, 1.5, 0.05, lambda v: f"{v:.2f}"),
        ]
        for i, row in enumerate(rows, start=1):
            self._slider(f, i, *row)

    def _build_system(self, f: ttk.Frame) -> None:
        """System tab: displays, camera, hotkey, autostart, notifications, files."""
        r = 0
        ttk.Label(f, text="Laptop display").grid(row=r, column=0, sticky="w", pady=3)
        names = (["left", "right", "primary"] + (["builtin"] if sys.platform == "darwin" else [])
                 + [s.name for s in list_screens()])
        self.display_var = tk.StringVar(value=self.cfg.laptop_display)
        cb = ttk.Combobox(f, values=names, textvariable=self.display_var, state="readonly", width=18)
        cb.grid(row=r, column=1, sticky="w", pady=3)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._set("laptop_display", self.display_var.get()))
        r += 1

        ttk.Label(f, text="Camera").grid(row=r, column=0, sticky="w", pady=3)
        self.cam_var = tk.IntVar(value=self.cfg.camera_index)
        sp = ttk.Spinbox(f, from_=0, to=5, width=5, textvariable=self.cam_var, state="readonly",
                         command=lambda: self._set("camera_index", int(self.cam_var.get())))
        sp.grid(row=r, column=1, sticky="w", pady=3)
        r += 1

        ttk.Label(f, text="Pause hotkey").grid(row=r, column=0, sticky="w", pady=3)
        hk = ttk.Frame(f)
        hk.grid(row=r, column=1, columnspan=2, sticky="w", pady=3)
        self.hotkey_var = tk.StringVar(value=self.cfg.hotkey)
        entry = ttk.Entry(hk, textvariable=self.hotkey_var, width=18)
        entry.pack(side="left")
        entry.bind("<Return>", lambda _e: self._apply_hotkey())
        ttk.Button(hk, text="Apply", command=self._apply_hotkey).pack(side="left", padx=6)
        self.hotkey_msg = ttk.Label(hk, text=("e.g. ctrl+alt+g (alt = Option), cmd+shift+f9" if sys.platform == "darwin"
                                 else "e.g. ctrl+alt+g, win+shift+f9"), style="Muted.TLabel")
        self.hotkey_msg.pack(side="left")
        r += 1

        self._check(f, r, "show_notifications", "Show notifications")
        r += 1
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(f, text="Start at login" if sys.platform == "darwin" else "Start with Windows", variable=self.autostart_var,
                        command=self._toggle_autostart).grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        r += 1
        row = ttk.Frame(f)
        row.grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(row, text="Open settings file", command=self.ctl.open_config).pack(side="left")
        ttk.Button(row, text="Open log", command=self.ctl.open_log).pack(side="left", padx=6)

    # ---------- widgets bound to config ----------
    def _slider(self, f, row, key, label, lo, hi, step, fmt):
        """Add a labelled slider for a numeric setting."""
        is_int = isinstance(getattr(Config(), key), int)
        var = tk.DoubleVar(value=getattr(self.cfg, key))
        ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=2)
        val = ttk.Label(f, width=8, anchor="e")
        val.grid(row=row, column=2, sticky="e", padx=(8, 0))

        def show(v):
            val.config(text=fmt(v))

        def changed(_=None):
            v = lo + round((float(var.get()) - lo) / step) * step
            v = int(round(v)) if is_int else round(v, 3)
            show(v)
            if getattr(self.cfg, key) != v:
                self._set(key, v)

        ttk.Scale(f, from_=lo, to=hi, variable=var, command=changed, length=210
                  ).grid(row=row, column=1, sticky="we", padx=(10, 0))
        show(getattr(self.cfg, key))
        self._vars[key] = (var, lambda: (var.set(getattr(self.cfg, key)), show(getattr(self.cfg, key))))

    def _check(self, f, row, key, text):
        """Add a checkbox bound to a boolean setting."""
        var = tk.BooleanVar(value=getattr(self.cfg, key))
        ttk.Checkbutton(f, text=text, variable=var, command=lambda: self._set(key, bool(var.get()))
                        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=(10, 0), pady=(4, 0))
        self._vars[key] = (var, lambda: var.set(getattr(self.cfg, key)))

    def _set(self, key: str, value) -> None:
        """Apply a changed setting to the config and engine, and schedule a save."""
        setattr(self.cfg, key, value)
        # keep the two thresholds consistent (exit must not exceed enter)
        if key == "laptop_enter_prob" and self.cfg.laptop_exit_prob > value:
            self.cfg.laptop_exit_prob = value
            self._vars["laptop_exit_prob"][1]()
        elif key == "laptop_exit_prob" and value > self.cfg.laptop_enter_prob:
            self.cfg.laptop_enter_prob = value
            self._vars["laptop_enter_prob"][1]()
        if key.startswith("iris_weight"):
            self.engine.apply_iris_weight()
        elif key == "camera_index":
            self.engine.restart_camera()
        log.info("Setting %s = %r", key, value)
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(500, self._flush_save)

    def _flush_save(self) -> None:
        """Write the settings to disk now."""
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
            self._save_job = None
        try:
            self.cfg.save()
        except OSError:
            log.exception("Could not save settings")

    def _reset_tuning(self) -> None:
        """Restore the tuning sliders to their defaults."""
        defaults = Config()
        for key in TUNING_KEYS:
            setattr(self.cfg, key, getattr(defaults, key))
            if key in self._vars:
                self._vars[key][1]()
        self.engine.apply_iris_weight()
        self._flush_save()

    # ---------- actions ----------
    def _toggle_pause(self) -> None:
        """Pause/resume from the panel."""
        self.ctl.toggle_pause()

    def _cam_toggled(self) -> None:
        """Turn the live camera preview on or off."""
        on = self.show_cam.get()
        self.engine.ui_preview = on and self._visible
        if on:
            self.cam_lbl.pack(before=self.nb, pady=(6, 0))
        else:
            self.cam_lbl.pack_forget()
            self.cam_lbl.configure(image="")
            self._cam_photo = None

    def _apply_hotkey(self) -> None:
        """Apply the hotkey typed into the box and show the result."""
        ok, msg = self.ctl.set_hotkey(self.hotkey_var.get().strip())
        self.hotkey_msg.config(text="Saved ✓" if ok else msg,
                               foreground="" if ok else WARN)
        if not ok:
            self.hotkey_var.set(self.cfg.hotkey)

    def _toggle_autostart(self) -> None:
        """Turn start-at-login on or off."""
        try:
            set_autostart(self.autostart_var.get())
        except OSError:
            log.exception("Could not change autostart")
        self.autostart_var.set(autostart_enabled())

    # ---------- live view ----------
    def _update_live(self) -> None:
        """Refresh status, preview, map and bar from the engine."""
        e = self.engine
        now = time.monotonic()
        if now - self._screens_at > 2.0:
            self._screens, self._screens_at = list_screens(), now

        self.pause_btn.config(text="Resume" if e.paused else "Pause")
        running = e.status == "running" and not e.paused
        color = COLORS.get(e.state, MUTED) if running and e.face else (WARN if running else MUTED)
        self.status_lbl.config(text=self.ctl.status_text(), foreground=color)
        if self.glasses_var.get() != self.cfg.glasses:
            self.glasses_var.set(self.cfg.glasses)

        if running:
            face = "face ✓" if e.face else "no face"
            self.info_lbl.config(text=f"{face}  ·  {e.fps_actual:.0f} fps  ·  {e.proc_ms:.0f} ms/frame")
        else:
            self.info_lbl.config(text="camera off" if e.paused else "")

        meta = e.model.meta if e.model is not None else None
        if meta:
            acc = meta.get("accuracy", {}).get("combined", {})
            pct = lambda v: f"{v * 100:.0f}%" if isinstance(v, (int, float)) else "?"
            self.calib_lbl.config(text=f"{meta.get('created', '')}  ·  "
                                       f"laptop {pct(acc.get('laptop'))} / monitor {pct(acc.get('monitor'))}")
        else:
            self.calib_lbl.config(text="not calibrated for this setting")

        self._draw_map(running)
        self._draw_bar(running)
        if self.show_cam.get():
            frame = e.preview_frame
            if frame is not None and e.preview_seq != self._cam_seq:
                self._cam_seq = e.preview_seq
                self._cam_photo = ImageTk.PhotoImage(Image.fromarray(frame))
                self.cam_lbl.configure(image=self._cam_photo)

    def _draw_map(self, running: bool) -> None:
        """Draw the display layout with the current gaze target highlighted."""
        c = self.map
        c.delete("all")
        W, H = int(c["width"]), int(c["height"])
        screens = self._screens
        if not screens:
            return
        laptop, monitor = resolve_roles(screens, self.cfg.laptop_display)
        minx, maxx = min(s.left for s in screens), max(s.right for s in screens)
        miny, maxy = min(s.top for s in screens), max(s.bottom for s in screens)
        scale = min((W - 24) / (maxx - minx), (H - 20) / (maxy - miny))
        ox = (W - (maxx - minx) * scale) / 2
        oy = (H - (maxy - miny) * scale) / 2
        to_canvas = lambda x, y: (ox + (x - minx) * scale, oy + (y - miny) * scale)

        e = self.engine
        active = e.state if running else None
        flash = e.last_jump if e.last_jump and time.monotonic() - e.last_jump[0] < 0.35 else None
        for s in screens:
            role = LAPTOP if s == laptop else MONITOR if s == monitor else None
            x0, y0 = to_canvas(s.left, s.top)
            x1, y1 = to_canvas(s.right, s.bottom)
            lit = role is not None and role == active
            fill = COLORS[role] if lit else SCREEN_OFF
            outline = "white" if flash and flash[1] == role else (COLORS[role] if role else "#444")
            c.create_rectangle(x0 + 3, y0 + 3, x1 - 3, y1 - 3, fill=fill, outline=outline,
                               width=3 if flash and flash[1] == role else 2)
            label = {LAPTOP: "Laptop", MONITOR: "Monitor"}.get(role, s.name)
            c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label,
                          fill="#0b0c0e" if lit else "#9aa0aa", font=ui_font(11, True))
        pos = get_cursor()
        if pos:
            x, y = to_canvas(*pos)
            c.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#ff5a5a", outline="white")

    def _draw_bar(self, running: bool) -> None:
        """Draw the laptop-vs-monitor probability bar."""
        c = self.bar
        c.delete("all")
        W, H = int(c["width"]), int(c["height"])
        x0, x1 = 12, W - 12
        y0, y1 = 4, 14
        c.create_rectangle(x0, y0, x1, y1, fill=SCREEN_OFF, outline="")
        e = self.engine
        if running and e.face:
            p = e.p_smooth  # 0 = monitor, 1 = laptop
            # laptop sits on the left of the map, so draw laptop-ness growing leftwards
            xp = x1 - (x1 - x0) * p
            c.create_rectangle(xp, y0, x1, y1, fill=COLORS[e.state], outline="")
        for prob, col in ((self.cfg.laptop_enter_prob, COLORS[LAPTOP]),
                          (self.cfg.laptop_exit_prob, COLORS[MONITOR])):
            x = x1 - (x1 - x0) * prob
            c.create_line(x, y0 - 3, x, y1 + 3, fill=col, width=2)
        c.create_text(x0, y1 + 5, text="laptop", anchor="nw", fill=MUTED, font=ui_font(7))
        c.create_text(x1, y1 + 5, text="monitor", anchor="ne", fill=MUTED, font=ui_font(7))
