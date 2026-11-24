#!/usr/bin/env bash
# Builds dist/LookHop.app and dist/LookHop-<version>-macos-<arch>.zip
#
#   ./packaging/macos/build.sh
#
# Uses .venv if it exists, otherwise the python3 on PATH (CI). The python must
# include Tk (python.org builds do; with Homebrew install python-tk).
set -euo pipefail
cd "$(dirname "$0")/../.."

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
echo "Using $PY"

"$PY" -m pip install --quiet -r requirements.txt pyinstaller

# App icon (generated, not committed) and the face model, bundled so it works offline.
"$PY" packaging/make_icons.py --icns-only
"$PY" -c "import sys; sys.path.insert(0, '.'); from lookhop import tracker; tracker.MODEL_PATH = tracker.BUNDLED_MODEL; print(tracker.ensure_model())"

"$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/macos/LookHop.spec

# Ad-hoc signature (no Developer ID): lets the app run locally and keeps a stable
# identity for the permissions you grant it. Users still right-click > Open once.
# Set LOOKHOP_SIGN_IDENTITY to a stable code-signing identity (e.g. a self-signed certificate)
# so macOS keeps the Camera/Accessibility grants across rebuilds. Plain ad-hoc signing ("-")
# changes the code hash every build, so the grants have to be given again after each update.
codesign --force --deep --sign "${LOOKHOP_SIGN_IDENTITY:--}" dist/LookHop.app
codesign --verify --deep --strict dist/LookHop.app

# Smoke-test the build: no camera, no windows; writes selftest.txt in the data folder.
rc=0
dist/LookHop.app/Contents/MacOS/LookHop --selftest || rc=$?
cat "$HOME/Library/Application Support/LookHop/selftest.txt"
[ "$rc" -eq 0 ] || { echo "Selftest failed (exit $rc)"; exit 1; }

VERSION=$("$PY" -c "import sys; sys.path.insert(0, '.'); import lookhop; print(lookhop.__version__)")
ZIP="dist/LookHop-$VERSION-macos-$(uname -m).zip"
rm -f "$ZIP"
ditto -c -k --keepParent dist/LookHop.app "$ZIP"
echo "Built $ZIP ($(du -h "$ZIP" | cut -f1))"
