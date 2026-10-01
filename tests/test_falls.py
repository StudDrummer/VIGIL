#!/usr/bin/env python3
"""
VIGIL fall detection validation.

Run:  python3 test_falls.py

Walks through real falls (onto a mattress) and ordinary activities that
commonly cause false alarms, and counts the detector's decisions directly
from camera.log.

Why the log and not the alerts table: the server rate-limits fall alerts
and rejects a rate-limited request BEFORE saving it, so during rapid
testing the alerts table undercounts. The detector prints [FALL] every
time it decides a fall happened, independent of the server.

The detector has a 30 s internal cooldown, so each scenario starts after
a 35 s gap.
"""
import os
import re
import subprocess
import sys
import time

BASE = "/home/rushil-mohan/passive-health-moniter"
LOG = os.path.join(BASE, "camera.log")
SLOG = os.path.join(BASE, "server.log")
GAP_S = 35

SCENARIOS = [
    ("Control: walk normally", False,
     "Walk normally back and forth in full view.", 20),
    ("Sit down quickly in a chair", False,
     "Walk to a chair and sit down briskly, as you would when tired.", 20),
    ("Bend to pick something up", False,
     "Bend over and pick up an object from the floor, then stand.", 20),
    ("Crouch and hold 10 s", False,
     "Squat or crouch down, hold about 10 seconds, then stand.", 25),
    ("Lie down slowly on bed or couch", False,
     "Lie down on a bed or couch the way you normally would. Stay 10 s.", 30),
    ("FALL: sideways onto mattress", True,
     "From KNEELING, drop sideways onto the mattress. Stay still 6 s.", 35),
    ("FALL: forward onto mattress", True,
     "From KNEELING, drop forward onto the mattress. Stay still 6 s.", 35),
    ("FALL: slump from chair to floor", True,
     "Sitting on a chair next to the mattress, slide off onto it. Stay 6 s.", 35),
]


def read_lines(path):
    try:
        with open(path, errors="replace") as f:
            return f.read().split("\n")
    except Exception:
        return []


def countdown(s, msg):
    for r in range(s, 0, -1):
        sys.stdout.write("\r    %s -- %2ds " % (msg, r))
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r" + " " * 64 + "\r")


def main():
    print("=" * 64)
    print("VIGIL FALL DETECTION VALIDATION")
    print("=" * 64)
    print("""
SAFETY -- read before starting:
  * Put a mattress or thick cushions FLAT ON THE FLOOR, fully in view.
  * Never fall from standing. Start every fall from kneeling.
  * Have someone nearby. Stop immediately if anything hurts.
  * The first detected fall will send a real SMS to the emergency
    contact. That is expected -- it also tests the alert path.
""")
    st = subprocess.run("systemctl is-active vigil-camera", shell=True,
                        capture_output=True, text=True).stdout.strip()
    print("camera service: %s" % st)
    if st != "active":
        print("Camera service is not running. Aborting.")
        return 1
    input("Press Enter when the mattress is set up and you are ready...")

    test_start = len(read_lines(SLOG))
    results = []

    for idx, (name, should, instr, window) in enumerate(SCENARIOS, 1):
        print("\n" + "-" * 64)
        print("[%d/%d] %s   (expected: %s)" %
              (idx, len(SCENARIOS), name, "DETECT" if should else "NO ALERT"))
        print("      %s" % instr)
        if idx > 1:
            countdown(GAP_S, "cooldown, relax")
        input("      Press Enter, then do it immediately...")
        start = len(read_lines(LOG))
        countdown(window, "recording")
        new = read_lines(LOG)[start:]
        falls = [ln for ln in new if "[FALL]" in ln]
        results.append((name, should, len(falls), falls))
        verdict = ("detected %d" % len(falls)) if falls else "no alert"
        ok = (len(falls) > 0) == should
        print("      result: %s   %s" % (verdict, "PASS" if ok else "FAIL"))
        for ln in falls[:2]:
            print("        %s" % ln.strip())

    print("\n" + "=" * 64)
    print("RESULTS")
    print("=" * 64)
    tp = sum(1 for n, s, c, _ in results if s and c > 0)
    fn = sum(1 for n, s, c, _ in results if s and c == 0)
    fp = sum(1 for n, s, c, _ in results if not s and c > 0)
    tn = sum(1 for n, s, c, _ in results if not s and c == 0)
    for n, s, c, _ in results:
        ok = (c > 0) == s
        print("  %-36s %-9s %s" % (n, ("DETECT" if c else "none"), "PASS" if ok else "FAIL"))
    nf = tp + fn
    nn = fp + tn
    print("\n  Sensitivity:      %d of %d falls detected" % (tp, nf))
    print("  False positives:  %d of %d ordinary activities flagged" % (fp, nn))

    print("\n  Alert path (server.log since test start):")
    slog = read_lines(SLOG)[test_start:]
    posts = [l for l in slog if "/alert/fall" in l]
    sms = [l for l in slog if "[SMS]" in l]
    print("    /alert/fall requests: %d" % len(posts))
    for l in posts[:4]:
        print("      %s" % l.strip())
    print("    SMS log lines: %d" % len(sms))
    for l in sms[:4]:
        print("      %s" % l.strip())
    print("\nSend this entire output.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
