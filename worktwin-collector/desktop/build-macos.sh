#!/bin/zsh
# Build a self-consistent app bundle: every resource must enter *before*
# PyInstaller performs the final macOS code signing pass.
set -euo pipefail
cd "$(dirname "$0")/.."

formal="${WORKTWIN_REQUIRE_NOTARIZATION:-0}"
if [[ "$formal" == 1 ]]; then
  : "${WORKTWIN_CODESIGN_IDENTITY:?Developer ID required}"
  : "${WORKTWIN_NOTARY_KEY_PATH:?Notarization key required}"
  : "${APPLE_NOTARY_KEY_ID:?Notarization key ID required}"
  : "${APPLE_NOTARY_ISSUER_ID:?Notarization issuer required}"
fi

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
  --collect-submodules mcp.server
  --collect-submodules mcp.shared
  --collect-submodules mcp_types
  --copy-metadata mcp
  --copy-metadata mcp-types
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

if [[ "$formal" == 1 ]]; then
  ditto -c -k --keepParent "$app" dist/WorkTwin-notary.zip
  python desktop/notarize-macos.py dist/WorkTwin-notary.zip "$app"
  rm -f dist/WorkTwin-notary.zip dist/WorkTwin-notary.zip.notary.json
fi

mkdir -p release
suffix="-unsigned"
if [[ "$formal" == 1 ]]; then
  suffix=""
fi
dmg="release/WorkTwin-Collector-1.2.0-macOS-$(uname -m)${suffix}.dmg"
# macOS Intel GitHub runners occasionally leave DiskImages busy at the
# first attempt. Retry only this specific observed hdiutil failure once.
if output=$(hdiutil create -volname 'WorkTwin Collector' -srcfolder "$app" -ov -format UDZO "$dmg" 2>&1); then
  echo "$output"
elif [[ "$output" == *"Resource busy"* ]]; then
  echo "$output" >&2
  echo "DiskImages is busy; retrying DMG creation once" >&2
  sleep 5
  hdiutil create -volname 'WorkTwin Collector' -srcfolder "$app" -ov -format UDZO "$dmg"
else
  echo "$output" >&2
  exit 1
fi
hdiutil verify "$dmg"

if [[ "$formal" == 1 ]]; then
  codesign --force --sign "$WORKTWIN_CODESIGN_IDENTITY" --timestamp "$dmg"
  python desktop/notarize-macos.py "$dmg" "$dmg"
  codesign --verify --strict --verbose=2 "$dmg"
  echo "Created Developer ID signed and notarized macOS DMG: $dmg"
else
  echo "::warning::Created ad-hoc signed DMG: $dmg"
  echo "::warning::Chrome-downloaded apps remain blocked by macOS Gatekeeper without a trusted Developer ID and Apple notarization."
fi
