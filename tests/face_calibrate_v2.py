#!/usr/bin/env python3
"""
VIGIL facial cue calibration (MediaPipe 1.x Tasks API).

Run in the venv:
    source ~/vigil-face/bin/activate
    python ~/passive-health-moniter/face_calibrate_v2.py --measure
    deactivate

WHY v2
------
mediapipe 1.0.1 removed mp.solutions. Version 1.x uses the Tasks API and
needs a downloaded model:

    curl -L -o face_landmarker.task \\
      https://storage.googleapis.com/mediapipe-models/face_landmarker/\\
face_landmarker/float16/1/face_landmarker.task

That API also exposes BLENDSHAPES -- 52 normalised expression scores.
Two matter here:

    jawOpen        how far the jaw is open, 0-1
    eyeBlinkL/R    how closed each eye is, 0-1

Blendshapes are better than raw pixel ratios for this application. A
mouth-aspect ratio shrinks as the face gets smaller in frame, so a
threshold tuned at two metres fails at four. A blendshape is normalised
by the model, so it stays comparable across distance.

Geometric MAR/EAR are collected too, as a cross-check and in case the
blendshapes turn out not to separate.

The talking stage is the one that decides whether this is shippable:
talking opens the mouth as well, so if yawning does not separate from
talking, the feature would fire during normal conversation.
"""
import argparse
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(BASE, "face_landmarker.task")
RAW = "/dev/shm/vigil_face.jpg"

MOUTH_TOP, MOUTH_BOT, MOUTH_L, MOUTH_R = 13, 14, 78, 308
L_EYE = (33, 160, 158, 133, 153, 144)
R_EYE = (362, 385, 387, 263, 373, 380)


def _d(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _ear(p, idx):
    a, b, c, d, e, f = [p[i] for i in idx]
    w = _d(a, d)
    return None if w <= 0 else (_d(b, f) + _d(c, e)) / (2.0 * w)


def _mar(p):
    w = _d(p[MOUTH_L], p[MOUTH_R])
    return None if w <= 0 else _d(p[MOUTH_TOP], p[MOUTH_BOT]) / w


def _stats(name, v):
    if not v:
        print("    %-10s no samples" % name)
        return None
    s = sorted(v)
    n = len(s)
    print("    %-10s n=%-4d min=%.3f p25=%.3f med=%.3f p75=%.3f max=%.3f"
          % (name, n, s[0], s[n // 4], s[n // 2], s[3 * n // 4], s[-1]))
    return s[n // 2]


def _p90(v):
    if not v:
        return None
    s = sorted(v)
    return s[int(0.9 * (len(s) - 1))]


def measure():
    try:
        import cv2
        import numpy as np
        import mediapipe as mp
        from mediapipe.tasks import python as mtp
        from mediapipe.tasks.python import vision
    except ImportError as e:
        print("Missing module: %s" % e)
        print("Activate the venv first: source ~/vigil-face/bin/activate")
        return 1

    if not os.path.exists(MODEL):
        print("Model not found: %s" % MODEL)
        print("Download it (see the header of this file).")
        return 1
    if not os.path.exists(RAW):
        print("%s not found. Run face_calibrate.py --patch with the MAIN" % RAW)
        print("python, restart vigil-camera, and stand in view.")
        return 1

    det = vision.FaceLandmarker.create_from_options(
        vision.FaceLandmarkerOptions(
            base_options=mtp.BaseOptions(model_asset_path=MODEL),
            output_face_blendshapes=True,
            num_faces=1))

    print("=" * 62)
    print("FACIAL CUE CALIBRATION")
    print("=" * 62)
    print("\nStand where a resident would normally be, NOT close to the camera.")
    print("Each stage samples 20 seconds. Face the camera throughout.\n")

    stages = [
        ("neutral", "Mouth closed, eyes open, look toward the camera."),
        ("yawning", "Yawn repeatedly -- wide, held about 2 seconds each."),
        ("eyes_closed", "Keep both eyes closed the entire time."),
        ("talking", "Talk continuously. This must NOT look like yawning."),
    ]
    out = {}

    for key, instr in stages:
        print("[%s] %s" % (key.upper(), instr))
        input("  Press Enter, then begin...")
        jaw, blink, mar, ear = [], [], [], []
        frames = faces = 0
        last = 0
        end = time.time() + 20
        while time.time() < end:
            sys.stdout.write("\r    sampling %2ds  faces=%d " % (int(end - time.time()), faces))
            sys.stdout.flush()
            try:
                st = os.stat(RAW)
                if st.st_mtime == last:
                    time.sleep(0.05)
                    continue
                last = st.st_mtime
                img = cv2.imread(RAW)
            except Exception:
                time.sleep(0.1)
                continue
            if img is None:
                continue
            frames += 1
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
            if not res.face_landmarks:
                continue
            faces += 1

            if res.face_blendshapes:
                bs = {c.category_name: c.score for c in res.face_blendshapes[0]}
                if "jawOpen" in bs:
                    jaw.append(bs["jawOpen"])
                bl = [bs.get("eyeBlinkLeft"), bs.get("eyeBlinkRight")]
                bl = [x for x in bl if x is not None]
                if bl:
                    blink.append(sum(bl) / len(bl))

            h, w = img.shape[:2]
            lm = res.face_landmarks[0]
            pts = {}
            for i in set([MOUTH_TOP, MOUTH_BOT, MOUTH_L, MOUTH_R] + list(L_EYE) + list(R_EYE)):
                pts[i] = (lm[i].x * w, lm[i].y * h)
            m = _mar(pts)
            le, re = _ear(pts, L_EYE), _ear(pts, R_EYE)
            if m is not None:
                mar.append(m)
            if le is not None and re is not None:
                ear.append((le + re) / 2.0)

        sys.stdout.write("\r" + " " * 60 + "\r")
        rate = (100.0 * faces / frames) if frames else 0
        print("  frames=%d  face found in %d (%.0f%%)" % (frames, faces, rate))
        _stats("jawOpen", jaw)
        _stats("eyeBlink", blink)
        _stats("MAR", mar)
        _stats("EAR", ear)
        out[key] = {"jaw": jaw, "blink": blink, "mar": mar, "ear": ear,
                    "rate": rate, "frames": frames}
        print()

    print("=" * 62)
    print("VERDICT")
    print("=" * 62)

    rates = [out[k]["rate"] for k in out]
    avg_rate = sum(rates) / max(1, len(rates))
    print("  face detection rate: %.0f%% of frames" % avg_rate)
    if avg_rate < 40:
        print("  Too low to build on. The face is too small at this distance.")
        print("  This is a real constraint of room-scale monitoring, worth")
        print("  reporting as a finding rather than shipping a flaky feature.")

    # yawn separation: yawning peaks vs the worst of neutral/talking
    yj = _p90(out["yawning"]["jaw"])
    nj = _p90(out["neutral"]["jaw"])
    tj = _p90(out["talking"]["jaw"])
    print("\n  jawOpen 90th percentile:")
    print("    neutral %s | talking %s | yawning %s"
          % (("%.3f" % nj) if nj else "NA",
             ("%.3f" % tj) if tj else "NA",
             ("%.3f" % yj) if yj else "NA"))
    if yj and nj and tj:
        floor = max(nj, tj)
        if yj > floor * 1.3:
            print("    yawn threshold: %.3f  (separates from talking)"
                  % (floor + (yj - floor) * 0.5))
        else:
            print("    NOT separable from talking -- yawn detection would fire")
            print("    during normal conversation. Do not ship it on this signal.")

    nb = _p90(out["neutral"]["blink"])
    cb = _p90(out["eyes_closed"]["blink"])
    print("\n  eyeBlink 90th percentile:")
    print("    open %s | closed %s"
          % (("%.3f" % nb) if nb else "NA", ("%.3f" % cb) if cb else "NA"))
    if nb is not None and cb is not None:
        if cb > nb * 1.5:
            print("    eye-closed threshold: %.3f" % (nb + (cb - nb) * 0.5))
        else:
            print("    NOT separable -- eye state unreliable at this distance.")

    print("\nSend this entire output.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="store_true")
    a = ap.parse_args()
    if a.measure:
        sys.exit(measure())
    ap.print_help()
