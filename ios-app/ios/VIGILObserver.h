//
//  VIGILObserver.h
//  VIGILApp
//
//  Background HKObserverQuery module — wakes the app when new HealthKit
//  data arrives even when the app is in the background or phone is locked.
//
//  Setup:
//    1. Add this .h and VIGILObserver.m to Xcode under VIGILApp/
//    2. In Info.plist add:
//         Key: NSHealthUpdateUsageDescription (already present)
//         Key: UIBackgroundModes → array → add "fetch" and "health-research"
//    3. In Xcode Signing & Capabilities → add "Background Modes"
//         Check: Background fetch
//         Check: HealthKit Background Delivery (if available)
//    4. In AppDelegate.mm add:
//         #import "VIGILObserver.h"
//         // In application:didFinishLaunchingWithOptions:
//         [VIGILObserver enableBackgroundDelivery];
//

#import <React/RCTBridgeModule.h>
#import <React/RCTEventEmitter.h>

@interface VIGILObserver : RCTEventEmitter <RCTBridgeModule>

/// Call once from AppDelegate after HealthKit is authorised.
/// Registers background delivery for key metrics.
+ (void)enableBackgroundDelivery;

@end
