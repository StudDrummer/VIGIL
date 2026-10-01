#!/usr/bin/env python3
"""
VIGIL facial cue service. Runs on the ~/vigil-face venv python.

Reads the upscaled head crop the camera pipeline writes, measures two
things, and posts them to the API:

  yawn      jawOpen above threshold for N consecutive samples. Single
            frames are ignored because talking briefly spikes jawOpen.
  PERCLOS   fraction of the last 60 s with eyes closed. This is the
            standard fatigue measure in driver-monitoring research.

Thresholds live in face_thresholds.json and were measured on this
camera at room distance, not taken from published values.
"""
import json
import os
import sys
import time
from collections import deque

import cv2
import requests
import mediapipe as mp
from mediapipe.tasks import python as mtp
from mediapipe.tasks.python import vision

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "face_thresholds.json")))
MODEL = os.path.join(BASE, "face_landmarker.task")
API_KEY = os.environ.get("VIGIL_API_KEY", "")
SERVER = CFG.get("server", "http://127.0.0.1:5001")
IMG = CFG.get("face_image", "/dev/shm/vigil_face.jpg")


def post(event_type, value):
    try:
        requests.post(SERVER + "/face/event",
                      json={"event_type": event_type, "value": round(float(value), 4)},
                      headers={"X-VIGIL-Key": API_KEY}, timeout=4)
    except Exception as e:
        print("[face] post failed: %s" % e, flush=True)


def main():
    if not os.path.exists(MODEL):
        print("[face] model missing: %s" % MODEL, flush=True)
        return 1
    det = vision.FaceLandmarker.create_from_options(
        vision.FaceLandmarkerOptions(
            base_options=mtp.BaseOptions(model_asset_path=MODEL),
            output_face_blendshapes=True, num_faces=1))
    print("[face] service started", flush=True)

    consec = 0
    last_yawn = 0.0
    closed = deque()            # (t, is_closed)
    last_perclos = time.time()
    last_mtime = 0.0
    seen = 0

    while True:
        try:
            st = os.stat(IMG)
        except FileNotFoundError:
            time.sleep(0.5)
            continue
        if st.st_mtime == last_mtime:
            time.sleep(0.05)
            continue
        last_mtime = st.st_mtime

        img = cv2.imread(IMG)
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        try:
            res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        except Exception:
            continue
        if not res.face_landmarks or not res.face_blendshapes:
            consec = 0
            continue

        seen += 1
        bs = {c.category_name: c.score for c in res.face_blendshapes[0]}
        jaw = bs.get("jawOpen", 0.0)
        blinks = [bs.get("eyeBlinkLeft"), bs.get("eyeBlinkRight")]
        blinks = [b for b in blinks if b is not None]
        blink = sum(blinks) / len(blinks) if blinks else 0.0
        now = time.time()

        # --- yawn: sustained, not a single frame -------------------------
        if jaw >= CFG["yawn_jaw_open"]:
            consec += 1
            if (consec >= CFG["yawn_min_consecutive"]
                    and now - last_yawn > CFG["yawn_cooldown_s"]):
                last_yawn = now
                print("[face] yawn jawOpen=%.3f" % jaw, flush=True)
                post("yawn", jaw)
        else:
            consec = 0

        # --- PERCLOS over a rolling window -------------------------------
        closed.append((now, blink >= CFG["eye_closed_blink"]))
        cutoff = now - CFG["perclos_window_s"]
        while closed and closed[0][0] < cutoff:
            closed.popleft()
        if now - last_perclos >= CFG["perclos_report_s"] and len(closed) >= 20:
            last_perclos = now
            frac = sum(1 for _t, c in closed if c) / float(len(closed))
            print("[face] perclos=%.3f n=%d" % (frac, len(closed)), flush=True)
            post("perclos", frac)

        if seen % 200 == 0:
            print("[face] %d faces processed" % seen, flush=True)


if __name__ == "__main__":
    sys.exit(main())
