#!/bin/bash
# Run VIGIL in an iOS Simulator. One command does everything:
#   bash tools/sim.sh          start Metro if needed, boot the simulator, build if needed, launch
#   bash tools/sim.sh --build  force a native rebuild (after changing anything under ios/)
# Xcode 27 has no Simulator.app; the simulator shows in Xcode > Open Developer Tool > Device Hub.
cd "$(dirname "$0")/.." || exit 1
BUNDLE=com.rushilmohan.vigilapp
APP="ios/build-sim/Build/Products/Debug-iphonesimulator/VIGILApp.app"

UDID="$(xcrun simctl list devices booted | grep -m1 'iPhone' | grep -oE '[0-9A-F-]{36}')"
[ -z "$UDID" ] && UDID="$(xcrun simctl list devices available | grep -m1 -E 'iPhone 1[5-9] Pro \(' | grep -oE '[0-9A-F-]{36}')"
[ -z "$UDID" ] && UDID="$(xcrun simctl list devices available | grep -m1 'iPhone' | grep -oE '[0-9A-F-]{36}')"
[ -z "$UDID" ] && { echo "No iPhone simulator found. Run: xcodebuild -downloadPlatform iOS"; exit 1; }
echo "Simulator: $UDID"
xcrun simctl boot "$UDID" 2>/dev/null
open -a "Device Hub" 2>/dev/null || open -a Simulator 2>/dev/null

if ! lsof -i :8081 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Starting Metro (log: /tmp/vigil-metro.log)..."
  nohup npx react-native start >/tmp/vigil-metro.log 2>&1 &
  for _ in $(seq 1 30); do lsof -i :8081 -sTCP:LISTEN >/dev/null 2>&1 && break; sleep 1; done
fi

if [ "$1" = "--build" ] || [ ! -d "$APP" ]; then
  echo "Building (the first time takes a few minutes)..."
  xcodebuild -workspace ios/VIGILApp.xcworkspace -scheme VIGILApp -configuration Debug \
    -destination "id=$UDID" -derivedDataPath ios/build-sim CODE_SIGNING_ALLOWED=NO -quiet || { echo "BUILD FAILED"; exit 1; }
fi
xcrun simctl install "$UDID" "$APP" || exit 1
xcrun simctl terminate "$UDID" "$BUNDLE" 2>/dev/null
xcrun simctl launch "$UDID" "$BUNDLE" && echo "DONE: VIGIL is running in the Simulator"
