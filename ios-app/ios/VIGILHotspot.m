//
//  VIGILHotspot.m
//  VIGILApp
//
//  APP_SETUP_V1: joins the VIGIL box's own setup Wi-Fi with one tap. iOS shows
//  its "Join Wi-Fi network?" prompt, so nobody has to open Settings.
//  Needs the Hotspot Configuration capability (paid Apple Developer team).
//  Without it, join() rejects and the app shows the Settings steps instead.
//
#import <React/RCTBridgeModule.h>
#import <NetworkExtension/NetworkExtension.h>

@interface VIGILHotspot : NSObject <RCTBridgeModule>
@end

@implementation VIGILHotspot

RCT_EXPORT_MODULE();

+ (BOOL)requiresMainQueueSetup { return NO; }

RCT_EXPORT_METHOD(join:(NSString *)ssid
                  password:(NSString *)password
                  resolver:(RCTPromiseResolveBlock)resolve
                  rejecter:(RCTPromiseRejectBlock)reject)
{
  NEHotspotConfiguration *cfg = [[NEHotspotConfiguration alloc] initWithSSID:ssid passphrase:password isWEP:NO];
  cfg.joinOnce = YES;   // the phone goes back to the home Wi-Fi when setup ends
  [[NEHotspotConfigurationManager sharedManager] applyConfiguration:cfg completionHandler:^(NSError *error) {
    if (error && !([error.domain isEqualToString:NEHotspotConfigurationErrorDomain] &&
                   error.code == NEHotspotConfigurationErrorAlreadyAssociated)) {
      reject([NSString stringWithFormat:@"%ld", (long)error.code], error.localizedDescription, error);
    } else {
      resolve(@YES);
    }
  }];
}

RCT_EXPORT_METHOD(leave:(NSString *)ssid)
{
  [[NEHotspotConfigurationManager sharedManager] removeConfigurationForSSID:ssid];
}

@end
