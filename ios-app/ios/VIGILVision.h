//
//  VIGILVision.h
//  VIGILApp
//
//  On-device body pose estimation using Apple Vision framework.
//  Replaces the Jetson camera pipeline entirely — no external hardware needed.
//
//  Computes in real-time using the iPhone's Neural Engine (A12+):
//    - Gait cadence (steps per minute)
//    - Gait asymmetry (left/right timing ratio)
//    - Estimated gait speed (normalised to body height)
//    - Posture score (forward head, shoulder symmetry, trunk lean)
//    - Fall detection (rapid hip descent + confirmed low position)
//
//  Usage from React Native:
//    import { NativeModules, NativeEventEmitter } from 'react-native';
//    const { VIGILVision } = NativeModules;
//
//    // Start monitoring
//    VIGILVision.startSession();
//
//    // Listen for metric updates (fires every ~2 seconds)
//    const emitter = new NativeEventEmitter(VIGILVision);
//    emitter.addListener('vigilVisionMetrics', (metrics) => {
//      console.log(metrics.cadenceSPM, metrics.asymmetryPct, metrics.postureScore);
//    });
//    emitter.addListener('vigilVisionFall', (event) => {
//      console.log('FALL DETECTED', event.hipDropPct, event.torsoAngle);
//    });
//
//    // Stop monitoring
//    VIGILVision.stopSession();
//

#import <React/RCTBridgeModule.h>
#import <React/RCTEventEmitter.h>

@interface VIGILVision : RCTEventEmitter <RCTBridgeModule>

@end
