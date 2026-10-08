#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .build-venv
source .build-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install '.[package]'
pyinstaller --clean --noconfirm --onedir --windowed --name WorkTwin \
  --collect-data worktwin --collect-submodules worktwin --collect-submodules uvicorn \
  desktop/launcher.py
mkdir -p dist/WorkTwin.app/Contents/Resources/third_party/rowboat
cp third_party/rowboat/* dist/WorkTwin.app/Contents/Resources/third_party/rowboat/
mkdir -p release
hdiutil create -volname 'WorkTwin Collector' -srcfolder dist/WorkTwin.app \
  -ov -format UDZO release/WorkTwin-Collector-1.0.0-macOS-$(uname -m).dmg
echo 'Created 1.0.0 macOS DMG (unsigned; not notarized)'
