#!/usr/bin/env python3
"""
VIGIL re-identification test -- non-destructive.

Run:  python3 test_reid_safe.py

WHY A SECOND VERSION
--------------------
test_reid.py deletes person_detections and persons before it runs. That
was right when identities were garbage and there was nothing to lose; it
is wrong now. Running it after the merge erased 2,653 detections of real
history. This one deletes nothing.

WHAT IT MEASURES
----------------
Single-resident mode is off, so identity now comes from ByteTrack plus
height and torso/leg proportion matching, with no pin. This checks that
the same person stays one identity across:

  * continuous walking
  * leaving the room
  * returning at a different distance

It counts identities CREATED during the test rather than total rows, so
existing residents do not affect the result, and it reads MATCH_DBG from
the log as the authoritative signal -- the persons table only updates on
a reporting cycle and can lag.
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


def log_lines():
    try:
        with open(LOG, errors="replace") as f:
            return f.read().count("[MATCH_DBG]")
    except Exception:
        return 0


def persons():
    return {r[0]: (r[1], r[2]) for r in
            q("SELECT id, name, frames_seen FROM persons")}


def countdown(s, msg):
    for r in range(s, 0, -1):
        sys.stdout.write("\r    %s -- %2ds " % (msg, r))
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r" + " " * 60 + "\r")


def main():
    st = subprocess.run("systemctl is-active vigil-camera", shell=True,
                        capture_output=True, text=True).stdout.strip()
    print("=" * 62)
    print("RE-IDENTIFICATION TEST (non-destructive)")
    print("=" * 62)
    print("camera service : %s" % st)
    if st != "active":
        print("Camera is not running. Aborting.")
        return 1

    mode = q("SELECT single_resident_mode, resident_person_id FROM config")
    print("single-resident: %s  resident: %s"
          % (mode[0][0] if mode else "?", mode[0][1] if mode else "?"))

    before = persons()
    dbg0 = log_lines()
    print("\nexisting identities: %d" % len(before))
    for pid, (name, frames) in list(before.items())[:6]:
        print("  %-14s %-18s frames=%s" % (pid, name or "(unnamed)", frames))
    print("\nNothing will be deleted.")
    input("\nPress Enter when the room is empty and you are ready...")

    print("\n[1/3] CONTINUOUS APPEARANCE")
    print("      Walk in and keep moving for 90 seconds. Turn around a few")
    print("      times and cross close to the camera -- those are the moments")
    print("      tracking is most likely to break.")
    input("      Press Enter, then start walking...")
    countdown(90, "walking")

    mid = persons()
    new_mid = [p for p in mid if p not in before]
    print("      identities created: %d %s"
          % (len(new_mid), new_mid if new_mid else ""))

    print("\n[2/3] ABSENCE")
    input("      Leave the room completely, then press Enter...")
    countdown(30, "room empty")

    print("\n[3/3] RETURN AT A DIFFERENT DISTANCE")
    print("      Walk back in and stand noticeably closer or further than before.")
    input("      Press Enter, then walk back in...")
    countdown(60, "walking at a different distance")

    after = persons()
    dbg1 = log_lines()
    created = [p for p in after if p not in before]

    print("\n" + "=" * 62)
    print("RESULT")
    print("=" * 62)
    print("  identities created during test : %d" % len(created))
    print("  MATCH_DBG events (new identity): %d" % (dbg1 - dbg0))
    for pid in created:
        name, frames = after[pid]
        print("    %-14s %-18s frames=%s" % (pid, name or "(unnamed)", frames))

    print()
    if len(created) <= 1 and (dbg1 - dbg0) <= 1:
        print("  PASS -- you were recognised as one person throughout,")
        print("  including the return at a different distance.")
        rc = 0
    elif len(created) <= 1:
        print("  PARTIAL -- one identity in the table, but %d MATCH_DBG events."
              % (dbg1 - dbg0))
        print("  Duplicates existed in memory and were not all reported.")
        print("  Workable, but fragmentation is happening under the surface.")
        rc = 0
    else:
        print("  FAIL -- %d identities for one person." % len(created))
        print("  Without the single-resident pin, matching is not holding.")
        print("  Run this for the reasons each new identity was created:")
        print("    grep -a MATCH_DBG camera.log | tail -10")
        rc = 1
    print("=" * 62)
    print("\nSend this entire output.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
