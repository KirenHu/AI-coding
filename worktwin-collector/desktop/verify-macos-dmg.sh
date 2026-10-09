#!/bin/zsh
# Test the actual shipped DMG, not only PyInstaller's build directory.
set -euo pipefail
cd "$(dirname "$0")/.."

images=(release/WorkTwin-Collector-1.1.5-macOS-$(uname -m)*.dmg)
if (( ${#images[@]} != 1 )); then
  echo "Expected one macOS DMG for $(uname -m), got ${#images[@]}" >&2
  exit 1
fi
dmg="${images[1]}"
hdiutil verify "$dmg"

temp="$(mktemp -d "${TMPDIR:-/tmp/}worktwin-dmg-XXXXXX")"
mount="$temp/mount"
mkdir -p "$mount"
attached=0
cleanup() {
  if (( attached )); then hdiutil detach "$mount" -quiet || true; fi
  rm -rf "$temp"
}
trap cleanup EXIT

hdiutil attach -readonly -nobrowse -quiet -mountpoint "$mount" "$dmg"
attached=1
app="$mount/WorkTwin.app"
test -f "$app/Contents/Info.plist"
test -x "$app/Contents/MacOS/WorkTwin"
test -s "$app/Contents/Resources/third_party/rowboat/LICENSE"
test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist")" = "io.github.kirenhu.worktwin"
codesign --verify --deep --strict --all-architectures --verbose=2 "$app"

# Finder users copy the app to Applications; exercise that file layout too.
# The smoke test starts the frozen binary, writes to a temporary SQLite DB,
# serves the real dashboard, and shuts down without touching employee data.
mkdir -p "$temp/Applications"
ditto "$app" "$temp/Applications/WorkTwin.app"
staged="$temp/Applications/WorkTwin.app"
codesign --verify --deep --strict --all-architectures --verbose=2 "$staged"
python scripts/smoke_desktop.py "$staged/Contents/MacOS/WorkTwin"

# Simulate a browser-downloaded app. Ad-hoc signing verifies binary integrity
# but does not confer Developer ID trust; be explicit rather than claiming that
# an unsigned DMG passes the Finder/Gatekeeper install path.
xattr -w com.apple.quarantine "0081;00000000;WorkTwin-CI;" "$staged"
if spctl --assess --type execute --verbose=3 "$staged"; then
  echo "Gatekeeper assessment: accepted"
else
  if [[ -n "${WORKTWIN_CODESIGN_IDENTITY:-}" ]]; then
    echo "::error::Developer ID build failed Gatekeeper assessment"
    exit 1
  fi
  echo "::warning::Gatekeeper assessment: rejected (expected for unsigned, ad-hoc signed build)."
  echo "::warning::Requires Developer ID signing and Apple notarization for ordinary browser-downloaded installation."
fi

echo "PASS: DMG checksum/container, mounted app, signature, copied app and native runtime"
