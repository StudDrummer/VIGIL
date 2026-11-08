"""
IDENTITY_V8: who is who, from every cue the camera has, learned from every
correction.

Five cues, each scored for a (track, person) pair:

  appearance  learned appearance fingerprint (vigil_reid.py, DINOv2)
  face        face match (OpenCV YuNet + SFace), only when a face is clearly
              visible; switch off per camera with VIGIL_FACE_ID=0
  body        body proportions: limb lengths relative to torso (person_hook)
  height      true standing height relative to everyone else (person_hook)
  walk        walking pattern: step rate, step length, arm swing

Each cue's raw score becomes evidence (a log-likelihood ratio: how much more
likely this score is for the same person than for someone else). The
distributions behind that are learned in this house: every correction or
confirmation in the app adds "same" examples for the named person and
"different" examples against everyone else, and two named people seen at the
same time are always "different". A cue that turns out not to separate the
people in this house gets a low weight; one that separates them well gets a
high one. Until enough examples exist, conservative starting values are used.

Nothing here stores images. Face and walk "templates" are lists of numbers,
kept only on this camera's computer, deleted with the person.
"""
import json
import math
import os
import threading
import time
from collections import deque

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNALS = ("appearance", "face", "body", "height", "walk")

# Starting values: (same mean, same sd, different mean, different sd, weight).
# appearance is filled in from the fingerprint model's own accept threshold.
# face values measured on LFW photos shrunk to room-distance face sizes
# (tests/calibrate_face.py); body/height/walk are deliberately cautious.
PRIORS = {
    "face": (0.50, 0.14, 0.06, 0.09, 1.0),
    "body": (-1.0, 0.6, -3.0, 1.3, 0.6),
    "height": (-0.03, 0.03, -0.10, 0.06, 0.5),
    "walk": (-1.0, 0.7, -2.5, 1.1, 0.4),
}
LLR_CLIP = 3.0
MIN_EXAMPLES = 12          # per class before a cue's own distribution replaces the starting value


def _gauss_ll(x, mu, sd):
    sd = max(sd, 1e-3)
    return -0.5 * ((x - mu) / sd) ** 2 - math.log(sd)


# ---------------------------------------------------------------- face
class FaceID:
    """Face embedding for one person's head region. Disabled cleanly when the
    models or the OpenCV face API are missing, or VIGIL_FACE_ID=0."""

    MIN_FACE_PX = 22        # face width in the camera frame; smaller faces are not used
    MIN_SCORE = 0.80

    def __init__(self):
        self.ok = False
        self.why = ""
        if os.environ.get("VIGIL_FACE_ID", "1") == "0":
            self.why = "turned off for this camera (VIGIL_FACE_ID=0)"
            return
        import cv2
        self.cv2 = cv2
        dirs = [os.environ.get("VIGIL_FACE_MODELS", ""), os.path.join(HERE, "models"), "/opt/vigil-code/models"]
        det = rec = None
        for d in dirs:
            if d and os.path.exists(os.path.join(d, "face_recognition_sface_2021dec.onnx")):
                det = os.path.join(d, "face_detection_yunet_2023mar.onnx")
                rec = os.path.join(d, "face_recognition_sface_2021dec.onnx")
                break
        if not rec:
            self.why = "face models not found"
            return
        if not hasattr(cv2, "FaceRecognizerSF"):
            self.why = "this OpenCV (%s) has no face API" % cv2.__version__
            return
        self.lock = threading.Lock()
        try:
            self.rec = cv2.FaceRecognizerSF.create(rec, "")
            self.rec.feature(np.zeros((112, 112, 3), dtype=np.uint8))
        except Exception as e:
            self.why = "could not load the face matching model: %s" % e
            return
        # The face finder (YuNet) needs OpenCV 4.8+. Without it, the face is
        # located from the eyes and nose the pose model already found
        # (FACE_KP_V1), which works on older OpenCV such as a Jetson's 4.5.4.
        self.mode = "keypoints"
        if os.environ.get("VIGIL_FACE_FINDER", "") != "keypoints" and hasattr(cv2, "FaceDetectorYN"):
            try:
                self.det = cv2.FaceDetectorYN.create(det, "", (320, 320), self.MIN_SCORE, 0.3, 5)
                self._det_path = det
                self.det.setInputSize((96, 96))
                self.det.detect(np.zeros((96, 96, 3), dtype=np.uint8))
                self.mode = "yunet"
            except Exception:
                self.mode = "keypoints"
        self.ok = True
        self.why = "face finder: %s" % ("YuNet" if self.mode == "yunet" else "eyes and nose from the pose model")

    def embed_photo(self, img):
        """PHOTO_SEED_V1: (vector, quality) for the largest clear face in a phone
        photo, or None. Needs the YuNet face finder (OpenCV 4.8+)."""
        if not self.ok or img is None or self.mode != "yunet":
            return None
        try:
            cv2 = self.cv2
            h, w = img.shape[:2]
            sc = min(1.0, 640.0 / max(h, w))
            if sc < 1.0:
                img = cv2.resize(img, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
            with self.lock:
                det = cv2.FaceDetectorYN.create(self._det_path, "", (img.shape[1], img.shape[0]), self.MIN_SCORE, 0.3, 5)
                _n, faces = det.detect(img)
                if faces is None or not len(faces):
                    return None
                f = max(faces, key=lambda r: r[2] * r[3])
                if f[2] < 40 or float(f[14]) < self.MIN_SCORE:
                    return None
                crop = self.rec.alignCrop(img, f)
                v = self.rec.feature(crop).flatten().astype(np.float32)
            n = float(np.linalg.norm(v))
            if n <= 0:
                return None
            return v / n, min(1.0, float(f[14]))
        except Exception:
            return None

    def embed(self, frame, kp):
        """(128-d unit vector, quality 0..1) for the face of the person with
        keypoints kp, or None when no clear face is visible."""
        if not self.ok or frame is None or kp is None:
            return None
        if self.mode == "keypoints":
            return self._embed_kp(frame, kp)
        try:
            k = np.asarray(kp, dtype=float)
            head = k[0:5]
            hv = head[head[:, 2] >= 0.3]
            if len(hv) < 2:
                return None
            cx, cy = float(hv[:, 0].mean()), float(hv[:, 1].mean())
            sh = k[5:7]
            sv = sh[sh[:, 2] >= 0.3]
            span = abs(float(sv[0][0]) - float(sv[1][0])) if len(sv) == 2 else 0.0
            span = max(span, float(hv[:, 0].max() - hv[:, 0].min()) * 2.5, 24.0)
            H, W = frame.shape[:2]
            x1, x2 = int(max(0, cx - span)), int(min(W, cx + span))
            y1, y2 = int(max(0, cy - span * 1.1)), int(min(H, cy + span * 0.9))
            if x2 - x1 < 16 or y2 - y1 < 16:
                return None
            crop = frame[y1:y2, x1:x2]
            scale = max(1.0, 240.0 / max(crop.shape[:2]))
            if scale > 1.0:
                crop = self.cv2.resize(crop, None, fx=scale, fy=scale, interpolation=self.cv2.INTER_CUBIC)
            with self.lock:
                self.det.setInputSize((crop.shape[1], crop.shape[0]))
                _n, faces = self.det.detect(crop)
                if faces is None or len(faces) == 0:
                    return None
                # the face nearest this person's nose, not someone behind them
                nx, ny = (cx - x1) * scale, (cy - y1) * scale
                f = min(faces, key=lambda r: (r[0] + r[2] / 2 - nx) ** 2 + (r[1] + r[3] / 2 - ny) ** 2)
                w_orig = float(f[2]) / scale
                if w_orig < self.MIN_FACE_PX or float(f[14]) < self.MIN_SCORE:
                    return None
                al = self.rec.alignCrop(crop, f)
                v = self.rec.feature(al).reshape(-1).astype("float32")
            n = float(np.linalg.norm(v))
            if n < 1e-6:
                return None
            q = min(1.0, w_orig / 70.0) * float(f[14])
            return v / n, q
        except Exception:
            return None


    def _embed_kp(self, frame, kp):
        """FACE_KP_V1: align the face from the pose keypoints (eyes, nose) and
        the standard face proportions, then compute the same face template."""
        try:
            k = np.asarray(kp, dtype=float)
            if min(k[0][2], k[1][2], k[2][2]) < 0.6:
                return None
            re_, le_, nose = k[2][:2], k[1][:2], k[0][:2]       # image-left eye first
            u = le_ - re_
            d = float(np.linalg.norm(u))
            if d < 9.0 or u[0] <= 0:
                return None                                   # too small, or facing away
            t = float(np.dot(nose - re_, u) / (d * d))
            if not 0.25 <= t <= 0.75:
                return None                                   # turned too far to the side
            mid = (re_ + le_) / 2
            mouth = mid + 2.03 * (nose - mid)
            rm, lm = mouth - 0.415 * u, mouth + 0.415 * u
            w = 2.4 * d
            row = np.array([[mid[0] - w / 2, mid[1] - w * 0.45, w, w * 1.2,
                             re_[0], re_[1], le_[0], le_[1], nose[0], nose[1],
                             rm[0], rm[1], lm[0], lm[1], float(min(k[0][2], k[1][2], k[2][2]))]],
                           dtype=np.float32)
            with self.lock:
                al = self.rec.alignCrop(frame, row)
                v = self.rec.feature(al).reshape(-1).astype("float32")
            n = float(np.linalg.norm(v))
            if n < 1e-6:
                return None
            q = min(1.0, d / 28.0) * float(min(k[0][2], k[1][2], k[2][2]))
            return v / n, q
        except Exception:
            return None


# ---------------------------------------------------------------- walk
class WalkTracker:
    """Per-track walking signature from keypoints: step rate, step length and
    arm swing, each relative to the person's own leg or arm length, measured
    only while they are actually walking."""

    def __init__(self):
        self.h = {}       # track -> deque of (t, hip_x, ankle_diff, wrist_diff, leg)

    def observe(self, tid, kp, now):
        if tid is None or kp is None:
            return
        k = np.asarray(kp, dtype=float)
        need = (11, 12, 13, 14, 15, 16)
        if any(k[i][2] < 0.35 for i in need):
            return
        hip = (k[11][:2] + k[12][:2]) / 2
        leg = (np.linalg.norm(k[11][:2] - k[15][:2]) + np.linalg.norm(k[12][:2] - k[16][:2])) / 2
        if leg < 30:
            return
        ad = (k[15][0] - k[16][0]) / leg
        wd = None
        if k[9][2] >= 0.35 and k[10][2] >= 0.35:
            wd = (k[9][0] - k[10][0]) / leg
        q = self.h.setdefault(tid, deque(maxlen=150))
        q.append((now, float(hip[0]), float(ad), wd, float(leg)))
        if len(self.h) > 300:
            for t in list(self.h)[:150]:
                self.h.pop(t, None)

    def signature(self, tid):
        """[steps per second, step length, arm swing] or None if no clear walk."""
        q = list(self.h.get(tid, ()))
        if len(q) < 24:
            return None
        q = [r for r in q if q[-1][0] - r[0] <= 6.0]
        if len(q) < 24:
            return None
        t = np.array([r[0] for r in q])
        dur = t[-1] - t[0]
        if dur < 2.0:
            return None
        leg = float(np.median([r[4] for r in q]))
        moved = abs(q[-1][1] - q[0][1]) / leg
        if moved < 1.5:                     # not walking across the room
            return None
        ad = np.array([r[2] for r in q])
        ad = ad - ad.mean()
        crossings = np.where(np.diff(np.sign(ad)) != 0)[0]
        if len(crossings) < 3:
            return None
        steps_per_s = len(crossings) / dur      # each sign change is one step
        if not 0.8 <= steps_per_s <= 3.5:
            return None
        step_len = float(np.percentile(np.abs(ad), 90))
        wd = [r[3] for r in q if r[3] is not None]
        arm = float(np.percentile(np.abs(np.array(wd) - np.mean(wd)), 90)) if len(wd) >= 12 else float("nan")
        return np.array([steps_per_s, step_len, arm], dtype=float)

    def forget(self, tid):
        self.h.pop(tid, None)


WALK_SCALE = np.array([0.25, 0.08, 0.08])      # typical within-person spread of each walk number


def walk_distance(v, refs):
    if v is None or not refs:
        return None
    best = None
    for r in refs:
        d = (v - r) / WALK_SCALE
        d = d[~np.isnan(d)]
        if len(d) < 2:
            continue
        x = float(np.sqrt(np.mean(d ** 2)))
        best = x if best is None else min(best, x)
    return best


# ---------------------------------------------------------------- fusion and learning
class Fusion:
    def __init__(self, path=None):
        self.path = path or os.path.join(HERE, "identity_v8.json")
        self.npz = self.path.replace(".json", "_faces.npz")
        self.face_gal = {}          # pid -> list of unit vectors (verified only)
        self.walk_gal = {}          # pid -> list of walk signatures (verified only)
        self.stats = {s: {"same": [], "diff": []} for s in SIGNALS}
        self.appearance_acc = None  # accept threshold of the fingerprint model
        self.log = deque(maxlen=200)
        self.saved = 0.0
        self.dirty = False
        self.load()

    # ---- persistence
    def load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            for s in SIGNALS:
                st = d.get("stats", {}).get(s)
                if st:
                    self.stats[s] = {"same": list(st.get("same", []))[-400:], "diff": list(st.get("diff", []))[-400:]}
            self.walk_gal = {p: [np.array(v, dtype=float) for v in vs] for p, vs in d.get("walk", {}).items()}
        except Exception:
            pass
        try:
            z = np.load(self.npz)
            for k in z.files:
                self.face_gal[k] = [v for v in z[k]]
        except Exception:
            pass

    def save(self, force=False):
        now = time.time()
        if not (force or (self.dirty and now - self.saved > 60)):
            return
        self.saved = now
        self.dirty = False
        try:
            d = {"stats": self.stats, "walk": {p: [[float(x) for x in v] for v in vs] for p, vs in self.walk_gal.items()},
                 "report": self.report()}
            with open(self.path + ".tmp", "w") as f:
                json.dump(d, f)
            os.replace(self.path + ".tmp", self.path)
            if self.face_gal:
                tmp = self.npz + ".tmp.npz"
                np.savez(tmp, **{p: np.array(vs, dtype="float32") for p, vs in self.face_gal.items() if vs})
                os.replace(tmp, self.npz)
            elif os.path.exists(self.npz):
                os.remove(self.npz)
        except Exception as e:
            print("[V8] save failed: %s" % e, flush=True)

    def drop(self, pid):
        self.face_gal.pop(pid, None)
        self.walk_gal.pop(pid, None)
        self.dirty = True
        self.save(force=True)

    # ---- raw scores for the cues this module owns
    def face_sim(self, embs, pid):
        g = self.face_gal.get(pid)
        if not g or not embs:
            return None
        G = np.array(g)
        best = []
        embs = [e for e in embs if e[1] >= 0.35] or []      # clear faces only
        if not embs:
            return None
        for v, _q in embs[-6:]:
            best.append(float(np.max(G @ v)))
        best.sort(reverse=True)
        return float(np.mean(best[:3]))

    def walk_sim(self, sig, pid):
        d = walk_distance(sig, self.walk_gal.get(pid))
        return None if d is None else -d

    # ---- turning raw scores into evidence
    def _params(self, s):
        if s == "appearance":
            a = self.appearance_acc if self.appearance_acc is not None else 0.70
            pri = (a + 0.08, 0.07, a - 0.14, 0.09, 1.0)
        else:
            pri = PRIORS[s]
        st = self.stats[s]
        if len(st["same"]) >= MIN_EXAMPLES and len(st["diff"]) >= MIN_EXAMPLES:
            ms, ss = float(np.mean(st["same"])), float(np.std(st["same"])) + 1e-3
            md, sd_ = float(np.mean(st["diff"])), float(np.std(st["diff"])) + 1e-3
            # blend with the starting value until there are plenty of examples
            n = min(len(st["same"]), len(st["diff"]))
            a = min(1.0, n / 60.0)
            ms, ss = a * ms + (1 - a) * pri[0], a * ss + (1 - a) * pri[1]
            md, sd_ = a * md + (1 - a) * pri[2], a * sd_ + (1 - a) * pri[3]
            # weight: how well this cue has separated people in this house
            acc = (np.mean([1.0 if self._llr_raw(x, ms, ss, md, sd_) > 0 else 0.0 for x in st["same"][-100:]])
                   + np.mean([1.0 if self._llr_raw(x, ms, ss, md, sd_) < 0 else 0.0 for x in st["diff"][-100:]])) / 2
            w = float(np.clip((acc - 0.5) * 2.2, 0.15, 1.0))
            w = a * w + (1 - a) * pri[4]
            return ms, ss, md, sd_, w
        return pri

    @staticmethod
    def _llr_raw(x, ms, ss, md, sd_):
        return _gauss_ll(x, ms, ss) - _gauss_ll(x, md, sd_)

    def validated(self, s):
        """VALIDATED_V1: a cue may count AGAINST a name only once this house's own
        examples show it separates people (weight 0.5 or more). Before that it can
        only support a name. Day 4 showed why: a face cue still on its starting
        values voted against everyone's real name, so everyone became Not sure."""
        st = self.stats[s]
        return (len(st["same"]) >= MIN_EXAMPLES and len(st["diff"]) >= MIN_EXAMPLES
                and self._params(s)[4] >= 0.5)

    def evidence(self, raw):
        """raw: {cue: score or None}. Returns (total, {cue: weighted evidence})."""
        out = {}
        for s, x in raw.items():
            if x is None or (isinstance(x, float) and math.isnan(x)):
                continue
            ms, ss, md, sd_, w = self._params(s)
            e = float(np.clip(self._llr_raw(x, ms, ss, md, sd_), -LLR_CLIP, LLR_CLIP)) * w
            if e < 0 and not self.validated(s):
                e = 0.0
            out[s] = e
        return float(sum(out.values())), out

    # ---- learning
    def add_example(self, s, x, same):
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return
        q = self.stats[s]["same" if same else "diff"]
        q.append(round(float(x), 4))
        del q[:-400]
        self.dirty = True

    def learn_face(self, pid, embs, max_n=80):
        g = self.face_gal.setdefault(pid, [])
        added = 0
        for v, q in embs:
            if q < 0.35:
                continue
            if g and float(np.max(np.array(g) @ v)) > 0.93:
                continue                       # nearly the same view already kept
            g.append(v)
            added += 1
        del g[:-max_n]
        if added:
            self.dirty = True
        return added

    def learn_walk(self, pid, sig, max_n=40):
        if sig is None:
            return False
        g = self.walk_gal.setdefault(pid, [])
        g.append(np.array(sig, dtype=float))
        del g[:-max_n]
        self.dirty = True
        return True

    def report(self):
        r = {}
        for s in SIGNALS:
            ms, ss, md, sd_, w = self._params(s)
            r[s] = {"same_examples": len(self.stats[s]["same"]), "different_examples": len(self.stats[s]["diff"]),
                    "weight": round(w, 2), "same_mean": round(ms, 3), "different_mean": round(md, 3)}
        r["faces_known"] = {p: len(v) for p, v in self.face_gal.items()}
        r["walks_known"] = {p: len(v) for p, v in self.walk_gal.items()}
        return r
