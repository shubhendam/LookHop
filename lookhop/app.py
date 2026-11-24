"""LookHop - the cursor follows your eyes between laptop and monitor.

    lookhop              run in the tray (calibrates first if needed)
    lookhop calibrate    (re)calibrate for the current glasses setting
    lookhop --selftest   check the install without camera or windows; writes selftest.txt
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from lookhop import __version__
from lookhop import platform as plat
from lookhop.config import APP_NAME, DATA_DIR, LOG_PATH, load_config

log = logging.getLogger(APP_NAME)


def setup_logging() -> None:
    """Log to a rotating file in the data folder (and stderr when there is a console)."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    if sys.stderr is not None:  # windowed builds / pythonw have no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    def log_uncaught(exc_type, exc, tb):
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
    sys.excepthook = log_uncaught
    threading.excepthook = lambda a: log.critical(
        "Uncaught exception in %s", a.thread.name if a.thread else "thread",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def selftest() -> int:
    """Exercise every bundled dependency without touching the camera or showing UI."""
    report, ok = [], True

    def step(name, fn):
        nonlocal ok
        try:
            report.append(f"PASS {name}: {fn()}")
        except Exception as e:  # noqa: BLE001
            ok = False
            report.append(f"FAIL {name}: {type(e).__name__}: {e}")

    import numpy as np

    def tracker():
        from lookhop.tracker import FaceFeatures, ensure_model
        path = ensure_model()
        ff = FaceFeatures()
        out = ff.process(np.full((480, 640, 3), 127, np.uint8))
        ff.close()
        return f"model={path} blank-frame->{out}"

    def tk():
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        v = root.tk.call("info", "patchlevel")
        root.destroy()
        return f"Tk {v}"

    step("version", lambda: __version__)
    step("platform", lambda: f"{plat.NAME} supported={plat.SUPPORTED}")
    step("opencv", lambda: __import__("cv2").__version__)
    step("mediapipe", lambda: __import__("mediapipe").__version__)
    step("face tracker", tracker)
    step("tkinter", tk)
    step("pystray", lambda: __import__("pystray").Icon.__module__)
    if plat.SUPPORTED:
        step("screens", lambda: [(s.name, s.width, s.height) for s in plat.list_screens()])
        step("hotkey parse", lambda: plat.parse_hotkey("ctrl+alt+g"))
    step("data dir", lambda: DATA_DIR)
    text = "\n".join(report) + f"\nRESULT: {'OK' if ok else 'FAILED'}\n"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "selftest.txt").write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        print(text)
    return 0 if ok else 1


def main(argv=None) -> int:
    """CLI entry point: parse args, check the OS layer, then calibrate / run the tray app."""
    parser = argparse.ArgumentParser(prog=APP_NAME.lower(), description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["run", "calibrate"], default="run")
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    plat.make_dpi_aware()  # before any window exists
    setup_logging()
    log.info("%s %s on %s", APP_NAME, __version__, plat.NAME)

    if not plat.SUPPORTED:
        plat.message_box(f"{APP_NAME} doesn't support {plat.NAME} yet.\n\n"
                         "The camera and calibration parts are ready - only the OS layer "
                         "is missing. Contributions are very welcome (see CONTRIBUTING.md).",
                         APP_NAME)
        return 1

    cfg = load_config()

    if args.command == "calibrate":
        plat.check_permissions(need_accessibility=False)
        from lookhop.calibrate import run_calibration
        return 0 if run_calibration(cfg) else 1

    mutex = plat.acquire_single_instance(APP_NAME)
    if mutex is None:
        plat.message_box(f"{APP_NAME} is already running - look for it in the tray.", APP_NAME)
        return 1

    plat.check_permissions()  # macOS: Camera + Accessibility prompts (no-op elsewhere)

    if not cfg.profile_path.exists():
        log.info("No calibration for glasses=%s yet - calibrating first", cfg.glasses)
        from lookhop.calibrate import run_calibration
        if not run_calibration(cfg):
            log.info("Calibration cancelled; exiting")
            return 1

    from lookhop.engine import Engine
    from lookhop.tray import TrayApp

    engine = Engine(cfg)
    tray = TrayApp(cfg, engine)  # also registers the hotkey
    engine.start()
    log.info("Running (mode=%s, glasses=%s, fps=%s)", cfg.mode, cfg.glasses, cfg.fps)
    try:
        tray.run()  # blocks until Quit
    finally:
        engine.stop()
        log.info("%s stopped", APP_NAME)
    return 0
