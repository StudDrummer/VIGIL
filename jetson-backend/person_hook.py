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
            if dst == "__deleted__":          # DELETE_PERSON_V1
                _signatures.pop(src, None)
                for _k, _v in list(_track_to_pid.items()):
                    if _v == src:
                        _track_to_pid.pop(_k, None)
                try:
                    _BODY.pop(src, None)
                except Exception:
                    pass
                if _VR is not None:
                    _VR.GALLERY.drop(src)
                    _VR.GALLERY.save(force=True)
                _PROP.pop(src, None)
                _PROP_VER.pop(src, None)
                if _V8 is not None:                                   # IDENTITY_V8
                    _V8.drop(src)
                    for _k, _v in list(_V8_VER.items()):
                        if _v == src:
                            _V8_VER.pop(_k, None)
                c.execute("UPDATE person_corrections SET applied=1 WHERE id=?", (cid,))
                print("[CORRECTION] deleted %s" % src, flush=True)
                continue
            if str(src).startswith("track:"):
                try:
                    _t = int(str(src).split(":", 1)[1])
                    _prev = _track_to_pid.get(_t)
                    if _prev and _prev != dst:                      # LEARN_V3: undo what was learned under the wrong name
                        if _VR is not None and _trk_fp.get(_t):
                            _VR.GALLERY.remove_recent(_prev, np.array(_trk_fp[_t]))
                        try:
                            _prop_learn(_t, dst, wrong=_prev)
                        except Exception:
                            pass
                        if _V8 is not None:
                            for _gal in (_V8.face_gal,):
                                _g = _gal.get(_prev)
                                _mine = [e[0] for e in _V8_TF.get(_t, ())]
                                if _g and _mine:
                                    _gal[_prev] = [v for v in _g if max(float(v @ m) for m in _mine) < 0.97]
                        print("[LEARN] track %s was wrongly %s: its views were taken back" % (_t, _prev), flush=True)
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
                    if not (_prev and _prev != dst):
                        _prop_learn(_t, dst)                      # LEARN_V2
                    _v8_learn_track(_t, dst, _named_ids() | {dst}, "named")   # IDENTITY_V8
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
            if _V8 is not None and src != dst:                    # IDENTITY_V8: merge what was learned
                for _gal in (_V8.face_gal, _V8.walk_gal):
                    if src in _gal:
                        _gal.setdefault(dst, []).extend(_gal.pop(src))
                _V8.dirty = True
            for k, v in list(_track_to_pid.items()):
                if v == src:
                    _track_to_pid[k] = dst
                    _prop_learn(k, dst, wrong=(src if src != dst else None))   # LEARN_V2
                    _v8_learn_track(k, dst, _named_ids() | {dst},
                                    "confirmed" if src == dst else "corrected from %s" % src)
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


# ---------------------------------------------------------------------------
# PROPORTIONS_V1 (watch-only): body proportions from the 17 body points.
#
# Each limb is measured relative to the person's own torso, so the numbers do
# not change with distance from the camera. They are only taken from upright,
# clearly visible poses, because a bent arm or a body turned sideways changes
# what a flat camera picture shows.
#
# This does NOT change any names. It records, for every named person in view,
# whether their proportions agree with the name VIGIL gave, so the 4-day trial
# shows whether it would have caught any mistake. It is switched on only if
# the trial shows it helps.
# ---------------------------------------------------------------------------
_PROP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proportions_check.json")
_PROP = {}                 # pid -> deque of ratio vectors (assigned frames)
_PROP_TRK = {}             # track id -> deque of ratio vectors
_PROP_LOG = {}             # track id -> last log time
_PROP_STATS = {"checks": 0, "agree": 0, "disagree": 0, "unsure": 0, "since": None, "events": []}
_PROP_SAVED = [0.0]


def _prop_vec(kp):
    try:
        k = np.asarray(kp, dtype=float)
        if k.shape[0] < 17 or min(k[i][2] for i in (5, 6, 11, 12, 13, 14)) < 0.5:
            return None
        P = lambda i: k[i][:2]
        sh, hp = (P(5) + P(6)) / 2, (P(11) + P(12)) / 2
        torso = float(np.linalg.norm(sh - hp))
        if torso < 30 or abs(sh[0] - hp[0]) > 0.3 * abs(hp[1] - sh[1]) or hp[1] <= sh[1]:
            return None                                 # small, or not upright
        d = lambda a, b: float(np.linalg.norm(P(a) - P(b))) / torso

        def pair(a1, b1, a2, b2, c=(0.5,)):
            vals = [d(a, b) for a, b in ((a1, b1), (a2, b2)) if k[a][2] >= c[0] and k[b][2] >= c[0]]
            return float(np.mean(vals)) if vals else None
        feats = [d(5, 6),                                # shoulder width
                 d(11, 12),                              # hip width
                 pair(5, 7, 6, 8),                       # upper arm
                 pair(11, 13, 12, 14)]                   # thigh
        shin = pair(13, 15, 14, 16)
        feats.append(shin)
        if any(f is None for f in feats[:4]):
            return None
        return np.array([f if f is not None else np.nan for f in feats], dtype=float)
    except Exception:
        return None


# PERSON_ACTIVITY_V1: how long each named person was moving vs still while in
# view, so the Today screen can show an activity ring for every person.
# Moving = the body centre (shoulders and hips) shifted by more than 12% of
# the person's torso length over about one second.
_ACT = {"acc": {}, "pos": {}, "sent": 0.0}


def _activity_observe(all_persons, assignments, named):
    now = time.time()
    for kp, (pid, _h) in zip(all_persons, assignments):
        if pid is None or pid not in named:
            continue
        try:
            k = np.asarray(kp, dtype=float)
            idx = [i for i in (5, 6, 11, 12) if k[i][2] >= 0.4]
            if len(idx) < 3:
                continue
            c = k[idx, :2].mean(axis=0)
            sh = [k[i][:2] for i in (5, 6) if k[i][2] >= 0.4]
            hp = [k[i][:2] for i in (11, 12) if k[i][2] >= 0.4]
            if not sh or not hp:
                continue
            torso = float(np.linalg.norm(np.mean(sh, axis=0) - np.mean(hp, axis=0)))
            if torso < 15:
                continue
        except Exception:
            continue
        hist = _ACT["pos"].setdefault(pid, deque(maxlen=40))
        hist.append((now, c, torso))
        acc = _ACT["acc"].setdefault(pid, {"active": 0.0, "still": 0.0, "last": now})
        dt = min(1.0, max(0.0, now - acc["last"]))
        acc["last"] = now
        old = [h for h in hist if now - h[0] >= 0.8]
        if not old or dt <= 0:
            continue
        t0, c0, tr0 = old[-1]
        moving = float(np.linalg.norm(c - c0)) / max(torso, tr0) > 0.12
        acc["active" if moving else "still"] += dt
    if now - _ACT["sent"] >= 60 and _ACT["acc"]:
        _ACT["sent"] = now
        items = [{"person_id": p, "active_s": round(a["active"], 1), "still_s": round(a["still"], 1)}
                 for p, a in _ACT["acc"].items() if a["active"] + a["still"] > 0]
        for a in _ACT["acc"].values():
            a["active"] = a["still"] = 0.0
        if items:
            try:
                requests.post(f"{SERVER_URL}/persons/activity", json={"items": items},
                              headers={"X-VIGIL-Key": API_KEY}, timeout=3)
            except Exception:
                pass


# LEARN_V2: every "Yes, this is..." and every correction in the app teaches
# body proportions too. The track's recent proportion measurements are stored
# as caregiver-verified for that person (and kept across restarts). Verified
# samples are preferred over automatic ones when comparing.
_PROP_VER = {}             # pid -> deque of verified ratio vectors
_PROP_VER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proportions_model.json")
_PROP_VER_SAVED = [0.0]


def _prop_learn(tid, pid, wrong=None):
    try:
        q = list(_PROP_TRK.get(tid, []))
        if not q or pid is None:
            return
        _PROP_VER.setdefault(pid, deque(maxlen=400)).extend(q)
        if wrong and wrong in _PROP:
            # those frames were filed under the wrong person: take them back out
            bad = {id(x) for x in q}
            _PROP[wrong] = deque([v for v in _PROP[wrong] if id(v) not in bad], maxlen=600)
        _PROP_VER_SAVED[0] = 0.0
        print("[LEARN] %d body-proportion samples verified for %s (now %d)"
              % (len(q), pid, len(_PROP_VER[pid])), flush=True)
        _prop_ver_save()
    except Exception as e:
        print("[LEARN] error: %s" % e, flush=True)


def _prop_ver_save():
    now = time.time()
    if now - _PROP_VER_SAVED[0] < 60:
        return
    _PROP_VER_SAVED[0] = now
    try:
        import json as _js
        data = {p: [[None if np.isnan(x) else round(float(x), 4) for x in v] for v in list(q)[-400:]]
                for p, q in _PROP_VER.items()}
        with open(_PROP_VER_FILE + ".tmp", "w") as f:
            _js.dump(data, f)
        os.replace(_PROP_VER_FILE + ".tmp", _PROP_VER_FILE)
    except Exception:
        pass


def _prop_ver_load():
    try:
        import json as _js
        with open(_PROP_VER_FILE) as f:
            data = _js.load(f)
        for p, vs in data.items():
            _PROP_VER[p] = deque([np.array([np.nan if x is None else x for x in v], dtype=float) for v in vs],
                                 maxlen=400)
        print("[LEARN] loaded verified body proportions for %d people" % len(_PROP_VER), flush=True)
    except Exception:
        pass


_prop_ver_load()


def _prop_veto(tid, pid, named):
    """LEARN_V2: True if the body proportions of this track clearly belong to a
    different named person than `pid`. Used only to hold back a name that the
    appearance match gave without a strong margin; it never names anyone."""
    tq = _PROP_TRK.get(tid)
    if not tq or len(tq) < 10:
        return False, ""
    refs = {p: r for p in named for r in [_prop_ref(p, verified_only=True)] if r is not None}
    if pid not in refs or len(refs) < 2:
        return False, ""
    tv = np.nanmedian(np.array(tq), axis=0)
    ds = {p: _prop_dist(tv, r) for p, r in refs.items()}
    ds = {p: x for p, x in ds.items() if x is not None}
    if pid not in ds:
        return False, ""
    best = min(ds, key=ds.get)
    if best != pid and ds[best] < 0.6 * ds[pid] and ds[pid] > 2.0:
        return True, "proportions fit %s (%.1f) not %s (%.1f)" % (best, ds[best], pid, ds[pid])
    return False, ""


def _prop_ref(pid, verified_only=False):
    vq = _PROP_VER.get(pid)
    if vq and len(vq) >= 15:
        a = np.array(vq)
        med = np.nanmedian(a, axis=0)
        mad = np.nanmedian(np.abs(a - med), axis=0) * 1.4826 + 0.02
        return med, mad
    if verified_only:
        return None
    q = _PROP.get(pid)
    if not q or len(q) < 40:
        return None
    a = np.array(q)
    med = np.nanmedian(a, axis=0)
    mad = np.nanmedian(np.abs(a - med), axis=0) * 1.4826 + 0.02
    return med, mad


def _prop_dist(v, ref):
    med, mad = ref
    z = np.abs(v - med) / mad
    z = z[~np.isnan(z)]
    return float(np.sqrt(np.mean(z ** 2))) if len(z) >= 3 else None


def _prop_observe(all_persons, assignments, track_ids, named):
    now = time.time()
    if _PROP_STATS["since"] is None:
        _PROP_STATS["since"] = time.strftime("%Y-%m-%d %H:%M")
    for i, (kp, (pid, _h)) in enumerate(zip(all_persons, assignments)):
        tid = track_ids[i] if (track_ids and i < len(track_ids)) else None
        v = _prop_vec(kp)
        if v is None:
            continue
        if tid is not None and not _v7_hold(tid):
            tq = _PROP_TRK.setdefault(tid, deque(maxlen=30))
            tq.append(v)
            if len(_PROP_TRK) > 300:
                for _k in list(_PROP_TRK)[:150]:
                    _PROP_TRK.pop(_k, None)
        if pid is None:
            continue
        if pid in named and not (tid is not None and _v7_hold(tid)):
            _PROP.setdefault(pid, deque(maxlen=600)).append(v)
        if tid is None:
            continue
        tq = _PROP_TRK.get(tid)
        if tq is None:
            continue
        if pid not in named or len(tq) < 15 or now - _PROP_LOG.get(tid, 0) < 60:
            continue
        refs = {p: r for p in named for r in [_prop_ref(p)] if r is not None}
        if pid not in refs or len(refs) < 2:
            continue
        _PROP_LOG[tid] = now
        tv = np.nanmedian(np.array(tq), axis=0)
        ds = {p: _prop_dist(tv, r) for p, r in refs.items()}
        ds = {p: x for p, x in ds.items() if x is not None}
        if pid not in ds:
            continue
        best = min(ds, key=ds.get)
        _PROP_STATS["checks"] += 1
        if best == pid:
            _PROP_STATS["agree"] += 1
            verdict = "agrees"
        elif ds[best] < 0.6 * ds[pid] and ds[pid] > 2.0:
            _PROP_STATS["disagree"] += 1
            verdict = "DISAGREES (looks like %s)" % best
            _PROP_STATS["events"] = (_PROP_STATS["events"] + [
                {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "named": pid, "proportions_say": best,
                 "d_named": round(ds[pid], 2), "d_other": round(ds[best], 2)}])[-200:]
        else:
            _PROP_STATS["unsure"] += 1
            verdict = "unsure"
        print("[PROPORTIONS] track %s named %s: %s %s" % (
            tid, pid, verdict, " ".join("%s=%.1f" % (p, x) for p, x in sorted(ds.items(), key=lambda t: t[1])[:3])),
            flush=True)
    if now - _PROP_SAVED[0] > 120:
        _PROP_SAVED[0] = now
        try:
            import json as _js
            with open(_PROP_FILE + ".tmp", "w") as f:
                _js.dump(_PROP_STATS, f)
            os.replace(_PROP_FILE + ".tmp", _PROP_FILE)
        except Exception:
            pass


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
            _nm = _named_ids()                    # STALE_DUPES_V1: named over an unnamed look-alike
            _fit = [p for sc_, p in col if sc_ >= _REID_MIN_SIM - 0.05]
            _fn = [p for p in _fit if p in _nm]
            if len(_fn) == 1:
                return _fn[0], "by clothes (named over an unnamed look-alike) | " + txt
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


# ---------------------------------------------------------------------------
# IDENTITY_V7: names must survive people crossing paths.
#
# The tracker follows each person by position and motion. When two people
# cross, it can hand their track numbers to each other, and the names went
# with the numbers. Now:
#   1. Any two people whose boxes overlap are marked as "crossed".
#   2. While crossed and for 2 s after, nothing is learned from them (a crop
#      of two overlapping people would teach the wrong look).
#   3. Once they separate, fresh fingerprints are taken every frame and the
#      names are re-checked jointly: if swapping them fits clearly better, the
#      names are swapped back. Logged as "[REID7] SWAP".
#   4. Every 2 s, with two or more people in view, all visible names are
#      re-checked the same way, so a mix-up that slipped through is corrected
#      within seconds rather than staying until someone opens the app.
# ---------------------------------------------------------------------------
_V7 = {"pending": {}, "last_cross": {}, "last_check": 0.0, "votes": {}}
V7_HOLD_S = 2.0
V7_SWAP_MARGIN = 0.04       # total similarity gain needed to change names
V7_MIN_FP = 3


def _v7_mark_crossings(boxes, tids):
    if not tids or len(boxes) < 2:
        return
    now = time.time()
    for i in range(len(boxes)):
        if i >= len(tids) or tids[i] is None or boxes[i] is None:
            continue
        for j in range(i + 1, len(boxes)):
            if j >= len(tids) or tids[j] is None or boxes[j] is None:
                continue
            a, b = boxes[i], boxes[j]
            iw = min(a[2], b[2]) - max(a[0], b[0])
            ih = min(a[3], b[3]) - max(a[1], b[1])
            if iw > 0 and ih > 0:
                # widen to "close": horizontal gap smaller than 10% of a box width also counts
                pass
            gap = max(a[0], b[0]) - min(a[2], b[2])
            wmin = min(a[2] - a[0], b[2] - b[0])
            if (iw > 0 and ih > 0) or gap < 0.1 * wmin:
                for t in (tids[i], tids[j]):
                    _V7["last_cross"][t] = now
                    _V7["pending"][t] = now


def _v7_hold(tid):
    """True while a track is crossing someone, or just after: do not learn from it."""
    if tid is None:
        return False
    return time.time() - _V7["last_cross"].get(tid, 0.0) < V7_HOLD_S or tid in _V7["pending"]


def _v7_fps_since(tid, since, n=6):
    q, qt = _trk_fp.get(tid, []), _trk_fp_t.get(tid, [])
    vs = [v for v, t in zip(q, qt) if t > since]
    return np.array(vs[-n:]) if vs else None


def _v7_verify(assignments, tids):
    if _VR is None or not tids or not _VR.EMBEDDER.kind:
        return assignments
    now = time.time()
    vis = [(i, tids[i], pid) for i, (pid, _h) in enumerate(assignments)
           if pid is not None and i < len(tids) and tids[i] is not None]
    # forget tracks no longer in view
    seen = {t for _i, t, _p in vis}
    for t in list(_V7["pending"]):
        if t not in seen and now - _V7["pending"][t] > 10:
            _V7["pending"].pop(t, None)
    if len(vis) < 2 and not any(t in _V7["pending"] for _i, t, _p in vis):
        return assignments
    due = now - _V7["last_check"] >= 2.0
    crossed = [t for _i, t, _p in vis if t in _V7["pending"]
               and now - _V7["last_cross"].get(t, 0) >= 0.5]      # separated for half a second
    if not due and not crossed:
        return assignments
    _V7["last_check"] = now
    # fingerprints taken after each track's last crossing (or recent ones)
    fp = {}
    for _i, t, _p in vis:
        since = _V7["last_cross"].get(t, now - 4.0)
        since = max(since, now - 4.0) if t not in _V7["pending"] else since
        v = _v7_fps_since(t, since)
        if v is not None and len(v) >= V7_MIN_FP:
            fp[t] = v
    cand = [(i, t, p) for i, t, p in vis if t in fp]
    if len(cand) < 1:
        return assignments
    names = [p for _i, _t, p in cand]
    import itertools
    sc = {}
    for _i, t, _p in cand:
        for p in set(names):
            s_ = _VR.GALLERY.score(fp[t], p)
            sc[(t, p)] = s_ if s_ is not None else -1.0
    cur = sum(sc[(t, p)] for _i, t, p in cand)
    best, best_perm = cur, None
    if len(cand) >= 2 and len(cand) <= 5:
        for perm in itertools.permutations(names):
            tot = sum(sc[(t, perm[k])] for k, (_i, t, _p) in enumerate(cand))
            if tot > best + 1e-9:
                best, best_perm = tot, perm
    for _i, t, _p in cand:
        _V7["pending"].pop(t, None)            # checked with clean fingerprints
    if best_perm is None or best - cur < V7_SWAP_MARGIN:
        _V7["votes"].clear()
        return assignments
    key = tuple((t, best_perm[k]) for k, (_i, t, _p) in enumerate(cand))
    _V7["votes"][key] = _V7["votes"].get(key, 0) + 1
    # right after a crossing one clear check is enough; otherwise two in a row
    need = 1 if crossed else 2
    if _V7["votes"][key] < need:
        return assignments
    _V7["votes"].clear()
    out = list(assignments)
    for k, (i, t, old) in enumerate(cand):
        new = best_perm[k]
        if new != old:
            _track_to_pid[t] = new
            out[i] = (new, out[i][1])
            _TRACK_BAD.pop(t, None)
            try:
                _V8_VER.pop(t, None)                 # IDENTITY_V8: no longer what the caregiver confirmed
            except Exception:
                pass
    print("[REID7] SWAP %s (gain %.3f%s)" % (
        ", ".join("track %s: %s -> %s" % (t, old, best_perm[k]) for k, (_i, t, old) in enumerate(cand)
                  if best_perm[k] != old), best - cur, ", after crossing" if crossed else ""), flush=True)
    return out


# REID_V6: learned appearance fingerprints with memory (vigil_reid.py).
try:
    import vigil_reid as _VR
except Exception as _e_vr:
    _VR = None
    print("[REID6] module not available: %s" % _e_vr, flush=True)
_trk_fp = {}            # track id -> recent fingerprints
_trk_fp_t = {}          # track id -> when each of those was taken (IDENTITY_V7)
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
    now = time.time()
    for t, v in zip(owners, E):
        q = _trk_fp.setdefault(t, [])
        q.append(v)
        del q[:-12]
        qt = _trk_fp_t.setdefault(t, [])
        qt.append(now)
        del qt[:-12]
        out[t] = v
    if len(_trk_fp) > 200:
        _trk_fp.clear()
        _trk_fp_t.clear()
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
        # STALE_DUPES_V1: an unnamed entry not seen for 30 minutes (an old
        # duplicate of a family member) no longer competes with the real,
        # named person; such look-alike duplicates made everyone "Not sure".
        _nm = _named_ids()
        _now6 = time.time()
        withfp = [p for p in _signatures if len(_VR.GALLERY.vecs.get(p, ())) > 0
                  and (p in _nm or _now6 - getattr(_signatures[p], "last_seen", 0.0) < 1800)]
        cands = [p for p in withfp if p not in excluded]
        refs = [p for p in withfp if p in excluded and (p in used or p in _VR.GALLERY.named)]
        if cands or refs:
            d, txt = _VR.GALLERY.decide(np.array(fps[-10:]), cands, _VR.EMBEDDER.kind, refs)
            ev["fpbest"] = _VR.GALLERY.last_best
            if d is None and _VR.GALLERY.last_kind in ("flat", "none"):
                ev["nobody"] = ev.get("nobody", 0) + 1
            if d == "?":
                top = [p for _s, p in _VR.GALLERY.last_top]
                # STALE_DUPES_V1: a named family member against an unnamed look-alike
                # entry (almost always a duplicate of that same person): name them.
                _tn = [p for p in top if p in _nm]
                if len(_tn) == 1 and len(top) >= 2 and _tn[0] not in excluded:
                    return _tn[0], "fingerprint (named over an unnamed look-alike) " + txt
                if len(top) == 2:
                    hd, hw = _reid_decide(ev, set(_signatures) - set(top))
                    # LEARN_V4: only when this house's own learning says height
                    # really separates these people
                    _hok = True
                    try:
                        if _V8 is not None and len(_V8.stats["height"]["same"]) >= 12:
                            _hok = _V8._params("height")[4] >= 0.5
                    except Exception:
                        pass
                    if hd in top and hw.startswith("by height") and _hok:
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


# ---------------------------------------------------------------------------
# IDENTITY_V8: one decision from every cue (vigil_identity.py), learned from
# every correction, one name per person in view, numbered "Not sure" labels.
# ---------------------------------------------------------------------------
try:
    import vigil_identity as _VI
    _V8 = _VI.Fusion()
    _V8_FACE = _VI.FaceID()
    _V8_WALK = _VI.WalkTracker()
    print("[V8] identity fusion ready; face matching %s" % (
        "on" if _V8_FACE.ok else "off (%s)" % _V8_FACE.why), flush=True)
except Exception as _e_v8:
    _V8 = _V8_FACE = _V8_WALK = None
    print("[V8] not available: %s" % _e_v8, flush=True)

_V8_TF = {}        # track -> recent (face vector, quality)
_V8_TH = {}        # track -> recent standing-height ratios
_V8_VER = {}       # track -> person the caregiver said this track is (until a crossing)
_V8_ST = {"last_check": 0.0, "votes": {}, "against": {}, "learned": {}, "frame": 0}
V8_NAME = 4.0      # evidence needed to give a name on fused evidence alone
V8_MARGIN = 3.0    # ... and how far ahead of the next person it must be
V8_SWAP = 3.0      # gain needed to swap names between people in view
V8_AGAINST = -3.5  # evidence against a name that releases it
_FRAME_LABELS = {"t": 0.0, "by_tid": {}}
_NS_NUM = {}       # track -> its number among several "Not sure" people


def _v8_observe(all_persons, track_ids, boxes, frame):
    """Collect face, height and walk measurements for each track this frame."""
    if _V8 is None or not track_ids:
        return
    _V8_ST["frame"] += 1
    if _VR is not None and _VR.EMBEDDER.kind:
        try:
            _V8.appearance_acc = float(_VR.GALLERY.thresholds(_VR.EMBEDDER.kind)[0])
        except Exception:
            pass
    now = time.time()
    fh = frame.shape[0] if frame is not None else None
    faces_left = 2          # about 25 ms each on a CPU; at most two a frame
    for i, kp in enumerate(all_persons):
        t = track_ids[i] if i < len(track_ids) else None
        if t is None or _is_occluded(i, boxes) or _v7_hold(t):
            continue
        try:
            _V8_WALK.observe(t, kp, now)
            bs = _stand_height(kp, fh)
            if bs is not None:
                r = _ratio(bs)
                if r is not None:
                    q = _V8_TH.setdefault(t, deque(maxlen=40))
                    q.append(r)
            if _V8_FACE.ok and faces_left > 0 and (_V8_ST["frame"] + i) % 4 == 0:
                faces_left -= 1
                fe = _V8_FACE.embed(frame, kp)
                if fe is not None:
                    _V8_TF.setdefault(t, deque(maxlen=12)).append(fe)
        except Exception:
            pass
    if len(_V8_TF) > 300 or len(_V8_TH) > 300:
        live = set(t for t in track_ids if t is not None)
        for d in (_V8_TF, _V8_TH):
            for t in list(d):
                if t not in live:
                    d.pop(t, None)


# ---------------------------------------------------------------------------
# ENROLL_V1: "Introduce everyone" learns a person properly before saying so.
# The app asks (vigil_enroll.json) to learn person P, seen as track T. This
# follows that person and collects what identity needs: face views, clothing
# fingerprints from the front, side and back, body proportions, standing
# height and the walk. Progress goes to vigil_enroll_state.json for the app's
# ring; only when every part is in does it commit them all to P at once.
# ---------------------------------------------------------------------------
_ENROLL_REQ = os.environ.get("VIGIL_ENROLL_REQ", "/dev/shm/vigil_enroll.json")
_ENROLL_OUT = os.environ.get("VIGIL_ENROLL_OUT", "/dev/shm/vigil_enroll_state.json")
EN_FACES = 8            # face views
EN_FRONT, EN_SIDE, EN_BACK = 8, 5, 5      # clothing fingerprints per view
EN_BODY = 12            # body proportion samples
EN_HEIGHT = 12          # standing height samples
EN_STAGE_MAX = {"still": 40.0, "turn": 50.0, "walk": 45.0}   # seconds in view before moving on with what we have
EN_LOST_S = 1.5
_EN = {"key": None, "pid": None, "name": None, "track": None, "track_hint": None, "stage": "still",
       "stage_t": 0.0, "last_tick": 0.0, "seen_t": 0.0, "faces": [], "face_ids": set(),
       "fp": {"front": [], "side": [], "back": []}, "body": [], "height": [], "walk": None,
       "notes": [], "done": False, "read_t": 0.0, "write_t": 0.0, "others": 0, "started": 0.0}


def _view_of(kp):
    """front / side / back from the keypoints of one person."""
    k = np.asarray(kp, dtype=float)
    c = lambda i: k[i][2] if k.shape[0] > i else 0.0
    nose, le, re = c(0), c(1), c(2)
    sw = abs(k[5][0] - k[6][0]) if c(5) >= 0.4 and c(6) >= 0.4 else None
    th = abs((k[11][1] + k[12][1]) / 2 - (k[5][1] + k[6][1]) / 2) if min(c(5), c(6), c(11), c(12)) >= 0.4 else None
    if sw is not None and th and sw < 0.32 * th:
        return "side"
    if nose >= 0.5 and le >= 0.4 and re >= 0.4:
        return "front"
    if nose < 0.3 and le < 0.3 and re < 0.3:
        return "back"
    return "side"


def _enroll_reset(req):
    _EN.update({"key": req.get("key"), "pid": req.get("person_id"), "name": req.get("name") or "them",
                "track": None, "track_hint": req.get("track"), "stage": "still", "stage_t": 0.0,
                "last_tick": time.time(), "started": time.time(), "seen_t": 0.0, "faces": [], "face_ids": set(),
                "fp": {"front": [], "side": [], "back": []}, "body": [], "height": [], "walk": None,
                "notes": [], "done": False, "others": 0})
    print("[ENROLL] learning %s (track hint %s)" % (_EN["pid"], _EN["track_hint"]), flush=True)


def _enroll_read():
    now = time.time()
    if now - _EN["read_t"] < 0.7:
        return
    _EN["read_t"] = now
    try:
        import json as _js
        with open(_ENROLL_REQ) as f:
            req = _js.load(f)
    except Exception:
        req = None
    if not req or req.get("stop") or now - float(req.get("t", 0)) > 600:
        if _EN["key"] is not None:
            print("[ENROLL] stopped", flush=True)
        _EN["key"] = None
        _EN["track"] = None
        return
    if req.get("key") != _EN["key"]:
        _enroll_reset(req)


def _en_parts():
    """Fraction complete of each part (face, front, side, back, body, height, walk)."""
    face_ok = _V8_FACE is not None and getattr(_V8_FACE, "ok", False)
    p = {"front": min(1.0, len(_EN["fp"]["front"]) / EN_FRONT),
         "body": min(1.0, len(_EN["body"]) / EN_BODY),
         "height": min(1.0, len(_EN["height"]) / EN_HEIGHT),
         "side": min(1.0, len(_EN["fp"]["side"]) / EN_SIDE),
         "back": min(1.0, len(_EN["fp"]["back"]) / EN_BACK),
         "walk": 1.0 if _EN["walk"] is not None else 0.0}
    if face_ok:
        p["face"] = min(1.0, len(_EN["faces"]) / EN_FACES)
    return p


_EN_STAGE_PARTS = {"still": ("face", "front", "body", "height"), "turn": ("side", "back"), "walk": ("walk",)}
_EN_ORDER = ("still", "turn", "walk")


def _enroll_find_track(track_ids):
    live = [t for t in track_ids if t is not None]
    if _EN["track"] in live and _track_to_pid.get(_EN["track"]) in (None, _EN["pid"]):
        return _EN["track"]
    h = _EN["track_hint"]
    # the app's correction for this track takes up to 3 s to apply; until then trust the hint
    if h in live and (_track_to_pid.get(h) in (None, _EN["pid"]) or time.time() - _EN["started"] < 6):
        return h
    for t in live:          # the person came back on a new track VIGIL already gave their name
        if _track_to_pid.get(t) == _EN["pid"] and not _TRACK_BAD.get(t):
            return t
    return None


def _enroll_tick(all_persons, track_ids, boxes, fps):
    _enroll_read()
    if _EN["key"] is None or _EN["done"]:
        _enroll_write()
        return
    now = time.time()
    dt = min(0.5, now - _EN["last_tick"])
    _EN["last_tick"] = now
    t = _enroll_find_track(track_ids or [])
    _EN["track"] = t
    _EN["others"] = max(0, len([x for x in (track_ids or []) if x is not None]) - (1 if t is not None else 0))
    if t is None:
        _enroll_write()
        return
    i = list(track_ids).index(t)
    kp = all_persons[i]
    _EN["seen_t"] = now
    occl = _is_occluded(i, boxes)
    if not occl:
        if t in fps:
            v = _view_of(kp)
            q = _EN["fp"][v]
            q.append(np.array(fps[t]))
            del q[:-16]
        for e in list(_V8_TF.get(t, ()))[-2:]:
            if id(e) not in _EN["face_ids"]:
                _EN["face_ids"].add(id(e))
                _EN["faces"].append(e)
                del _EN["faces"][:-20]
        tq = _PROP_TRK.get(t)
        if tq:
            _EN["body"] = list(tq)[-EN_BODY:]
        th = _V8_TH.get(t)
        if th:
            _EN["height"] = list(th)[-EN_HEIGHT:]
        if _V8_WALK is not None:
            try:
                sig = _V8_WALK.signature(t)
                if sig is not None:
                    _EN["walk"] = sig
            except Exception:
                pass
    # stages move on when their parts are in, or after long enough in view
    _EN["stage_t"] += dt
    parts = _en_parts()
    st = _EN["stage"]
    need = [k for k in _EN_STAGE_PARTS[st] if k in parts]
    complete = all(parts[k] >= 1.0 for k in need)
    if not complete and _EN["stage_t"] >= EN_STAGE_MAX[st]:
        short = [k for k in need if parts[k] < 1.0]
        _EN["notes"].append("%s: not enough %s" % (st, ", ".join(short)))
        complete = True
    if complete:
        nxt = _EN_ORDER.index(st) + 1
        if nxt < len(_EN_ORDER):
            _EN["stage"] = _EN_ORDER[nxt]
            _EN["stage_t"] = 0.0
        else:
            _enroll_commit(t)
    _enroll_write()


def _enroll_commit(t):
    pid = _EN["pid"]
    got = []
    try:
        if _track_to_pid.get(t) != pid:
            _track_to_pid[t] = pid
        _V8_VER[t] = pid
        if pid not in _signatures:
            _nd = _PersonSignature(pid)
            _nd.reported = True
            _nd.photo_reported = True
            _signatures[pid] = _nd
        if _VR is not None:
            vecs = [v for b in ("front", "side", "back") for v in _EN["fp"][b][-8:]]
            if vecs:
                _VR.GALLERY.add(pid, np.array(vecs), pin=True)
                _VR.GALLERY.save(force=True)
                got.append("clothing from %d views" % sum(1 for b in ("front", "side", "back") if _EN["fp"][b]))
        try:
            _prop_learn(t, pid)
            if _EN["body"]:
                got.append("build")
        except Exception:
            pass
        if _V8 is not None:
            nf = _V8.learn_face(pid, list(_EN["faces"]))
            if nf:
                got.append("face (%d views)" % len(_EN["faces"]))
            if _EN["walk"] is not None and _V8.learn_walk(pid, _EN["walk"]):
                got.append("walk")
            if _EN["height"]:
                got.append("height")
            _v8_learn_track(t, pid, _named_ids() | {pid}, "introduced")
            _V8.save(force=True)
    except Exception as e:
        _EN["notes"].append("save error: %s" % e)
    _EN["done"] = True
    _EN["learned"] = got
    print("[ENROLL] %s learned: %s%s" % (pid, ", ".join(got) or "nothing",
          (" | " + "; ".join(_EN["notes"])) if _EN["notes"] else ""), flush=True)


_EN_PROMPT = {"still": "Stand still and face the camera",
              "turn": "Now turn slowly in a full circle",
              "walk": "Now walk across the room and back"}


def _enroll_write():
    now = time.time()
    if now - _EN["write_t"] < 0.4:
        return
    _EN["write_t"] = now
    try:
        import json as _js
        if _EN["key"] is None:
            out = {"t": now, "active": False}
        else:
            parts = _en_parts()
            st = _EN["stage"]
            si = _EN_ORDER.index(st)
            need = [k for k in _EN_STAGE_PARTS[st] if k in parts]
            sp = (sum(parts[k] for k in need) / len(need)) if need else 1.0
            pct = 1.0 if _EN["done"] else min(0.99, (si + sp) / len(_EN_ORDER))
            lost = _EN["track"] is None
            out = {"t": now, "active": True, "key": _EN["key"], "person_id": _EN["pid"], "name": _EN["name"],
                   "stage": "done" if _EN["done"] else st, "stage_index": si, "stages": len(_EN_ORDER),
                   "progress": round(pct, 3), "parts": {k: round(v, 2) for k, v in parts.items()},
                   "in_view": not lost, "others_in_view": _EN["others"], "done": _EN["done"],
                   "learned": _EN.get("learned") or [], "notes": _EN["notes"],
                   "prompt": ("Done" if _EN["done"] else
                              ("Step back in front of the camera" if lost else _EN_PROMPT[st]))}
        with open(_ENROLL_OUT + ".tmp", "w") as f:
            _js.dump(out, f)
        os.replace(_ENROLL_OUT + ".tmp", _ENROLL_OUT)
    except Exception:
        pass


def _v8_raw(tid, pid):
    """Raw score of each cue for 'track tid is person pid' (None = no data)."""
    raw = {}
    try:
        fps = _trk_fp.get(tid, [])
        if _VR is not None and _VR.EMBEDDER.kind and len(fps) >= 3 and len(_VR.GALLERY.vecs.get(pid, ())) > 0:
            raw["appearance"] = _VR.GALLERY.score(np.array(fps[-6:]), pid)
    except Exception:
        pass
    try:
        raw["face"] = _V8.face_sim(list(_V8_TF.get(tid, ())), pid)
    except Exception:
        pass
    try:
        tq = _PROP_TRK.get(tid)
        if tq and len(tq) >= 10:
            ref = _prop_ref(pid, verified_only=True) or _prop_ref(pid)
            if ref is not None:
                d = _prop_dist(np.nanmedian(np.array(tq), axis=0), ref)
                raw["body"] = None if d is None else -d
    except Exception:
        pass
    try:
        th = _V8_TH.get(tid)
        pr = _body_ratio(pid)
        if th and len(th) >= 8 and pr:
            raw["height"] = -abs(float(np.median(th)) - pr) / pr
    except Exception:
        pass
    try:
        raw["walk"] = _V8.walk_sim(_V8_WALK.signature(tid), pid)
    except Exception:
        pass
    return {k: v for k, v in raw.items() if v is not None}


def _v8_rank(tid, cands):
    out = []
    for p in cands:
        tot, ev = _V8.evidence(_v8_raw(tid, p))
        if ev:
            out.append((tot, p, ev))
    out.sort(key=lambda x: -x[0])
    return out


def _v8_txt(rk, n=2):
    return "; ".join("%s %+.1f (%s)" % (p, t, " ".join("%s%+.1f" % (k[:4], v) for k, v in sorted(e.items())))
                     for t, p, e in rk[:n])


def _v8_decide_new(tid, matched, why, used):
    """Second opinion on a track nobody has claimed yet."""
    if _V8 is None or tid is None:
        return matched, why
    m = _read_mode()
    named = [p for p in _named_ids() if p in _signatures and p not in used]
    if m["mode"] == "resident" and m["resident"]:
        named = [p for p in named if p == m["resident"]]
    if not named:
        return matched, why
    rk = _v8_rank(tid, named)
    if not rk:
        return matched, why
    best_t, best_p, best_e = rk[0]
    second = rk[1][0] if len(rk) > 1 else 0.0
    txt = _v8_txt(rk)
    strong = best_e.get("face", 0) >= 1.5 or best_e.get("appearance", 0) >= 1.5
    if matched not in (None, "?"):
        mine = next((t for t, p, _e in rk if p == matched), None)
        if mine is None:
            return matched, why
        if best_p != matched and best_t >= 2.0 and best_t - mine >= V8_MARGIN:
            trained = all(len(_V8.stats["appearance"][c]) >= 30 for c in ("same", "diff"))
            if (best_e.get("face", 0) >= 2.0 or (trained and len(best_e) >= 2)) \
                    and best_t >= V8_NAME and best_t - mine >= V8_MARGIN + 1:
                return best_p, "V8 overrides %s | %s" % (matched, txt)
            return "?", why + " | V8 doubts: " + txt
        if mine <= V8_AGAINST:
            return "?", why + " | V8 against: " + txt
        return matched, why
    if best_t >= V8_NAME and best_t - second >= V8_MARGIN and len(best_e) >= 2 and strong:
        return best_p, "V8 " + txt
    return matched, why


def _v8_learn_track(tid, pid, others, tag):
    """A caregiver said track tid is pid: every cue learns from it."""
    if _V8 is None or tid is None or pid is None:
        return
    try:
        raw_same = _v8_raw(tid, pid)
        for s_, x in raw_same.items():
            _V8.add_example(s_, x, True)
        nd = 0
        for q in others:
            if q == pid:
                continue
            for s_, x in _v8_raw(tid, q).items():
                _V8.add_example(s_, x, False)
                nd += 1
        nf = _V8.learn_face(pid, list(_V8_TF.get(tid, ())))
        nw = _V8.learn_walk(pid, _V8_WALK.signature(tid))
        _V8_VER[tid] = pid
        _V8.save(force=True)
        print("[LEARN] V8 %s: track %s is %s | cues %s | %d comparisons with others | +%d face views%s"
              % (tag, tid, pid, ",".join(sorted(raw_same)) or "none yet", nd, nf, " +walk" if nw else ""),
              flush=True)
    except Exception as e:
        print("[LEARN] V8 error: %s" % e, flush=True)


def _v8_check(assignments, tids):
    """Every 2 s: re-check the names on everyone in view with all cues, keep
    learning from people the caregiver confirmed, and release a name the
    evidence clearly contradicts."""
    if _V8 is None or not tids:
        return assignments
    now = time.time()
    if now - _V8_ST["last_check"] < 2.0:
        return assignments
    _V8_ST["last_check"] = now
    vis = [(i, tids[i], pid) for i, (pid, _h) in enumerate(assignments)
           if pid is not None and i < len(tids) and tids[i] is not None and not _v7_hold(tids[i])]
    if not vis:
        return assignments
    named_all = [p for p in _named_ids() if p in _signatures]
    out = list(assignments)
    # 1) learning from caregiver-confirmed tracks, every 30 s each
    for _i, t, p in vis:
        if _V8_VER.get(t) == p and now - _V8_ST["learned"].get(t, 0) > 30:
            _V8_ST["learned"][t] = now
            for s_, x in _v8_raw(t, p).items():
                _V8.add_example(s_, x, True)
            for q in named_all:
                if q != p:
                    for s_, x in _v8_raw(t, q).items():
                        _V8.add_example(s_, x, False)
            _V8.learn_face(p, list(_V8_TF.get(t, ())))
            _V8.learn_walk(p, _V8_WALK.signature(t))
    _V8.save()
    if _SINGLE_MODE[0]:
        return out
    # 2) joint check of the names in view
    names = [p for _i, _t, p in vis]
    sc = {}
    for _i, t, _p in vis:
        for p in set(names):
            tot, ev = _V8.evidence(_v8_raw(t, p))
            sc[(t, p)] = (tot, ev)
    if len(vis) >= 2 and len(vis) <= 5:
        import itertools
        cur = sum(sc[(t, p)][0] for _i, t, p in vis)
        best, best_perm = cur, None
        for perm in itertools.permutations(names):
            tot = sum(sc[(t, perm[k])][0] for k, (_i, t, _p) in enumerate(vis))
            if tot > best + 1e-9:
                best, best_perm = tot, perm
        if best_perm is not None and best - cur >= V8_SWAP:
            key = tuple(best_perm)
            _V8_ST["votes"][key] = _V8_ST["votes"].get(key, 0) + 1
            if _V8_ST["votes"][key] >= 2:
                _V8_ST["votes"].clear()
                moved = []
                for k, (i, t, old) in enumerate(vis):
                    new = best_perm[k]
                    if new != old:
                        _track_to_pid[t] = new
                        out[i] = (new, out[i][1])
                        _V8_VER.pop(t, None)
                        moved.append("track %s: %s -> %s" % (t, old, new))
                print("[V8] SWAP %s (gain %.1f)" % (", ".join(moved), best - cur), flush=True)
                return out
        else:
            _V8_ST["votes"].clear()
    # 3) a name the evidence clearly contradicts is released for re-identification
    for i, t, p in vis:
        tot, ev = sc[(t, p)]
        verified = _V8_VER.get(t) == p
        against = tot <= V8_AGAINST and (not verified or ev.get("face", 0) <= -2.5)
        if against:
            _V8_ST["against"][t] = _V8_ST["against"].get(t, 0) + 1
        else:
            _V8_ST["against"].pop(t, None)
        if _V8_ST["against"].get(t, 0) >= 3:
            _V8_ST["against"].pop(t, None)
            _track_to_pid.pop(t, None)
            _V8_VER.pop(t, None)
            _trk_status[t] = "Not sure"
            _evidence.pop(t, None)
            out[i] = (None, out[i][1])
            print("[V8] track %s released from %s: evidence %+.1f (%s)" % (
                t, p, tot, " ".join("%s%+.1f" % (k[:4], v) for k, v in sorted(ev.items()))), flush=True)
    if len(_V8_ST["learned"]) > 300:
        _V8_ST["learned"].clear()
    return out


_LAST_SEEN = {}    # named person -> (time, centre x, centre y, box height) when last on screen


def _v8_relink(tid, idx, kp, boxes, used):
    """RELINK_V1: the tracker lost someone for a moment (sat down, walked
    behind someone) and they reappear as a new track in the same place. Keep
    their name instead of identifying them from scratch: every fresh decision
    is a chance to get it wrong."""
    b = boxes[idx] if idx < len(boxes) else None
    if b is None:
        return None, ""
    cx, cy, h = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, max(b[3] - b[1], 1.0)
    for j, o in enumerate(boxes):                 # born next to someone else: too risky
        if j != idx and o is not None:
            if abs((o[0] + o[2]) / 2 - cx) < 0.5 * h and abs((o[1] + o[3]) / 2 - cy) < 0.6 * h:
                return None, ""
    now = time.time()
    named = _named_ids()
    cands = []
    for pid, (t, x, y, hh) in list(_LAST_SEEN.items()):
        gap = now - t
        if pid in used or pid not in named or gap > 3.0 or gap < 0.2:
            continue
        if pid in _track_to_pid.values() and any(
                v == pid and k != tid and _V7["last_cross"].get(k, 0) > now - 1 for k, v in _track_to_pid.items()):
            continue
        d = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5 / max(h, hh)
        if d < 0.6 and 0.6 < h / max(hh, 1.0) < 1.6:
            cands.append((d, gap, pid))
    if not cands:
        return None, ""
    cands.sort()
    if len(cands) >= 2 and cands[1][0] < 2.5 * cands[0][0]:
        return None, ""
    d, gap, pid = cands[0]
    if _V8 is not None:
        tot, _ev = _V8.evidence(_v8_raw(tid, pid))
        if tot < -2.0:
            return None, ""
    return pid, "back after %.1f s, %.0f%% of a body height away" % (gap, 100 * d)


def _same_body(a, b):
    """Two skeletons drawn on one person (the detector doubled them)."""
    try:
        a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
        m = (a[:, 2] >= 0.3) & (b[:, 2] >= 0.3)
        if m.sum() < 6:
            return False
        ta = _kp_box(a)
        if ta is None:
            return False
        size = max(ta[2] - ta[0], ta[3] - ta[1], 1.0)
        d = np.linalg.norm(a[m, :2] - b[m, :2], axis=1)
        return float(np.median(d)) < 0.06 * size
    except Exception:
        return False


def _v8_finalize(assignments, tids, all_persons):
    """One name per person in view. Doubled skeletons are hidden; if two people
    still carry one name, the better-supported one keeps it and the other
    becomes "Not sure" for the caregiver to fix. Several "Not sure" people are
    numbered, the same way on the video and in the In view now list."""
    out = list(assignments)
    n = len(out)
    hidden = set()
    for i in range(n):
        for j in range(i + 1, n):
            if j not in hidden and i not in hidden and _same_body(all_persons[i], all_persons[j]):
                hidden.add(j)
                out[j] = (None, None)
    owner = {}
    for i, (pid, _h) in enumerate(out):
        if pid is None or i in hidden:
            continue
        if pid not in owner:
            owner[pid] = i
            continue
        j = owner[pid]
        ti = tids[i] if tids and i < len(tids) else None
        tj = tids[j] if tids and j < len(tids) else None

        def support(k, t):
            if _V8_VER.get(t) == pid:
                return 99.0
            if _V8 is not None and t is not None:
                return _V8.evidence(_v8_raw(t, pid))[0]
            return 0.0
        keep, lose = (i, j) if support(i, ti) > support(j, tj) else (j, i)
        tl = tids[lose] if tids and lose < len(tids) else None
        out[lose] = (None, out[lose][1])
        owner[pid] = keep
        if tl is not None:
            if _track_to_pid.get(tl) == pid:
                _track_to_pid.pop(tl, None)
            _trk_status[tl] = "Not sure"
            _evidence.pop(tl, None)
        print("[V8] %s was on two people; kept on track %s, track %s is now Not sure" % (
            pid, tids[keep] if tids and keep < len(tids) else "?", tl), flush=True)
    labels = []
    unsure = []
    for i, (pid, _h) in enumerate(out):
        t = tids[i] if tids and i < len(tids) else None
        if i in hidden:
            labels.append("__dup__")
            continue
        if pid is not None:
            labels.append(None)
            continue
        st = _trk_status.get(t) if t is not None else None
        labels.append(st)
        if st == "Not sure":
            unsure.append((i, t))
    if len(unsure) >= 2:
        taken = {_NS_NUM[t] for _i, t in unsure if t in _NS_NUM}
        for i, t in unsure:
            if t not in _NS_NUM or list(_NS_NUM[x] for _j, x in unsure).count(_NS_NUM[t]) > 1:
                k = 1
                while k in taken:
                    k += 1
                _NS_NUM[t] = k
                taken.add(k)
            labels[i] = "Not sure %d" % _NS_NUM[t]
    else:
        _NS_NUM.clear()
    by_tid = {}
    for i, (pid, _h) in enumerate(out):
        t = tids[i] if tids and i < len(tids) else None
        if t is not None:
            by_tid[t] = (pid, labels[i])
    _FRAME_LABELS["t"] = time.time()
    _FRAME_LABELS["by_tid"] = by_tid
    return out, labels


def reload_known_persons() -> None:
    """Load named persons from DB at startup so IDs persist across restarts."""
    import sqlite3
    # Single-resident configuration, refreshed on every startup.
    try:
        _c = sqlite3.connect(os.path.expanduser('~/passive-health-moniter/vigil.db'))
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
        conn = sqlite3.connect(os.path.expanduser('~/passive-health-moniter/vigil.db'))
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
                try:                        # STALE_DUPES_V1: when they were really last seen
                    from datetime import datetime as _dt
                    sig.last_seen = _dt.fromisoformat(str(last_seen_at).replace("Z", "")).timestamp()
                except Exception:
                    pass
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
            _FRAME_LABELS["t"] = time.time()
            _FRAME_LABELS["by_tid"] = {}
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
        try:
            _v7_mark_crossings(_boxes, track_ids)          # IDENTITY_V7
        except Exception:
            pass
        _fps = {}
        if _VR is not None and _current_frame is not None and track_ids:
            _fp_frame[0] += 1
            _want = []
            for _i, _k in enumerate(all_persons):
                _t = track_ids[_i] if _i < len(track_ids) else None
                if _t is None or _is_occluded(_i, _boxes):
                    continue
                if _t not in _track_to_pid or _fp_frame[0] % 3 == 0 or _t in _V7["pending"] \
                        or _t == _EN["track"]:                     # ENROLL_V1: every frame while learning them
                    _want.append((_t, _k))
            if _want:
                _fps = _fp_for(_want[:4], _current_frame)
        try:                                                     # IDENTITY_V8
            _v8_observe(all_persons, track_ids, _boxes, _current_frame)
        except Exception as _e8o:
            print("[V8] observe error: %s" % _e8o, flush=True)
        try:                                                     # ENROLL_V1
            _enroll_tick(all_persons, track_ids, _boxes, _fps)
        except Exception as _een:
            print("[ENROLL] error: %s" % _een, flush=True)
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
                    if not _TRACK_BAD.get(tid) and not _occl and not _v7_hold(tid):
                        _bs = _stand_height(kp, _current_frame.shape[0] if _current_frame is not None else None)
                        if _bs is not None:
                            _ground_add(_bs)
                            _body_add(pid, _bs)
                    if _VR is not None and tid in _fps and not _TRACK_BAD.get(tid) and not _v7_hold(tid):
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
            if tid not in _evidence and not _occl:              # RELINK_V1
                try:
                    _rl, _rw = _v8_relink(tid, _idx, kp, _boxes, _used_this_frame)
                except Exception:
                    _rl, _rw = None, ""
                if _rl is not None:
                    print("[V8] RELINK track %s -> %s | %s" % (tid, _rl, _rw), flush=True)
                    _track_to_pid[tid] = _rl
                    _used_this_frame.add(_rl)
                    if _rl in _signatures:
                        _signatures[_rl].update(height_px, shape_r, colour_r)
                    _trk_status.pop(tid, None)
                    frame_assignments.append((_rl, height_px))
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
            if matched not in (None, "?") and not str(_why).startswith("fingerprint strong"):
                try:                                              # LEARN_V2
                    _vt, _vw = _prop_veto(tid, matched, _named_ids())
                    if _vt:
                        print("[REID] track %s: held back %s | %s" % (tid, matched, _vw), flush=True)
                        matched, _why = "?", _why + " | " + _vw
                except Exception:
                    pass
            try:                                                 # IDENTITY_V8
                matched, _why = _v8_decide_new(tid, matched, _why, _used_this_frame)
            except Exception as _e8d:
                print("[V8] decide error: %s" % _e8d, flush=True)
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
                try:                                             # LEARN_V4: no duplicate of family
                    _fam = [p for p in _named_ids() if p in _signatures and p not in _used_this_frame]
                    _rk = _v8_rank(tid, _fam) if (_V8 is not None and _fam) else []
                except Exception:
                    _rk = []
                if _rk and _rk[0][0] > -1.0:
                    _trk_status[tid] = "Not sure"
                    ev["n"] = _REID_GIVE_UP - 10
                    if not ev.get("nn"):
                        ev["nn"] = 1
                        print("[REID] track %s -> NOT SURE yet | not a new person: %s" % (tid, _v8_txt(_rk)), flush=True)
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

        try:                                                     # IDENTITY_V7
            frame_assignments = _v7_verify(frame_assignments, track_ids)
        except Exception as _e7:
            print("[REID7] verify error: %s" % _e7, flush=True)
        try:                                                     # IDENTITY_V8
            frame_assignments = _v8_check(frame_assignments, track_ids)
        except Exception as _e8c:
            print("[V8] check error: %s" % _e8c, flush=True)
        try:
            frame_assignments, _labels8 = _v8_finalize(frame_assignments, track_ids, all_persons)
        except Exception as _e8f:
            print("[V8] finalize error: %s" % _e8f, flush=True)
            _labels8 = [(_trk_status.get(track_ids[_j]) if (track_ids and _j < len(track_ids)) else None)
                        for _j in range(len(frame_assignments))]

        try:                                                     # RELINK_V1
            _nw = time.time()
            for _j, (_p, _hh) in enumerate(frame_assignments):
                _bb = _boxes[_j] if _j < len(_boxes) else None
                if _p is not None and _bb is not None:
                    _LAST_SEEN[_p] = (_nw, (_bb[0] + _bb[2]) / 2, (_bb[1] + _bb[3]) / 2, max(_bb[3] - _bb[1], 1.0))
        except Exception:
            pass

        # all_persons[0] is the largest = the resident being measured for gait
        main_id = frame_assignments[0][0] if frame_assignments else None
        _write_in_view([a[0] for a in frame_assignments], _labels8,
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

        try:                                                     # PROPORTIONS_V1
            _prop_observe(all_persons, frame_assignments, track_ids, _named_ids())
        except Exception:
            pass
        try:                                                     # PERSON_ACTIVITY_V1
            _activity_observe(all_persons, frame_assignments, _named_ids())
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
