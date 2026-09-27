#!/bin/zsh
# Builds YouTube Zeus and installs it in /Applications.
#   ./scripts/build.sh            build Release and install
#   ./scripts/build.sh --no-install
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
mkdir -p build
xcodegen generate --quiet
xcodebuild -project YouTubeZeus.xcodeproj -scheme YouTubeZeus -configuration Release \
  -derivedDataPath build/DerivedData -destination 'platform=macOS' build 2>&1 | tee build/last-build.log | \
  grep -E "error:|warning: .*(deprecated|unused)|BUILD (SUCCEEDED|FAILED)" || true
APP="build/DerivedData/Build/Products/Release/YouTube Zeus.app"
[[ -d "$APP" ]] || { echo "Build failed - see build/last-build.log"; exit 1; }
if [[ "${1:-}" != "--no-install" ]]; then
  pkill -x "YouTube Zeus" 2>/dev/null && sleep 1 || true
  rm -rf "/Applications/YouTube Zeus.app"
  ditto "$APP" "/Applications/YouTube Zeus.app"
  echo "Installed /Applications/YouTube Zeus.app"
fi
