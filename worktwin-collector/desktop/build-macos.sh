#!/bin/zsh
# Build a self-consistent app bundle: every resource must enter *before*
# PyInstaller performs the final macOS code signing pass.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .build-venv
source .build-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install '.[package]'

# PyInstaller ad-hoc signs without an Apple Developer ID. A locally verified
# ad-hoc signature is not Gatekeeper trust for an internet-downloaded app.
export PYINSTALLER_STRICT_BUNDLE_CODESIGN_ERROR=1
export PYINSTALLER_VERIFY_BUNDLE_SIGNATURE=1

args=(
  --clean --noconfirm --onedir --windowed
  --name WorkTwin
  --osx-bundle-identifier io.github.kirenhu.worktwin
  --collect-data worktwin
  --collect-data certifi
  --collect-submodules worktwin
  --collect-submodules uvicorn
  --collect-submodules keyring.backends
  --add-data 'third_party/rowboat/LICENSE:third_party/rowboat'
  --add-data 'third_party/rowboat/NOTICE.md:third_party/rowboat'
)
if [[ -n "${WORKTWIN_CODESIGN_IDENTITY:-}" ]]; then
  args+=(--codesign-identity "$WORKTWIN_CODESIGN_IDENTITY")
fi
pyinstaller "${args[@]}" desktop/launcher.py

app="dist/WorkTwin.app"
# Never copy files into the app *after* signing: this previously invalidated
# its resource seal and could surface as macOS's "app is damaged" dialog.
test -s "$app/Contents/Resources/third_party/rowboat/LICENSE"
test -s "$app/Contents/Resources/third_party/rowboat/NOTICE.md"
codesign --verify --deep --strict --all-architectures --verbose=2 "$app"

mkdir -p release
suffix="-unsigned"
if [[ -n "${WORKTWIN_CODESIGN_IDENTITY:-}" ]]; then
  suffix=""
fi
dmg="release/WorkTwin-Collector-1.1.2-macOS-$(uname -m)${suffix}.dmg"
hdiutil create -volname 'WorkTwin Collector' -srcfolder "$app" -ov -format UDZO "$dmg"
hdiutil verify "$dmg"

if [[ -n "${WORKTWIN_CODESIGN_IDENTITY:-}" ]]; then
  echo "Created signed macOS DMG: $dmg (Apple notarization is still required)."
else
  echo "::warning::Created ad-hoc signed DMG: $dmg"
  echo "::warning::Chrome-downloaded apps remain blocked by macOS Gatekeeper without a trusted Developer ID and Apple notarization."
fi
