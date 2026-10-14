"""
VIGIL Person Hook v2 — multi-person tracking + main-resident identification.

Key change from v1: this now accepts the SAME `all_persons` list that
activity_monitor.py already computes via select_all_persons() (sorted
largest-first), instead of re-extracting keypoints independently from
the raw YOLO result. This guarantees index 0 == the person being
measured for gait, so we can always answer "which person_id is the
resident's reading currently for?" with zero ambiguity.
"""
import os
import time
import numpy as np
import requests
from collections import deque

SERVER_URL = "http://localhost:5001"
API_KEY    = os.environ.get("VIGIL_API_KEY", "")
ROOM_ID    = "room_1"
MAX_PHOTO_ATTEMPTS = 50   # give up trying for a head crop and register with a fallback photo
MAX_PHOTO_B64_BYTES = 200 * 1024

def _crop_head(frame, keypoints, conf_thresh=0.3):
    """Crop head region from COCO pose keypoints. Returns None if no
    reliable head detection (person facing away, occluded, too small)."""
    head = keypoints[0:5]                      # nose, eyes, ears
    valid = head[head[:, 2] > conf_thresh]     # filter by confidence
    if len(valid) < 2:
        return None                            # not enough head landmarks
    xs, ys = valid[:, 0], valid[:, 1]
    cx, cy = float(xs.mean()), float(ys.mean())
    # Scale crop box off shoulder width if available, else head spread
    shoulders = keypoints[5:7]
    sv = shoulders[shoulders[:, 2] > conf_thresh]
    if len(sv) == 2:
        span = abs(float(sv[0][0]) - float(sv[1][0]))
        box = max(span * 1.1, 60)
    else:
        box = max(float(xs.max() - xs.min()), float(ys.max() - ys.min())) * 3.5
        box = max(box, 60)
    h, w = frame.shape[:2]
    x1 = max(0, int(cx - box)); x2 = min(w, int(cx + box))
    y1 = max(0, int(cy - box * 1.2)); y2 = min(h, int(cy + box * 0.9))
    if x2 - x1 < 40 or y2 - y1 < 40:
        return None                            # too small to be useful
    return frame[y1:y2, x1:x2]


def _post_person_photo(person_id: str, image_bgr) -> None:
    """Encode an image (head crop or fallback full frame) and POST it as
    this person's identification photo. Resized to 240x240 and capped at
    200KB of base64 — anything larger is dropped rather than sent."""
    try:
        import cv2, base64
        # Pad to a square instead of stretching, then resize.
        _h, _w = image_bgr.shape[:2]
        _s = max(_h, _w)
        _sq = cv2.copyMakeBorder(image_bgr, (_s - _h) // 2, _s - _h - (_s - _h) // 2,
                                 (_s - _w) // 2, _s - _w - (_s - _w) // 2,
                                 cv2.BORDER_CONSTANT, value=(0, 0, 0))
        small = cv2.resize(_sq, (240, 240))
        ok, buf = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok:
            return
        b64 = base64.b64encode(buf.tobytes()).decode('utf-8')
        if len(b64) > MAX_PHOTO_B64_BYTES:
            return
        requests.post(
            f"{SERVER_URL}/persons/{person_id}/photo",
            json={"photo_b64": b64},
            headers={"X-VIGIL-Key": API_KEY},
            timeout=5,
        )
    except Exception:
        pass




def _estimate_shape(kp, conf=0.3):
    """Rotation-invariant body proportion: torso length / leg length.

    Both measurements are vertical, so they shrink together with distance
    (scale-invariant) AND are barely affected by the person turning about
    their vertical axis (rotation-invariant).

    An earlier version used shoulder_width / torso_length. That is
    scale-invariant but rotation-VARIANT: turning sideways collapses
    apparent shoulder width while torso length is unchanged, swinging the
    ratio by 70%+ during ordinary walking. It fragmented identities badly.
    """
    try:
        ls, rs = kp[5], kp[6]      # shoulders
        lh, rh = kp[11], kp[12]    # hips
        la, ra = kp[15], kp[16]    # ankles

        sh_ok = max(float(ls[2]), float(rs[2])) >= conf
        hp_ok = max(float(lh[2]), float(rh[2])) >= conf
        an_ok = max(float(la[2]), float(ra[2])) >= conf
        if not (sh_ok and hp_ok and an_ok):
            return None

        sy = float(ls[1]) if float(ls[2]) >= float(rs[2]) else float(rs[1])
        hy = float(lh[1]) if float(lh[2]) >= float(rh[2]) else float(rh[1])
        ay = float(la[1]) if float(la[2]) >= float(ra[2]) else float(ra[1])

        torso = abs(hy - sy)
        leg = abs(ay - hy)
        if torso < 15.0 or leg < 20.0:
            return None
        ratio = torso / leg
        if ratio <= 0.15 or ratio >= 3.0:
            return None
        return ratio
    except Exception:
        return None


# Correlation runs -1..1. Same clothing under changing light and pose
# still correlates strongly; different clothing does not.
COLOUR_REJECT = 0.25   # below this, appearance differs -> different person
COLOUR_MATCH = 0.55   # above this, appearance agrees -> allow wide height drift
COLOUR_STRONG = 0.65   # above this, accept whatever the height


def _estimate_colour(kp, frame, conf=0.3):
    """HSV histogram of the torso. Independent of distance and pose.

    Apparent height swings ~34% within a single appearance as someone
    moves around a room. Clothing colour does not, which is why it
    separates two people that body geometry cannot.
    """
    if frame is None:
        return None
    try:
        import cv2
        ls, rs, lh, rh = kp[5], kp[6], kp[11], kp[12]
        pts = [p for p in (ls, rs, lh, rh) if float(p[2]) >= conf]
        if len(pts) < 3:
            return None
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
        w, h = x2 - x1, y2 - y1
        if w < 8 or h < 8:
            return None
        # shrink inward so background at the edges does not colour the result
        x1 += w * 0.20; x2 -= w * 0.20
        y1 += h * 0.15; y2 -= h * 0.15
        H, W = frame.shape[:2]
        x1 = max(0, int(x1)); x2 = min(W, int(x2))
        y1 = max(0, int(y1)); y2 = min(H, int(y2))
        if x2 - x1 < 4 or y2 - y1 < 4:
            return None
        hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
        # Coarse bins on purpose. 16x8 was brittle: a small lighting shift
        # moved mass between neighbouring bins and similarity collapsed.
        hist = cv2.calcHist([hsv], [0, 1], None, [8, 4], [0, 180, 0, 256])
        hist = hist.flatten().astype('float32')
        total = float(hist.sum())
        if total <= 0:
            return None
        return hist / total
    except Exception:
        return None


def _colour_sim(a, b):
    """Correlation between two torso histograms, -1..1.

    Intersection was too harsh on sparse histograms -- the same person
    scored 0.05 to 0.33 between frames. Correlation compares the shape of
    the distribution rather than its overlap, which tolerates the small
    shifts that do not mean a different person.
    """
    try:
        if a is None or b is None:
            return None
        import cv2
        return float(cv2.compareHist(np.asarray(a, dtype='float32'),
                                     np.asarray(b, dtype='float32'),
                                     cv2.HISTCMP_CORREL))
    except Exception:
        return None


class _PersonSignature:
    def __init__(self, pid):
        self.id = pid
        self.height_samples = deque(maxlen=50)
        self.shape_samples = deque(maxlen=50)   # scale-invariant proportions
        self.colour_samples = deque(maxlen=30)  # torso HSV histograms
        self.first_seen = time.time()
        self.last_seen = time.time()
        self.frames_seen = 0
        self.last_detected = 0.0   # 0 until a real detection arrives
        self.reported = False
        self.photo_reported = False
        self.photo_attempts = 0
        self.name = None          # filled in once the app identifies them
        self.is_resident = False

    @property
    def height_est(self):
        # Below 5 samples, fall back to a plain mean so a brand-new signature
        # is still matchable on the next frame. Requiring the 5-sample
        # percentile here made height_est None for every signature younger
        # than 5 frames, and matching skips any signature with height_est
        # None — so a signature could never accumulate past 1 sample and a
        # new identity was created almost every frame.
        if not self.height_samples:
            return None
        if len(self.height_samples) < 5:
            return float(np.mean(self.height_samples))
        return float(np.percentile(list(self.height_samples), 75))

    @property
    def shape_est(self):
        """Median proportion. Median rather than mean because a single bad
        pose estimate can produce a wild ratio."""
        if len(self.shape_samples) < 3:
            return None
        return float(np.median(list(self.shape_samples)))

    @property
    def colour_est(self):
        """Mean torso histogram. Needs a few samples so one bad crop
        (background, motion blur) cannot define a person's appearance."""
        # One sample is enough to start. Requiring three meant a new
        # signature never became matchable before being replaced by
        # another new signature -- colour could never engage at all.
        if len(self.colour_samples) < 1:
            return None
        try:
            m = np.mean(np.stack(list(self.colour_samples)), axis=0)
            t = float(m.sum())
            return (m / t) if t > 0 else None
        except Exception:
            return None

    @property
    def confidence(self):
        h = len(self.height_samples) / 50.0
        f = min(1.0, self.frames_seen / 100.0)
        return (h + f) / 2.0

    def matches(self, height_px, shape=None, colour=None,
                threshold=0.25, shape_threshold=0.25):
        """Identity match on either signal.

        Requiring BOTH height and shape to agree caused severe
        fragmentation: 13 identities from one person whose heights were
        all within 5%. Under real pose noise the same person routinely
        fails one signal on any given frame. Two genuinely different
        people are unlikely to agree on either.
        """
        h = self.height_est
        if h is None or h <= 0:
            return False

        height_ok = abs(height_px - h) / h <= threshold

        s = self.shape_est
        if shape is None or s is None or s <= 0:
            return height_ok

        # Both signals must agree. Accepting either one merged two different
        # adults into a single identity in live testing, which silently
        # corrupts the baseline. Requiring both can split one person
        # instead -- visible, and fixable from the naming sheet.
        shape_ok = abs(shape - s) / s <= shape_threshold

        # Appearance first when both sides have it. Clothing colour is
        # independent of distance and pose, which is exactly where height
        # fails: height swung 34% within one person's single appearance.
        ce = self.colour_est
        if colour is not None and ce is not None:
            sim = _colour_sim(colour, ce)
            if sim is not None:
                if sim < COLOUR_REJECT:
                    return False            # different clothing, different person
                if sim >= COLOUR_STRONG:
                    return True      # partly visible people have no usable height
                if sim >= COLOUR_MATCH:
                    return abs(height_px - h) / h <= 0.50

        return height_ok and shape_ok

    def update(self, height_px, shape=None, colour=None):
        self.height_samples.append(height_px)
        if shape is not None:
            self.shape_samples.append(shape)
        if colour is not None:
            self.colour_samples.append(colour)
        self.last_seen = time.time()
        self.last_detected = time.time()   # real detections only
        self.frames_seen += 1


_signatures = {}
_id_counter = [0]
_last_report = [0.0]

_merge_check = [0.0]


def _fresh(ts, hours=12.0):
    """Clothing changes daily, so stored colour is trusted only same-day.
    Restoring yesterday's shirt would make the colour check reject the
    right person, since a strong mismatch is treated as a different one."""
    if not ts:
        return False
    try:
        from datetime import datetime as _dt
        t = _dt.fromisoformat(str(ts).replace("Z", ""))
        return (_dt.now() - t).total_seconds() < hours * 3600
    except Exception:
        return False


def _apply_merges():
    """Fold in merges made from the app. Without this the camera keeps the
    old id in memory and re-reports it, resurrecting the duplicate."""
    now = time.time()
    if now - _merge_check[0] < 15:
        return
    _merge_check[0] = now
    try:
        import requests as _rq
        r = _rq.get("http://127.0.0.1:5001/persons/merges",
                    headers={"X-VIGIL-Key": os.environ.get("VIGIL_API_KEY", "")},
                    timeout=2)
        merges = r.json().get("merges", [])
    except Exception:
        return
    for m in merges:
        src, dst = m.get("source"), m.get("target")
        if not src or not dst or src == dst or src not in _signatures:
            continue
        s = _signatures.pop(src)
        if _VR is not None:
            _VR.GALLERY.merge(src, dst)
        d = _signatures.get(dst)
        if d is None:
            d = _PersonSignature(dst)
            d.reported = True
            _signatures[dst] = d
        for attr in ("height_samples", "shape_samples", "colour_samples"):
            try:
                getattr(d, attr).extend(getattr(s, attr))
            except Exception:
                pass
        try:
            d.last_seen = max(d.last_seen, s.last_seen)
            d.last_detected = max(getattr(d, "last_detected", 0.0),
                                  getattr(s, "last_detected", 0.0))
            d.frames_seen += s.frames_seen
        except Exception:
            pass
        for k, v in list(_track_to_pid.items()):
            if v == src:
                _track_to_pid[k] = dst
        print("[person_hook] merge applied: %s -> %s" % (src, dst), flush=True)


_pending_new = {}       # ByteTrack id -> consecutive unmatched frames
_NEW_ID_FRAMES = 12     # ~1 s at 11 fps before a new person is created


def _kp_box(kp, conf=0.3):
    try:
        pts = kp[kp[:, 2] >= conf]
        if len(pts) < 3:
            return None
        return (float(pts[:, 0].min()), float(pts[:, 1].min()),
                float(pts[:, 0].max()), float(pts[:, 1].max()))
    except Exception:
        return None


def _is_occluded(i, boxes, frac=0.15):
    """True if detection i overlaps another detection by more than `frac`
    of the smaller box. Its torso crop then likely contains the other
    person's clothes, so colour from it cannot be trusted."""
    a = boxes[i] if i < len(boxes) else None
    if a is None:
        return False
    area_a = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    for j, b in enumerate(boxes):
        if j == i or b is None:
            continue
        iw = min(a[2], b[2]) - max(a[0], b[0])
        ih = min(a[3], b[3]) - max(a[1], b[1])
        if iw <= 0 or ih <= 0:
            continue
        area_b = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
        if (iw * ih) / min(area_a, area_b) > frac:
            return True
    return False


_TRACK_BAD = {}       # TRACK_VERIFY_V1: track id -> consecutive mismatching frames
_corr_check = [0.0]


def _track_mismatch(tid, pid, colour):
    """True once a track has clearly stopped looking like the person whose
    name it carries. Then the track is released for re-identification."""
    try:
        # REID_V61: when fingerprints know this person, they decide, not clothing colour.
        if _VR is not None and _VR.EMBEDDER.kind and len(_VR.GALLERY.vecs.get(pid, ())) >= 10:
            _q = _trk_fp.get(tid, [])
            if len(_q) >= 4:
                _v = np.array(_q[-5:])
                _own = _VR.GALLERY.score(_v, pid) or 0.0
                _alt = [((_VR.GALLERY.score(_v, p) or 0.0), p) for p in _VR.GALLERY.named
                        if p != pid and p in _signatures]
                _alt = max(_alt) if _alt else (0.0, None)
                _acc = _VR.GALLERY.thresholds(_VR.EMBEDDER.kind)[0]
                if _alt[0] >= _acc - 0.04 and _alt[0] > _own + 0.10:
                    _track_to_pid.pop(tid, None)
                    _TRACK_BAD.pop(tid, None)
                    _trk_fp[tid] = _q[-5:]
                    print("[TRACK_VERIFY] track %s looks like %s, not %s (%.2f vs %.2f); re-identifying"
                          % (tid, _alt[1], pid, _alt[0], _own), flush=True)
                    return True
            _TRACK_BAD.pop(tid, None)
            return False
        sg = _signatures.get(pid)
        if sg is None or colour is None or len(sg.colour_samples) < 5:
            _TRACK_BAD.pop(tid, None)
            return False
        sv = _colour_sim(colour, sg.colour_est)
        if sv is None or sv >= COLOUR_REJECT:
            _TRACK_BAD.pop(tid, None)
            return False
        _TRACK_BAD[tid] = _TRACK_BAD.get(tid, 0) + 1
        if len(_TRACK_BAD) > 300:
            _TRACK_BAD.clear()
        if _TRACK_BAD.get(tid, 0) >= 6:
            _track_to_pid.pop(tid, None)
            _TRACK_BAD.pop(tid, None)
            print("[TRACK_VERIFY] track %s stopped matching %s; re-identifying" % (tid, pid), flush=True)
            return True
    except Exception:
        pass
    return False


def _apply_corrections():
    """Apply 'this is someone else' corrections made in the app."""
    now = time.time()
    if now - _corr_check[0] < 3:
        return
    _corr_check[0] = now
    try:
        import sqlite3 as _sq
        c = _sq.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vigil.db"), timeout=2)
        try:
            rows = c.execute("SELECT id, src, dst FROM person_corrections WHERE applied=0").fetchall()
        except Exception:
            rows = []
        for cid, src, dst in rows:
            if str(src).startswith("track:"):
                try:
                    _t = int(str(src).split(":", 1)[1])
                    _track_to_pid[_t] = dst
                    _trk_status.pop(_t, None)
                    _evidence.pop(_t, None)
                    if dst not in _signatures:
                        _nd = _PersonSignature(dst)
                        _nd.reported = True
                        _nd.photo_reported = True
                        _signatures[dst] = _nd
                    if _VR is not None and _trk_fp.get(_t):
                        _VR.GALLERY.add(dst, np.array(_trk_fp[_t][-8:]), pin=True)
                        _VR.GALLERY.save(force=True)
                    print("[CORRECTION] track %s -> %s" % (_t, dst), flush=True)
                except Exception as _e:
                    print("[CORRECTION] track error: %s" % _e, flush=True)
                c.execute("UPDATE person_corrections SET applied=1 WHERE id=?", (cid,))
                continue
            s = _signatures.get(src)
            d = _signatures.get(dst)
            if d is None:
                d = _PersonSignature(dst)
                d.reported = True
                d.photo_reported = True
                _signatures[dst] = d
            if s is not None:
                samples = list(s.colour_samples)
                recent = samples[-10:]
                s.colour_samples.clear()
                s.colour_samples.extend(samples[:-10])
                d.colour_samples.extend(recent)
                _bq = list(_BODY.get(src, []))
                if _bq:
                    _BODY[src] = deque(_bq[:-10], maxlen=60)
                    for _b in _bq[-10:]:
                        _body_add(dst, _b)
                d.last_seen = now
                d.last_detected = now
            for k, v in list(_track_to_pid.items()):
                if v == src:
                    _track_to_pid[k] = dst
                    _fpk = _trk_fp.get(k)
                    if _VR is not None and _fpk:
                        if src != dst:
                            _VR.GALLERY.remove_recent(src, np.array(_fpk))
                        _VR.GALLERY.add(dst, np.array(_fpk[-8:]), pin=True)
                        _VR.GALLERY.save(force=True)
            c.execute("UPDATE person_corrections SET applied=1 WHERE id=?", (cid,))
            print("[CORRECTION] %s -> %s" % (src, dst), flush=True)
        c.commit()
        c.close()
    except Exception as e:
        print("[CORRECTION] error: %s" % e, flush=True)


_evidence = {}          # REID_V4: track id -> collected samples
_REID_MIN_SAMPLES = 8   # about 0.7 s of clean clothing samples before deciding
_REID_GIVE_UP = 28      # frames (about 2.5 s) before an unclear track becomes a new person
_REID_MIN_SIM = 0.60
_REID_MARGIN = 0.12
_in_view_t = [0.0]


# BODY_V1: clothing-independent identity from true (perspective-corrected) height.
_BODY = {}                                  # pid -> deque of (foot_y, height) samples, both / frame height
_GROUND = {"pts": deque(maxlen=2000), "a": 0.0, "b": None, "new": 0}
_BODY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "body_model.json")
_body_saved = [0.0]


def _stand_height(kp, fh):
    """(foot_y, height) as fractions of frame height, from an upright pose
    with head, shoulders, hips and ankles visible; otherwise None."""
    try:
        if fh is None or fh <= 0:
            return None
        k = np.asarray(kp, dtype=float)
        if k.shape[0] < 17:
            return None
        ok = lambda i: k[i][2] >= 0.4
        head = [k[i] for i in (0, 1, 2, 3, 4) if ok(i)]
        ank = [k[i] for i in (15, 16) if ok(i)]
        if not head or not ank or not (ok(5) and ok(6) and ok(11) and ok(12)):
            return None
        sx, sy = (k[5][0] + k[6][0]) / 2, (k[5][1] + k[6][1]) / 2
        hx, hy = (k[11][0] + k[12][0]) / 2, (k[11][1] + k[12][1]) / 2
        dy = hy - sy
        if dy <= 0 or abs(hx - sx) > 0.35 * dy:
            return None                     # not upright
        top = min(p[1] for p in head)
        foot = max(p[1] for p in ank)
        h = foot - top
        if h < 0.15 * fh or foot > 0.995 * fh:
            return None                     # too small, or feet cut off by the frame edge
        return (foot / fh, h / fh)
    except Exception:
        return None


def _ground_add(bs):
    g = _GROUND
    g["pts"].append(bs)
    g["new"] += 1
    if g["b"] is None or g["new"] >= 50:
        g["new"] = 0
        try:
            pts = np.array(list(g["pts"]))
            if len(pts) >= 30:
                f, h = pts[:, 0], pts[:, 1]
                if float(f.max() - f.min()) >= 0.12:
                    a, b = np.polyfit(f, h, 1)
                    g["a"], g["b"] = float(a), float(b)
                else:
                    g["a"], g["b"] = 0.0, float(np.median(h))
        except Exception:
            pass


def _ratio(bs):
    """Height relative to everyone else, with distance from the camera removed."""
    g = _GROUND
    if g["b"] is None:
        return None
    d = g["a"] * bs[0] + g["b"]
    return (bs[1] / d) if d > 1e-6 else None


def _body_ratio(pid):
    q = _BODY.get(pid)
    if not q or len(q) < 10:
        return None
    rs = [r for r in (_ratio(s) for s in q) if r is not None]
    return float(np.median(rs)) if len(rs) >= 10 else None


def _body_add(pid, bs):
    _BODY.setdefault(pid, deque(maxlen=60)).append(bs)


def _body_save():
    now = time.time()
    if now - _body_saved[0] < 60:
        return
    _body_saved[0] = now
    try:
        import json as _js
        data = {"ground": [list(p) for p in list(_GROUND["pts"])[-1000:]],
                "people": {k: [list(s) for s in list(v)[-40:]] for k, v in _BODY.items()}}
        with open(_BODY_FILE + ".tmp", "w") as f:
            _js.dump(data, f)
        os.replace(_BODY_FILE + ".tmp", _BODY_FILE)
    except Exception:
        pass


def _body_load():
    try:
        import json as _js
        with open(_BODY_FILE) as f:
            data = _js.load(f)
        for p in data.get("ground", []):
            _GROUND["pts"].append(tuple(p))
        _GROUND["new"] = 50
        if _GROUND["pts"]:
            _ground_add(_GROUND["pts"][-1])
        for pid, ss in data.get("people", {}).items():
            for s in ss:
                _body_add(pid, tuple(s))
        print("[BODY] loaded %d ground samples, %d people" % (len(_GROUND["pts"]), len(_BODY)), flush=True)
    except Exception:
        pass


_body_load()


def _reid_decide(ev, used):
    """Decide who a track is from clothing AND true height (BODY_V1).
    Returns (person_id or None, log text)."""
    try:
        cs = ev["c"]
        if cs:
            m = np.mean(np.stack(cs), axis=0)
            t = float(m.sum())
            mean_c = (m / t) if t > 0 else None
        else:
            mean_c = None
        rs = [r for r in (_ratio(s) for s in ev.get("b", [])) if r is not None]
        r_t = float(np.median(rs)) if len(rs) >= 5 else None
        col, hgt = [], []
        for pid, sig in _signatures.items():
            if pid in used:
                continue
            r_s = _body_ratio(pid)
            hd = abs(r_t / r_s - 1.0) if (r_t and r_s) else None
            ce = sig.colour_est
            if mean_c is not None and ce is not None:
                sv = _colour_sim(mean_c, ce)
                body_known = r_s is not None or len(_BODY.get(pid, ())) == 0
                if sv is not None and ((hd is not None and hd <= 0.08) or (hd is None and (r_t is None or not body_known))):
                    col.append((sv, pid))
            if hd is not None:
                hgt.append((hd, pid))
        col.sort(reverse=True)
        hgt.sort()
        txt = "clothes=%s height=%s r=%s n=%d/%d" % (
            ",".join("%s:%.2f" % (p, s) for s, p in col[:2]) or "none",
            ",".join("%s:%.1f%%" % (p, d * 100) for d, p in hgt[:2]) or "none",
            ("%.3f" % r_t) if r_t else "NA", len(cs), len(rs))
        if col and col[0][0] >= _REID_MIN_SIM and (len(col) == 1 or col[0][0] - col[1][0] >= _REID_MARGIN):
            return col[0][1], "by clothes+height | " + txt
        if hgt and hgt[0][0] <= 0.04 and (len(hgt) == 1 or hgt[1][0] - hgt[0][0] >= 0.04):
            return hgt[0][1], "by height | " + txt
        # NOT_SURE_V1: two known people both fit -> keep watching, never a new person.
        if len(col) >= 2 and col[0][0] >= _REID_MIN_SIM and col[1][0] >= _REID_MIN_SIM - 0.05:
            return "?", "NOT SURE " + txt
        if len(hgt) >= 2 and hgt[0][0] <= 0.04 and hgt[1][0] <= 0.06:
            return "?", "NOT SURE " + txt
        if col and col[0][0] >= 0.45:
            return "?", "NOT SURE " + txt
        return None, txt
    except Exception as e:
        return None, "error %s" % e


def _write_in_view(ids, labels=None, tids=None):
    """Who is in the picture right now, for the app."""
    now = time.time()
    if now - _in_view_t[0] < 0.5:
        return
    _in_view_t[0] = now
    _body_save()
    if _VR is not None:
        _VR.GALLERY.save()
    try:
        import json as _js
        with open("/dev/shm/.vigil_in_view.tmp", "w") as f:
            _js.dump({"t": now, "ids": list(ids), "labels": list(labels or []),
                      "tids": [int(t) if t is not None else None for t in (tids or [])]}, f)
        os.replace("/dev/shm/.vigil_in_view.tmp", "/dev/shm/vigil_in_view.json")
    except Exception:
        pass


# REID_V6: learned appearance fingerprints with memory (vigil_reid.py).
try:
    import vigil_reid as _VR
except Exception as _e_vr:
    _VR = None
    print("[REID6] module not available: %s" % _e_vr, flush=True)
_trk_fp = {}            # track id -> recent fingerprints
_trk_status = {}        # track id -> "Not sure" / "Visitor" while unnamed
_mode = {"mode": "family", "t": 0.0, "resident": None}
_fp_frame = [0]


def _read_mode():
    now = time.time()
    if now - _mode["t"] < 10:
        return _mode
    _mode["t"] = now
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        import json as _js
        with open(os.path.join(here, "vigil_mode.json")) as f:
            _mode["mode"] = _js.load(f).get("mode", "family")
    except Exception:
        pass
    try:
        import sqlite3 as _sq
        c = _sq.connect(os.path.join(here, "vigil.db"), timeout=2)
        r = c.execute("SELECT resident_person_id FROM config").fetchone()
        c.close()
        _mode["resident"] = r[0] if r and r[0] else None
    except Exception:
        pass
    try:
        if _VR is not None:
            import sqlite3 as _sq
            c = _sq.connect(os.path.join(here, "vigil.db"), timeout=2)
            _VR.GALLERY.named = {r[0] for r in c.execute(
                "SELECT id FROM persons WHERE name IS NOT NULL AND TRIM(name) != ''")}
            c.close()
    except Exception:
        pass
    return _mode


def _fp_for(pairs, frame):
    """Fingerprints for (track id, keypoints) pairs; remembers them per track."""
    if _VR is None or frame is None or not pairs:
        return {}
    crops, owners = [], []
    for t, k in pairs:
        c = _VR.crop_person(frame, k)
        if c is not None:
            crops.append(c)
            owners.append(t)
    E = _VR.EMBEDDER.embed(crops) if crops else None
    if E is None:
        return {}
    out = {}
    for t, v in zip(owners, E):
        q = _trk_fp.setdefault(t, [])
        q.append(v)
        del q[:-12]
        out[t] = v
    if len(_trk_fp) > 200:
        _trk_fp.clear()
    return out


def _reid6_decide(tid, ev, used):
    """REID_V61: fingerprints decide; clothing+height only for people with no
    fingerprints yet, and true height as a tie-break between two look-alikes."""
    m = _read_mode()
    resident = m["resident"] if m["mode"] == "resident" else None
    fps = _trk_fp.get(tid, [])
    excluded = set(used)
    fptxt = ""
    if resident:
        excluded |= {p for p in _signatures if p != resident}
    if _VR is not None and _VR.EMBEDDER.kind and len(fps) >= 3:
        withfp = [p for p in _signatures if len(_VR.GALLERY.vecs.get(p, ())) > 0]
        cands = [p for p in withfp if p not in excluded]
        refs = [p for p in withfp if p in excluded and (p in used or p in _VR.GALLERY.named)]
        if cands or refs:
            d, txt = _VR.GALLERY.decide(np.array(fps[-10:]), cands, _VR.EMBEDDER.kind, refs)
            ev["fpbest"] = _VR.GALLERY.last_best
            if d is None and _VR.GALLERY.last_kind in ("flat", "none"):
                ev["nobody"] = ev.get("nobody", 0) + 1
            if d == "?":
                top = [p for _s, p in _VR.GALLERY.last_top]
                if len(top) == 2:
                    hd, hw = _reid_decide(ev, set(_signatures) - set(top))
                    if hd in top and hw.startswith("by height"):
                        return hd, "fingerprint+height | " + txt + " | " + hw
                if len(fps) >= 6:
                    ev["unsure"] = ev.get("unsure", 0) + 1
            if d is not None:
                return d, "fingerprint " + txt
            if resident and cands:
                return None, "fingerprint " + txt
            # Fingerprints say none of these people. Clothing may only be used
            # for people who have no fingerprints yet.
            excluded |= set(withfp)
            fptxt = " || " + txt
    # REID_V62: once fingerprints are in use, clothing never names anyone.
    if _VR is not None and not _VR.EMBEDDER.failed and _VR.GALLERY.vecs:
        if not _VR.EMBEDDER.kind:
            return None, "waiting for the fingerprint model"
        if len(fps) < 3:
            return None, "waiting for a clear view"
        if not fptxt:
            ev["nobody"] = ev.get("nobody", 0) + 1
        return None, (fptxt[4:] if fptxt else "fingerprints: nobody known")
    d2, w2 = _reid_decide(ev, excluded)
    return d2, w2 + fptxt


def _reid6_new_ok(tid, ev):
    """REID_V61: may this track become a NEW person?"""
    if _VR is None or _VR.EMBEDDER.failed or not _VR.GALLERY.vecs:
        return True                                  # no fingerprints in use: old behaviour
    if not _VR.EMBEDDER.kind:
        return False                                 # model still loading
    if len(_trk_fp.get(tid, [])) < 3:
        return False                                 # no fingerprint yet: never on no evidence
    # Only when the fingerprints keep saying "nobody we know", and rarely
    # "maybe a family member".
    nob, uns = ev.get("nobody", 0), ev.get("unsure", 0)
    return nob >= 5 and nob >= 3 * uns


def _reid6_early(tid, ev, used):
    """A strong fingerprint match can name someone after 3 frames."""
    if _VR is None or len(_trk_fp.get(tid, [])) < 3:
        return False
    r = _reid6_decide(tid, ev, used)
    if r[0] not in (None, "?") and (r[1].startswith("fingerprint strong")
                                   or (r[1].startswith("fingerprint") and len(_trk_fp.get(tid, [])) >= 6)):
        ev["early"] = r
        return True
    return False


def reload_known_persons() -> None:
    """Load named persons from DB at startup so IDs persist across restarts."""
    import sqlite3
    # Single-resident configuration, refreshed on every startup.
    try:
        _c = sqlite3.connect('/home/rushil-mohan/passive-health-moniter/vigil.db')
        _r = _c.execute(
            "SELECT single_resident_mode, resident_person_id FROM config").fetchone()
        _c.close()
        if _r:
            _SINGLE_MODE[0] = bool(_r[0])
            _RESIDENT_PID[0] = _r[1]
            if _SINGLE_MODE[0]:
                print("[person_hook] single-resident mode: %s" % _RESIDENT_PID[0])
    except Exception:
        pass
    try:
        conn = sqlite3.connect('/home/rushil-mohan/passive-health-moniter/vigil.db')
        rows = conn.execute(
            'SELECT id, height_px, confidence, frames_seen, colour_hist, last_seen_at FROM persons '
            "WHERE height_px IS NOT NULL AND (name IS NOT NULL OR datetime(last_seen_at) > datetime('now','localtime','-1 day'))"
        ).fetchall()
        # Move the id counter past every id already stored, so a restart
        # can never regenerate an existing id and merge two people.
        try:
            _mx = 0
            for (_i,) in conn.execute("SELECT id FROM persons").fetchall():
                try:
                    _mx = max(_mx, int(str(_i).rsplit("_", 1)[-1]))
                except Exception:
                    pass
            if _mx > _id_counter[0]:
                _id_counter[0] = _mx
        except Exception:
            pass
        conn.close()
        for pid, height_px, conf, frames_seen, colour_hist, last_seen_at in rows:
            if pid not in _signatures:
                sig = _PersonSignature(pid)
                count = min(50, max(5, int((frames_seen or 100) / 60)))
                for _ in range(count):
                    sig.height_samples.append(height_px)
                sig.frames_seen = frames_seen or 100
                # Restore appearance so this person is matchable on their
                # first frame after a restart, not only after rebuilding
                # several samples from scratch.
                if colour_hist and _fresh(last_seen_at):
                    try:
                        _v = np.array([float(x) for x in colour_hist.split(",")],
                                      dtype='float32')
                        if _v.size >= 8:
                            for _ in range(3):
                                sig.colour_samples.append(_v)
                    except Exception:
                        pass
                sig.reported = True
                sig.photo_reported = True
                sig.photo_reported = True   # already identified in a prior run — don't re-crop
                _signatures[pid] = sig
                print(f'[person_hook] Restored: {pid} height={height_px}')
    except Exception as e:
        print(f'[person_hook] reload_known_persons error: {e}')

_last_main_id = [None]   # person_id
_track_to_pid = {}
_last_refresh = [0.0]
_SINGLE_MODE = [False]    # single-resident mode enabled
_RESIDENT_PID = [None]    # configured resident person_id   # last live persons refresh
_MIN_FRAMES_TO_REPORT = 8   # never write a persons row with no measurements   # ByteTrack id -> VIGIL person_id
_current_frame = None  # most recent frame for photo capture of all_persons[0] as of the most recent call


def _next_id():
    _id_counter[0] += 1
    return f"person_{_id_counter[0]}"


_named_cache = [0.0, set()]


def _named_ids():
    """NAMED_PERSISTENT_V1: ids of people a caregiver has named. Cached 20 s."""
    now = time.time()
    if now - _named_cache[0] < 20:
        return _named_cache[1]
    try:
        import sqlite3 as _sq
        _c = _sq.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vigil.db"), timeout=2)
        _rows = _c.execute("SELECT id FROM persons WHERE name IS NOT NULL AND name != ''").fetchall()
        _c.close()
        _named_cache[1] = {r[0] for r in _rows}
        _named_cache[0] = now
    except Exception:
        _named_cache[0] = now - 15      # retry soon, keep the last known set
    return _named_cache[1]


def _cleanup():
    now = time.time()
    named = _named_ids()
    dead = []
    for pid, s in list(_signatures.items()):
        age = now - s.last_seen
        if pid in named:
            # A known person is remembered while out of view. Yesterday's
            # clothes must not veto today's match, so colour expires.
            if age > 12 * 3600 and len(s.colour_samples):
                s.colour_samples.clear()
            continue
        if age > 120:
            dead.append(pid)
    for pid in dead:
        del _signatures[pid]


def _estimate_height(kp, conf_threshold=0.25):
    nose_ok = kp[0, 2] >= conf_threshold
    lank_ok = kp[15, 2] >= conf_threshold
    rank_ok = kp[16, 2] >= conf_threshold
    if not nose_ok or not (lank_ok or rank_ok):
        return None
    ankle_y = kp[15, 1] if lank_ok else kp[16, 1]
    h = float(ankle_y - kp[0, 1])
    return h if h > 0 else None


def vigil_track_persons(all_persons, room_id=None, frame=None, track_ids=None):
    """
    Call once per frame with `all_persons` — the SAME list activity_monitor.py
    already built via select_all_persons(results[0], CONF_THRESHOLD), sorted
    largest-first. all_persons[0] is always the resident being measured.

    Returns: person_id (str) of all_persons[0], or None if no one is in frame.
    Never raises — every code path is wrapped so a tracking bug can never
    crash the camera pipeline.
    """
    global _current_frame
    try:
        room_id = room_id or ROOM_ID
        if frame is not None:
            _current_frame = frame
        if not all_persons:
            _last_main_id[0] = None
            _write_in_view([])
            return None

        _cleanup()
        frame_assignments = []  # (person_id, height_px) for this frame, in all_persons order

        global _track_to_pid
        # Two detections in the same frame cannot be the same person.
        _used_this_frame = set()
        _apply_corrections()
        _apply_merges()
        _boxes = [_kp_box(_k) for _k in all_persons]
        _fps = {}
        if _VR is not None and _current_frame is not None and track_ids:
            _fp_frame[0] += 1
            _want = []
            for _i, _k in enumerate(all_persons):
                _t = track_ids[_i] if _i < len(track_ids) else None
                if _t is None or _is_occluded(_i, _boxes):
                    continue
                if _t not in _track_to_pid or _fp_frame[0] % 3 == 0:
                    _want.append((_t, _k))
            if _want:
                _fps = _fp_for(_want[:4], _current_frame)
        for _idx, kp in enumerate(all_persons):
            height_px = _estimate_height(kp)
            shape_r = _estimate_shape(kp)
            colour_r = _estimate_colour(kp, _current_frame)
            _occl = _is_occluded(_idx, _boxes)
            if _occl:
                colour_r = None     # torso crop likely includes the other person
            tid = track_ids[_idx] if (track_ids and _idx < len(track_ids)) else None

            # Single-resident mode: the primary (largest) detection is the
            # configured resident. Identity no longer has to be re-derived
            # from apparent height, which halves during a fall -- exactly
            # when identity must not break. Other simultaneous people below
            # still go through normal matching.
            if _idx == 0 and _SINGLE_MODE[0] and _RESIDENT_PID[0]:
                _rp = _RESIDENT_PID[0]
                _sg = _signatures.get(_rp)
                if _sg is None:
                    _sg = _PersonSignature(_rp)
                    _sg.reported = True
                    _signatures[_rp] = _sg
                if height_px is not None:
                    _sg.update(height_px, shape_r, colour_r)
                if tid is not None:
                    _track_to_pid[tid] = _rp
                _used_this_frame.add(_rp)
                frame_assignments.append((_rp, height_px))
                continue

            # PRIMARY: ByteTrack identity. YOLO maintains this across frames
            # using motion and appearance. Apparent height varies ~10x with
            # distance and partial occlusion, so it cannot be the primary key.
            if tid is not None and tid in _track_to_pid \
                    and _track_to_pid[tid] not in _used_this_frame \
                    and not _track_mismatch(tid, _track_to_pid[tid], colour_r):
                pid = _track_to_pid[tid]
                if pid in _signatures and height_px is not None:
                    # A frame whose clothing disagrees never feeds the record.
                    _signatures[pid].update(height_px, shape_r, None if _TRACK_BAD.get(tid) else colour_r)
                    if not _TRACK_BAD.get(tid) and not _occl:
                        _bs = _stand_height(kp, _current_frame.shape[0] if _current_frame is not None else None)
                        if _bs is not None:
                            _ground_add(_bs)
                            _body_add(pid, _bs)
                    if _VR is not None and tid in _fps and not _TRACK_BAD.get(tid):
                        _sc = _VR.GALLERY.score(_fps[tid][None], pid)
                        _acc = _VR.GALLERY.thresholds(_VR.EMBEDDER.kind)[0]
                        if _sc is None or _sc >= _acc:
                            _VR.GALLERY.add(pid, _VR.GALLERY.exclusive_rows(_fps[tid][None], pid))
                        elif _sc < _acc - 0.15 and len(_VR.GALLERY.vecs.get(pid, ())) < 10:
                            _TRACK_BAD[tid] = _TRACK_BAD.get(tid, 0) + 2   # fingerprint strongly disagrees
                _used_this_frame.add(pid)
                frame_assignments.append((pid, height_px))
                continue

            if height_px is None:
                frame_assignments.append((None, None))
                continue

            # SECONDARY (REID_V4): a track nobody has claimed yet. Collect about
            # a second of evidence, then decide on the average, and only when
            # one person is clearly the best match.
            if tid is None:
                frame_assignments.append((None, None))
                continue
            ev = _evidence.setdefault(tid, {"c": [], "h": [], "n": 0})
            ev["n"] += 1
            if colour_r is not None:
                ev["c"].append(colour_r)
                ev["c"] = ev["c"][-15:]
            ev["h"].append(height_px)
            ev["h"] = ev["h"][-15:]
            _bs = _stand_height(kp, _current_frame.shape[0] if _current_frame is not None else None)
            if _bs is not None and not _occl:
                _ground_add(_bs)
                ev.setdefault("b", []).append(_bs)
                ev["b"] = ev["b"][-15:]
            if len(_evidence) > 200:
                _evidence.clear()
            if len(ev["c"]) < _REID_MIN_SAMPLES and ev["n"] < _REID_GIVE_UP \
                    and not _reid6_early(tid, ev, _used_this_frame):
                frame_assignments.append((None, None))     # "Identifying..."
                continue
            matched, _why = ev.pop("early", None) or _reid6_decide(tid, ev, _used_this_frame)
            if matched == "?":
                _trk_status[tid] = "Not sure"
                ev["n"] = min(ev["n"], _REID_GIVE_UP - 1)
                if not ev.get("amb"):
                    ev["amb"] = 1
                    print("[REID] track %s -> NOT SURE yet | %s" % (tid, _why), flush=True)
                frame_assignments.append((None, None))
                continue
            if matched:
                print("[REID] track %s -> %s | %s" % (tid, matched, _why), flush=True)
                _signatures[matched].update(height_px, shape_r, colour_r)
                _track_to_pid[tid] = matched
                _used_this_frame.add(matched)
                for _b in ev.get("b", []):
                    _body_add(matched, _b)
                if _VR is not None and _trk_fp.get(tid):
                    # REID_V63: only a strong match (or a tap) teaches a look
                    if _why.startswith("fingerprint strong") or len(_VR.GALLERY.vecs.get(matched, ())) < 5:
                        _VR.GALLERY.add(matched, _VR.GALLERY.exclusive_rows(np.array(_trk_fp[tid][-6:]), matched))
                _trk_status.pop(tid, None)
                _evidence.pop(tid, None)
                _pending_new.pop(tid, None)
                frame_assignments.append((matched, height_px))
                continue
            else:
                if _occl or ev["n"] < _REID_GIVE_UP:
                    frame_assignments.append((None, None))  # still unclear
                    continue
                if _read_mode()["mode"] != "resident" and not _reid6_new_ok(tid, ev):
                    if ev.get("unsure", 0) > ev.get("nobody", 0):
                        _trk_status[tid] = "Not sure"
                    ev["n"] = _REID_GIVE_UP - 10      # look again in about a second
                    frame_assignments.append((None, None))
                    continue
                if _read_mode()["mode"] == "resident":
                    _trk_status[tid] = "Visitor"
                    ev["n"] = 0
                    frame_assignments.append((None, None))
                    continue
                print("[REID] track %s -> NEW person | %s" % (tid, _why), flush=True)
                _evidence.pop(tid, None)
                _pending_new.pop(tid, None)
                # MATCH_DBG: log why no existing signature matched, so
                # thresholds can be set from observed data rather than
                # guessed. Remove once tracking is validated.
                try:
                    _cands = []
                    for _pid, _sg in _signatures.items():
                        _he = _sg.height_est
                        _se = _sg.shape_est
                        _hd = (abs(height_px - _he) / _he * 100) if _he else None
                        _sd = (abs(shape_r - _se) / _se * 100) if (_se and shape_r) else None
                        _cs = _colour_sim(colour_r, _sg.colour_est)
                        _cands.append("%s(h%.0f d%s s%s ds%s c%s)" % (
                            _pid,
                            _he if _he else -1,
                            ("%.0f%%" % _hd) if _hd is not None else "NA",
                            ("%.3f" % _se) if _se else "NA",
                            ("%.0f%%" % _sd) if _sd is not None else "NA",
                            ("%.2f" % _cs) if _cs is not None else "NA"))
                    print("[MATCH_DBG] NEW id | this: h=%.0f shape=%s tid=%s | tried: %s" % (
                        height_px,
                        ("%.3f" % shape_r) if shape_r else "None",
                        tid,
                        " ".join(_cands) if _cands else "(none yet)"))
                except Exception:
                    pass
                new_id = _next_id()
                sig = _PersonSignature(new_id)
                sig.update(height_px, shape_r, colour_r)
                _signatures[new_id] = sig
                if _VR is not None and _trk_fp.get(tid):
                    _VR.GALLERY.add(new_id, np.array(_trk_fp[tid][-6:]))
                _used_this_frame.add(new_id)
                if tid is not None:
                    _track_to_pid[tid] = new_id
                    if len(_track_to_pid) > 500:
                        for _k in list(_track_to_pid)[:250]:
                            _track_to_pid.pop(_k, None)
                frame_assignments.append((new_id, height_px))

        # all_persons[0] is the largest = the resident being measured for gait
        main_id = frame_assignments[0][0] if frame_assignments else None
        _write_in_view([a[0] for a in frame_assignments],
                       [(_trk_status.get(track_ids[_j]) if (track_ids and _j < len(track_ids)) else None)
                        for _j in range(len(frame_assignments))],
                       [(track_ids[_j] if (track_ids and _j < len(track_ids)) else None)
                        for _j in range(len(frame_assignments))])
        _last_main_id[0] = main_id

        # Keep persons rows live. The server UPDATEs existing rows on
        # /persons/detect, but each person used to be posted only once, so
        # last_seen_at, frames_seen and height_px froze after first report.
        # Only refresh people who (a) already have a row and (b) were seen in
        # the last 10 s -- the server stamps last_seen_at at request time.
        _now_r = time.time()
        if _now_r - _last_refresh[0] >= 20:
            _last_refresh[0] = _now_r
            _live = []
            for _rpid, _rsg in list(_signatures.items()):
                if not getattr(_rsg, "reported", False):
                    continue
                if _now_r - getattr(_rsg, "last_detected", 0.0) > 10:
                    continue
                _he = _rsg.height_est
                # Send appearance too. Without it a restart leaves only
                # height to match on, and height varies far too much.
                _ce = _rsg.colour_est
                _ch = None
                if _ce is not None:
                    try:
                        _ch = ",".join("%.4f" % float(x) for x in _ce)
                    except Exception:
                        _ch = None
                _live.append({
                    "person_id": _rpid,
                    "frames_seen": int(_rsg.frames_seen),
                    "height_px": round(_he, 1) if _he else None,
                    "confidence": round(float(_rsg.confidence), 3),
                    "colour_hist": _ch,
                })
            if _live:
                try:
                    requests.post(f"{SERVER_URL}/persons/detect",
                                  json={"room_id": room_id, "persons": _live},
                                  headers={"X-VIGIL-Key": API_KEY}, timeout=3)
                except Exception:
                    pass

        # Head-crop identification photo — attempted every frame (not gated by
        # the 60s metadata cycle) so we can retry until the person actually
        # faces the camera. Only marks photo_reported once a real head crop
        # succeeds, or after MAX_PHOTO_ATTEMPTS failed tries (person who never
        # faces the camera still gets registered with a fallback full-frame shot).
        if frame is not None:
            for kp, (pid, _height_px) in zip(all_persons, frame_assignments):
                if pid is None:
                    continue
                sig = _signatures.get(pid)
                if sig is None or sig.photo_reported:
                    continue
                if sig.confidence < 0.4 or sig.frames_seen < 30:
                    continue
                crop = _crop_head(frame, kp)
                if crop is not None:
                    _post_person_photo(pid, crop)
                    sig.photo_reported = True
                else:
                    sig.photo_attempts += 1
                    if sig.photo_attempts >= MAX_PHOTO_ATTEMPTS:
                        # No usable head crop: photograph this person's body,
                        # never the whole room.
                        try:
                            _pts = kp[kp[:, 2] >= 0.3]
                            if len(_pts) >= 5:
                                _x1, _y1 = _pts[:, :2].min(axis=0)
                                _x2, _y2 = _pts[:, :2].max(axis=0)
                                _pad = 0.12 * max(_x2 - _x1, _y2 - _y1)
                                _fh, _fw = frame.shape[:2]
                                _body = frame[max(0, int(_y1 - _pad)):min(_fh, int(_y2 + _pad)),
                                              max(0, int(_x1 - _pad)):min(_fw, int(_x2 + _pad))]
                                if _body.shape[0] >= 40 and _body.shape[1] >= 20:
                                    _post_person_photo(pid, _body)
                        except Exception:
                            pass
                        sig.photo_reported = True

        # Periodic reporting of newly-confident persons to the server
        now = time.time()
        if now - _last_report[0] >= 60:
            to_report = [
                pid for pid, s in _signatures.items()
                if not s.reported and s.confidence >= 0.4 and s.frames_seen >= 30
            ]
            if to_report:
                _last_report[0] = now
                payload = []
                for pid in to_report:
                    s = _signatures[pid]
                    payload.append({
                        "person_id": pid,
                        "height_px": round(s.height_est, 1) if s.height_est else None,
                        "confidence": round(s.confidence, 3),
                        "frames_seen": s.frames_seen,
                    })
                    s.reported = True
                try:
                    requests.post(
                        f"{SERVER_URL}/persons/detect",
                        json={"room_id": room_id, "persons": payload},
                        headers={"X-VIGIL-Key": API_KEY},
                        timeout=3,
                    )
                except Exception:
                    pass

        return main_id
    except Exception:
        return None


def get_main_person_id():
    """Returns the person_id of the currently-tracked resident (all_persons[0]
    from the most recent vigil_track_persons() call), or None."""
    return _last_main_id[0]


def get_active_count(window_sec=30):
    now = time.time()
    return sum(1 for s in _signatures.values() if now - s.last_seen < window_sec)
