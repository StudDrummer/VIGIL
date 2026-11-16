#!/bin/bash
# Build VIGIL into an iOS Simulator and launch it (Metro must be running: npx react-native start).
# Xcode 27 has no Simulator.app; the simulator shows in Xcode > Open Developer Tool > Device Hub.
cd "$(dirname "$0")/.." || exit 1
UDID="$(xcrun simctl list devices available | grep -m1 -E 'iPhone 1[5-9] Pro \(' | grep -oE '[0-9A-F-]{36}')"
[ -z "$UDID" ] && UDID="$(xcrun simctl list devices available | grep -m1 'iPhone' | grep -oE '[0-9A-F-]{36}')"
[ -z "$UDID" ] && { echo "No iPhone simulator found. Run: xcodebuild -downloadPlatform iOS"; exit 1; }
echo "Simulator: $UDID"
xcrun simctl boot "$UDID" 2>/dev/null
open -a "Device Hub" 2>/dev/null || open -a Simulator 2>/dev/null
echo "Building (first time takes a few minutes)..."
xcodebuild -workspace ios/VIGILApp.xcworkspace -scheme VIGILApp -configuration Debug \
  -destination "id=$UDID" -derivedDataPath ios/build-sim CODE_SIGNING_ALLOWED=NO -quiet || { echo "BUILD FAILED"; exit 1; }
APP="ios/build-sim/Build/Products/Debug-iphonesimulator/VIGILApp.app"
xcrun simctl install "$UDID" "$APP" && xcrun simctl launch "$UDID" com.rushilmohan.vigilapp && echo "DONE: VIGIL is running in the Simulator"
