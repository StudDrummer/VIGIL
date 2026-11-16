#!/usr/bin/env python3
"""
VIGIL: App Store readiness check (Mac side, no camera needed).

Run:
    cd ~/Desktop/VIGILApp
    python3 check_app_ready.py

WHAT IT CHECKS
--------------
Things that commonly cause rejection or a crash on a reviewer's phone,
and none of which need the camera unit:

  1. TypeScript builds cleanly
  2. No developer wording or literal \\u escapes in user-visible text
  3. Demo Mode covers every screen, and pollers are gated
  4. Info.plist: HealthKit and local-network usage strings, transport
     security, export-compliance flag, version numbers
  5. HealthKit entitlement present
  6. 1024px App Store icon present
  7. Bundle identifier is not the React Native template default
  8. Privacy policy and terms are live over HTTPS with the required
     SMS language, and the privacy policy mentions health data
  9. The "not a medical device" disclaimer is in the app

Prints a pass / warn / fail summary.
"""
import glob
import json
import os
import plistlib
import re
import subprocess
import sys

APP = "App.tsx"
R = []
SYM = {"PASS": "  ok  ", "WARN": " warn ", "FAIL": " FAIL "}


def rec(name, status, detail=""):
    R.append((name, status, detail))
    print("  [%s] %-46s %s" % (SYM[status], name, detail))


def sh(cmd, t=180):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def hdr(t):
    print("\n" + "=" * 64 + "\n" + t + "\n" + "=" * 64)


def check_build():
    hdr("1. BUILD")
    rc, out = sh("npx tsc --noEmit")
    rec("TypeScript compiles", "PASS" if rc == 0 and not out else "FAIL",
        "" if rc == 0 and not out else out.split("\n")[0][:60])


def check_text(c):
    hdr("2. USER-VISIBLE TEXT")
    jargon = re.compile(r"Tailscale|Jetson|systemctl|sudo |Twilio|localhost|ssh ")
    hits = []
    for i, ln in enumerate(c.split("\n"), 1):
        s = ln.strip()
        if s.startswith(("//", "*", "/*")):
            continue
        code = s.split("//")[0]
        if jargon.search(code) and ("<Text" in code or "'" in code or '"' in code
                                    or not any(k in code for k in ("const ", "import ", "=>"))):
            hits.append(i)
    rec("developer wording in text", "PASS" if not hits else "WARN",
        "none" if not hits else "check lines %s" % hits[:10])

    esc = [i for i, ln in enumerate(c.split("\n"), 1)
           if re.search(r">[^<{]*\\u[0-9a-fA-F]{4}", ln)]
    rec("no literal \\u escapes in JSX text", "PASS" if not esc else "FAIL",
        "" if not esc else "lines %s" % esc[:8])

    rec("medical-device disclaimer", "PASS" if "not a medical device" in c else "FAIL")
    rec("SMS consent disclosure", "PASS" if "Message and data rates may apply" in c else "FAIL")

    n = len(re.findall(r"console\.log\(", c))
    rec("console.log calls", "PASS" if n < 40 else "WARN", "%d" % n)


def check_demo(c):
    hdr("3. DEMO MODE")
    for name in ("DEMO_TODAY", "DEMO_HISTORY", "DEMO_ALERTS", "DEMO_PERSONS",
                 "DEMO_CAMERA", "DEMO_ROOMS", "DEMO_INSIGHTS", "DEMO_SIGNALS",
                 "DEMO_FACE", "DEMO_ROUTINE", "DEMO_PATTERNS", "DEMO_PTRENDS"):
        rec(name, "PASS" if ("const %s" % name) in c else "FAIL")
    guards = c.count("||demoMode")
    rec("pollers gated on demoMode", "PASS" if guards >= 7 else "FAIL", "%d guards" % guards)
    rec("Demo entry in onboarding", "PASS" if "Explore VIGIL in Demo Mode" in c
        or "Open Demo Mode" in c else "FAIL")
    rec("demo banner present", "PASS" if "DEMO MODE" in c else "FAIL")

    rec("API key bundled in app", "WARN" if re.search(r"VIGIL_KEY\s*=\s*'[A-Za-z0-9_\-]{20,}'", c)
        else "PASS", "acceptable for pilot; per-unit keys in v2")
    rec("hardcoded server address", "WARN" if "100.107." in c else "PASS",
        "fallback only; reviewers use Demo Mode")


def find_plist():
    for p in glob.glob("ios/*/Info.plist"):
        if "Pods" not in p and "Tests" not in p:
            return p
    return None


def check_ios():
    hdr("4. iOS CONFIGURATION")
    p = find_plist()
    if not p:
        rec("Info.plist found", "FAIL", "not under ios/*/")
        return
    rec("Info.plist found", "PASS", p)
    try:
        pl = plistlib.load(open(p, "rb"))
    except Exception as e:
        rec("Info.plist readable", "FAIL", str(e)[:60])
        return

    for key, why, sev in [
        ("NSHealthShareUsageDescription", "required to read HealthKit", "FAIL"),
        ("NSLocalNetworkUsageDescription", "explains the local-network prompt", "WARN"),
        ("ITSAppUsesNonExemptEncryption", "skips export questions each build", "WARN"),
    ]:
        v = pl.get(key)
        if v is None or v == "":
            rec(key, sev, "missing -- " + why)
        else:
            rec(key, "PASS", str(v)[:50])

    ats = pl.get("NSAppTransportSecurity", {}) or {}
    if ats.get("NSAllowsLocalNetworking"):
        rec("transport security", "PASS", "NSAllowsLocalNetworking")
    elif ats.get("NSAllowsArbitraryLoads"):
        rec("transport security", "WARN", "AllowsArbitraryLoads -- reviewers may ask why")
    else:
        rec("transport security", "WARN", "no local-network exception; plain-http to the unit may fail")

    rec("version", "PASS", "%s (%s)" % (pl.get("CFBundleShortVersionString"),
                                        pl.get("CFBundleVersion")))

    ents = [e for e in glob.glob("ios/*/*.entitlements") if "Pods" not in e]
    hk = False
    for e in ents:
        try:
            if plistlib.load(open(e, "rb")).get("com.apple.developer.healthkit"):
                hk = True
        except Exception:
            pass
    rec("HealthKit entitlement", "PASS" if hk else "FAIL",
        os.path.basename(ents[0]) if ents else "no .entitlements file")

    icon = glob.glob("ios/*/Images.xcassets/AppIcon.appiconset/Contents.json")
    ok = False
    if icon:
        d = os.path.dirname(icon[0])
        try:
            for img in json.load(open(icon[0])).get("images", []):
                if img.get("size") == "1024x1024" and img.get("filename") \
                        and os.path.exists(os.path.join(d, img["filename"])):
                    ok = True
        except Exception:
            pass
    rec("1024px App Store icon", "PASS" if ok else "FAIL")

    rc, out = sh("grep -h PRODUCT_BUNDLE_IDENTIFIER ios/*.xcodeproj/project.pbxproj | sort -u")
    ids = sorted(set(re.findall(r"=\s*\"?([^\";]+)\"?;", out)))
    bad = any("org.reactjs.native.example" in i for i in ids)
    rec("bundle identifier", "WARN" if bad else "PASS", ", ".join(ids)[:60])


def url_has(url, needle):
    rc, code = sh("curl -s -o /dev/null -w '%%{http_code}' -L '%s'" % url, 30)
    rc2, body = sh("curl -s -L '%s'" % url, 30)
    return code, (needle.lower() in body.lower())


def check_web():
    hdr("5. POLICY PAGES (live)")
    for url, needle, label in [
        ("https://vigilhealth.me/privacy.html", "marketing or promotional", "privacy: SMS sharing statement"),
        ("https://vigilhealth.me/terms.html", "Carriers are not liable", "terms: SMS program terms"),
        ("https://vigilhealth.me/privacy.html", "health", "privacy: covers health data"),
        ("https://vigilhealth.me", "VIGIL", "support / marketing site"),
    ]:
        code, ok = url_has(url, needle)
        rec(label, "PASS" if code == "200" and ok else "FAIL", "HTTP %s" % code)


def summary():
    hdr("SUMMARY")
    f = [r for r in R if r[1] == "FAIL"]
    w = [r for r in R if r[1] == "WARN"]
    print("  passed %d   failed %d   warnings %d" % (len(R) - len(f) - len(w), len(f), len(w)))
    if f:
        print("\n  FIX BEFORE SUBMITTING:")
        for n, _s, d in f:
            print("    %s  %s" % (n, d))
    if w:
        print("\n  Review:")
        for n, _s, d in w:
            print("    %s  %s" % (n, d))
    print("\n  " + ("READY" if not f else "NOT READY -- %d blocker(s)" % len(f)))
    return 1 if f else 0


if __name__ == "__main__":
    if not os.path.exists(APP):
        print("Run from ~/Desktop/VIGILApp")
        sys.exit(1)
    src = open(APP).read()
    print("VIGIL APP STORE READINESS")
    check_build()
    check_text(src)
    check_demo(src)
    check_ios()
    check_web()
    sys.exit(summary())
