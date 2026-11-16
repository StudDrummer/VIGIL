# VIGIL iOS app

React Native 0.74, one screen file (`App.tsx`) plus a few native modules under `ios/`.

## Build it

```
npm install
cd ios && bundle install && bundle exec pod install && cd ..
bash tools/sim.sh            # runs it in an iPhone simulator (starts Metro too)
```

For a phone or TestFlight, open `ios/VIGILApp.xcworkspace` in Xcode, choose
your own team under Signing, then Product > Archive.

## Before it will talk to a monitoring unit

Two placeholders are deliberately left in this public copy:

| Where | Placeholder | Replace with |
|---|---|---|
| `App.tsx` | `your-jetson-ip`, `YOUR_VIGIL_API_KEY` | the unit's address and its API key (local mode only; accounts need neither) |
| `ios/VIGILApp/Info.plist` | `your-jetson-ip` | the same address, so iOS allows plain HTTP to it on the home network |

The demo (Welcome > Try the demo) needs neither.

## Native modules

| File | What it does |
|---|---|
| `ios/VIGILApp/AppDelegate.mm` | App start, push notification token (`VIGILPush`) |
| `ios/VIGILHotspot.m` | Joins the relay box's setup Wi-Fi during box setup |
| `ios/VIGILObserver.*`, `ios/VIGILWalking.*`, `ios/VIGILApp/WalkingMetrics.*` | Apple Health background delivery and walking measures |
| `ios/VIGILVision.*` | An earlier on-phone pose experiment (Apple Vision); not used by the current screens |

`tools/check_app_ready.py` checks the things that most often cause an App
Store rejection (usage strings, export compliance, icon, demo coverage).
When the build number changes in Xcode, change `APP_VERSION` in `App.tsx` too.
