#!/usr/bin/env python3
"""
VIGIL gait calibration.

Run:  python3 calibrate_gait.py --patch        (one time: read ratio from config)
      python3 calibrate_gait.py --calibrate    (measure and store the ratio)
      python3 calibrate_gait.py --show

WHY
---
Gait speed is computed as  speed = 2 x step_length / stride_period,
with step_length = 0.41 x height. The 0.41 figure comes from open-ground
walking. Measured indoors:

    true speed       1.021 m/s   (5 m in 4.96 / 4.67 / 5.08 s)
    VIGIL reported   1.160 m/s   (+13.6%)
    mean cadence     95.4 steps/min

    true step length = 1.021 / (95.4/60) = 0.643 m
    as a fraction of height (1.78 m)     = 0.361
    assumed 0.41 / actual 0.361          = 1.136  -> the 13.6% error

So the error is one uncalibrated constant, and it is person- and
room-specific: stride length shortens indoors with turns and furniture.
Hardcoding 0.361 would just be fitting one person's one test. Instead the
ratio becomes a config value measured per resident, which is how gait
measurement is normally calibrated.

CALIBRATION
-----------
Walk a measured distance several times at a normal pace, in camera view.
This reads the cadence VIGIL recorded during that window, derives the
ratio that makes VIGIL agree with the stopwatch, and stores it.
"""
import argparse
import ast
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta

BASE = "/home/rushil-mohan/passive-health-moniter"
DB = os.path.join(BASE, "vigil.db")
AM = os.path.join(BASE, "activity_monitor.py")

INIT_OLD = "        self.resident_height_cm = 170.0"
INIT_NEW = ("        self.resident_height_cm = 170.0\n"
            "        self.step_length_ratio = 0.41   # calibrated per resident")

CFG_OLD = '                self.resident_height_cm = float(cfg.get("resident_height_cm") or 170)'
CFG_NEW = (CFG_OLD +
           '\n                self.step_length_ratio = float(cfg.get("step_length_ratio") or 0.41)')

STRIDE_OLD = "stride_len=0.41*(client.resident_height_cm/100.0)"
STRIDE_NEW = "stride_len=client.step_length_ratio*(client.resident_height_cm/100.0)"


def col_exists(conn, table, col):
    return col in [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def ensure_column():
    conn = sqlite3.connect(DB)
    try:
        if not col_exists(conn, "config", "step_length_ratio"):
            conn.execute("ALTER TABLE config ADD COLUMN step_length_ratio REAL")
            conn.commit()
            print("  [OK] added config.step_length_ratio")
        else:
            print("  [SKIP] config.step_length_ratio exists")
    finally:
        conn.close()


def patch():
    with open(AM) as f:
        c = f.read()
    ensure_column()
    if "step_length_ratio" in c:
        print("  [SKIP] activity_monitor already reads the ratio")
        return 0
    counts = (c.count(INIT_OLD), c.count(CFG_OLD), c.count(STRIDE_OLD))
    print("  anchors found: init=%d config=%d stride=%d" % counts)
    if counts[0] != 1 or counts[1] != 1 or counts[2] < 1:
        print("ABORTED -- anchors missing. Nothing written.")
        return 1
    shutil.copy(AM, "%s.bak_calib_%d" % (AM, int(time.time())))
    c = c.replace(INIT_OLD, INIT_NEW, 1)
    c = c.replace(CFG_OLD, CFG_NEW, 1)
    n = c.count(STRIDE_OLD)
    c = c.replace(STRIDE_OLD, STRIDE_NEW)
    try:
        ast.parse(c)
    except SyntaxError as e:
        print("SYNTAX ERROR line %s: %s -- NOT SAVED" % (e.lineno, e.msg))
        return 1
    with open(AM, "w") as f:
        f.write(c)
    print("  [OK] %d stride_len site(s) now use the calibrated ratio" % n)
    print("\nNext: sudo systemctl restart vigil-camera")
    return 0


def show():
    conn = sqlite3.connect(DB)
    try:
        if not col_exists(conn, "config", "step_length_ratio"):
            print("config.step_length_ratio not present -- run --patch first")
            return 0
        r = conn.execute("SELECT resident_height_cm, step_length_ratio FROM config").fetchone()
        print("height_cm          : %s" % (r[0] if r else "?"))
        print("step_length_ratio  : %s (default 0.41 when unset)" % (r[1] if r else "?"))
    finally:
        conn.close()
    return 0


def calibrate(distance_m, times_s, window_min):
    true_speeds = [distance_m / t for t in times_s]
    true_speed = sum(true_speeds) / len(true_speeds)
    print("\nStopwatch:")
    for t, s in zip(times_s, true_speeds):
        print("  %.2f s  ->  %.3f m/s" % (t, s))
    print("  mean true speed: %.3f m/s" % true_speed)

    since = (datetime.utcnow() - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB)
    try:
        rows = conn.execute(
            "SELECT cadence_spm, gait_speed_ms FROM camera_metrics "
            "WHERE recorded_at >= ? AND cadence_spm IS NOT NULL", (since,)).fetchall()
    finally:
        conn.close()
    if not rows:
        print("\nNo cadence recorded in the last %d minutes." % window_min)
        print("Walk in view, wait ~40 s for a sync, then rerun.")
        return 1

    cad = [r[0] for r in rows]
    spd = [r[1] for r in rows if r[1] is not None]
    mean_cad = sum(cad) / len(cad)
    mean_spd = (sum(spd) / len(spd)) if spd else None
    print("\nVIGIL over the last %d min (%d samples):" % (window_min, len(rows)))
    print("  mean cadence : %.1f steps/min" % mean_cad)
    if mean_spd:
        print("  mean speed   : %.3f m/s  (%+.1f%% vs stopwatch)"
              % (mean_spd, (mean_spd - true_speed) / true_speed * 100))

    conn = sqlite3.connect(DB)
    try:
        h = conn.execute("SELECT resident_height_cm FROM config").fetchone()
    finally:
        conn.close()
    height_m = float(h[0]) / 100.0 if h and h[0] else 1.70
    steps_per_s = mean_cad / 60.0
    if steps_per_s <= 0:
        print("Cadence is zero; cannot calibrate.")
        return 1
    step_len = true_speed / steps_per_s
    ratio = step_len / height_m

    print("\nDerived:")
    print("  height            : %.2f m" % height_m)
    print("  true step length  : %.3f m" % step_len)
    print("  step_length_ratio : %.4f   (was using 0.41)" % ratio)

    if not (0.25 <= ratio <= 0.55):
        print("\nRatio outside the plausible 0.25-0.55 range. Not storing.")
        print("Check that the distance and times are right and that you")
        print("walked continuously in view for the whole window.")
        return 1

    ensure_column()
    conn = sqlite3.connect(DB)
    try:
        conn.execute("UPDATE config SET step_length_ratio=?", (round(ratio, 4),))
        conn.commit()
    finally:
        conn.close()
    print("\n  [OK] stored step_length_ratio = %.4f" % ratio)
    print("  Restart, then re-run the 5 m walk to confirm the error drops.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--patch", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--distance", type=float, default=5.0,
                    help="metres per walk (default 5)")
    ap.add_argument("--times", type=str, default="",
                    help="comma-separated seconds, e.g. 4.96,4.67,5.08")
    ap.add_argument("--window", type=int, default=5,
                    help="minutes of recent data to use (default 5)")
    a = ap.parse_args()

    if a.patch:
        sys.exit(patch())
    if a.show:
        sys.exit(show())
    if a.calibrate:
        if not a.times.strip():
            print("Provide --times, e.g. --times 4.96,4.67,5.08")
            sys.exit(1)
        ts = [float(x) for x in a.times.split(",") if x.strip()]
        sys.exit(calibrate(a.distance, ts, a.window))
    ap.print_help()
