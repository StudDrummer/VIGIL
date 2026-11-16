#import "AppDelegate.h"
#import "VIGILObserver.h"

#import <React/RCTBundleURLProvider.h>
#import <React/RCTLinkingManager.h>
#import <React/RCTBridgeModule.h>
#import <UserNotifications/UserNotifications.h>

// APPLE_PUSH_V1: alerts arrive as notifications from the VIGIL app itself.
// Needs the Push Notifications capability (paid Apple Developer team). Without
// it, register() rejects and the app keeps working, just without app alerts.
static NSString *gPushToken = nil;
static NSMutableArray *gPushWaiters = nil;

static NSObject *VIGILPushLock(void) {
  static NSObject *lock; static dispatch_once_t once;
  dispatch_once(&once, ^{ lock = [NSObject new]; gPushWaiters = [NSMutableArray new]; });
  return lock;
}

static void VIGILPushDeliver(NSString *token, NSError *err) {
  NSArray *waiters;
  @synchronized (VIGILPushLock()) {
    if (token) gPushToken = token;
    waiters = [gPushWaiters copy];
    [gPushWaiters removeAllObjects];
  }
  for (NSArray *w in waiters) {
    if (token) ((RCTPromiseResolveBlock)w[0])(token);
    else ((RCTPromiseRejectBlock)w[1])(@"push", err.localizedDescription ?: @"no token", err);
  }
}

@interface VIGILPush : NSObject <RCTBridgeModule>
@end

@implementation VIGILPush
RCT_EXPORT_MODULE();
+ (BOOL)requiresMainQueueSetup { return NO; }

RCT_EXPORT_METHOD(enable:(RCTPromiseResolveBlock)resolve rejecter:(RCTPromiseRejectBlock)reject)
{
  UNAuthorizationOptions opts = UNAuthorizationOptionAlert | UNAuthorizationOptionSound | UNAuthorizationOptionBadge;
  [[UNUserNotificationCenter currentNotificationCenter] requestAuthorizationWithOptions:opts
      completionHandler:^(BOOL granted, NSError *error) {
    if (!granted) { reject(@"denied", @"Notifications are turned off for VIGIL", error); return; }
    @synchronized (VIGILPushLock()) {
      [gPushWaiters addObject:@[[resolve copy], [reject copy]]];
    }
    dispatch_async(dispatch_get_main_queue(), ^{
      [[UIApplication sharedApplication] registerForRemoteNotifications];
    });
  }];
}

RCT_EXPORT_METHOD(status:(RCTPromiseResolveBlock)resolve rejecter:(RCTPromiseRejectBlock)reject)
{
  [[UNUserNotificationCenter currentNotificationCenter] getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *st) {
    UNAuthorizationStatus a = st.authorizationStatus;
    resolve(a == UNAuthorizationStatusAuthorized || a == UNAuthorizationStatusProvisional ? @"on"
            : a == UNAuthorizationStatusDenied ? @"off" : @"ask");
  }];
}

RCT_EXPORT_METHOD(openSettings)
{
  dispatch_async(dispatch_get_main_queue(), ^{
    [[UIApplication sharedApplication] openURL:[NSURL URLWithString:UIApplicationOpenSettingsURLString]
                                       options:@{} completionHandler:nil];
  });
}
@end

@interface AppDelegate () <UNUserNotificationCenterDelegate>
@end

@implementation AppDelegate

- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)launchOptions
{
  self.moduleName = @"VIGILApp";
  // You can add your custom initial props in the dictionary below.
  // They will be passed down to the ViewController used by React Native.
  self.initialProps = @{};

  
  [VIGILObserver enableBackgroundDelivery];
  [UNUserNotificationCenter currentNotificationCenter].delegate = self;   // APPLE_PUSH_V1: show alerts while VIGIL is open too
  return [super application:application didFinishLaunchingWithOptions:launchOptions];
}

- (void)application:(UIApplication *)application didRegisterForRemoteNotificationsWithDeviceToken:(NSData *)deviceToken
{
  const unsigned char *b = (const unsigned char *)deviceToken.bytes;
  NSMutableString *hex = [NSMutableString stringWithCapacity:deviceToken.length * 2];
  for (NSUInteger i = 0; i < deviceToken.length; i++) { [hex appendFormat:@"%02x", b[i]]; }
  VIGILPushDeliver(hex, nil);
}

- (void)application:(UIApplication *)application didFailToRegisterForRemoteNotificationsWithError:(NSError *)error
{
  VIGILPushDeliver(nil, error);
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
       willPresentNotification:(UNNotification *)notification
         withCompletionHandler:(void (^)(UNNotificationPresentationOptions))completionHandler
{
  completionHandler(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList |
                    UNNotificationPresentationOptionSound);
}

- (NSURL *)sourceURLForBridge:(RCTBridge *)bridge
{
  return [self bundleURL];
}

- (NSURL *)bundleURL
{
#if DEBUG
  return [[RCTBundleURLProvider sharedSettings] jsBundleURLForBundleRoot:@"index"];
#else
  return [[NSBundle mainBundle] URLForResource:@"main" withExtension:@"jsbundle"];
#endif
}

@end

// SCENE_LIFECYCLE_V1: the current iOS SDK refuses to launch apps without a
// UIScene life cycle. React Native 0.74 still builds its window in the app
// delegate, so this scene delegate moves that window's root view controller
// into a window attached to the scene.
@interface SceneDelegate : UIResponder <UIWindowSceneDelegate>
@property (nonatomic, strong) UIWindow *window;
@end

@implementation SceneDelegate

- (void)scene:(UIScene *)scene
    willConnectToSession:(UISceneSession *)session
                 options:(UISceneConnectionOptions *)connectionOptions
{
  if (![scene isKindOfClass:[UIWindowScene class]]) {
    return;
  }
  AppDelegate *app = (AppDelegate *)[UIApplication sharedApplication].delegate;
  UIViewController *root = app.window.rootViewController;
  app.window.rootViewController = nil;
  UIWindow *window = [[UIWindow alloc] initWithWindowScene:(UIWindowScene *)scene];
  window.rootViewController = root;
  self.window = window;
  app.window = window;
  [window makeKeyAndVisible];
  // APP_SETUP_V1: the app was opened by scanning the setup card's QR code.
  NSURL *url = connectionOptions.URLContexts.anyObject.URL;
  if (url) {
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2.5 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
      [RCTLinkingManager application:[UIApplication sharedApplication] openURL:url options:@{}];
    });
  }
}

- (void)scene:(UIScene *)scene openURLContexts:(NSSet<UIOpenURLContext *> *)URLContexts
{
  NSURL *url = URLContexts.anyObject.URL;
  if (url) {
    [RCTLinkingManager application:[UIApplication sharedApplication] openURL:url options:@{}];
  }
}

@end
