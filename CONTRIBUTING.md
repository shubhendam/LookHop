# Contributing to LookHop

Thanks for helping! The most valuable contribution right now is a **Linux port**.
Everything except a thin OS layer is already cross-platform.

## Dev setup

```bash
git clone https://github.com/shubhendam/LookHop.git
cd LookHop
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m lookhop              # run (calibrates on first start)
python -m lookhop calibrate    # recalibrate
python -m lookhop --selftest   # checks deps without camera or windows
```

Settings, calibration and logs go to your user data folder (`%APPDATA%\LookHop`,
`~/Library/Application Support/LookHop`, `~/.config/lookhop`), never into the repo.

## Project layout

```
lookhop/
├── app.py            entry point: CLI, logging, startup flow, --selftest
├── config.py         settings dataclass, data-folder paths, config migrations
├── tracker.py        webcam + MediaPipe -> [yaw, pitch, iris_h, iris_v]     (cross-platform)
├── model.py          calibration classifier (numpy logistic regression)      (cross-platform)
├── calibrate.py      fullscreen calibration UI (Tk)                          (cross-platform)
├── engine.py         gaze state machine, jump/focus/typing-guard logic       (cross-platform)
├── ui.py             control panel (Tk)                                      (cross-platform)
├── tray.py           tray icon + app controller (pystray)                    (cross-platform)
└── platform/         ← everything OS-specific lives here
    ├── __init__.py   picks the backend for this OS + documents the contract
    ├── base.py       Screen dataclass, laptop/monitor role logic (shared)
    ├── windows/      ✅ complete (ctypes / Win32)
    ├── macos/        ✅ complete (pyobjc: Quartz / ApplicationServices)
    └── linux/        🛠️ stub with an implementation guide
packaging/
├── make_icons.py     renders assets/lookhop.ico + logo.png (+ .icns for the mac build)
├── macos/            PyInstaller spec + build.sh (.app bundle)
└── windows/          PyInstaller spec + build.ps1
```

## Porting to a new OS

1. Open `lookhop/platform/<os>/__init__.py`. Its docstring lists every function
   with the system APIs to use.
2. Implement the functions (split into files like `windows/` does if it grows),
   then set `SUPPORTED = True`.
3. The contract is in `lookhop/platform/__init__.py`. The rules that matter most:
   - `list_screens()` and `get_cursor()` / `set_cursor()` must use **one shared
     coordinate space** (top-left origin, like Windows).
   - `last_input_tick()` must change on **keyboard** input. That's how the typing
     guard works.
   - `focus_screen()` should pick the **front-most normal app window** on that
     screen and skip desktop, dock, panels and minimised windows.
4. Run `python -m lookhop --selftest`, then calibrate and try it for real.
5. Add a `packaging/<os>/` build and a job in `.github/workflows/release.yml`.

Please mention the OS version, desktop environment (for Linux) and camera you tested with in the PR.

## Guidelines

- Keep the runtime light. It runs all day; check *ms/frame* in the control panel.
- New settings go in `Config` (with a comment), the control panel, and the README settings table.
  If you change a default, add a migration in `load_config()` (see `CONFIG_VERSION`).
- Never write personal data (settings, calibration, frames) anywhere but the user data folder.
- Don't commit binaries. Releases are built by CI from a tag.

## Releasing (maintainers)

1. Bump `__version__` in `lookhop/__init__.py`.
2. `git tag v0.1.1 && git push --tags`
3. GitHub Actions builds the Windows and macOS zips, runs the selftest, and publishes the release.
