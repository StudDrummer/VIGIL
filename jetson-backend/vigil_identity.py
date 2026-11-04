"""
VIGIL identity fusion (IDENTITY_V8): one decision from every signal.

Who someone is used to be decided almost entirely by an appearance
fingerprint of their body crop, which is mostly clothing. Body proportions
could only hold a name back. Faces and walking were not used at all.

Now five independent signals each vote:

  face         a 128-number face signature (OpenCV YuNet finds the face,
               SFace describes it). Only used when the face is turned to
               the camera and large enough. The strongest signal by far.
  appearance   the existing body fingerprint (vigil_reid.py).
  proportions  shoulder, hip, arm and leg lengths relative to the torso.
  height       true height with distance from the camera removed.
  gait         walking rhythm: steps per second, stride, arm swing, bounce.

Each signal's vote is a log-likelihood ratio ("how much more likely is this
measurement if the track IS person P than if it is someone else"). The votes
are added, and a name is given only when the total is clearly in favour of
one person.

How much each signal counts is LEARNED in each home. Every "Yes, this is..."
and every correction in the app is a verified example: the score each signal
gave the right person (should be high) and every other person (should be
low) is recorded, and a small calibration model is refit per signal. A
signal that keeps getting it right in this home earns a larger vote; one
that does not (gait seen from a bad angle, two people of the same height)
is turned down, or off. Before there is enough data, conservative starting
values are used, with a cap on how much any one signal can say.

Privacy: no face image is stored. Only the 128 numbers are kept, per home,
and they cannot be turned back into a picture. Face recognition can be
switched off per camera (identity_settings.json, or VIGIL_FACE_ID=0); doing
so also erases every stored face signature.

Everything here is defensive: no function raises to the camera loop.
"""
import json
import os
import threading
import time
from collections import deque

import numpy as np

VERSION = "IDENTITY_V8"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("VIGIL_IDENTITY_DIR") or HERE
STATE_NPZ = os.path.join(DATA, "identity_faces.npz")
STATE_JSON = os.path.join(DATA, "identity_v8.json")
REPORT_JSON = os.path.join(DATA, "identity_report.json")
SETTINGS_JSON = os.path.join(DATA, "identity_settings.json")
YUNET = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"
MODEL_DIRS = [os.environ.get("VIGIL_MODEL_DIR", ""), os.path.join(HERE, "models"), "/opt/vigil-code/models"]

SIGNALS = ("face", "appearance", "proportions", "height", "gait")

# Starting calibration, used until a home has verified examples:
#   vote = a * (score - c)
# Scores are "higher = more alike" for every signal.
PRIOR = {
    "face": (14.0, 0.36),          # SFace cosine; OpenCV's published same-person threshold is 0.363
    "appearance": (12.0, 0.70),    # c is replaced by the gallery's own learned accept threshold
    "proportions": (1.0, -2.0),    # score = -(robust distance); about 1 for the same person
    "height": (0.5, -4.0),         # score = -(percent difference in true height)
    "gait": (0.7, -2.0),           # score = -(robust distance)
}
# The most any one signal can say (against, for). Keeps a weak or
# miscalibrated signal from overruling the others. Clothing is one-sided on
# purpose: looking like someone's usual outfit is good evidence it is them,
# but NOT looking like it is weak evidence it is not (people change clothes).
CAP = {"face": (-6.0, 8.0), "appearance": (-2.0, 5.0), "proportions": (-2.5, 2.5),
       "height": (-2.5, 2.5), "gait": (-2.0, 2.0)}
MIN_PAIRS = 12        # verified positive AND negative examples before a signal's calibration is learned
PRIOR_WEIGHT = 16.0   # how many examples the starting values are worth
MAX_PAIRS = 3000

NAME_POSTERIOR = 0.92   # give a name only above this probability
NAME_MIN_VOTE = 2.5     # ...and with at least this much evidence for that person
FACE_MAX = 60           # face signatures kept per person
FACE_MAX_PINNED = 40    # of which caregiver-verified
FACE_DIVERSITY = 0.92   # do not keep a face signature almost identical to one we have


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def face_enabled():
    """Per camera. Env VIGIL_FACE_ID=0 turns it off; the app setting file wins."""
    on = os.environ.get("VIGIL_FACE_ID", "1").strip().lower() not in ("0", "off", "false", "no")
    try:
        with open(SETTINGS_JSON) as f:
            v = json.load(f).get("face")
        if v is not None:
            on = bool(v)
    except (OSError, ValueError):
        pass
    return on


# ---------------------------------------------------------------- face model
class FaceModel:
    """YuNet face detector + SFace face signature. Loads lazily; never raises."""

    def __init__(self):
        self.det = None
        self.rec = None
        self.failed = False
        self.ms = None
        self._lock = threading.Lock()

    def _find(self, name):
        for d in MODEL_DIRS:
            if d and os.path.isfile(os.path.join(d, name)):
                return os.path.join(d, name)
        return None

    def load(self):
        if self.rec is not None or self.failed:
            return self.rec is not None
        with self._lock:
            if self.rec is not None or self.failed:
                return self.rec is not None
            try:
                import cv2
                dp, rp = self._find(YUNET), self._find(SFACE)
                if not dp or not rp:
                    raise RuntimeError("face model files not found in %s" % [d for d in MODEL_DIRS if d])
                self.det = cv2.FaceDetectorYN.create(dp, "", (160, 160), 0.6, 0.3, 50)
                self.rec = cv2.FaceRecognizerSF.create(rp, "")
                print("[FACE] models ready", flush=True)
                return True
            except Exception as e:
                self.failed = True
                print("[FACE] not available: %s" % e, flush=True)
                return False

    @staticmethod
    def head_box(shape, kp, conf=0.5):
        """Square box around the head from pose points, only when the face is
        turned to the camera (nose and both eyes clearly visible)."""
        k = np.asarray(kp, dtype=float)
        if k.shape[0] < 5 or k[0][2] < conf or k[1][2] < conf or k[2][2] < conf:
            return None
        eye_d = float(np.linalg.norm(k[1][:2] - k[2][:2]))
        ears = [k[i][:2] for i in (3, 4) if k[i][2] >= 0.3]
        ear_d = float(np.linalg.norm(ears[0] - ears[1])) if len(ears) == 2 else 0.0
        side = max(3.6 * eye_d, 1.9 * ear_d)
        if eye_d < 5 or side < 18:
            return None                                  # too far away for a usable face
        cx = float((k[0][0] + k[1][0] + k[2][0]) / 3.0)
        cy = float((k[1][1] + k[2][1]) / 2.0 * 0.6 + k[0][1] * 0.4)
        h, w = shape[:2]
        x1, y1 = int(max(0, cx - side / 2)), int(max(0, cy - side * 0.55))
        x2, y2 = int(min(w, cx + side / 2)), int(min(h, cy + side * 0.6))
        if x2 - x1 < 16 or y2 - y1 < 16:
            return None
        return x1, y1, x2, y2

    def embed(self, frame_bgr, kp):
        """(signature, quality 0..1) for this person's face, or None."""
        if frame_bgr is None or not self.load():
            return None
        try:
            import cv2
            box = self.head_box(frame_bgr.shape, kp)
            if box is None:
                return None
            x1, y1, x2, y2 = box
            crop = frame_bgr[y1:y2, x1:x2]
            scale = min(4.0, max(1.0, 192.0 / max(crop.shape[:2])))
            if scale > 1.0:
                crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            t0 = time.time()
            ch, cw = crop.shape[:2]
            self.det.setInputSize((cw, ch))
            _n, faces = self.det.detect(crop)
            if faces is None or len(faces) == 0:
                return None
            # the face nearest the middle of the head box belongs to this person
            ctr = np.array([cw / 2.0, ch / 2.0])
            f = min(faces, key=lambda r: float(np.linalg.norm(r[0:2] + r[2:4] / 2 - ctr)))
            fw_orig = float(f[2]) / scale
            if fw_orig < 18:
                return None
            # frontalness from the face's own landmarks: nose between the eyes
            re, le, nose = f[4:6], f[6:8], f[8:10]
            ed = float(np.linalg.norm(le - re))
            if ed < 1e-3:
                return None
            yaw = abs(float(nose[0] - (le[0] + re[0]) / 2.0)) / ed
            frontal = float(np.clip(1.0 - yaw / 0.45, 0.0, 1.0))
            if frontal < 0.25:
                return None
            aligned = self.rec.alignCrop(crop, f)
            v = np.asarray(self.rec.feature(aligned), dtype=np.float32).reshape(-1)
            n = float(np.linalg.norm(v))
            if n < 1e-6:
                return None
            ms = (time.time() - t0) * 1000.0
            self.ms = ms if self.ms is None else 0.9 * self.ms + 0.1 * ms
            q = float(f[14]) * min(1.0, fw_orig / 48.0) * (0.5 + 0.5 * frontal)
            return v / n, float(np.clip(q, 0.0, 1.0))
        except Exception as e:
            print("[FACE] embed error: %s" % e, flush=True)
            return None


# ---------------------------------------------------------------- gait
def _pt(k, i, c=0.4):
    return k[i][:2] if k[i][2] >= c else None


class Gait:
    """Walking signature per track from the 17 body points over a few seconds."""
    WINDOW_S = 6.0

    def __init__(self):
        self.trk = {}          # tid -> deque of (t, hip_x, hip_y, ankle_d, wrist_d, L, T)

    def add(self, tid, kp, t=None):
        try:
            k = np.asarray(kp, dtype=float)
            if tid is None or k.shape[0] < 17:
                return
            need = (11, 12, 13, 14, 15, 16)
            if min(k[i][2] for i in need) < 0.4 or min(k[i][2] for i in (5, 6)) < 0.4:
                return
            P = lambda i: k[i][:2]
            L = (np.linalg.norm(P(11) - P(13)) + np.linalg.norm(P(13) - P(15))
                 + np.linalg.norm(P(12) - P(14)) + np.linalg.norm(P(14) - P(16))) / 2.0
            sh, hp = (P(5) + P(6)) / 2, (P(11) + P(12)) / 2
            T = float(np.linalg.norm(sh - hp))
            if L < 25 or T < 15:
                return
            wl, wr = _pt(k, 9), _pt(k, 10)
            wd = float(np.linalg.norm(wl - wr)) if (wl is not None and wr is not None) else np.nan
            q = self.trk.setdefault(tid, deque(maxlen=150))
            q.append((time.time() if t is None else t, float(hp[0]), float(hp[1]),
                      float(np.linalg.norm(P(15) - P(16))), wd, float(L), T))
            if len(self.trk) > 300:
                for key in list(self.trk)[:150]:
                    self.trk.pop(key, None)
        except Exception:
            pass

    def signature(self, tid):
        """[cadence (steps/s), stride (ankle swing / leg), arm swing / torso,
        bounce / leg], or None if the track has not been walking clearly."""
        try:
            q = self.trk.get(tid)
            if not q or len(q) < 20:
                return None
            a = np.array(q, dtype=float)
            a = a[a[:, 0] >= a[-1, 0] - self.WINDOW_S]
            if len(a) < 20:
                return None
            t = a[:, 0]
            dur = t[-1] - t[0]
            if dur < 2.5:
                return None
            L = float(np.median(a[:, 5]))
            T = float(np.median(a[:, 6]))
            path = float(np.sum(np.hypot(np.diff(a[:, 1]), np.diff(a[:, 2]))))
            if path / L / dur < 0.15:
                return None                              # not walking
            fs = 10.0
            tt = np.arange(t[0], t[-1], 1.0 / fs)
            if len(tt) < 24:
                return None
            d = np.interp(tt, t, a[:, 3] / L)
            d = d - np.polyval(np.polyfit(tt - tt[0], d, 1), tt - tt[0])
            spec = np.abs(np.fft.rfft(d * np.hanning(len(d)))) ** 2
            freqs = np.fft.rfftfreq(len(d), 1.0 / fs)
            band = (freqs >= 0.6) & (freqs <= 2.8)
            if not band.any() or spec[1:].sum() <= 0:
                return None
            i = int(np.argmax(np.where(band, spec, 0)))
            if spec[i] / spec[1:].sum() < 0.2:
                return None                              # no clear rhythm
            cadence = float(freqs[i])
            stride = float(np.percentile(a[:, 3] / L, 90) - np.percentile(a[:, 3] / L, 10))
            w = a[:, 4] / T
            arm = float(np.nanpercentile(w, 90) - np.nanpercentile(w, 10)) if np.mean(~np.isnan(w)) >= 0.6 else np.nan
            hy = a[:, 2] / L
            hy = hy - np.polyval(np.polyfit(t - t[0], hy, 1), t - t[0])
            bounce = float(np.percentile(hy, 90) - np.percentile(hy, 10))
            return np.array([cadence, stride, arm, bounce], dtype=float)
        except Exception:
            return None

    def drop(self, tid):
        self.trk.pop(tid, None)


GAIT_FLOOR = np.array([0.08, 0.05, 0.05, 0.02])


def _robust_ref(samples, floor, n_min):
    if not samples or len(samples) < n_min:
        return None
    a = np.array(samples, dtype=float)
    med = np.nanmedian(a, axis=0)
    mad = np.nanmedian(np.abs(a - med), axis=0) * 1.4826
    return med, np.maximum(np.nan_to_num(mad, nan=0.0), floor)


def _robust_dist(v, ref):
    med, mad = ref
    z = np.abs(np.asarray(v, dtype=float) - med) / mad
    z = z[~np.isnan(z)]
    return float(np.sqrt(np.mean(z ** 2))) if len(z) >= 2 else None


# ---------------------------------------------------------------- calibration
class Calibration:
    """Per signal: score -> vote, learned from verified examples."""

    def __init__(self, sig):
        self.sig = sig
        self.pairs = deque(maxlen=MAX_PAIRS)    # (score, 1 same person / 0 different)
        self.a, self.c = PRIOR[sig]
        self.b = -self.a * self.c
        self.learned = False
        self.auc = None
        self.accept_hint = None                  # appearance: the gallery's own threshold

    def counts(self):
        pos = sum(1 for _s, y in self.pairs if y)
        return pos, len(self.pairs) - pos

    def vote(self, s):
        if s is None or not np.isfinite(s):
            return 0.0
        if not self.learned:
            c = self.accept_hint if (self.sig == "appearance" and self.accept_hint is not None) else self.c
            v = self.a * (s - c)
        else:
            v = self.a * s + self.b
        return float(np.clip(v, CAP[self.sig][0], CAP[self.sig][1]))

    def fit(self):
        """Weighted logistic regression of same/different on score, with the
        starting values mixed in as PRIOR_WEIGHT pseudo-examples. The result is
        a likelihood-ratio vote: the class balance of the examples is removed."""
        pos, neg = self.counts()
        if pos < MIN_PAIRS or neg < MIN_PAIRS:
            self.learned = False
            self.auc = None
            return
        s = np.array([p[0] for p in self.pairs], dtype=float)
        y = np.array([p[1] for p in self.pairs], dtype=float)
        w = np.where(y > 0, 0.5 / pos, 0.5 / neg) * len(y)          # balance the two classes
        a0, c0 = PRIOR[self.sig]
        if self.sig == "appearance" and self.accept_hint is not None:
            c0 = self.accept_hint
        grid = np.linspace(np.percentile(s, 5), np.percentile(s, 95), 8)
        ps = _sigmoid(a0 * (grid - c0))
        S = np.concatenate([s, grid, grid])
        Y = np.concatenate([y, np.ones_like(grid), np.zeros_like(grid)])
        W = np.concatenate([w, ps * PRIOR_WEIGHT / len(grid), (1 - ps) * PRIOR_WEIGHT / len(grid)])
        X = np.stack([S, np.ones_like(S)], axis=1)
        beta = np.array([a0, -a0 * c0], dtype=float)
        for _ in range(50):
            p = _sigmoid(X @ beta)
            g = X.T @ (W * (Y - p)) - 1e-4 * beta
            H = -(X.T * (W * p * (1 - p))) @ X - 1e-4 * np.eye(2)
            try:
                step = np.linalg.solve(H, g)
            except np.linalg.LinAlgError:
                break
            beta = beta - step
            if np.abs(step).max() < 1e-6:
                break
        a, b = float(beta[0]), float(beta[1])
        if not np.isfinite(a) or not np.isfinite(b):
            return
        if a < 0.05 * PRIOR[self.sig][0]:
            a, b = 0.0, 0.0                      # this signal does not tell people apart here: ignore it
        self.a, self.b, self.learned = a, b, True
        # AUC: how often a same-person score beats a different-person score
        sp, sn = s[y > 0], s[y == 0]
        if len(sp) and len(sn):
            sp_, sn_ = sp[-400:], sn[-1200:]
            self.auc = float(np.mean(sp_[:, None] > sn_[None, :]) + 0.5 * np.mean(sp_[:, None] == sn_[None, :]))

    def status(self):
        pos, neg = self.counts()
        if not self.learned:
            st = "starting values (needs %d confirmed and %d corrected comparisons)" % (MIN_PAIRS, MIN_PAIRS)
        elif self.a == 0.0:
            st = "ignored: does not tell people apart in this home"
        else:
            st = "learned"
        return {"status": st, "same_examples": pos, "different_examples": neg,
                "accuracy_auc": None if self.auc is None else round(self.auc, 3),
                "weight": round(self.a, 3), "cap": list(CAP[self.sig])}

    def to_json(self):
        return {"pairs": [[round(float(s), 4), int(y)] for s, y in self.pairs]}

    def from_json(self, d):
        for s, y in d.get("pairs", []):
            self.pairs.append((float(s), int(y)))
        self.fit()


# ---------------------------------------------------------------- the model
class IdentityModel:
    def __init__(self):
        self.face = FaceModel()
        self.gait = Gait()
        self.cal = {s: Calibration(s) for s in SIGNALS}
        self.fvec = {}        # pid -> (n x 128)
        self.fpin = {}        # pid -> (n,) bool, caregiver-verified
        self.gait_ver = {}    # pid -> deque of verified gait signatures
        self.gait_auto = {}   # pid -> deque of automatic ones
        self.trk_face = {}    # tid -> deque of (t, vec, quality)
        self.trk_gait = {}    # tid -> (t, signature) last good one
        self.stats = {"named_by_fusion": 0, "held_back": 0, "overruled": 0, "face_swaps": 0,
                      "new_blocked": 0, "learned_events": 0, "since": time.strftime("%Y-%m-%d %H:%M")}
        self._saved = 0.0
        self._report_t = 0.0
        self._face_on = None
        self._face_t = 0.0
        self.load()

    # ---------------- settings
    def face_on(self):
        now = time.time()
        if self._face_on is None or now - self._face_t > 10:
            self._face_t = now
            on = face_enabled()
            if self._face_on and not on:
                self.erase_faces()
            self._face_on = on
        return self._face_on

    def erase_faces(self):
        self.fvec.clear()
        self.fpin.clear()
        self.trk_face.clear()
        self.cal["face"].pairs.clear()
        self.cal["face"].fit()
        try:
            os.remove(STATE_NPZ)
        except OSError:
            pass
        self.save(force=True)
        print("[FACE] face recognition off: all face signatures erased", flush=True)

    # ---------------- observing tracks
    def observe(self, tid, kp, frame_bgr=None, want_face=False):
        """Called for every visible track. Gait every frame; face only when asked
        (the caller rate-limits it, the face model costs a few milliseconds)."""
        if tid is None:
            return
        self.gait.add(tid, kp)
        g = self.gait.signature(tid)
        if g is not None:
            self.trk_gait[tid] = (time.time(), g)
        if want_face and frame_bgr is not None and self.face_on():
            r = self.face.embed(frame_bgr, kp)
            if r is not None:
                q = self.trk_face.setdefault(tid, deque(maxlen=12))
                q.append((time.time(), r[0], r[1]))
        if len(self.trk_face) > 300:
            for k in list(self.trk_face)[:150]:
                self.trk_face.pop(k, None)
        if len(self.trk_gait) > 300:
            for k in list(self.trk_gait)[:150]:
                self.trk_gait.pop(k, None)

    def track_faces(self, tid, since=None):
        q = self.trk_face.get(tid)
        if not q:
            return None, 0.0
        items = [(v, qq) for t, v, qq in q if since is None or t >= since]
        if not items:
            return None, 0.0
        return np.stack([v for v, _ in items]), float(np.mean([qq for _, qq in items]))

    def track_gait(self, tid, max_age=20.0):
        r = self.trk_gait.get(tid)
        if r is None or time.time() - r[0] > max_age:
            return None
        return r[1]

    def forget_track(self, tid):
        self.trk_face.pop(tid, None)
        self.trk_gait.pop(tid, None)
        self.gait.drop(tid)

    # ---------------- per-signal scores this module owns
    def face_score(self, F, pid):
        G = self.fvec.get(pid)
        if F is None or G is None or len(G) == 0:
            return None
        best = (F @ G.T).max(axis=1)
        return float(np.median(best))

    def gait_score(self, g, pid):
        if g is None:
            return None
        ver = list(self.gait_ver.get(pid, ()))
        ref = _robust_ref(ver, GAIT_FLOOR, 4) or _robust_ref(ver + list(self.gait_auto.get(pid, ())), GAIT_FLOOR, 8)
        if ref is None:
            return None
        d = _robust_dist(g, ref)
        return None if d is None else -d

    def own_scores(self, tid, cands, since=None):
        """{pid: {"face": s, "gait": s}} plus face quality and count."""
        F, fq = self.track_faces(tid, since) if self.face_on() else (None, 0.0)
        g = self.track_gait(tid)
        out = {}
        for p in cands:
            d = {}
            fs = self.face_score(F, p)
            if fs is not None:
                d["face"] = fs
            gs = self.gait_score(g, p)
            if gs is not None:
                d["gait"] = gs
            out[p] = d
        nf = 0 if F is None else len(F)
        # several clear looks that agree say more than one (but they come from
        # one sighting, so not n times more)
        return out, {"face": (0.5 + 0.5 * fq) * min(1.5, np.sqrt(nf / 2.0)) if nf else 0.0}

    # ---------------- the decision
    def fuse(self, scores, quality=None, new_weight=0.5, appearance_accept=None):
        """scores: {pid: {signal: score}}. Returns a dict with the best person,
        its probability, the votes, and whether a name may be given."""
        quality = quality or {}
        if appearance_accept is not None:
            self.cal["appearance"].accept_hint = float(appearance_accept)
        votes, parts = {}, {}
        for p, d in scores.items():
            tot, br = 0.0, {}
            for s, v in d.items():
                if s not in self.cal or v is None:
                    continue
                w = quality.get(s, 1.0)
                x = w * self.cal[s].vote(v)
                br[s] = round(x, 2)
                tot += x
            votes[p] = tot
            parts[p] = br
        if not votes:
            return {"best": None, "p": 0.0, "vote": 0.0, "votes": {}, "parts": {}, "ok": False, "p_new": 1.0}
        names = list(votes)
        logits = np.array([votes[p] for p in names] + [np.log(max(new_weight, 1e-6))])
        logits = logits - logits.max()
        post = np.exp(logits) / np.exp(logits).sum()
        i = int(np.argmax(post[:-1]))
        best = names[i]
        strong = any(k in parts[best] for k in ("face", "appearance"))
        ok = bool(post[i] >= NAME_POSTERIOR and votes[best] >= NAME_MIN_VOTE and strong)
        return {"best": best, "p": float(post[i]), "vote": float(votes[best]), "p_new": float(post[-1]),
                "votes": {p: round(v, 2) for p, v in votes.items()}, "parts": parts, "ok": ok}

    @staticmethod
    def describe(r, top=3):
        if not r or not r.get("votes"):
            return "fusion: no evidence"
        order = sorted(r["votes"].items(), key=lambda t: -t[1])[:top]
        return "fusion best=%s p=%.2f new=%.2f | %s" % (
            r["best"], r["p"], r["p_new"],
            " ".join("%s:%+.1f(%s)" % (p, v, ",".join("%s%+.1f" % (k[:2], x) for k, x in r["parts"].get(p, {}).items()))
                     for p, v in order))

    # ---------------- learning
    def learn(self, tid, pid, others, other_scores=None, wrong=None):
        """A caregiver said this track is `pid` (a confirmation or a correction).

        other_scores: {cand: {signal: score}} for the signals person_hook owns
        (appearance, proportions, height), computed BEFORE anything was added
        to pid's memory, so the examples are honest. `others` are the other
        named people this track was compared against."""
        try:
            cands = [pid] + [p for p in others if p != pid]
            own, _q = self.own_scores(tid, cands)
            for c in cands:
                d = dict((other_scores or {}).get(c, {}))
                d.update(own.get(c, {}))
                for s, v in d.items():
                    if s in self.cal and v is not None and np.isfinite(v):
                        self.cal[s].pairs.append((float(v), 1 if c == pid else 0))
            for s in SIGNALS:
                self.cal[s].fit()
            F, _fq = self.track_faces(tid)
            if F is not None and self.face_on():
                if wrong and wrong != pid:
                    self._face_remove_similar(wrong, F)
                self._face_add(pid, F, pin=True)
            g = self.track_gait(tid, max_age=120.0)
            if g is not None:
                self.gait_ver.setdefault(pid, deque(maxlen=60)).append(g)
            self.stats["learned_events"] += 1
            print("[LEARN8] %s verified as %s: faces=%d gait=%s compared with %d others" % (
                tid, pid, 0 if F is None else len(F), "yes" if g is not None else "no", len(cands) - 1), flush=True)
            self.save(force=True)
            self.report(force=True)
        except Exception as e:
            print("[LEARN8] error: %s" % e, flush=True)

    def auto_learn(self, tid, pid, fusion_p):
        """A confident name (not verified): keep the face and gait as ordinary
        memory. Never used as a calibration example."""
        try:
            if fusion_p < 0.97:
                return
            F, fq = self.track_faces(tid, since=time.time() - 10)
            if F is not None and fq >= 0.5 and self.face_on():
                keep = []
                for v in F:
                    own = self.face_score(v[None], pid)
                    rivals = [self.face_score(v[None], p) for p in self.fvec if p != pid]
                    rivals = [r for r in rivals if r is not None]
                    if (own is None or own >= PRIOR["face"][1]) and (not rivals or max(rivals) < PRIOR["face"][1]):
                        keep.append(v)
                if keep:
                    self._face_add(pid, np.stack(keep), pin=False)
            g = self.track_gait(tid)
            if g is not None:
                self.gait_auto.setdefault(pid, deque(maxlen=60)).append(g)
            self.save()
        except Exception:
            pass

    def _face_add(self, pid, F, pin):
        V = self.fvec.get(pid, np.zeros((0, 128), np.float32))
        P = self.fpin.get(pid, np.zeros((0,), bool))
        for v in np.asarray(F, np.float32):
            if len(V) and float((V @ v).max()) >= FACE_DIVERSITY:
                if pin:   # already have it: mark the closest one as verified
                    P[int(np.argmax(V @ v))] = True
                continue
            V = np.vstack([V, v[None]])
            P = np.append(P, pin)
        # trim: unverified first (oldest), then the most redundant verified
        while len(V) > FACE_MAX or int(P.sum()) > FACE_MAX_PINNED:
            if int(P.sum()) > FACE_MAX_PINNED or not (~P).any():
                idx = np.where(P)[0]
                S = V[idx] @ V[idx].T
                np.fill_diagonal(S, -1)
                drop = idx[int(np.argmax(S.max(axis=1)))]
            else:
                drop = int(np.where(~P)[0][0])
            V = np.delete(V, drop, axis=0)
            P = np.delete(P, drop)
        self.fvec[pid], self.fpin[pid] = V, P

    def _face_remove_similar(self, pid, F):
        V, P = self.fvec.get(pid), self.fpin.get(pid)
        if V is None or not len(V):
            return
        sim = (V @ np.asarray(F, np.float32).T).max(axis=1)
        keep = ~((sim >= 0.5) & ~P)
        self.fvec[pid], self.fpin[pid] = V[keep], P[keep]

    def merge(self, src, dst):
        if src in self.fvec:
            self._face_add(dst, self.fvec.pop(src), pin=False)
            self.fpin.pop(src, None)
        for store in (self.gait_ver, self.gait_auto):
            if src in store:
                store.setdefault(dst, deque(maxlen=60)).extend(store.pop(src))
        self.save(force=True)

    def drop(self, pid):
        for store in (self.fvec, self.fpin, self.gait_ver, self.gait_auto):
            store.pop(pid, None)
        self.save(force=True)

    # ---------------- persistence and report
    def save(self, force=False):
        now = time.time()
        if not force and now - self._saved < 60:
            return
        self._saved = now
        try:
            if self.fvec:
                arrs = {}
                for i, p in enumerate(sorted(self.fvec)):
                    arrs["v%d" % i] = self.fvec[p]
                    arrs["p%d" % i] = self.fpin[p]
                arrs["ids"] = np.array(sorted(self.fvec))
                tmp = STATE_NPZ + ".tmp.npz"
                np.savez(tmp, **arrs)
                os.replace(tmp, STATE_NPZ)
            elif os.path.exists(STATE_NPZ):
                os.remove(STATE_NPZ)
            d = {"version": VERSION,
                 "cal": {s: c.to_json() for s, c in self.cal.items()},
                 "gait_ver": {p: [[None if np.isnan(x) else round(float(x), 4) for x in g] for g in q]
                              for p, q in self.gait_ver.items()},
                 "gait_auto": {p: [[None if np.isnan(x) else round(float(x), 4) for x in g] for g in q]
                               for p, q in self.gait_auto.items()},
                 "stats": self.stats}
            with open(STATE_JSON + ".tmp", "w") as f:
                json.dump(d, f)
            os.replace(STATE_JSON + ".tmp", STATE_JSON)
        except Exception as e:
            print("[IDENTITY8] save error: %s" % e, flush=True)

    def load(self):
        try:
            if os.path.exists(STATE_NPZ):
                z = np.load(STATE_NPZ, allow_pickle=False)
                for i, p in enumerate(z["ids"]):
                    self.fvec[str(p)] = z["v%d" % i].astype(np.float32)
                    self.fpin[str(p)] = z["p%d" % i].astype(bool)
        except Exception as e:
            print("[IDENTITY8] face memory not loaded: %s" % e, flush=True)
        try:
            with open(STATE_JSON) as f:
                d = json.load(f)
            for s, c in d.get("cal", {}).items():
                if s in self.cal:
                    self.cal[s].from_json(c)
            conv = lambda g: np.array([np.nan if x is None else x for x in g], dtype=float)
            for p, gs in d.get("gait_ver", {}).items():
                self.gait_ver[p] = deque((conv(g) for g in gs), maxlen=60)
            for p, gs in d.get("gait_auto", {}).items():
                self.gait_auto[p] = deque((conv(g) for g in gs), maxlen=60)
            self.stats.update(d.get("stats", {}))
            print("[IDENTITY8] loaded: faces for %d people, %s" % (
                len(self.fvec), ", ".join("%s %s" % (s, "learned" if c.learned else "start") for s, c in self.cal.items())),
                flush=True)
        except (OSError, ValueError):
            pass
        except Exception as e:
            print("[IDENTITY8] state not loaded: %s" % e, flush=True)

    def report(self, force=False):
        now = time.time()
        if not force and now - self._report_t < 60:
            return
        self._report_t = now
        try:
            on = self.face_on()
            d = {"version": VERSION, "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "face_recognition": "on" if on else "off",
                 "face_model": ("ready" if self.face.rec is not None else
                                ("unavailable" if self.face.failed else "loads on first face")) if on else "off",
                 "face_ms": None if self.face.ms is None else round(self.face.ms, 1),
                 "signals": {s: c.status() for s, c in self.cal.items()},
                 "people": {p: {"face_signatures": int(len(self.fvec.get(p, ()))),
                                "face_verified": int(self.fpin.get(p, np.zeros(0, bool)).sum()),
                                "walks_verified": len(self.gait_ver.get(p, ())),
                                "walks_auto": len(self.gait_auto.get(p, ()))}
                            for p in sorted(set(self.fvec) | set(self.gait_ver) | set(self.gait_auto))},
                 "decisions": self.stats}
            with open(REPORT_JSON + ".tmp", "w") as f:
                json.dump(d, f, indent=1)
            os.replace(REPORT_JSON + ".tmp", REPORT_JSON)
        except Exception:
            pass


MODEL = IdentityModel()
