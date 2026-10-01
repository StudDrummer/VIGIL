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
        small = cv2.resize(image_bgr, (240, 240))
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
            'SELECT id, height_px, confidence, frames_seen FROM persons '
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
        for pid, height_px, conf, frames_seen in rows:
            if pid not in _signatures:
                sig = _PersonSignature(pid)
                count = min(50, max(5, int((frames_seen or 100) / 60)))
                for _ in range(count):
                    sig.height_samples.append(height_px)
                sig.frames_seen = frames_seen or 100
                sig.reported = True
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


def _cleanup():
    now = time.time()
    dead = [pid for pid, s in _signatures.items() if now - s.last_seen > 120]
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
            return None

        _cleanup()
        frame_assignments = []  # (person_id, height_px) for this frame, in all_persons order

        global _track_to_pid
        # Two detections in the same frame cannot be the same person.
        _used_this_frame = set()
        for _idx, kp in enumerate(all_persons):
            height_px = _estimate_height(kp)
            shape_r = _estimate_shape(kp)
            colour_r = _estimate_colour(kp, _current_frame)
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
                    and _track_to_pid[tid] not in _used_this_frame:
                pid = _track_to_pid[tid]
                if pid in _signatures and height_px is not None:
                    _signatures[pid].update(height_px, shape_r, colour_r)
                _used_this_frame.add(pid)
                frame_assignments.append((pid, height_px))
                continue

            if height_px is None:
                frame_assignments.append((None, None))
                continue

            # SECONDARY: height match, only for a track we have not seen
            # before -- re-entry after occlusion, or after a restart.
            matched = None
            best_diff = float('inf')
            for pid, sig in _signatures.items():
                if sig.height_est is None:
                    continue
                if pid in _used_this_frame:
                    continue   # claimed by another person in this frame
                diff = abs(height_px - sig.height_est) / sig.height_est
                if diff < best_diff and sig.matches(height_px, shape_r, colour_r):
                    best_diff = diff
                    matched = pid

            if matched:
                _signatures[matched].update(height_px, shape_r, colour_r)
                if tid is not None:
                    _track_to_pid[tid] = matched
                _used_this_frame.add(matched)
                frame_assignments.append((matched, height_px))
            else:
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
                _used_this_frame.add(new_id)
                if tid is not None:
                    _track_to_pid[tid] = new_id
                    if len(_track_to_pid) > 500:
                        for _k in list(_track_to_pid)[:250]:
                            _track_to_pid.pop(_k, None)
                frame_assignments.append((new_id, height_px))

        # all_persons[0] is the largest = the resident being measured for gait
        main_id = frame_assignments[0][0] if frame_assignments else None
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
                _live.append({
                    "person_id": _rpid,
                    "frames_seen": int(_rsg.frames_seen),
                    "height_px": round(_he, 1) if _he else None,
                    "confidence": round(float(_rsg.confidence), 3),
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
                        _post_person_photo(pid, frame)
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
