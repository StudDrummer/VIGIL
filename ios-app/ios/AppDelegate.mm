#import "AppDelegate.h"
#import "VIGILObserver.h"

#import <React/RCTBundleURLProvider.h>
#import <React/RCTLinkingManager.h>

@implementation AppDelegate

- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)launchOptions
{
  self.moduleName = @"VIGILApp";
  // You can add your custom initial props in the dictionary below.
  // They will be passed down to the ViewController used by React Native.
  self.initialProps = @{};

  
  [VIGILObserver enableBackgroundDelivery];
  return [super application:application didFinishLaunchingWithOptions:launchOptions];
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
