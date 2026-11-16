//
//  VIGILVision.m
//  VIGILApp
//
//  On-device body pose, gait, posture, and fall detection.
//  Uses AVFoundation (camera) + Vision (pose estimation) + CoreMotion (accelerometer).
//
//  Add to Xcode:
//    1. Drag VIGILVision.h and VIGILVision.m into VIGILApp/ in Xcode
//    2. In Info.plist add:
//         NSCameraUsageDescription → "VIGIL uses the camera to analyse your gait and posture"
//    3. In Podfile (already there if react-native-svg is installed):
//         No new pods needed — Vision and AVFoundation are system frameworks
//

#import "VIGILVision.h"
#import <AVFoundation/AVFoundation.h>
#import <Vision/Vision.h>
#import <CoreMotion/CoreMotion.h>
#import <UIKit/UIKit.h>

// ── Gait analysis constants ──────────────────────────────────
static const NSInteger kGaitWindowFrames   = 90;    // 3s at 30fps
static const NSInteger kMinPeaksForCadence = 3;
static const double    kMetricsEmitInterval = 2.0;  // emit JS event every 2s
static const double    kMinCadenceSPM       = 25.0; // below this = not walking

// ── Fall detection constants ─────────────────────────────────
static const double kFallDescentVelThresh  = 0.35;  // hip drop/sec as fraction of body height
static const double kFallConfirmSeconds    = 1.8;   // must stay low this long to confirm
static const double kFallRecoverSeconds    = 4.0;   // cooldown after a fall

// ── Posture thresholds ────────────────────────────────────────
static const double kForwardHeadWarnDeg    = 15.0;
static const double kShoulderAsymWarnFrac  = 0.05;  // fraction of body height
static const double kTrunkLeanWarnDeg      = 8.0;

// ── Vision joint names ────────────────────────────────────────
// VNHumanBodyPoseObservationJointName constants
// Available: root, neck, leftShoulder, rightShoulder, leftElbow, rightElbow,
//            leftWrist, rightWrist, leftHip, rightHip, leftKnee, rightKnee,
//            leftAnkle, rightAnkle, head, leftEar, rightEar, leftEye, rightEye

// ─────────────────────────────────────────────────────────────
// Fall state machine
// ─────────────────────────────────────────────────────────────
typedef NS_ENUM(NSInteger, VIGILFallState) {
    VIGILFallStateUpright    = 0,
    VIGILFallStateDescending = 1,
    VIGILFallStateFallen     = 2,
    VIGILFallStateRecovering = 3,
};


@interface VIGILVision () <AVCaptureVideoDataOutputSampleBufferDelegate>

// AVFoundation
@property (nonatomic, strong) AVCaptureSession           *captureSession;
@property (nonatomic, strong) AVCaptureVideoDataOutput   *videoOutput;
@property (nonatomic, strong) dispatch_queue_t            captureQueue;

// Vision
@property (nonatomic, strong) VNDetectHumanBodyPoseRequest *poseRequest;
@property (nonatomic, strong) dispatch_queue_t             visionQueue;

// Gait state
@property (nonatomic, strong) NSMutableArray<NSNumber *> *leftAnkleYHistory;
@property (nonatomic, strong) NSMutableArray<NSNumber *> *rightAnkleYHistory;
@property (nonatomic, strong) NSMutableArray<NSNumber *> *hipXHistory;
@property (nonatomic, strong) NSMutableArray<NSNumber *> *hipYHistory;
@property (nonatomic, assign) CGFloat                    lastBodyHeight;    // px

// Fall state machine
@property (nonatomic, assign) VIGILFallState              fallState;
@property (nonatomic, assign) NSTimeInterval              fallStateEntered;
@property (nonatomic, assign) CGFloat                     fallDescentStartY;
@property (nonatomic, assign) CGFloat                     prevHipY;
@property (nonatomic, assign) NSTimeInterval              prevHipTime;

// Metrics emission
@property (nonatomic, assign) NSTimeInterval              lastEmitTime;
@property (nonatomic, assign) BOOL                        hasListeners;
@property (nonatomic, assign) BOOL                        sessionRunning;

@end


@implementation VIGILVision

RCT_EXPORT_MODULE();

// ── React Native bridge setup ────────────────────────────────

- (instancetype)init {
    if (self = [super init]) {
        _captureQueue     = dispatch_queue_create("com.vigil.capture",  DISPATCH_QUEUE_SERIAL);
        _visionQueue      = dispatch_queue_create("com.vigil.vision",   DISPATCH_QUEUE_SERIAL);
        _leftAnkleYHistory  = [NSMutableArray new];
        _rightAnkleYHistory = [NSMutableArray new];
        _hipXHistory        = [NSMutableArray new];
        _hipYHistory        = [NSMutableArray new];
        _fallState          = VIGILFallStateUpright;
        _fallStateEntered   = 0;
        _fallDescentStartY  = 0;
        _prevHipY           = 0;
        _prevHipTime        = 0;
        _lastEmitTime       = 0;
        _hasListeners       = NO;
        _sessionRunning     = NO;
        _lastBodyHeight     = 0;
    }
    return self;
}

- (NSArray<NSString *> *)supportedEvents {
    return @[@"vigilVisionMetrics", @"vigilVisionFall", @"vigilVisionState"];
}

- (void)startObserving  { self.hasListeners = YES;  }
- (void)stopObserving   { self.hasListeners = NO;   }

+ (BOOL)requiresMainQueueSetup { return NO; }


// ── JS API ───────────────────────────────────────────────────

RCT_EXPORT_METHOD(startSession:(RCTPromiseResolveBlock)resolve
                  reject:(RCTPromiseRejectBlock)reject) {
    if (self.sessionRunning) {
        resolve(@{@"status": @"already_running"});
        return;
    }

    // Check camera permission
    AVAuthorizationStatus status = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
    if (status == AVAuthorizationStatusDenied || status == AVAuthorizationStatusRestricted) {
        reject(@"permission", @"Camera permission denied. Enable in Settings.", nil);
        return;
    }

    if (status == AVAuthorizationStatusNotDetermined) {
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL granted) {
            if (granted) {
                [self _startCaptureSession];
                resolve(@{@"status": @"started"});
            } else {
                reject(@"permission", @"Camera permission denied.", nil);
            }
        }];
        return;
    }

    [self _startCaptureSession];
    resolve(@{@"status": @"started"});
}

RCT_EXPORT_METHOD(stopSession:(RCTPromiseResolveBlock)resolve
                  reject:(RCTPromiseRejectBlock)reject) {
    [self _stopCaptureSession];
    resolve(@{@"status": @"stopped"});
}

RCT_EXPORT_METHOD(getSessionState:(RCTPromiseResolveBlock)resolve
                  reject:(RCTPromiseRejectBlock)reject) {
    resolve(@{@"running": @(self.sessionRunning)});
}


// ── Camera session ───────────────────────────────────────────

- (void)_startCaptureSession {
    dispatch_async(self.captureQueue, ^{
        self.captureSession = [[AVCaptureSession alloc] init];
        self.captureSession.sessionPreset = AVCaptureSessionPreset640x480;

        // Use front camera for selfie-style monitoring, back for room monitoring
        // Start with back camera (better for room/fall monitoring)
        AVCaptureDevice *device = [AVCaptureDevice defaultDeviceWithDeviceType:AVCaptureDeviceTypeBuiltInWideAngleCamera
                                                                     mediaType:AVMediaTypeVideo
                                                                      position:AVCaptureDevicePositionBack];
        if (!device) {
            NSLog(@"[VIGILVision] No camera found");
            return;
        }

        NSError *error = nil;
        AVCaptureDeviceInput *input = [AVCaptureDeviceInput deviceInputWithDevice:device error:&error];
        if (error || !input) {
            NSLog(@"[VIGILVision] Camera input error: %@", error);
            return;
        }

        self.videoOutput = [[AVCaptureVideoDataOutput alloc] init];
        self.videoOutput.videoSettings = @{
            (id)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_420YpCbCr8BiPlanarFullRange)
        };
        self.videoOutput.alwaysDiscardsLateVideoFrames = YES;
        [self.videoOutput setSampleBufferDelegate:self queue:self.captureQueue];

        [self.captureSession beginConfiguration];
        if ([self.captureSession canAddInput:input])   [self.captureSession addInput:input];
        if ([self.captureSession canAddOutput:self.videoOutput]) [self.captureSession addOutput:self.videoOutput];
        [self.captureSession commitConfiguration];

        // Set frame rate to 15fps — enough for gait, saves battery
        [device lockForConfiguration:nil];
        device.activeVideoMinFrameDuration = CMTimeMake(1, 15);
        device.activeVideoMaxFrameDuration = CMTimeMake(1, 15);
        [device unlockForConfiguration];

        // Vision request
        self.poseRequest = [[VNDetectHumanBodyPoseRequest alloc] init];

        self.sessionRunning = YES;
        [self.captureSession startRunning];
        NSLog(@"[VIGILVision] Session started at 15fps");

        if (self.hasListeners) {
            [self sendEventWithName:@"vigilVisionState" body:@{@"running": @YES}];
        }
    });
}

- (void)_stopCaptureSession {
    dispatch_async(self.captureQueue, ^{
        if (self.captureSession.isRunning) {
            [self.captureSession stopRunning];
        }
        self.captureSession = nil;
        self.videoOutput    = nil;
        self.sessionRunning = NO;
        NSLog(@"[VIGILVision] Session stopped");

        if (self.hasListeners) {
            [self sendEventWithName:@"vigilVisionState" body:@{@"running": @NO}];
        }
    });
}


// ── Frame processing ─────────────────────────────────────────

- (void)captureOutput:(AVCaptureOutput *)output
didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
       fromConnection:(AVCaptureConnection *)connection {

    CVImageBufferRef imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer);
    if (!imageBuffer) return;

    NSTimeInterval now = CACurrentMediaTime();

    // Run Vision pose request
    NSError *error = nil;
    VNImageRequestHandler *handler = [[VNImageRequestHandler alloc]
        initWithCVPixelBuffer:imageBuffer
                  orientation:kCGImagePropertyOrientationRight   // portrait
                      options:@{}];

    [handler performRequests:@[self.poseRequest] error:&error];
    if (error) return;

    VNHumanBodyPoseObservation *obs = self.poseRequest.results.firstObject;
    if (!obs) return;

    // Extract joints
    CGPoint hipL     = [self _point:obs joint:VNHumanBodyPoseObservationJointNameLeftHip];
    CGPoint hipR     = [self _point:obs joint:VNHumanBodyPoseObservationJointNameRightHip];
    CGPoint ankleL   = [self _point:obs joint:VNHumanBodyPoseObservationJointNameLeftAnkle];
    CGPoint ankleR   = [self _point:obs joint:VNHumanBodyPoseObservationJointNameRightAnkle];
    CGPoint shoulderL= [self _point:obs joint:VNHumanBodyPoseObservationJointNameLeftShoulder];
    CGPoint shoulderR= [self _point:obs joint:VNHumanBodyPoseObservationJointNameRightShoulder];
    CGPoint neck     = [self _point:obs joint:VNHumanBodyPoseObservationJointNameNeck];
    CGPoint head     = [self _point:obs joint:VNHumanBodyPoseObservationJointNameNose];

    // Confidence filter — Vision gives normalised coords (0-1) with confidence in observation
    // We use presence check; CGPointEqualToPoint(pt, CGPointZero) means joint not detected
    BOOL hipsVisible     = !CGPointEqualToPoint(hipL, CGPointZero) && !CGPointEqualToPoint(hipR, CGPointZero);
    BOOL anklesVisible   = !CGPointEqualToPoint(ankleL, CGPointZero) || !CGPointEqualToPoint(ankleR, CGPointZero);
    BOOL shouldersVisible= !CGPointEqualToPoint(shoulderL, CGPointZero) && !CGPointEqualToPoint(shoulderR, CGPointZero);

    if (!hipsVisible) return;

    // Midpoints (Vision coords: 0,0 = bottom-left, 1,1 = top-right)
    CGPoint hipMid = CGPointMake((hipL.x + hipR.x) / 2.0, (hipL.y + hipR.y) / 2.0);

    // Body height estimate (shoulder to ankle in normalised coords)
    CGFloat bodyHeight = 0;
    if (shouldersVisible && anklesVisible) {
        CGPoint shldMid = CGPointMake((shoulderL.x+shoulderR.x)/2.0, (shoulderL.y+shoulderR.y)/2.0);
        CGPoint ankleMid = !CGPointEqualToPoint(ankleL, CGPointZero) ? ankleL : ankleR;
        bodyHeight = fabs(shldMid.y - ankleMid.y);
        if (bodyHeight > 0.05) self.lastBodyHeight = bodyHeight;
    }
    CGFloat bh = self.lastBodyHeight > 0.01 ? self.lastBodyHeight : 0.5;

    // ── Gait history ──────────────────────────────────────────
    if (!CGPointEqualToPoint(ankleL, CGPointZero)) {
        [self.leftAnkleYHistory  addObject:@(ankleL.y)];
    }
    if (!CGPointEqualToPoint(ankleR, CGPointZero)) {
        [self.rightAnkleYHistory addObject:@(ankleR.y)];
    }
    [self.hipXHistory addObject:@(hipMid.x)];
    [self.hipYHistory addObject:@(hipMid.y)];

    // Trim history to window
    while (self.leftAnkleYHistory.count  > kGaitWindowFrames) [self.leftAnkleYHistory  removeObjectAtIndex:0];
    while (self.rightAnkleYHistory.count > kGaitWindowFrames) [self.rightAnkleYHistory removeObjectAtIndex:0];
    while (self.hipXHistory.count        > kGaitWindowFrames) [self.hipXHistory        removeObjectAtIndex:0];
    while (self.hipYHistory.count        > kGaitWindowFrames) [self.hipYHistory        removeObjectAtIndex:0];

    // ── Fall detection ────────────────────────────────────────
    CGFloat hipY = hipMid.y;  // 0=bottom 1=top in Vision coords

    // Hip vertical velocity (fraction of body height per second)
    CGFloat hipVelY = 0;
    if (self.prevHipTime > 0 && bh > 0) {
        double dt = now - self.prevHipTime;
        // In Vision coords, Y decreases as person falls (moving toward floor = lower Y value)
        hipVelY = (self.prevHipY - hipY) / dt / bh;
    }
    self.prevHipY   = hipY;
    self.prevHipTime = now;

    // Trunk angle for fall context
    CGFloat trunkAngle = 0;
    if (shouldersVisible) {
        CGPoint shldMid = CGPointMake((shoulderL.x+shoulderR.x)/2.0, (shoulderL.y+shoulderR.y)/2.0);
        trunkAngle = [self _angleFromVertical:hipMid to:shldMid];
    }

    NSString *fallEvent = [self _updateFallFSM:hipY hipVelY:hipVelY bodyHeight:bh trunkAngle:trunkAngle now:now];

    // ── Periodic metrics emission ─────────────────────────────
    if (now - self.lastEmitTime >= kMetricsEmitInterval) {
        self.lastEmitTime = now;
        NSDictionary *metrics = [self _computeMetrics:obs shoulderL:shoulderL shoulderR:shoulderR neck:neck head:head hipMid:hipMid bh:bh];

        if (self.hasListeners && metrics) {
            [self sendEventWithName:@"vigilVisionMetrics" body:metrics];
        }
    }

    // Emit fall event if detected
    if ([fallEvent isEqualToString:@"fall_detected"] && self.hasListeners) {
        CGFloat dropPct = (self.fallDescentStartY > 0) ? fabs(self.fallDescentStartY - hipY) / bh : 0;
        [self sendEventWithName:@"vigilVisionFall" body:@{
            @"detectedAt":  @([[NSDate date] timeIntervalSince1970] * 1000),
            @"hipDropPct":  @(dropPct),
            @"torsoAngle":  @(trunkAngle),
        }];
    }
}


// ── Metrics computation ───────────────────────────────────────

- (NSDictionary *)_computeMetrics:(VNHumanBodyPoseObservation *)obs
                        shoulderL:(CGPoint)shoulderL
                        shoulderR:(CGPoint)shoulderR
                              neck:(CGPoint)neck
                              head:(CGPoint)head
                           hipMid:(CGPoint)hipMid
                               bh:(CGFloat)bh {

    NSMutableDictionary *m = [NSMutableDictionary new];

    // ── Cadence ───────────────────────────────────────────────
    NSNumber *cadence = [self _computeCadence];
    if (cadence) m[@"cadenceSPM"] = cadence;

    // ── Walking / not walking ─────────────────────────────────
    BOOL isWalking = cadence && cadence.doubleValue > kMinCadenceSPM;
    m[@"isWalking"] = @(isWalking);

    // ── Gait speed (normalised to body height per stride) ─────
    if (isWalking && self.hipXHistory.count >= 10 && bh > 0.05) {
        double firstX = self.hipXHistory.firstObject.doubleValue;
        double lastX  = self.hipXHistory.lastObject.doubleValue;
        double spanNorm = fabs(lastX - firstX) / bh;  // body heights of horizontal travel
        double secs     = self.hipXHistory.count / 15.0;  // at 15fps
        double speedNorm = spanNorm / secs;             // body-heights per second
        // Convert to m/s: average human height = 1.7m
        // speedNorm × 1.7 ≈ m/s (rough, calibration-independent)
        double speedMPS  = speedNorm * 1.7;
        m[@"walkingSpeedMPS"]  = @(round(speedMPS * 100.0) / 100.0);
        m[@"walkingSpeedNorm"] = @(round(speedNorm * 1000.0) / 1000.0);
    }

    // ── Gait asymmetry ────────────────────────────────────────
    NSNumber *asymmetry = [self _computeAsymmetry];
    if (asymmetry) m[@"asymmetryPct"] = asymmetry;

    // ── Posture ───────────────────────────────────────────────
    if (!CGPointEqualToPoint(shoulderL, CGPointZero) && !CGPointEqualToPoint(shoulderR, CGPointZero)) {
        CGPoint shldMid = CGPointMake((shoulderL.x+shoulderR.x)/2.0, (shoulderL.y+shoulderR.y)/2.0);

        // Forward head posture: angle of neck→head from vertical
        if (!CGPointEqualToPoint(neck, CGPointZero) && !CGPointEqualToPoint(head, CGPointZero)) {
            double neckAngle = [self _angleFromVertical:neck to:head];
            m[@"neckAngleDeg"]       = @(round(neckAngle * 10.0) / 10.0);
            m[@"forwardHeadFlag"]    = @(neckAngle > kForwardHeadWarnDeg);
        }

        // Shoulder asymmetry: height diff normalised by body height
        double shoulderAsymFrac = fabs(shoulderL.y - shoulderR.y) / bh;
        m[@"shoulderAsymPct"]    = @(round(shoulderAsymFrac * 1000.0) / 10.0);  // as %
        m[@"shoulderAsymFlag"]   = @(shoulderAsymFrac > kShoulderAsymWarnFrac);

        // Trunk lateral lean: shoulder midpoint to hip midpoint from vertical
        double trunkLean = [self _angleFromVertical:hipMid to:shldMid];
        m[@"trunkLeanDeg"]       = @(round(trunkLean * 10.0) / 10.0);
        m[@"trunkLeanFlag"]      = @(trunkLean > kTrunkLeanWarnDeg);

        // Composite posture score 0-100
        double score = 0;
        if (m[@"neckAngleDeg"])    score += MIN(40.0, [m[@"neckAngleDeg"] doubleValue]    * (40.0/30.0));
        if (m[@"shoulderAsymPct"]) score += MIN(30.0, [m[@"shoulderAsymPct"] doubleValue] * (30.0/10.0));
        if (m[@"trunkLeanDeg"])    score += MIN(30.0, [m[@"trunkLeanDeg"] doubleValue]     * (30.0/15.0));
        m[@"postureScore"]  = @(round(MIN(100.0, score)));
        m[@"postureFlag"]   = @(score > 50.0);
    }

    // ── Fall state ────────────────────────────────────────────
    m[@"fallState"] = @(self.fallState);

    return m.count > 1 ? m : nil;
}


// ── Gait analysis helpers ─────────────────────────────────────

- (NSNumber *)_computeCadence {
    if (self.leftAnkleYHistory.count < 15) return nil;

    NSArray *arr = [self.leftAnkleYHistory copy];
    NSInteger n  = arr.count;

    // Find min/max for threshold
    double mn = [arr[0] doubleValue], mx = [arr[0] doubleValue];
    for (NSNumber *v in arr) {
        mn = MIN(mn, v.doubleValue);
        mx = MAX(mx, v.doubleValue);
    }
    double span = mx - mn;
    if (span < 0.01) return nil;  // no movement
    double threshold = mn + span * 0.3;

    // Simple peak detection: local maxima above threshold
    NSMutableArray<NSNumber *> *peaks = [NSMutableArray new];
    NSInteger minDist = 4;  // at 15fps, min 4 frames between peaks (~250ms)
    for (NSInteger i = 1; i < n - 1; i++) {
        double prev = [arr[i-1] doubleValue];
        double curr = [arr[i]   doubleValue];
        double next = [arr[i+1] doubleValue];
        if (curr > prev && curr > next && curr > threshold) {
            if (peaks.count == 0 || (i - peaks.lastObject.integerValue) >= minDist) {
                [peaks addObject:@(i)];
            }
        }
    }

    if (peaks.count < kMinPeaksForCadence) return nil;

    // Average interval between peaks
    double totalGap = 0;
    for (NSInteger i = 1; i < (NSInteger)peaks.count; i++) {
        totalGap += (peaks[i].doubleValue - peaks[i-1].doubleValue);
    }
    double avgFrames    = totalGap / (peaks.count - 1);
    double stepsPerSec  = 15.0 / avgFrames;  // 15fps
    double cadenceSPM   = stepsPerSec * 60.0;

    return cadenceSPM > 0 ? @(round(cadenceSPM * 10.0) / 10.0) : nil;
}


- (NSNumber *)_computeAsymmetry {
    if (self.leftAnkleYHistory.count < 15 || self.rightAnkleYHistory.count < 15) return nil;

    // Get step intervals for each foot
    NSArray *left  = [self.leftAnkleYHistory  copy];
    NSArray *right = [self.rightAnkleYHistory copy];

    double lInterval = [self _meanPeakInterval:left];
    double rInterval = [self _meanPeakInterval:right];

    if (lInterval <= 0 || rInterval <= 0) return nil;

    double total    = lInterval + rInterval;
    double asymFrac = fabs(lInterval - rInterval) / total;
    double asymPct  = asymFrac * 100.0;

    return @(round(asymPct * 10.0) / 10.0);
}


- (double)_meanPeakInterval:(NSArray<NSNumber *> *)arr {
    if (arr.count < 10) return -1;
    NSInteger n  = arr.count;
    double mn = [arr[0] doubleValue], mx = [arr[0] doubleValue];
    for (NSNumber *v in arr) { mn = MIN(mn, v.doubleValue); mx = MAX(mx, v.doubleValue); }
    if (mx - mn < 0.01) return -1;
    double threshold = mn + (mx - mn) * 0.3;

    NSMutableArray<NSNumber *> *peaks = [NSMutableArray new];
    for (NSInteger i = 1; i < n - 1; i++) {
        double prev = [arr[i-1] doubleValue], curr = [arr[i] doubleValue], next = [arr[i+1] doubleValue];
        if (curr > prev && curr > next && curr > threshold) {
            if (peaks.count == 0 || (i - peaks.lastObject.integerValue) >= 4)
                [peaks addObject:@(i)];
        }
    }
    if (peaks.count < 2) return -1;
    double total = 0;
    for (NSInteger i = 1; i < (NSInteger)peaks.count; i++) total += (peaks[i].doubleValue - peaks[i-1].doubleValue);
    return total / (peaks.count - 1);
}


// ── Fall state machine ────────────────────────────────────────

- (NSString *)_updateFallFSM:(CGFloat)hipY
                     hipVelY:(CGFloat)hipVelY
                  bodyHeight:(CGFloat)bh
                  trunkAngle:(CGFloat)trunkAngle
                         now:(NSTimeInterval)now {

    switch (self.fallState) {
        case VIGILFallStateUpright: {
            // Rapid downward movement → enter DESCENDING
            // prevHipY - hipY is positive when hip moves downward (Y decreases in Vision coords)
            if (hipVelY > kFallDescentVelThresh) {
                self.fallState        = VIGILFallStateDescending;
                self.fallStateEntered = now;
                self.fallDescentStartY = hipY;
                NSLog(@"[VIGILVision] UPRIGHT → DESCENDING  vel=%.2f", hipVelY);
            }
            break;
        }
        case VIGILFallStateDescending: {
            double elapsed = now - self.fallStateEntered;
            double dropFrac = fabs(self.fallDescentStartY - hipY) / bh;

            if (elapsed > kFallConfirmSeconds) {
                if (dropFrac > 0.30) {
                    // Stayed low — genuine fall
                    self.fallState        = VIGILFallStateFallen;
                    self.fallStateEntered = now;
                    NSLog(@"[VIGILVision] DESCENDING → FALLEN  drop=%.0f%%", dropFrac*100);
                    return @"fall_detected";
                } else {
                    // Recovered — was bending down
                    self.fallState        = VIGILFallStateUpright;
                    self.fallStateEntered = now;
                    NSLog(@"[VIGILVision] DESCENDING → UPRIGHT (recovered)");
                }
            }
            break;
        }
        case VIGILFallStateFallen: {
            if (now - self.fallStateEntered > 1.0) {
                self.fallState        = VIGILFallStateRecovering;
                self.fallStateEntered = now;
            }
            // fall_detected already emitted on entry (DESCENDING→FALLEN); stay silent here
            break;
        }
        case VIGILFallStateRecovering: {
            if (now - self.fallStateEntered > kFallRecoverSeconds) {
                self.fallState        = VIGILFallStateUpright;
                self.fallStateEntered = now;
                NSLog(@"[VIGILVision] RECOVERING → UPRIGHT");
            }
            break;
        }
    }
    return @"normal";
}


// ── Geometry helpers ──────────────────────────────────────────

/// Extract a joint point from a pose observation, returns CGPointZero if not confident
- (CGPoint)_point:(VNHumanBodyPoseObservation *)obs
            joint:(VNHumanBodyPoseObservationJointName)joint {
    NSError *err = nil;
    VNRecognizedPoint *pt = [obs recognizedPointForJointName:joint error:&err];
    if (err || !pt || pt.confidence < 0.3) return CGPointZero;
    return pt.location;  // normalised 0-1, origin bottom-left
}

/// Angle in degrees between two points from vertical axis
- (double)_angleFromVertical:(CGPoint)from to:(CGPoint)to {
    double dx = to.x - from.x;
    double dy = to.y - from.y;
    if (dx == 0 && dy == 0) return 0;
    double angle = atan2(fabs(dx), fabs(dy)) * (180.0 / M_PI);
    return angle;
}

@end
