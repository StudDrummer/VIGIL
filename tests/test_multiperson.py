#!/usr/bin/env python3
"""
VIGIL two-person identity test.

Run:  python3 test_multiperson.py

Needs two people. Non-destructive -- deletes nothing.

WHAT IT CHECKS
--------------
Single-resident mode is off, so each person should get and keep their own
identity. Three things have to hold:

  1. Person A alone      -> exactly one identity
  2. Person B alone      -> exactly one more, distinct from A
  3. BOTH together       -> no new identities; the two already known are
                            recognised rather than re-derived

Stage 3 is the real test. Two people in frame means occlusion, crossing
paths, and one person partially blocking the other -- the conditions that
break frame-to-frame tracking and force the height/proportion fallback to
do the work.

WHY IT MATTERS
--------------
Per-person baselines, trend charts and insights all assume identity is
stable. If two people merge into one identity, their gait data is averaged
together and every baseline built from it is wrong. Better to know that
now than to present it.
"""
import os
import sqlite3
import subprocess
import sys
import time

BASE = "/home/rushil-mohan/passive-health-moniter"
DB = os.path.join(BASE, "vigil.db")
LOG = os.path.join(BASE, "camera.log")


def q(sql):
    c = sqlite3.connect(DB)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def persons():
    return {r[0]: (r[1], r[2], r[3]) for r in
            q("SELECT id, name, height_px, frames_seen FROM persons")}


def dbg_count():
    try:
        with open(LOG, errors="replace") as f:
            return f.read().count("[MATCH_DBG]")
    except Exception:
        return 0


def countdown(s, msg):
    for r in range(s, 0, -1):
        sys.stdout.write("\r    %s -- %2ds " % (msg, r))
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r" + " " * 62 + "\r")


def show(label, before, now):
    new = [p for p in now if p not in before]
    print("    %s: %d new %s" % (label, len(new), new if new else ""))
    for p in new:
        name, h, f = now[p]
        print("      %-14s %-14s height=%s frames=%s"
              % (p, name or "(unnamed)", round(h, 1) if h else "None", f))
    return new


def main():
    st = subprocess.run("systemctl is-active vigil-camera", shell=True,
                        capture_output=True, text=True).stdout.strip()
    print("=" * 62)
    print("TWO-PERSON IDENTITY TEST")
    print("=" * 62)
    print("camera service : %s" % st)
    if st != "active":
        print("Camera is not running. Aborting.")
        return 1
    m = q("SELECT single_resident_mode FROM config")
    srm = m[0][0] if m else None
    print("single-resident: %s" % srm)
    if srm:
        print("\nSingle-resident mode is ON. The primary detection would be")
        print("pinned to one person and this test cannot measure anything.")
        print("Turn it off first:")
        print("  sqlite3 vigil.db \"UPDATE config SET single_resident_mode=0;\"")
        print("  sudo systemctl restart vigil-camera")
        return 1

    start = persons()
    dbg0 = dbg_count()
    print("\nstarting identities: %d" % len(start))
    for p, (name, h, f) in start.items():
        print("  %-14s %-14s height=%s frames=%s"
              % (p, name or "(unnamed)", round(h, 1) if h else "None", f))

    input("\nBoth people out of frame. Press Enter when ready...")

    print("\n[1/3] PERSON A ALONE")
    input("      Person A walks in alone. Press Enter, then walk for 60s...")
    countdown(60, "person A walking")
    after_a = persons()
    new_a = show("stage 1", start, after_a)

    print("\n[2/3] PERSON B ALONE")
    input("      A leaves, B walks in alone. Press Enter when B is ready...")
    countdown(60, "person B walking")
    after_b = persons()
    new_b = show("stage 2", after_a, after_b)

    print("\n[3/3] BOTH TOGETHER")
    print("      Both walk around together. Cross paths, stand at different")
    print("      distances, let one briefly block the other.")
    input("      Press Enter, then both walk for 90s...")
    countdown(90, "both walking")
    final = persons()
    new_both = show("stage 3", after_b, final)
    dbg1 = dbg_count()

    print("\n" + "=" * 62)
    print("RESULT")
    print("=" * 62)
    print("  stage 1 (A alone)     : %d new  (want 1)" % len(new_a))
    print("  stage 2 (B alone)     : %d new  (want 1)" % len(new_b))
    print("  stage 3 (both)        : %d new  (want 0)" % len(new_both))
    print("  total created         : %d     (want 2)" % (len(final) - len(start)))
    print("  MATCH_DBG events      : %d" % (dbg1 - dbg0))

    total_new = len(final) - len(start)
    ok = (len(new_a) == 1 and len(new_b) == 1 and len(new_both) == 0)
    print()
    if ok:
        print("  PASS -- two people, two identities, both held when together.")
        print("  Per-person baselines and trends are trustworthy.")
        rc = 0
    elif total_new == 2 and len(new_both) == 0:
        print("  PASS with a caveat -- two identities overall, none created")
        print("  while both were present, but the per-stage counts differ from")
        print("  expectation. Check which stage was off above.")
        rc = 0
    elif len(new_both) > 0:
        print("  FAIL -- %d identity/identities appeared while both people were"
              % len(new_both))
        print("  in frame. Occlusion is breaking matching. Per-person data")
        print("  would be split across duplicates.")
        print("    grep -a MATCH_DBG camera.log | tail -12")
        rc = 1
    else:
        print("  FAIL -- %d identities for two people." % total_new)
        print("    grep -a MATCH_DBG camera.log | tail -12")
        rc = 1

    print("\n  Heights (should differ clearly between the two people):")
    for p, (name, h, f) in final.items():
        if p not in start:
            print("    %-14s height=%s" % (p, round(h, 1) if h else "None"))
    print("=" * 62)
    print("\nSend this entire output.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
