#!/usr/bin/env python3
"""FALL_LAB_V1: replay stick-figure sequences through the fall detector.

  python3 tools/fall_eval.py                 synthetic scenarios at 5 and 2 frames/s
  python3 tools/fall_eval.py PROBES_DIR      real recordings from the fall lab (fall_probes/)

A probe's label says what really happened: labels starting with "fall" are
falls, everything else is ordinary activity. A fall counts as caught if the
detector raises a fall alert, or a floor alert within FLOOR_S seconds.
"""
import glob
import json
import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "jetson-backend"))
sys.modules.setdefault("ultralytics", types.SimpleNamespace(YOLO=object))
import activity_monitor as am  # noqa: E402
import fall_sim  # noqa: E402

FLOOR_S = float(os.environ.get("FALL_EVAL_FLOOR_S", am.FLOOR_ALERT_S))


def run(frames, shape, zones, cls=None):
    """frames: [(t, kp 17x3 pixels)]. Returns (fall_at, floor_at) seconds or None."""
    fd = (cls or am.FallDetector)()
    fd._zones = [(z["x1"], z["y1"], z["x2"], z["y2"]) for z in zones]
    fd._zmtime = -1                                   # no hot reload from disk
    fd._in_zone_orig = fd._in_zone
    t0 = frames[0][0]
    base = 1e6 if t0 < 1e5 else 0.0                   # synthetic clocks start at 0; real ones are wall time
    fall_at = floor_at = None
    for t, kp in frames:
        t = t + base
        fd._zchk = 1e18                               # keep the zones we set
        if fd.update(kp, shape, now=t) and fall_at is None:
            fall_at = t - base - t0
        if floor_at is None and fd.floor_alert_due(FLOOR_S, now=t):
            floor_at = t - base - t0
    return fall_at, floor_at


def synthetic(cls=None, quiet=False):
    rows, ok = [], 0
    for fps in (5.0, 2.0):
        for i, (name, is_fall, steps) in enumerate(fall_sim.SCENARIOS):
            for side in (1.0, -1.0):
                fr = fall_sim.sequence(steps, fps=fps, seed=i, d=side)
                if is_fall:                            # keep watching long enough for a floor alert
                    last = fr[-1][1]
                    t = fr[-1][0]
                    while t < FLOOR_S + 10:
                        t += 1.0 / fps
                        k = last.copy(); k[:, :2] += np.random.default_rng(int(t * 10)).normal(0, 3, (17, 2))
                        fr.append((t, k))
                fall_at, floor_at = run(fr, fall_sim.SHAPE, fall_sim.BED_ZONE, cls)
                alerted = fall_at is not None or (is_fall and floor_at is not None)
                good = alerted == is_fall
                ok += good
                rows.append((fps, name, side, is_fall, fall_at, floor_at, good))
    if not quiet:
        for fps, name, side, is_fall, fa, fl, good in rows:
            print("%s %2.0f fps %-44s %s  fall=%s floor=%s" % ("PASS" if good else "MISS" if is_fall else "FALSE",
                  fps, name + (" (mirrored)" if side < 0 else ""), "fall" if is_fall else "ok  ",
                  "-" if fa is None else "%.1fs" % fa, "-" if fl is None else "%.0fs" % fl))
    return ok, len(rows), rows


def probes(d, cls=None):
    rows = []
    truth = {}
    try:
        for r in json.load(open(os.path.join(d, "lab_results.json"))):
            truth[r["probe"]] = (bool(r["is_fall"]), bool(r["alerted"]))
    except Exception:
        pass
    for p in sorted(glob.glob(os.path.join(d, "*.json"))):
        if p.endswith("lab_results.json"):
            continue
        j = json.load(open(p))
        label = j.get("label") or os.path.basename(p)
        shape = tuple(j.get("shape") or fall_sim.SHAPE)
        tracks = j.get("tracks") or {}
        if not tracks:
            continue
        tid, fr = max(tracks.items(), key=lambda kv: len(kv[1]))
        frames = []
        for t, xy, c in fr:
            k = np.zeros((17, 3))
            k[:, 0] = np.array(xy)[:, 0] * shape[1]
            k[:, 1] = np.array(xy)[:, 1] * shape[0]
            k[:, 2] = c
            frames.append((t, k))
        zones = j.get("zones") or []
        fall_at, floor_at = run(frames, shape, zones, cls)
        is_fall = truth[label][0] if label in truth else ("fall" in label.lower().split("_"))
        alerted = fall_at is not None or (is_fall and floor_at is not None)
        rows.append((label, is_fall, fall_at, floor_at, alerted == is_fall, len(frames)))
        print("%s %-40s %s  fall=%s floor=%s (%d frames)" % ("PASS" if alerted == is_fall else "MISS" if is_fall else "FALSE",
              label, "fall" if is_fall else "ok  ", "-" if fall_at is None else "%.1fs" % fall_at,
              "-" if floor_at is None else "%.0fs" % floor_at, len(frames)))
    if rows:
        f = [r for r in rows if r[1]]; a = [r for r in rows if not r[1]]
        print("\nFalls caught %d/%d   ordinary activity left alone %d/%d" % (sum(r[4] for r in f), len(f), sum(r[4] for r in a), len(a)))
    return rows


if __name__ == "__main__":
    if len(sys.argv) > 1:
        probes(sys.argv[1])
    else:
        ok, n, rows = synthetic()
        f = [r for r in rows if r[3]]; a = [r for r in rows if not r[3]]
        print("\nFalls caught %d/%d   ordinary activity left alone %d/%d" % (sum(r[6] for r in f), len(f), sum(r[6] for r in a), len(a)))
