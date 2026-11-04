"""Measures the face cue on real face photos (LFW) shrunk to the face sizes a
room camera sees, to set IDENTITY_V8's starting values honestly.
Usage: python3 tests/calibrate_face.py   (downloads LFW through scikit-learn once)"""
import os
import sys
import numpy as np
import cv2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import vigil_identity as vi  # noqa: E402
from sklearn.datasets import fetch_lfw_people  # noqa: E402

d = fetch_lfw_people(min_faces_per_person=30, resize=1.0, color=True, slice_=None,
                     data_home=os.environ.get("LFW_HOME", "/tmp/lfw"))
imgs, tgt = d.images, d.target
F = vi.FaceID()
assert F.ok, F.why
rng = np.random.default_rng(0)
people = sorted(set(tgt))
FACE_W = 105.0            # LFW faces are about this wide in the 250 px photos


def kp_for(s):
    c = 125 * s
    k = np.zeros((17, 3))
    k[:, 2] = 0.9
    k[0] = (c, 120 * s, .9); k[1] = (c - 18 * s, 110 * s, .9); k[2] = (c + 18 * s, 110 * s, .9)
    k[3] = (c - 45 * s, 115 * s, .9); k[4] = (c + 45 * s, 115 * s, .9)
    k[5] = (c - 110 * s, 235 * s, .9); k[6] = (c + 110 * s, 235 * s, .9)
    return k


for target in (24, 32, 45, 70, 105):
    s = target / FACE_W
    embs = {}
    tried = got = 0
    for p in people:
        idx = np.where(tgt == p)[0][:12]
        for i in idx:
            im = (imgs[i] * (255 if imgs[i].max() <= 1.0 else 1)).astype("uint8")[:, :, ::-1]
            # put the photo in a bigger frame, shrunk to this face size
            small = cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            tried += 1
            r = F.embed(small, kp_for(s))
            if r is None:
                continue
            got += 1
            embs.setdefault(p, []).append(r[0])
    same, diff = [], []
    ps = [p for p in embs if len(embs[p]) >= 2]
    for p in ps:
        v = embs[p]
        for a in range(len(v)):
            for b in range(a + 1, len(v)):
                same.append(float(v[a] @ v[b]))
    for _ in range(4000):
        a, b = rng.choice(ps, 2, replace=False)
        diff.append(float(embs[a][rng.integers(len(embs[a]))] @ embs[b][rng.integers(len(embs[b]))]))
    same, diff = np.array(same), np.array(diff)
    thr = 0.30
    print("face %3d px: found %3.0f%% | same %.2f+-%.2f  different %.2f+-%.2f | at %.2f: same kept %.0f%%, different wrongly kept %.1f%%"
          % (target, 100.0 * got / tried, same.mean(), same.std(), diff.mean(), diff.std(), thr,
             100 * (same >= thr).mean(), 100 * (diff >= thr).mean()))
