#import "VIGILWalking.h"
#import <HealthKit/HealthKit.h>

@implementation VIGILWalking

RCT_EXPORT_MODULE(VIGILWalking);

static HKHealthStore *_store = nil;

+ (HKHealthStore *)store {
  if (!_store) _store = [[HKHealthStore alloc] init];
  return _store;
}

- (void)query:(HKQuantityTypeIdentifier)identifier
         unit:(HKUnit *)unit
     callback:(RCTResponseSenderBlock)callback {

  HKQuantityType *type = [HKQuantityType quantityTypeForIdentifier:identifier];

  // Request permission first, then query
  [[VIGILWalking store] requestAuthorizationToShareTypes:nil
    readTypes:[NSSet setWithObject:type]
    completion:^(BOOL success, NSError *authErr) {

      NSDate *start = [NSDate dateWithTimeIntervalSinceNow:-7*24*3600]; // 7 days back
      NSPredicate *pred = [HKQuery predicateForSamplesWithStartDate:start
                                                            endDate:[NSDate date]
                                                            options:HKQueryOptionNone];
      NSSortDescriptor *sort = [NSSortDescriptor sortDescriptorWithKey:HKSampleSortIdentifierStartDate
                                                             ascending:NO];
      HKSampleQuery *q = [[HKSampleQuery alloc]
        initWithSampleType:type predicate:pred limit:500
        sortDescriptors:@[sort]
        resultsHandler:^(HKSampleQuery *query, NSArray *results, NSError *err) {
          if (err || !results) {
            callback(@[[NSNull null], @[]]);
            return;
          }
          NSISO8601DateFormatter *fmt = [[NSISO8601DateFormatter alloc] init];
          NSMutableArray *out = [NSMutableArray array];
          for (HKQuantitySample *s in results) {
            [out addObject:@{
              @"value":     @([s.quantity doubleValueForUnit:unit]),
              @"startDate": [fmt stringFromDate:s.startDate],
              @"endDate":   [fmt stringFromDate:s.endDate],
            }];
          }
          callback(@[[NSNull null], out]);
        }];
      [[VIGILWalking store] executeQuery:q];
    }];
}

RCT_EXPORT_METHOD(getWalkingSpeed:(NSDictionary *)opts callback:(RCTResponseSenderBlock)cb) {
  [self query:HKQuantityTypeIdentifierWalkingSpeed unit:[HKUnit unitFromString:@"m/s"] callback:cb];
}

RCT_EXPORT_METHOD(getWalkingAsymmetry:(NSDictionary *)opts callback:(RCTResponseSenderBlock)cb) {
  [self query:HKQuantityTypeIdentifierWalkingAsymmetryPercentage unit:[HKUnit percentUnit] callback:cb];
}

RCT_EXPORT_METHOD(getWalkingStepLength:(NSDictionary *)opts callback:(RCTResponseSenderBlock)cb) {
  [self query:HKQuantityTypeIdentifierWalkingStepLength unit:[HKUnit meterUnit] callback:cb];
}

RCT_EXPORT_METHOD(getWalkingDoubleSupportPercentage:(NSDictionary *)opts callback:(RCTResponseSenderBlock)cb) {
  [self query:HKQuantityTypeIdentifierWalkingDoubleSupportPercentage unit:[HKUnit percentUnit] callback:cb];
}

@end
