#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .build-venv
source .build-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[package]'
pyinstaller --clean --noconfirm --onedir --windowed --name WorkTwin \
  --collect-data worktwin --collect-submodules uvicorn \
  desktop/launcher.py
mkdir -p release
hdiutil create -volname 'WorkTwin Collector' -srcfolder dist/WorkTwin.app \
  -ov -format UDZO release/WorkTwin-Collector-macOS.dmg
echo 'Created release/WorkTwin-Collector-macOS.dmg (unsigned; not notarized)'
