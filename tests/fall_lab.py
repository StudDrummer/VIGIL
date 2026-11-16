#!/usr/bin/env python3
"""FALL_LAB_V1: guided fall test that RECORDS every trial.

Run on the Jetson, with VIGIL running and the camera seeing the whole room:

    tmux new -s lab        (keeps running if the connection drops; reattach: tmux attach -t lab)
    python3 ~/VIGIL/tests/fall_lab.py

For each trial it tells you what to do, counts down, then (a) checks whether
VIGIL raised a fall alert and (b) saves the stick figures of the last 40 s
(fall_probes/NN_label.json). At the end it prints the score and packs every
recording into ~/vigil_fall_lab.tar.gz, so the detector can be replayed and
tuned on YOUR falls, not made-up ones.

Use a thick mattress or crash pad for every fall. Stop at any time with Ctrl-C:
everything recorded so far is kept and packed.
"""
import json
import os
import re
import subprocess
import sys
import tarfile
import time

BASE = os.environ.get("VIGIL_JETSON_DIR", os.path.expanduser("~/passive-health-moniter"))
LOGS = [os.path.join(BASE, p) for p in ("camera.log", "logs/camera.log")]
PROBES = os.path.join(BASE, "fall_probes")
FLAG = "/dev/shm/vigil_probe"
GAP_S = 32           # the detector waits 30 s between alerts

# (label, is_fall, instructions, seconds to do it and stay)
TRIALS = [
    ("walk", False, "Walk normally back and forth across the room.", 20),
    ("sit_chair_fast", False, "Walk to a chair and drop into it briskly, as you would when tired.", 20),
    ("bend_pick_up", False, "Bend down, pick something up off the floor, stand up.", 15),
    ("crouch_10s", False, "Crouch or squat, hold 10 seconds, stand up.", 20),
    ("lie_bed", False, "Lie down on the bed or couch the normal way. Stay 10 seconds.", 25),
    ("sit_floor_on_purpose", False, "Sit down on the floor on purpose, cross-legged, like you would to talk or eat. Stay 20 s.", 35),
    ("kneel_look_under", False, "Kneel and look under the bed or couch for 10 seconds, then stand.", 20),
    ("fall_forward", True, "From KNEELING by the mattress, fall forward onto it. Stay still 20 s.", 30),
    ("fall_sideways", True, "From KNEELING, fall sideways onto the mattress. Stay still 20 s.", 30),
    ("fall_backward_sit", True, "From SQUATTING, fall back onto your bottom on the mattress, then lie back. Stay 20 s.", 30),
    ("fall_toward_camera", True, "Kneel facing the camera and fall forward TOWARD the camera onto the mattress. Stay 20 s.", 30),
    ("fall_slump_chair_to_floor", True, "Sit on a chair at the mattress edge, slide off onto the mattress quickly, end SITTING on it. Stay 25 s.", 40),
    ("fall_slow_collapse", True, "From standing, sink slowly (about 3 s) down to the mattress as if fainting, end lying. Stay 20 s.", 30),
    ("fall_behind_furniture", True, "Fall onto the mattress where a chair or table partly hides you. Stay 20 s.", 30),
]


def log_size():
    for p in LOGS:
        if os.path.exists(p):
            return p, os.path.getsize(p)
    return None, 0


def falls_since(path, offset):
    if not path:
        return []
    with open(path, errors="replace") as f:
        f.seek(offset)
        return [l.strip() for l in f if "[FALL]" in l]


def countdown(s, msg):
    for r in range(s, 0, -1):
        sys.stdout.write("\r    %s %2ds " % (msg, r)); sys.stdout.flush(); time.sleep(1)
    print("\r    %s done.   " % msg)


def pack(results):
    with open(os.path.join(PROBES, "lab_results.json"), "w") as f:
        json.dump(results, f, indent=1)
    out = os.path.expanduser("~/vigil_fall_lab.tar.gz")
    with tarfile.open(out, "w:gz") as tz:
        tz.add(PROBES, arcname="fall_probes")
        for p in LOGS:
            if os.path.exists(p):
                tz.add(p, arcname="camera.log")
                break
    print("\nPacked: %s  (send this file)" % out)


def main():
    os.makedirs(PROBES, exist_ok=True)
    path, _ = log_size()
    if not path:
        print("Cannot find camera.log in %s. Set VIGIL_JETSON_DIR." % BASE)
        sys.exit(1)
    print(__doc__)
    # LAB_RESUME_V1: results are saved after every trial; a dropped connection loses nothing
    saved = os.path.join(PROBES, "lab_results.json")
    results, start = [], 1
    try:
        prev = json.load(open(saved))
        if prev and len(prev) < len(TRIALS) and input("Continue the last run from trial %d? (Y/n) " % (len(prev) + 1)).lower() != "n":
            results, start = prev, len(prev) + 1
    except Exception:
        pass
    input("Press Enter when the room is clear, the mattress is down and you are ready...")
    stamp = time.strftime("%m%d%H%M")
    try:
        for i, (label, is_fall, how, secs) in enumerate(TRIALS, 1):
            if i < start:
                continue
            print("\n[%d/%d] %s%s" % (i, len(TRIALS), "FALL: " if is_fall else "", how))
            input("    Press Enter, then start within 3 seconds...")
            path, off = log_size()
            countdown(3, "get ready")
            countdown(secs, "go")
            name = "%s_%02d_%s" % (stamp, i, label)
            with open(FLAG, "w") as f:
                f.write(name)
            time.sleep(2.5)                        # the camera saves the recording within 1 s
            hits = falls_since(path, off)
            ok = bool(hits) == is_fall
            results.append({"label": label, "is_fall": is_fall, "alerted": bool(hits), "log": hits, "probe": name})
            print("    %s  VIGIL %s" % ("RIGHT" if ok else "WRONG", "raised a fall alert" if hits else "stayed quiet"))
            json.dump(results, open(saved, "w"), indent=1)
            if i < len(TRIALS):
                countdown(GAP_S, "stand up and rest")
    except KeyboardInterrupt:
        print("\nStopped early.")
    f_ = [r for r in results if r["is_fall"]]; a_ = [r for r in results if not r["is_fall"]]
    print("\nFalls caught %d/%d   ordinary activity left alone %d/%d" % (
        sum(r["alerted"] for r in f_), len(f_), sum(not r["alerted"] for r in a_), len(a_)))
    pack(results)


if __name__ == "__main__":
    main()
