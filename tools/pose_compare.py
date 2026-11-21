#!/usr/bin/env python3
"""MODEL_CHECK_V1: compare two pose models on the fall lab's own video clips, on the Jetson.

    python3 ~/VIGIL/tests/fall_lab.py --video            # once: records a clip of every trial
    python3 ~/VIGIL/tools/pose_compare.py                # yolov8n-pose (today) vs yolo11n-pose
    python3 ~/VIGIL/tools/pose_compare.py A.pt B.pt      # any two models

For every clip, each model finds people and their 17 body points, ByteTrack follows them (the same
tracker settings VIGIL uses), and the result goes through VIGIL's own fall detector with the room's
bed and couch areas. A clip whose name contains "_fall_" is a real fall; the others are ordinary
activity. It prints, per model: falls caught, ordinary activity left alone, frames per second on
this machine, how often a person was found, and average body-point confidence.

Switch only if the new model catches at least as many falls, leaves ordinary activity alone at
least as well, and runs at least as fast. To switch, add to the camera service:
    Environment=VIGIL_POSE_MODEL=yolo11n-pose.pt
Nothing leaves the Jetson; the clips are read locally.
"""
import glob
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("VIGIL_JETSON_DIR", os.path.expanduser("~/passive-health-moniter"))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "jetson-backend"))


def load_zones():
    try:
        with open(os.path.join(BASE, "fall_zones.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def keypoint_tracks(model, path, tracker_cfg):
    """{track id: [(t, 17x3 keypoints in pixels)]}, frame size, frames/s of inference, found ratio, mean conf."""
    import cv2
    times = None
    try:
        with open(path + ".json") as f:
            times = json.load(f)["times"]
    except (OSError, ValueError, KeyError):
        pass
    cap = cv2.VideoCapture(path)
    tracks, n, found, confs, spent, shape = {}, 0, 0, [], 0.0, None
    try:
        model.predictor = None                     # fresh tracker state for every clip
    except Exception:
        pass
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        shape = frame.shape
        t = times[n] if times and n < len(times) else n / 15.0
        n += 1
        t0 = time.time()
        r = model.track(frame, persist=True, tracker=tracker_cfg, verbose=False)[0]
        spent += time.time() - t0
        if r.keypoints is None or len(r.keypoints.data) == 0:
            continue
        found += 1
        kps = r.keypoints.data.cpu().numpy()
        ids = r.boxes.id.cpu().numpy().astype(int) if r.boxes is not None and r.boxes.id is not None else range(len(kps))
        for tid, kp in zip(ids, kps):
            tracks.setdefault(int(tid), []).append((t, kp.astype(np.float32)))
            confs.append(float(kp[:, 2].mean()))
    cap.release()
    return tracks, shape, (n / spent if spent else 0.0), (found / n if n else 0.0), (float(np.mean(confs)) if confs else 0.0)


def main():
    from ultralytics import YOLO                    # first: fall_eval stubs ultralytics if it is missing
    import fall_eval                                # VIGIL's own fall detector, replayed offline
    models = sys.argv[1:3] if len(sys.argv) >= 3 else ["yolov8n-pose.pt", "yolo11n-pose.pt"]
    clips = sorted(glob.glob(os.path.join(BASE, "fall_probes", "videos", "*.mp4")))
    if not clips:
        sys.exit("No clips yet. Run: python3 ~/VIGIL/tests/fall_lab.py --video")
    tracker = os.path.join(BASE, "vigil_bytetrack.yaml")
    if not os.path.exists(tracker):
        tracker = os.path.join(HERE, "..", "jetson-backend", "vigil_bytetrack.yaml")
    zones = load_zones()
    print("%d clips, %d bed/couch areas\n" % (len(clips), len(zones)))
    summary = {}
    for m in models:
        try:
            model = YOLO(m)
        except Exception as e:
            print("%s: could not load (%s). YOLO11 needs ultralytics 8.3 or newer: pip install -U ultralytics" % (m, e))
            continue
        caught = falls = quiet = ordinary = 0
        fps_all, found_all, conf_all = [], [], []
        print("== %s" % m)
        for c in clips:
            name = os.path.basename(c)
            is_fall = "_fall_" in name
            tracks, shape, fps, found, conf = keypoint_tracks(model, c, tracker)
            fps_all.append(fps); found_all.append(found); conf_all.append(conf)
            alerted = False
            for frames in tracks.values():
                if len(frames) < 5 or shape is None:
                    continue
                fall_at, floor_at = fall_eval.run(frames, shape, zones)
                if fall_at is not None or (is_fall and floor_at is not None):
                    alerted = True
                    break
            if is_fall:
                falls += 1; caught += alerted
            else:
                ordinary += 1; quiet += not alerted
            print("  %-5s %-46s %s" % ("fall" if is_fall else "ok", name[:46],
                                        ("RIGHT" if alerted == is_fall else "WRONG") + ("  (alert)" if alerted else "")))
        summary[m] = (caught, falls, quiet, ordinary, np.mean(fps_all), np.mean(found_all), np.mean(conf_all))
    print("\n%-20s %-14s %-18s %-8s %-10s %s" % ("model", "falls caught", "ordinary left alone", "fps", "person found", "point conf"))
    for m, (c, f, q, o, fps, fd, cf) in summary.items():
        print("%-20s %-14s %-18s %-8.1f %-10s %.2f" % (m, "%d/%d" % (c, f), "%d/%d" % (q, o), fps, "%.0f%%" % (fd * 100), cf))
    if len(summary) == 2:
        (a, sa), (b, sb) = summary.items()
        better = sb[0] >= sa[0] and sb[2] >= sa[2] and sb[4] >= sa[4] * 0.95
        print("\n%s %s." % (b, "is at least as good on every count: worth switching" if better
                            else "is not better on every count: keep %s" % a))


if __name__ == "__main__":
    main()
