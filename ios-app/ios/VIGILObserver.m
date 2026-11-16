//
//  VIGILObserver.m
//  VIGILApp
//
//  Registers HKObserverQuery for key metrics. When HealthKit has new data
//  (e.g. the Watch synced overnight HRV or a new walking speed sample),
//  iOS wakes the app in the background and fires a JS event.
//
//  The React Native layer listens for 'vigilHealthKitUpdate' events and
//  triggers a sync without requiring the user to open the app.
//

#import "VIGILObserver.h"
#import <HealthKit/HealthKit.h>
#import <React/RCTLog.h>

// Metrics to observe for background delivery
// Frequency: HKUpdateFrequencyImmediate is best for alerts
// but Apple limits background wakes — HKUpdateFrequencyHourly is more reliable
static NSDictionary<NSString *, NSNumber *> *kObservedTypes(void) {
    return @{
        // Vitals — measured frequently throughout day
        @"HeartRate":                     @(HKUpdateFrequencyImmediate),
        @"HeartRateVariabilitySDNN":      @(HKUpdateFrequencyHourly),
        @"RestingHeartRate":              @(HKUpdateFrequencyHourly),
        @"OxygenSaturation":              @(HKUpdateFrequencyHourly),
        @"RespiratoryRate":               @(HKUpdateFrequencyHourly),
        // Activity — accumulated daily
        @"StepCount":                     @(HKUpdateFrequencyHourly),
        @"ActiveEnergyBurned":            @(HKUpdateFrequencyHourly),
        @"FlightsClimbed":                @(HKUpdateFrequencyHourly),
        @"AppleExerciseTime":             @(HKUpdateFrequencyHourly),
        @"AppleStandTime":                @(HKUpdateFrequencyHourly),
        // Mobility — measured during walks
        @"WalkingSpeed":                  @(HKUpdateFrequencyImmediate),
        @"WalkingStepLength":             @(HKUpdateFrequencyImmediate),
        @"WalkingAsymmetryPercentage":    @(HKUpdateFrequencyImmediate),
        @"WalkingDoubleSupportPercentage":@(HKUpdateFrequencyImmediate),
        // Sleep — measured overnight
        @"SleepAnalysis":                 @(HKUpdateFrequencyHourly),
    };
}

@interface VIGILObserver ()
@property (nonatomic, strong) HKHealthStore *healthStore;
@property (nonatomic, strong) NSMutableArray<HKObserverQuery *> *activeQueries;
@property (nonatomic, assign) BOOL hasListeners;
@end

@implementation VIGILObserver

RCT_EXPORT_MODULE();

// ── React Native lifecycle ────────────────────────────────────

- (instancetype)init {
    if (self = [super init]) {
        _healthStore  = [[HKHealthStore alloc] init];
        _activeQueries = [NSMutableArray new];
        _hasListeners  = NO;
    }
    return self;
}

- (NSArray<NSString *> *)supportedEvents {
    return @[@"vigilHealthKitUpdate"];
}

- (void)startObserving  { self.hasListeners = YES;  }
- (void)stopObserving   { self.hasListeners = NO;   }

+ (BOOL)requiresMainQueueSetup { return NO; }

// ── Public API ───────────────────────────────────────────────

/// Called once from AppDelegate after HealthKit authorisation.
+ (void)enableBackgroundDelivery {
    if (![HKHealthStore isHealthDataAvailable]) return;

    static VIGILObserver *sharedInstance = nil;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        sharedInstance = [[VIGILObserver alloc] init];
    });
    [sharedInstance _registerAllObservers];
    RCTLogInfo(@"[VIGILObserver] Background delivery enabled for %lu metrics",
               (unsigned long)kObservedTypes().count);
}

/// JS-callable: start observing (alternative to calling from AppDelegate)
RCT_EXPORT_METHOD(startBackgroundObserver:(RCTPromiseResolveBlock)resolve
                  reject:(RCTPromiseRejectBlock)reject) {
    if (![HKHealthStore isHealthDataAvailable]) {
        reject(@"unavailable", @"HealthKit not available on this device", nil);
        return;
    }
    [self _registerAllObservers];
    resolve(@{@"started": @YES, @"count": @(kObservedTypes().count)});
}

/// JS-callable: stop all observers
RCT_EXPORT_METHOD(stopBackgroundObserver:(RCTPromiseResolveBlock)resolve
                  reject:(RCTPromiseRejectBlock)reject) {
    @synchronized (self.activeQueries) {
        for (HKObserverQuery *q in self.activeQueries) {
            [self.healthStore stopQuery:q];
        }
        [self.activeQueries removeAllObjects];
    }
    [self.healthStore disableAllBackgroundDeliveryWithCompletion:^(BOOL success, NSError *error) {
        if (!success) {
            RCTLogWarn(@"[VIGILObserver] Failed to disable background delivery: %@", error.localizedDescription);
        }
        resolve(@{@"stopped": @YES});
    }];
}

// ── Private: register observers ──────────────────────────────

- (void)_registerAllObservers {
    NSDictionary *types = kObservedTypes();

    for (NSString *identifier in types) {
        HKQuantityType *type = [HKQuantityType quantityTypeForIdentifier:identifier];
        if (!type) {
            // Handle category types (SleepAnalysis)
            HKCategoryType *catType = [HKCategoryType categoryTypeForIdentifier:identifier];
            if (catType) {
                [self _registerObserverForSampleType:catType
                                           frequency:(HKUpdateFrequency)[types[identifier] integerValue]
                                          identifier:identifier];
            }
            continue;
        }
        [self _registerObserverForSampleType:type
                                   frequency:(HKUpdateFrequency)[types[identifier] integerValue]
                                  identifier:identifier];
    }
}

- (void)_registerObserverForSampleType:(HKSampleType *)sampleType
                             frequency:(HKUpdateFrequency)frequency
                            identifier:(NSString *)identifier {
    __weak VIGILObserver *weakSelf = self;

    HKObserverQuery *query = [[HKObserverQuery alloc]
        initWithSampleType:sampleType
                 predicate:nil
           updateHandler:^(HKObserverQuery * _Nonnull q,
                           HKObserverQueryCompletionHandler _Nonnull completionHandler,
                           NSError * _Nullable error) {
        if (error) {
            RCTLogWarn(@"[VIGILObserver] Observer error for %@: %@", identifier, error);
            completionHandler();
            return;
        }
        RCTLogInfo(@"[VIGILObserver] New HealthKit data: %@", identifier);

        // Fire JS event — React Native layer will call performSync
        VIGILObserver *strongSelf = weakSelf;
        if (strongSelf && strongSelf.hasListeners) {
            [strongSelf sendEventWithName:@"vigilHealthKitUpdate"
                                    body:@{
                @"metric":    identifier,
                @"timestamp": @([[NSDate date] timeIntervalSince1970] * 1000),
            }];
        }

        // IMPORTANT: always call completion handler
        completionHandler();
    }];

    [self.healthStore executeQuery:query];
    @synchronized (self.activeQueries) {
        [self.activeQueries addObject:query];
    }

    // Request background delivery from HealthKit
    [self.healthStore enableBackgroundDeliveryForType:sampleType
                                            frequency:frequency
                                          withCompletion:^(BOOL success, NSError *error) {
        if (!success) {
            RCTLogWarn(@"[VIGILObserver] Background delivery failed for %@: %@",
                       identifier, error.localizedDescription);
        }
    }];
}

@end
