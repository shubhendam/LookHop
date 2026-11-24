# PyInstaller spec for the macOS build.  Run via packaging/macos/build.sh
# Produces dist/LookHop.app (a menu-bar app: no Dock icon).
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).resolve().parents[1]
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "lookhop" / "__init__.py").read_text()).group(1)
MODEL = ROOT / "models" / "face_landmarker.task"
ICON = ROOT / "assets" / "lookhop.icns"
for needed in (MODEL, ICON):
    if not needed.exists():
        raise SystemExit(f"Missing {needed} - run build.sh, it creates it first.")

# MediaPipe's Tasks API loads a native library + data files at runtime.
datas = collect_data_files("mediapipe") + [(str(MODEL), "models")]
binaries = collect_dynamic_libs("mediapipe")

a = Analysis(
    [str(ROOT / "lookhop" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["pystray._darwin", "PIL._tkinter_finder", "AppKit", "Foundation", "Quartz",
                   "ApplicationServices", "AVFoundation", "UserNotifications", "CoreMedia", "dispatch",
                   "lookhop.platform.macos"],
    # (matplotlib must stay: mediapipe imports it at load time)
    excludes=["IPython", "pytest", "tensorflow", "jax", "torch"],
    noarchive=False,
)
# Not needed: OpenCV's video-file codecs (the webcam uses AVFoundation) and Pillow's AVIF plugin.
_DROP = ("opencv_videoio_ffmpeg", "_avif")
a.binaries = [b for b in a.binaries if not any(d in Path(b[0]).name for d in _DROP)]
pyz = PYZ(a.pure)

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LookHop", console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="LookHop", upx=False)
app = BUNDLE(
    coll,
    name="LookHop.app",
    icon=str(ICON),
    bundle_identifier="io.github.shubhendam.lookhop",
    version=VERSION,
    info_plist={
        "CFBundleName": "LookHop",
        "CFBundleDisplayName": "LookHop",
        "CFBundleShortVersionString": VERSION,
        "LSUIElement": True,  # menu-bar app, no Dock icon
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSCameraUsageDescription":
            "LookHop uses the camera to see which screen you are looking at. "
            "Frames are processed on this Mac and never saved or sent anywhere.",
        "NSHumanReadableCopyright": "MIT License",
    },
)
