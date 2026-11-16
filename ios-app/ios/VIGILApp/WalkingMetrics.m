#import "WalkingMetrics.h"
#import <HealthKit/HealthKit.h>

@implementation WalkingMetrics
RCT_EXPORT_MODULE();

- (void)queryType:(HKQuantityTypeIdentifier)identifier
             unit:(HKUnit *)unit
        startDate:(NSDate *)start
          endDate:(NSDate *)end
       completion:(void(^)(NSArray *results, NSError *error))completion {
  HKHealthStore *store = [[HKHealthStore alloc] init];
  HKQuantityType *type = [HKQuantityType quantityTypeForIdentifier:identifier];
  NSPredicate *pred = [HKQuery predicateForSamplesWithStartDate:start endDate:end options:HKQueryOptionStrictStartDate];
  NSSortDescriptor *sort = [NSSortDescriptor sortDescriptorWithKey:HKSampleSortIdentifierStartDate ascending:NO];

  NSISO8601DateFormatter *fmt = [[NSISO8601DateFormatter alloc] init];
  fmt.formatOptions = NSISO8601DateFormatWithInternetDateTime | NSISO8601DateFormatWithFractionalSeconds;
  fmt.timeZone = [NSTimeZone localTimeZone];

  HKSampleQuery *query = [[HKSampleQuery alloc] initWithSampleType:type predicate:pred limit:500 sortDescriptors:@[sort] resultsHandler:^(HKSampleQuery *q, NSArray *samples, NSError *err) {
    if (err || !samples) { completion(@[], err); return; }
    NSMutableArray *out = [NSMutableArray new];
    for (HKQuantitySample *s in samples) {
      [out addObject:@{
        @"value":     @([s.quantity doubleValueForUnit:unit]),
        @"startDate": [fmt stringFromDate:s.startDate],
        @"endDate":   [fmt stringFromDate:s.endDate],
      }];
    }
    completion(out, nil);
  }];
  [store executeQuery:query];
}

RCT_EXPORT_METHOD(getWalkingSpeed:(NSDictionary *)options
                  resolver:(RCTPromiseResolveBlock)resolve
                  rejecter:(RCTPromiseRejectBlock)reject) {
  if (@available(iOS 14.0, *)) {
    NSDate *start = [NSDate dateWithTimeIntervalSinceNow:-7*24*3600];
    [self queryType:HKQuantityTypeIdentifierWalkingSpeed unit:[HKUnit unitFromString:@"m/s"]
          startDate:start endDate:[NSDate date] completion:^(NSArray *r, NSError *e) {
      e ? reject(@"err", e.localizedDescription, e) : resolve(r);
    }];
  } else { resolve(@[]); }
}

RCT_EXPORT_METHOD(getWalkingAsymmetry:(NSDictionary *)options
                  resolver:(RCTPromiseResolveBlock)resolve
                  rejecter:(RCTPromiseRejectBlock)reject) {
  if (@available(iOS 14.0, *)) {
    NSDate *start = [NSDate dateWithTimeIntervalSinceNow:-7*24*3600];
    [self queryType:HKQuantityTypeIdentifierWalkingAsymmetryPercentage unit:[HKUnit percentUnit]
          startDate:start endDate:[NSDate date] completion:^(NSArray *r, NSError *e) {
      e ? reject(@"err", e.localizedDescription, e) : resolve(r);
    }];
  } else { resolve(@[]); }
}

RCT_EXPORT_METHOD(getWalkingStepLength:(NSDictionary *)options
                  resolver:(RCTPromiseResolveBlock)resolve
                  rejecter:(RCTPromiseRejectBlock)reject) {
  if (@available(iOS 14.0, *)) {
    NSDate *start = [NSDate dateWithTimeIntervalSinceNow:-7*24*3600];
    [self queryType:HKQuantityTypeIdentifierWalkingStepLength unit:[HKUnit meterUnit]
          startDate:start endDate:[NSDate date] completion:^(NSArray *r, NSError *e) {
      e ? reject(@"err", e.localizedDescription, e) : resolve(r);
    }];
  } else { resolve(@[]); }
}

RCT_EXPORT_METHOD(getWalkingDoubleSupport:(NSDictionary *)options
                  resolver:(RCTPromiseResolveBlock)resolve
                  rejecter:(RCTPromiseRejectBlock)reject) {
  if (@available(iOS 14.0, *)) {
    NSDate *start = [NSDate dateWithTimeIntervalSinceNow:-7*24*3600];
    [self queryType:HKQuantityTypeIdentifierWalkingDoubleSupportPercentage unit:[HKUnit percentUnit]
          startDate:start endDate:[NSDate date] completion:^(NSArray *r, NSError *e) {
      e ? reject(@"err", e.localizedDescription, e) : resolve(r);
    }];
  } else { resolve(@[]); }
}

+ (BOOL)requiresMainQueueSetup { return NO; }
@end
