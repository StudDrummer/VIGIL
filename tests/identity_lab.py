#!/usr/bin/env python3
"""IDENTITY_LAB_V1: measures how well VIGIL names people, on the Jetson.

    python3 ~/VIGIL/tests/identity_lab.py

You type who is in your home (as named in the app). Then, one person at a
time (and pairs if you want), each does a short routine in front of the camera:
face it, walk around, turn away, sit, leave. Twice a second the lab reads who
VIGIL says is in view (the same list the app shows) and compares it with who
is really there. At the end it prints, per person:

  right   their name was shown          wrong   someone else's name was shown
  unsure  "Identifying..." or no name   first   seconds until their name first appeared

and packs the results and the camera log into ~/vigil_identity_lab.tar.gz.
"""
import json
import os
import sqlite3
import sys
import tarfile
import time

BASE = os.environ.get("VIGIL_JETSON_DIR", os.path.expanduser("~/passive-health-moniter"))
IN_VIEW = "/dev/shm/vigil_in_view.json"
ROUTINE = [("Walk in and stand facing the camera", 12), ("Walk around the room", 20),
           ("Turn your back to the camera", 10), ("Sit down in a chair", 12), ("Stand up and walk out of view", 8)]


def names():
    c = sqlite3.connect(os.path.join(BASE, "vigil.db"))
    m = {r[0]: r[1] for r in c.execute("SELECT id, name FROM persons WHERE name IS NOT NULL AND TRIM(name) != ''")}
    c.close()
    return m


def sample(idmap):
    try:
        d = json.load(open(IN_VIEW))
        if time.time() - float(d.get("t", 0)) > 3:
            return None
    except Exception:
        return None
    ids = d.get("ids") or []
    labels = d.get("labels") or []
    shown = [idmap.get(p, "?") for p in ids if p]
    unsure = sum(1 for i, p in enumerate(ids) if not p and not (i < len(labels) and labels[i] == "__dup__"))
    return shown, unsure


def run_trial(people_here, idmap):
    log = []
    t0 = time.time()
    for step, secs in ROUTINE:
        print("    %s (%d s)" % (step, secs))
        end = time.time() + secs
        while time.time() < end:
            s = sample(idmap)
            if s is not None:
                log.append((round(time.time() - t0, 1), step, s[0], s[1]))
            time.sleep(0.5)
    return log


def score(log, person, others):
    right = wrong = unsure = 0
    first = None
    for t, step, shown, uns in log:
        if step.startswith("Stand up and walk out"):
            continue
        if person in shown:
            right += 1
            first = t if first is None else first
        elif any(n != "?" for n in shown):     # someone else's name on them
            wrong += 1
        else:
            unsure += 1
    n = max(1, right + wrong + unsure)
    return {"right": round(right / n, 2), "wrong": round(wrong / n, 2), "unsure": round(unsure / n, 2), "first_s": first}


def main():
    print(__doc__)
    idmap = names()
    print("People VIGIL knows: %s" % (", ".join(sorted(set(idmap.values()))) or "nobody yet"))
    who = [w.strip() for w in input("Who will take part (names as in the app, comma separated)? ").split(",") if w.strip()]
    results = {"people": who, "trials": []}
    try:
        for p in who:
            input("\n%s alone: everyone else out of view. Press Enter, then %s walks in..." % (p, p))
            log = run_trial([p], idmap)
            sc = score(log, p, [o for o in who if o != p])
            results["trials"].append({"who": [p], "score": sc, "log": log})
            print("    %s: right %d%%  wrong %d%%  unsure %d%%  first named after %s s" % (
                p, sc["right"] * 100, sc["wrong"] * 100, sc["unsure"] * 100, sc["first_s"]))
        if len(who) >= 2 and input("\nAlso test two people together? (y/N) ").lower().startswith("y"):
            a, b = who[0], who[1]
            input("%s and %s together. Press Enter, then both walk in..." % (a, b))
            log = run_trial([a, b], idmap)
            both = sum(1 for _t, _s, shown, _u in log if a in shown and b in shown)
            swap = sum(1 for _t, _s, shown, _u in log if shown.count(a) > 1 or shown.count(b) > 1)
            results["trials"].append({"who": [a, b], "both_named": round(both / max(1, len(log)), 2), "log": log})
            print("    both named correctly %d%% of the time" % (100 * both / max(1, len(log))))
    except KeyboardInterrupt:
        print("\nStopped early.")
    out = os.path.expanduser("~/vigil_identity_lab.tar.gz")
    js = os.path.join(BASE, "identity_lab_results.json")
    json.dump(results, open(js, "w"), indent=1)
    with tarfile.open(out, "w:gz") as tz:
        tz.add(js, arcname="identity_lab_results.json")
        for p in ("camera.log", "logs/camera.log"):
            if os.path.exists(os.path.join(BASE, p)):
                tz.add(os.path.join(BASE, p), arcname="camera.log")
                break
    print("\nPacked: %s  (send this file)" % out)


if __name__ == "__main__":
    main()
