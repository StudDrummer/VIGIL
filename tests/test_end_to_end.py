#!/usr/bin/env python3
"""
VIGIL end-to-end readiness test.

Run on the Jetson, with your iPhone in hand:

    cd ~/passive-health-moniter
    python3 test_end_to_end.py            # everything
    python3 test_end_to_end.py --server   # automated checks only, ~2 min

WHY THIS EXISTS
---------------
Every component of this system has worked in isolation while the path
between components was broken. The acknowledgement button rendered, the
route existed, the database column existed -- and acknowledgements still
never persisted, because the API key in the app was stale and the error
was swallowed. Nothing short of tracing a whole path catches that.

The automated phase checks what a machine can check. The guided phase
asks you about the things only a person can see: does the video actually
move, does the naming sheet appear, does Demo Mode populate every tab.
Half the failures in this project were visual or interaction-level.

Nothing here writes to your data except one test fall alert, which is
acknowledged and deleted before the script finishes.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

BASE = "/home/rushil-mohan/passive-health-moniter"
DB = os.path.join(BASE, "vigil.db")
CAMLOG = os.path.join(BASE, "camera.log")
URL = "http://localhost:5001"

results = []      # (section, name, status, detail)   status: PASS FAIL WARN SKIP


def rec(section, name, ok, detail=""):
    status = ok if isinstance(ok, str) else ("PASS" if ok else "FAIL")
    results.append((section, name, status, detail))
    mark = {"PASS": "  ok  ", "FAIL": " FAIL ", "WARN": " warn ", "SKIP": " skip "}[status]
    print("  [%s] %-42s %s" % (mark, name, detail))


def sh(cmd, timeout=25):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception as e:
        return "ERR:%s" % e


def key():
    out = sh("sudo grep -oP '(?<=VIGIL_API_KEY=).*' /etc/vigil/vigil.env")
    return out.strip()


def api(path, method="GET", body=None, k=None, timeout=15):
    h = "-H 'X-VIGIL-Key: %s'" % (k or "")
    if method == "POST":
        data = "-d '%s'" % json.dumps(body or {})
        cmd = ("curl -s -m %d -X POST -H 'Content-Type: application/json' %s %s %s%s"
               % (timeout, h, data, URL, path))
    else:
        cmd = "curl -s -m %d %s %s%s" % (timeout, h, URL, path)
    out = sh(cmd, timeout + 5)
    try:
        parsed = json.loads(out)
    except Exception:
        return {"_raw": out[:200]}
    # Several endpoints return a JSON array. Wrap it so callers can treat
    # every response the same way.
    if isinstance(parsed, list):
        return {"_list": parsed, "_count": len(parsed)}
    return parsed


def q(sql, default=None):
    try:
        c = sqlite3.connect(DB)
        try:
            return c.execute(sql).fetchall()
        finally:
            c.close()
    except Exception:
        return default if default is not None else []


def hdr(t):
    print("\n" + "=" * 66)
    print(t)
    print("=" * 66)


# ============================================================ 1 ENVIRONMENT
def check_environment():
    hdr("1. ENVIRONMENT")
    for svc in ("vigil", "vigil-camera", "vigil-face"):
        st = sh("systemctl is-active %s" % svc)
        rec("env", "%s service" % svc, st == "active",
            st if st != "active" else "")
    up = sh("systemctl show vigil-camera -p ActiveEnterTimestamp --value")
    rec("env", "camera uptime", "PASS", up)

    df = sh("df -h %s | tail -1 | awk '{print $5}'" % BASE).replace("%", "")
    try:
        pct = int(df)
        rec("env", "disk usage", "PASS" if pct < 85 else "WARN", "%s%% used" % pct)
    except Exception:
        rec("env", "disk usage", "WARN", df)

    pm = sh("sudo nvpmodel -q 2>/dev/null | head -2 | tail -1")
    rec("env", "power mode", "PASS", pm or "unknown")

    fps = sh("grep -a PERF_DBG %s | tail -1" % CAMLOG)
    m = re.search(r"fps=([\d.]+)", fps)
    if m:
        v = float(m.group(1))
        rec("env", "pipeline frame rate", "PASS" if v >= 8 else "WARN",
            "%.1f fps" % v)
    else:
        rec("env", "pipeline frame rate", "WARN", "no PERF_DBG (debug off)")

    tb = sh("tail -400 %s | grep -a -c Traceback" % CAMLOG)
    rec("env", "recent tracebacks", tb == "0", "%s in last 400 lines" % tb)


# =========================================================== 2 CREDENTIALS
def check_credentials(k):
    hdr("2. CREDENTIALS")
    rec("cred", "API key present", bool(k), "%d chars" % len(k) if k else "MISSING")
    h = api("/health", k=k)
    rec("cred", "API key accepted", h.get("status") == "ok", str(h)[:60])

    sid = sh("sudo grep -oP '(?<=VIGIL_TWILIO_SID=).*' /etc/vigil/vigil.env")
    rec("cred", "Twilio SID format", sid.startswith("AC"),
        "starts with AC" if sid.startswith("AC") else "WRONG PREFIX")
    sms = sh("grep -a '\\[SMS\\]' %s | tail -1" % os.path.join(BASE, "server.log"))
    if "Sent" in sms:
        rec("cred", "last SMS attempt", "PASS", "delivered")
    elif sms:
        rec("cred", "last SMS attempt", "WARN", sms[-70:])
    else:
        rec("cred", "last SMS attempt", "WARN", "no SMS attempted yet")


# ============================================================= 3 ENDPOINTS
def registered_routes():
    """Routes actually declared in Server.py. Read, not assumed."""
    out = sh("grep -oP \'(?<=@app.route\\(\")[^\"]+\' %s/Server.py" % BASE)
    return [r.strip() for r in out.split("\n") if r.strip()]


def check_endpoints(k):
    hdr("3. API ENDPOINTS")
    routes = registered_routes()
    rec("api", "routes registered in Server.py", len(routes) > 10,
        "%d found" % len(routes))

    # Paths this test knows how to exercise, with a key that proves the
    # response is real rather than an error page.
    wanted = [
        ("/health", "status"), ("/summary/today", None),
        ("/persons", None), ("/camera/status", None), ("/config", None),
        ("/insights/latest", "residents"), ("/signals/latest", "signals"),
        ("/routine/latest", "items"), ("/patterns/latest", "hourly"),
        ("/face/latest", "yawns_today"), ("/alerts/unseen", "unseen"),
    ]
    # Whichever alert-listing route this server actually has.
    for cand in ("/alerts", "/alerts/recent", "/alerts/list", "/alerts/all"):
        if cand in routes:
            wanted.insert(2, (cand, None))
            break

    for path, want in wanted:
        if path not in routes and not path.endswith("/today"):
            rec("api", path, "WARN", "not registered on this server")
            continue
        r = api(path, k=k)
        if "_raw" in r:
            rec("api", path, False, str(r["_raw"])[:50])
            continue
        if "error" in r:
            rec("api", path, False, str(r["error"])[:50])
            continue
        if "_list" in r:
            rec("api", path, True, "%d items" % r["_count"])
            continue
        ok = (want is None) or (want in r)
        rec("api", path, ok, "" if ok else "missing key '%s'" % want)

    snap = sh("curl -s -m 8 -o /tmp/_vsnap.jpg -w '%%{http_code}' "
              "-H 'X-VIGIL-Key: %s' %s/camera/snapshot" % (k, URL))
    size = os.path.getsize("/tmp/_vsnap.jpg") if os.path.exists("/tmp/_vsnap.jpg") else 0
    rec("api", "/camera/snapshot", snap == "200" and size > 5000, "%d bytes" % size)

    sh("curl -s -m 4 -H 'X-VIGIL-Key: %s' %s/camera/stream -o /tmp/_vstream.bin"
       % (k, URL), timeout=12)
    ssz = os.path.getsize("/tmp/_vstream.bin") if os.path.exists("/tmp/_vstream.bin") else 0
    rec("api", "/camera/stream (4s)", ssz > 50000, "%d bytes" % ssz)


def check_database():
    hdr("4. DATABASE")
    ic = q("PRAGMA integrity_check")
    rec("db", "integrity", bool(ic) and ic[0][0] == "ok",
        ic[0][0] if ic else "unreadable")

    for t in ("camera_metrics", "alerts", "persons", "person_detections",
              "daily_summary", "activity_events", "person_baselines"):
        n = q("SELECT COUNT(*) FROM %s" % t, [(0,)])[0][0]
        rec("db", "%s rows" % t, n > 0 if t != "alerts" else True, str(n))

    r = q("SELECT MAX(recorded_at) FROM camera_metrics")
    last = r[0][0] if r and r[0][0] else None
    fresh = False
    if last:
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
            fresh = age < 300
            rec("db", "camera_metrics freshness", "PASS" if fresh else "WARN",
                "%.0f s old" % age)
        except Exception:
            rec("db", "camera_metrics freshness", "WARN", str(last))
    else:
        rec("db", "camera_metrics freshness", False, "no rows")

    named = q("SELECT COUNT(*) FROM persons WHERE name IS NOT NULL AND name!=''",
              [(0,)])[0][0]
    rec("db", "named resident configured", named > 0, "%d named" % named)

    bl = q("SELECT COUNT(*) FROM person_baselines", [(0,)])[0][0]
    rec("db", "baselines built", "PASS" if bl > 0 else "WARN",
        "%d metrics" % bl)

    bad = q("SELECT COUNT(*) FROM camera_metrics WHERE cadence_spm>180 "
            "OR cadence_spm<40", [(0,)])[0][0]
    rec("db", "no impossible cadence values", bad == 0, "%d bad rows" % bad)


# ========================================================== 5 NIGHTLY JOBS
def check_jobs():
    hdr("5. NIGHTLY JOBS")
    cron = sh("crontab -l 2>/dev/null")
    for name in ("person_baseline.py", "person_patterns.py", "Night_anamaly.py"):
        active = any(name in l and not l.strip().startswith("#")
                     for l in cron.split("\n"))
        rec("cron", name, active, "scheduled" if active else "not scheduled")
    retired = any("ml_scorer" in l and l.strip().startswith("#")
                  for l in cron.split("\n"))
    rec("cron", "synthetic ml_scorer retired", "PASS" if retired else "WARN",
        "commented out" if retired else "still scheduled")

    r = q("SELECT MAX(computed_at) FROM person_baselines")
    rec("cron", "baselines last computed", "PASS", str(r[0][0]) if r else "never")


# ========================================================= 6 ALERT PATH
def check_alert_path(k):
    hdr("6. ALERT PATH (end to end)")
    before = q("SELECT COUNT(*) FROM alerts", [(0,)])[0][0]
    r = api("/alert/fall", "POST",
            {"detected_at": datetime.now().isoformat(), "hip_drop_pct": 0.9,
             "torso_angle": 80.0, "message": "END-TO-END TEST - not a real fall",
             "room_id": "room_1"}, k=k)
    accepted = "error" not in r and "_raw" not in r
    rec("alert", "fall alert accepted", accepted or "rate" in str(r).lower(),
        str(r)[:60])

    time.sleep(1)
    rows = q("SELECT id, alert_type, seen FROM alerts ORDER BY id DESC LIMIT 1")
    stored = bool(rows) and q("SELECT COUNT(*) FROM alerts", [(0,)])[0][0] > before
    rec("alert", "alert stored in database", stored or "rate-limited" in str(r),
        str(rows[0]) if rows else "none")

    if stored and rows:
        aid = rows[0][0]
        a = api("/alerts/ack", "POST", {"id": aid, "acknowledged_by": "E2E test"}, k=k)
        seen = q("SELECT seen FROM alerts WHERE id=?" .replace("?", str(aid)))
        ok = bool(seen) and seen[0][0] == 1
        rec("alert", "acknowledgement persists", ok, str(a)[:50])
        try:
            c = sqlite3.connect(DB)
            c.execute("DELETE FROM alerts WHERE id=?", (aid,))
            c.commit(); c.close()
            rec("alert", "test alert cleaned up", True, "removed id %d" % aid)
        except Exception as e:
            rec("alert", "test alert cleaned up", False, str(e)[:50])

    u = api("/alerts/unseen", k=k)
    rec("alert", "unacknowledged backlog", "PASS" if u.get("unseen", 0) < 20 else "WARN",
        "%s unseen" % u.get("unseen"))


# ======================================================== 7 SHARED FRAMES
def check_frames():
    hdr("7. SHARED FRAMES")
    for p, label in (("/dev/shm/vigil_latest.jpg", "annotated frame"),
                     ("/dev/shm/vigil_raw.jpg", "raw frame"),
                     ("/dev/shm/vigil_face.jpg", "head crop")):
        if os.path.exists(p):
            age = time.time() - os.path.getmtime(p)
            sz = os.path.getsize(p)
            rec("frames", label, "PASS" if age < 120 else "WARN",
                "%.0fs old, %d bytes" % (age, sz))
        else:
            rec("frames", label, "WARN", "missing (nobody in view?)")


# ========================================================== 8 GUIDED APP
APP_CHECKS = [
    ("Fresh launch reaches onboarding or the dashboard without crashing", True),
    ("Today tab shows metrics (not blank, no NaN or undefined anywhere)", True),
    ("Live camera view MOVES continuously, not a slideshow", True),
    ("Trends tab renders charts with history", True),
    ("Alerts tab lists alerts with correct coloured badges", True),
    ("Tapping Acknowledge clears the alert AND it stays cleared after reload", True),
    ("AI Insights shows gait cards with baselines", True),
    ("AI Insights shows Daily Routine section", True),
    ("AI Insights shows Activity Pattern chart", True),
    ("AI Insights shows Health Signals section", True),
    ("AI Insights shows Facial Cues section", True),
    ("Settings: all four sub-tabs render", True),
    ("Settings > Monitored People lists the resident with a photo", True),
    ("Tapping the resident opens the detail screen with trend charts", True),
    ("Walk in view: naming sheet appears for an unidentified person", True),
    ("Naming sheet: typing a name keeps every character (no dropped letters)", True),
    ("Naming sheet offers to merge into an existing resident", True),
    ("--- now put the phone in AIRPLANE MODE (wifi off too) ---", None),
    ("Offline: 'No monitoring unit connected' card with Demo Mode button", True),
    ("Demo Mode: amber banner reads correctly (no \\u2014 literal text)", True),
    ("Demo Mode: Today, Trends, Alerts, Insights, Settings all populated", True),
    ("Demo Mode: person detail opens with charts", True),
    ("Demo Mode: Exit returns to the live app", True),
    ("--- turn the network back on ---", None),
    ("App reconnects and shows live data again", True),
]


def guided():
    hdr("8. GUIDED APP CHECKS")
    print("Have the phone in hand. Answer y / n / s (skip) for each.\n")
    for text, needs in APP_CHECKS:
        if needs is None:
            print("\n  %s\n" % text)
            input("  Press Enter when ready...")
            continue
        while True:
            a = input("  %s  [y/n/s] " % text).strip().lower()
            if a in ("y", "n", "s"):
                break
        rec("app", text[:58],
            "PASS" if a == "y" else "SKIP" if a == "s" else "FAIL")


# ============================================================== SUMMARY
def summary():
    hdr("SUMMARY")
    fails = [r for r in results if r[2] == "FAIL"]
    warns = [r for r in results if r[2] == "WARN"]
    passes = [r for r in results if r[2] == "PASS"]
    skips = [r for r in results if r[2] == "SKIP"]
    print("  passed %d   failed %d   warnings %d   skipped %d"
          % (len(passes), len(fails), len(warns), len(skips)))

    if fails:
        print("\n  BLOCKERS -- fix before submitting:")
        for s, n, _st, d in fails:
            print("    [%s] %s  %s" % (s, n, d))
    if warns:
        print("\n  Warnings -- review, may be acceptable:")
        for s, n, _st, d in warns:
            print("    [%s] %s  %s" % (s, n, d))

    print()
    if not fails and not warns:
        print("  READY TO SUBMIT")
    elif not fails:
        print("  READY with caveats -- read the warnings above")
    else:
        print("  NOT READY -- %d blocker(s)" % len(fails))
    print("=" * 66)
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", action="store_true",
                    help="automated checks only, skip the guided app phase")
    a = ap.parse_args()

    print("=" * 66)
    print("VIGIL END-TO-END READINESS TEST")
    print("  %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("=" * 66)

    check_environment()
    k = key()
    check_credentials(k)
    check_endpoints(k)
    check_database()
    check_jobs()
    check_alert_path(k)
    check_frames()
    if not a.server:
        guided()
    sys.exit(summary())
