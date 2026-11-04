#!/bin/bash
set -eu
TASK_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$TASK_DIR"
PYTHON="${PYTHON:-.venv/bin/python}"
"$PYTHON" prepare_vendor.py
"$PYTHON" -m PyInstaller --noconfirm --clean --onedir --name atrust-splitd \
    --specpath build --workpath build/pyinstaller --distpath dist \
    --hidden-import yaml --add-data "$TASK_DIR/container:container" \
    --add-binary "$TASK_DIR/vendor/tun2socks:vendor" --add-data "$TASK_DIR/vendor/tun2socks-LICENSE:vendor" \
    --add-data "$TASK_DIR/vendor/README.txt:vendor" "$TASK_DIR/service.py"
APP_PATH="$TASK_DIR/dist/ATrust Split.app"
mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources"
swiftc -swift-version 5 -target arm64-apple-macos13.0 -parse-as-library -O \
    -framework AppKit app/Main.swift -o "$APP_PATH/Contents/MacOS/ATrustSplit"
ditto "$TASK_DIR/dist/atrust-splitd" "$APP_PATH/Contents/Resources/runtime"
cp app/install.command app/uninstall.command app/使用说明.txt "$APP_PATH/Contents/Resources/"
chmod 755 "$APP_PATH/Contents/Resources/"*.command install.command
"$PYTHON" - <<'PY'
from pathlib import Path
import plistlib
from version import VERSION
Path('dist/ATrust Split.app/Contents/Info.plist').write_bytes(plistlib.dumps({
    'CFBundleIdentifier': 'io.github.chuyun-shen.atrust-split',
    'CFBundleName': 'aTrust 分流',
    'CFBundleDisplayName': 'aTrust 分流',
    'CFBundleExecutable': 'ATrustSplit',
    'CFBundlePackageType': 'APPL',
    'CFBundleShortVersionString': VERSION,
    'CFBundleVersion': VERSION,
    'LSUIElement': True,
    'LSMinimumSystemVersion': '13.0',
    'NSHighResolutionCapable': True,
}))
PY
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"
"$APP_PATH/Contents/MacOS/ATrustSplit" --self-test
"$APP_PATH/Contents/Resources/runtime/atrust-splitd" --help
printf '\nBuilt %s\nNo services installed or network settings changed.\n' "$APP_PATH"
