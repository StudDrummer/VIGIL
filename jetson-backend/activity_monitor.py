# VIGIL_PERSON_DEDUP
def _iou(box_a, box_b):
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0

def dedup_persons(persons_with_boxes, iou_thresh=0.5):
    """persons_with_boxes: list of (keypoints, box). Removes overlapping duplicates,
    keeping the larger-area detection (more likely the real, unobstructed one)."""
    if len(persons_with_boxes) <= 1:
        return persons_with_boxes
    persons_with_boxes = sorted(
        persons_with_boxes,
        key=lambda p: (p[1][2]-p[1][0]) * (p[1][3]-p[1][1]),
        reverse=True
    )
    kept = []
    for kp, box in persons_with_boxes:
        if all(_iou(box, kb) < iou_thresh for _, kb in kept):
            kept.append((kp, box))
    return kept

_recent_counts = []
def smoothed_person_count(raw_count, window=5):
    """Requires the count to be stable across the last `window` frames
    before trusting it — kills single-frame corruption spikes."""
    _recent_counts.append(raw_count)
    if len(_recent_counts) > window:
        _recent_counts.pop(0)
    if len(_recent_counts) < window:
        return min(_recent_counts) if _recent_counts else 0
    from collections import Counter
    return Counter(_recent_counts).most_common(1)[0][0]

"""
VIGIL Camera Pipeline v3 - Camera-First MVP
Works with RTSP camera or USB webcam (--source 0 for testing)
"""
import argparse, os, sys, time
from collections import deque
from datetime import datetime
import base64
import cv2, numpy as np, requests

try:
    from ultralytics import YOLO
except ImportError:
    print("ERROR: pip3 install ultralytics --break-system-packages")
    sys.exit(1)
# VIGIL_PERSON_HOOK
try:
    from person_hook import vigil_track_persons, reload_known_persons
    reload_known_persons()  # VIGIL_PERSON_STARTUP — load named persons immediately
except Exception as _ph_err:
    print(f"[WARN] person_hook import failed: {_ph_err}")
    def vigil_track_persons(*a, **k): return None
    def reload_known_persons(): pass


try:
    from person_tracker import PersonTracker
    PERSON_TRACKING = True
except ImportError:
    PERSON_TRACKING = False
    print("[WARN] person_tracker.py not found — single-person mode")

DEFAULT_SERVER     = "http://localhost:5001"
MODEL_PATH         = "yolov8n-pose.pt"
CONF_THRESHOLD     = 0.25
TARGET_FPS         = 12
HEARTBEAT_SEC      = 30
CAMERA_SYNC_SEC    = 20
STILLNESS_ALERT_MIN= 90
ACTIVE_HOURS_START = 7
ACTIVE_HOURS_END   = 22
STATE_DEBOUNCE_SEC = 8
ABSENT_TIMEOUT_SEC = 15
FALL_CONFIRM_SEC   = 2.5
FALL_COOLDOWN_SEC  = 30

KP_NOSE=0; KP_LS=5; KP_RS=6; KP_LE=7; KP_RE=8
KP_LW=9; KP_RW=10; KP_LH=11; KP_RH=12; KP_LA=15; KP_RA=16

class ServerClient:
    def __init__(self, base_url):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            'X-VIGIL-Key': os.environ.get('VIGIL_API_KEY', ''),
            'Content-Type': 'application/json',
        })
        self.backoff = 1
        self.resident_height_cm = 170.0
        self.step_length_ratio = 0.41   # calibrated per resident
        self.resident_name = ""
        self.resident_pid = None   # HOME_RESIDENT_V1
        self._last_config = 0
        self.room_id = 'room_1'

    def _post(self, path, payload, timeout=5):
        try:
            r = self.session.post(f"{self.base}{path}", json=payload, timeout=timeout)
            if r.status_code == 200:
                self.backoff = 1; return True
        except Exception as e:
            print(f"[SERVER] {path} failed: {e}")
        self.backoff = min(self.backoff * 2, 60)
        return False

    def sync_camera(self, payload): self._post("/sync/camera", payload)

    def activity_event(self, activity, started_at, ended_at=None, duration_s=None):
        self._post("/activity/event", {
            "activity": activity,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_s": duration_s,
            "room_id": self.room_id,
        })

    def heartbeat(self, person_present: bool, fps: float,
                  camera_mode: str, person_count: int = 0):
        self._post("/camera/heartbeat", {
            "person_present": person_present,
            "person_count": person_count,
            "fps": round(fps, 1),
            "camera_mode": camera_mode,
            "room_id": self.room_id,
        })

    def alert_fall(self, hip_drop_pct: float, torso_angle: float):
        self._post("/alert/fall", {
            "detected_at": datetime.now().isoformat(),
            "hip_drop_pct": hip_drop_pct,
            "torso_angle": torso_angle,
            "message": "Fall detected by camera",
            "resident_name": self.resident_name,
            "room_id": self.room_id,
        })

    def alert_stillness(self, duration_s):
        self._post("/alert/stillness", {"duration_s": duration_s})

    def post_snapshot(self, frame):
        """Post a compressed JPEG snapshot every 2 seconds for live view."""
        try:
            import cv2
            _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 40])
            b64 = base64.b64encode(buf).decode('utf-8')
            self._post("/camera/snapshot/post", {"frame": b64}, timeout=3)
        except Exception:
            pass

    def refresh_config(self):
        now = time.time()
        if now - self._last_config < 300: return
        self._last_config = now
        try:
            r = self.session.get(f"{self.base}/config", timeout=5)
            if r.status_code == 200:
                cfg = r.json()
                self.resident_height_cm = float(cfg.get("resident_height_cm") or 170)
                self.step_length_ratio = float(cfg.get("step_length_ratio") or 0.41)
                self.resident_name = cfg.get("resident_name") or ""
                self.resident_pid = cfg.get("resident_person_id") or None
        except Exception as e:
            print(f"[SERVER] config fetch failed: {e}")

class Calibrator:
    def __init__(self):
        self.max_body_px = deque(maxlen=300)


    def _cadence_from_count(self, window_s=10.0, min_strikes=4):
        """Steps per minute from strike count over a wall-clock window.

        Independent of frame rate. Returns None when there is not enough
        recent evidence, so a stale value is never reported as current.
        """
        import time as _t
        now = _t.time()
        recent = [x for x in self._strike_times if now - x <= window_s]
        if len(recent) < min_strikes:
            return None
        span = recent[-1] - recent[0]
        if span < 1.0:
            return None
        # len(recent)-1 intervals across span seconds
        spm = (len(recent) - 1) / span * 60.0
        if spm < 40.0 or spm > 180.0:
            return None
        return round(spm, 1)

    def update(self, nose_y, ankle_y, ok):
        if ok and ankle_y > nose_y:
            self.max_body_px.append(ankle_y - nose_y)

    def px_to_m(self, height_cm):
        if not self.max_body_px: return 0.005
        body_px = float(np.percentile(list(self.max_body_px), 90))
        return (height_cm / 100.0) / body_px if body_px > 0 else 0.005

class GaitTracker:
    def __init__(self, fps):
        self.fps = fps
        self._lc, self._rc = [], []
        self._strike_times = []   # wall-clock times of accepted strikes
        self._l_times = []   # wall-clock times, left strikes
        self._r_times = []   # wall-clock times, right strikes
        self._l_last_strike_t = 0.0
        self._r_last_strike_t = 0.0
        self._l_ratio_hist = []
        self._r_ratio_hist = []
        self._l_last_strike = -9999
        self._r_last_strike = -9999
        self._jitter_rejected = 0
        self._l_last_strike = -9999   # frame idx of last accepted L strike
        self._r_last_strike = -9999   # frame idx of last accepted R strike
        self._jitter_rejected = 0     # diagnostic counter
        self._lin, self._rin = False, False
        self._strides = deque(maxlen=20)
        self._lw = deque(maxlen=int(4*fps))
        self._rw = deque(maxlen=int(4*fps))
        self._widths = deque(maxlen=30)
        self._cadence_hist = deque(maxlen=int(30*fps))
        self._stride_count = 0
        self.cadence_spm = 0.0
        self.asymmetry_pct = 0.0
        self.stride_variability = 0.0
        self.arm_swing_asymmetry = 0.0
        self.step_width_px = 0.0
        self.cadence_variability = 0.0

    def _get(self, kp, idx):
        if kp[idx, 2] >= CONF_THRESHOLD:
            return kp[idx, 0], kp[idx, 1]
        return None

    def update(self, kp, frame_idx, frame_h):
        la = self._get(kp, KP_LA); ra = self._get(kp, KP_RA)
        lh = self._get(kp, KP_LH); rh = self._get(kp, KP_RH)
        lw = self._get(kp, KP_LW); rw = self._get(kp, KP_RW)
        le = self._get(kp, KP_LE); re = self._get(kp, KP_RE)

        # Tracks whether a heel-strike (new stride) was actually observed this
        # call. cadence_spm is recomputed below whenever enough stride history
        # exists, but _cadence_last_updated must only advance on a genuine new
        # stride — otherwise a person standing still (still detected every
        # frame, but not walking) makes an old cadence look perpetually fresh.
        new_stride = False

        if la and lh:
            _s = self._get(kp, KP_LS)
            _torso = abs(lh[1]-_s[1]) if _s else 0
            if _torso > 20:
                _ratio = (la[1]-lh[1]) / _torso
                self._l_ratio_hist.append(_ratio)
                if len(self._l_ratio_hist) > 15:
                    self._l_ratio_hist = self._l_ratio_hist[-15:]
                # Heel strike = local maximum of leg extension. Prominence
                # threshold adapts to this person's observed range, because
                # the absolute ratio shifts with camera distance and angle.
                if len(self._l_ratio_hist) >= 5:
                    _h = self._l_ratio_hist
                    _a, _b, _c = _h[-3], _h[-2], _h[-1]
                    _rng = max(_h) - min(_h)
                    _prom = max(0.015, _rng * 0.25)
                    _is_peak = (_b > _a) and (_b > _c) and (_b - min(_a, _c)) >= _prom
                    _now_t = __import__('time').time()
                    # Physiological refractory: one foot cannot strike
                    # twice within 0.70s. Wall-clock, so frame-rate
                    # assumptions cannot corrupt it.
                    if _is_peak and (_now_t - self._l_last_strike_t) >= 0.70:
                        self._l_last_strike_t = _now_t
                        self._l_last_strike = frame_idx
                        self._lc.append(frame_idx)
                        self._strike_times.append(__import__('time').time())
                        if len(self._strike_times) > 60:
                            self._strike_times = self._strike_times[-60:]
                        self._lin = True
                        self._l_times.append(_now_t)
                        if len(self._l_times) > 30:
                            self._l_times = self._l_times[-30:]
                        if len(self._l_times) >= 2:
                            # Real elapsed seconds. No fps term.
                            t = self._l_times[-1] - self._l_times[-2]
                            if 0.4 <= t <= 2.5:
                                self._strides.append(t); self._stride_count += 1
                                new_stride = True
                        if len(self._lc) > 20:
                            self._lc = self._lc[-10:]
                        if frame_idx % 3 == 0:
                            _dbg_print(f"[STRIKE_L] ratio={_b:.3f} prom={_prom:.3f} "
                                  f"range={_rng:.3f} strides={len(self._strides)}")
                    elif _is_peak:
                        self._jitter_rejected += 1

        if ra and rh:
            _s = self._get(kp, KP_RS)
            _torso = abs(rh[1]-_s[1]) if _s else 0
            if _torso > 20:
                _ratio = (ra[1]-rh[1]) / _torso
                self._r_ratio_hist.append(_ratio)
                if len(self._r_ratio_hist) > 15:
                    self._r_ratio_hist = self._r_ratio_hist[-15:]
                # Heel strike = local maximum of leg extension. Prominence
                # threshold adapts to this person's observed range, because
                # the absolute ratio shifts with camera distance and angle.
                if len(self._r_ratio_hist) >= 5:
                    _h = self._r_ratio_hist
                    _a, _b, _c = _h[-3], _h[-2], _h[-1]
                    _rng = max(_h) - min(_h)
                    _prom = max(0.015, _rng * 0.25)
                    _is_peak = (_b > _a) and (_b > _c) and (_b - min(_a, _c)) >= _prom
                    _now_t = __import__('time').time()
                    # Physiological refractory: one foot cannot strike
                    # twice within 0.70s. Wall-clock, so frame-rate
                    # assumptions cannot corrupt it.
                    if _is_peak and (_now_t - self._r_last_strike_t) >= 0.70:
                        self._r_last_strike_t = _now_t
                        self._r_last_strike = frame_idx
                        self._rc.append(frame_idx)
                        self._strike_times.append(__import__('time').time())
                        if len(self._strike_times) > 60:
                            self._strike_times = self._strike_times[-60:]
                        self._rin = True
                        self._r_times.append(_now_t)
                        if len(self._r_times) > 30:
                            self._r_times = self._r_times[-30:]
                        if len(self._r_times) >= 2:
                            # Real elapsed seconds. No fps term.
                            t = self._r_times[-1] - self._r_times[-2]
                            if 0.4 <= t <= 2.5:
                                self._strides.append(t); self._stride_count += 1
                                new_stride = True
                        if len(self._rc) > 20:
                            self._rc = self._rc[-10:]
                        if frame_idx % 3 == 0:
                            _dbg_print(f"[STRIKE_R] ratio={_b:.3f} prom={_prom:.3f} "
                                  f"range={_rng:.3f} strides={len(self._strides)}")
                    elif _is_peak:
                        self._jitter_rejected += 1

        if len(self._strides) >= 6:
            arr = np.array(self._strides)
            m = np.mean(arr)
            self.stride_variability = float(np.std(arr, ddof=1)/m*100) if m > 0 else 0.0
            # Prefer counting strikes against wall-clock time; fall back to
            # the interval formula when the count window lacks data.
            _cc = self._cadence_from_count() if hasattr(self, '_cadence_from_count') else None
            if _cc is not None:
                self.cadence_spm = _cc
            else:
                _iv = 120.0/m if m > 0 else 0.0
                self.cadence_spm = round(_iv, 1) if 40.0 <= _iv <= 180.0 else None
            if new_stride:
                self._cadence_last_updated = __import__('time').time()
                self._hip_peaks = []  # primary method succeeded — clear stale fallback state
                if self.cadence_spm is not None:
                    self._cadence_hist.append(self.cadence_spm)

        if len(self._cadence_hist) >= 30:
            arr = np.array(self._cadence_hist)
            m = np.mean(arr)
            self.cadence_variability = float(np.std(arr, ddof=1)/m*100) if m > 0 else 0.0

        if len(self._lc) >= 3 and len(self._rc) >= 3:
            lr = np.diff(self._lc[-4:])/self.fps
            rr = np.diff(self._rc[-4:])/self.fps
            if len(lr) and len(rr):
                lm, rm = np.mean(lr), np.mean(rr)
                total = lm+rm
                self.asymmetry_pct = float(abs(lm-rm)/total*100) if total > 0 else 0.0

        if lw and le: self._lw.append(lw[1]-le[1])
        if rw and re: self._rw.append(rw[1]-re[1])
        if len(self._lw) >= 20 and len(self._rw) >= 20:
            la_a = float(np.ptp(self._lw)); ra_a = float(np.ptp(self._rw))
            total = la_a+ra_a
            self.arm_swing_asymmetry = float(abs(la_a-ra_a)/total*100) if total > 0 else 0.0

        if la and ra and frame_h > 0:
            lat = abs(la[0]-ra[0])
            norm = (lat/frame_h)*100
            if 1.0 <= norm <= 40.0:
                self._widths.append(norm)
                if len(self._widths) >= 5:
                    self.step_width_px = float(np.median(self._widths))

        # Hip oscillation fallback — estimates cadence from lateral hip sway
        # when heel contact detection fails (low ankle confidence)
        if lh is not None and rh is not None:
            hip_mid_x = (lh[0] + rh[0]) / 2
            if not hasattr(self, '_hip_x_hist'):
                self._hip_x_hist = deque(maxlen=int(8 * self.fps))
                self._hip_peaks = []
                self._last_peak_dir = 0
            self._hip_x_hist.append(hip_mid_x)
            if len(self._hip_x_hist) >= int(2 * self.fps):
                arr = list(self._hip_x_hist)
                mid = len(arr) // 2
                left_avg = sum(arr[:mid]) / mid
                right_avg = sum(arr[mid:]) / (len(arr) - mid)
                # Detect direction change = one step
                current_dir = 1 if arr[-1] > arr[-2] else -1
                if hasattr(self, '_last_peak_dir') and current_dir != self._last_peak_dir:
                    self._last_peak_dir = current_dir
                    now_frame = frame_idx
                    self._hip_peaks.append(now_frame)
                    if len(self._hip_peaks) >= 3:
                        # Two direction changes = one full stride
                        intervals = [
                            (self._hip_peaks[i] - self._hip_peaks[i-2]) / self.fps
                            for i in range(2, len(self._hip_peaks))
                            if 0.8 <= (self._hip_peaks[i] - self._hip_peaks[i-2]) / self.fps <= 2.0
                        ]
                        # Previously only fired once (while cadence_spm==0.0) and never
                        # touched _cadence_last_updated, so a fallback-derived cadence
                        # could never pass the payload's freshness check and was always
                        # reported as None despite gait.cadence_spm holding a value.
                        if intervals:
                            avg_stride = sum(intervals) / len(intervals)
                            _c = 120.0 / avg_stride if avg_stride > 0 else 0.0
                            # Reject physiologically impossible cadence rather
                            # than writing garbage. Adult walking is 90-120 spm.
                            self.cadence_spm = round(_c, 1) if 40.0 <= _c <= 180.0 else None
                            self._cadence_last_updated = __import__('time').time()
                        if len(self._hip_peaks) > 20:
                            self._hip_peaks = self._hip_peaks[-10:]

class BedExitDetector:
    """BED_EXIT_V1 -- getting up from a bed or sofa zone at night."""

    NIGHT_START = 22
    NIGHT_END = 6
    REST_MIN_S = 60.0       # lying in a zone at least this long first
    LYING_ASPECT = 0.90     # skeleton box wider than tall
    UPRIGHT_ASPECT = 0.65   # clearly standing
    UPRIGHT_HOLD_S = 3.0    # standing must persist, not flicker
    COOLDOWN_S = 600.0
    TEST_FLAG = "/dev/shm/vigil_bedexit_test"

    def __init__(self, fall):
        self.fall = fall
        self.rest_since = None
        self.rested_s = 0.0
        self.upright_since = None
        self.last_alert = 0.0

    def _active_window(self):
        if os.path.exists(self.TEST_FLAG):
            return True
        h = time.localtime().tm_hour
        return h >= self.NIGHT_START or h < self.NIGHT_END

    def _reset(self):
        self.rest_since = None
        self.rested_s = 0.0
        self.upright_since = None
        self._situp_since = None
        self._situp_sent = False

    def update(self, kp, frame_shape, client):
        if not getattr(self.fall, "_zones", None) or not self._active_window():
            self._reset()
            return
        try:
            pts = kp[kp[:, 2] >= CONF_THRESHOLD]
        except Exception:
            return
        if len(pts) < 5:
            return                      # brief loss of pose keeps the state
        w = float(pts[:, 0].max() - pts[:, 0].min())
        h = float(pts[:, 1].max() - pts[:, 1].min())
        if h < 10.0:
            return
        aspect = w / h
        now = time.time()
        if now - getattr(self, "_dbg_t", 0.0) > 2.0:
            self._dbg_t = now
            _dbg_print("[BED_DBG] a=%.2f zone=%s rest=%.0f upright_for=%s h=%.0f pts=%d" % (
                aspect, self.fall._in_zone(pts, frame_shape), self.rested_s,
                ("%.1f" % (now - self.upright_since)) if self.upright_since else "-",
                h, len(pts)))

        # BED_SITUP_V1: after a real rest, sitting upright on the bed for 3 s
        # is the moment before standing. Warn once per rest.
        _ang = self.fall._torso_deg(kp)
        if (_ang is not None and _ang < 35.0 and self.rest_since is not None
                and self.rested_s >= self.REST_MIN_S
                and self.fall._in_zone(pts, frame_shape)):
            if getattr(self, '_situp_since', None) is None:
                self._situp_since = now
            elif now - self._situp_since >= 3.0 and not getattr(self, '_situp_sent', False):
                self._situp_sent = True
                try:
                    client._post('/alert/bed_sitting', {'rested_s': round(self.rested_s, 1)})
                except Exception:
                    pass
                print('[BED_SITUP] sitting up after resting %.0f s' % self.rested_s, flush=True)
            return
        self._situp_since = None
        if aspect >= self.LYING_ASPECT and self.fall._in_zone(pts, frame_shape):
            if self.rest_since is None:
                self.rest_since = now
            self.rested_s = now - self.rest_since
            self.upright_since = None
            return

        if aspect <= self.UPRIGHT_ASPECT:
            if self.rest_since is None or self.rested_s < self.REST_MIN_S:
                self._reset()           # upright without a real rest first
                return
            if self.upright_since is None:
                self.upright_since = now
            if now - self.upright_since < self.UPRIGHT_HOLD_S:
                return
            if now - self.last_alert > self.COOLDOWN_S:
                self.last_alert = now
                try:
                    client._post("/alert/bed_exit", {"rested_s": round(self.rested_s, 1)})
                except Exception:
                    pass
                print("[BED_EXIT] up after resting %.0f s" % self.rested_s, flush=True)
            self._reset()
        # sitting on the edge (between the two thresholds) keeps the state


_SK_EDGES = [(5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12), (11, 12),
             (11, 13), (13, 15), (12, 14), (14, 16), (0, 5), (0, 6)]


def _write_skeleton_frame(results, shape):
    """PRIVACY_VIEW_V1: stick figures on a dark canvas, no room, no faces."""
    try:
        h, w = shape[:2]
        canvas = np.full((h, w, 3), 24, np.uint8)
        if len(results) > 0 and getattr(results[0], "keypoints", None) is not None:
            data = results[0].keypoints.data
            data = data.cpu().numpy() if hasattr(data, "cpu") else np.asarray(data)
            for p in data:
                for a, b in _SK_EDGES:
                    if p[a][2] >= 0.3 and p[b][2] >= 0.3:
                        cv2.line(canvas, (int(p[a][0]), int(p[a][1])),
                                 (int(p[b][0]), int(p[b][1])), (120, 220, 90), 5, cv2.LINE_AA)
                if p[0][2] >= 0.3:
                    cv2.circle(canvas, (int(p[0][0]), int(p[0][1])), 18, (120, 220, 90), 4, cv2.LINE_AA)
                for x, y, c in p[5:]:
                    if c >= 0.3:
                        cv2.circle(canvas, (int(x), int(y)), 5, (255, 255, 255), -1, cv2.LINE_AA)
        cv2.imwrite("/dev/shm/.vigil_skel.tmp.jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 70])
        os.replace("/dev/shm/.vigil_skel.tmp.jpg", "/dev/shm/vigil_skeleton.jpg")
    except Exception:
        pass


# FALL_REPLAY_V1: last 30 s of body points (no image), 5 per second.
_REPLAY = deque(maxlen=150)
_REPLAY_LAST = [0.0]


def _replay_push(kp, shape):
    try:
        now = time.time()
        if now - _REPLAY_LAST[0] < 0.2:
            return
        _REPLAY_LAST[0] = now
        h, w = float(shape[0]), float(shape[1])
        _REPLAY.append([round(now, 2),
                        [[round(float(p[0]) / w, 3), round(float(p[1]) / h, 3)] for p in kp],
                        [round(float(p[2]), 2) for p in kp]])
    except Exception:
        pass


_NAME_CACHE = [0.0, {}]


def _vigil_names():
    """NAME_OVERLAY_V1: id -> name for named people, refreshed every 10 s."""
    now = time.time()
    if now - _NAME_CACHE[0] > 10:
        _NAME_CACHE[0] = now
        try:
            import sqlite3 as _sq
            _c = _sq.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vigil.db"), timeout=2)
            _NAME_CACHE[1] = {r[0]: r[1] for r in _c.execute(
                "SELECT id, name FROM persons WHERE name IS NOT NULL AND name != ''")}
            _c.close()
        except Exception:
            pass
    return _NAME_CACHE[1]


def _vigil_annotate(results, frame):
    """Skeleton and box, labelled with the person's name instead of
    'id:7 person 0.87'."""
    try:
        if len(results) == 0:
            return frame
        r = results[0]
        img = r.plot(labels=False, conf=False)
        boxes = getattr(r, "boxes", None)
        if boxes is None or getattr(boxes, "xyxy", None) is None:
            return img
        xyxy = boxes.xyxy.cpu().numpy() if hasattr(boxes.xyxy, "cpu") else np.asarray(boxes.xyxy)
        ids = None
        if getattr(boxes, "id", None) is not None:
            _i = boxes.id.cpu().numpy() if hasattr(boxes.id, "cpu") else np.asarray(boxes.id)
            ids = [int(v) for v in _i]
        try:
            import person_hook as _ph
            t2p = dict(getattr(_ph, "_track_to_pid", {}) or {})
            st = dict(getattr(_ph, "_trk_status", {}) or {})
        except Exception:
            t2p = {}
            st = {}
        names = _vigil_names()
        for i, b in enumerate(xyxy):
            tid = ids[i] if ids is not None and i < len(ids) else None
            pid = t2p.get(tid) if tid is not None else None
            name = names.get(pid) if pid else None
            label = name or ((st.get(tid) or "Identifying...") if pid is None else "New person")
            known = bool(name)
            x1, y1 = int(b[0]), int(b[1])
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.9, 2)
            y0 = max(0, y1 - th - 16)
            cv2.rectangle(img, (x1, y0), (x1 + tw + 18, y0 + th + 14),
                          (75, 215, 50) if known else (70, 70, 70), -1)
            cv2.putText(img, label, (x1 + 9, y0 + th + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                        (10, 20, 4) if known else (255, 255, 255), 2, cv2.LINE_AA)
        return img
    except Exception:
        try:
            return results[0].plot()
        except Exception:
            return frame


class FallDetector:
    """FALL_DETECTOR_V4 (v4.2) -- torso goes horizontal fast and stays down."""

    UP_ASPECT = 1.00            # compact: box taller than wide ...
    UP_TORSO_DEG = 40.0         # ... and torso near vertical (stand, kneel, squat)
    DOWN_TORSO_DEG = 55.0       # torso within 35 deg of horizontal
    DOWN_ASPECT_WITH_TORSO = 1.00
    DOWN_ASPECT_NO_TORSO = 1.80 # fallback when shoulders/hips are not visible
    MAX_DESCENT_S = 2.5         # measured: chair slumps 1.78 s and 2.07 s
    CANCEL_COMPACT_S = 1.0      # must look upright this long to cancel a fall
    MIN_PTS_COMPACT = 8         # 'upright' needs enough of the body visible
    HOLD_S = 3.0                # and stays down this long
    MIN_PTS = 5

    def __init__(self):
        self.state = "upright"
        self.last_up_t = 0.0
        self.down_since = None
        self.compact_since = None
        self.last_alert = 0.0
        self.hip_drop_pct = 0.0
        self.torso_angle = 0.0
        self.aspect = 0.0
        self._zones = self._load_zones()

    @staticmethod
    def _load_zones():
        try:
            import json
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fall_zones.json")
            with open(path) as f:
                data = json.load(f)
            zones = [(float(z["x1"]), float(z["y1"]),
                      float(z["x2"]), float(z["y2"])) for z in data]
            print("[FALL] %d furniture zone(s) loaded" % len(zones))
            return zones
        except Exception:
            return []

    def _in_zone(self, pts, frame_shape):
        # ZONES_HOT_RELOAD_V1: pick up zones saved from the app within 5 s.
        try:
            _now = time.time()
            if _now - getattr(self, "_zchk", 0.0) > 5:
                self._zchk = _now
                _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fall_zones.json")
                _m = os.path.getmtime(_p) if os.path.exists(_p) else 0
                if _m != getattr(self, "_zmtime", None):
                    if getattr(self, "_zmtime", None) is not None:
                        self._zones = self._load_zones()
                    self._zmtime = _m
        except Exception:
            pass
        if not self._zones or frame_shape is None:
            return False
        try:
            fh, fw = float(frame_shape[0]), float(frame_shape[1])
            cx = float(pts[:, 0].mean()) / fw
            cy = float(pts[:, 1].mean()) / fh
            for (x1, y1, x2, y2) in self._zones:
                if x1 <= cx <= x2 and y1 <= cy <= y2:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _torso_deg(kp):
        """Angle of the shoulder-to-hip line from vertical. 0 = upright,
        90 = lying flat. None when shoulders or hips are not visible."""
        try:
            ls, rs, lh, rh = kp[KP_LS], kp[KP_RS], kp[KP_LH], kp[KP_RH]
            if min(float(ls[2]), float(rs[2]), float(lh[2]), float(rh[2])) < 0.3:
                return None
            dx = (float(ls[0]) + float(rs[0])) / 2 - (float(lh[0]) + float(rh[0])) / 2
            dy = (float(ls[1]) + float(rs[1])) / 2 - (float(lh[1]) + float(rh[1])) / 2
            return float(np.degrees(np.arctan2(abs(dx), abs(dy) + 1e-6)))
        except Exception:
            return None

    @staticmethod
    def _legs_standing(kp, frame_shape):
        """FALL_V43: thigh hanging below the hip and shin continuing down =
        the person is on their feet (bending over), not lying on the floor."""
        try:
            fh = float(frame_shape[0]) if frame_shape is not None else None
            def pt(i, c=0.25):
                return (float(kp[i][0]), float(kp[i][1])) if float(kp[i][2]) >= c else None
            ls, rs, lh, rh = pt(5), pt(6), pt(11), pt(12)
            sh = [p for p in (ls, rs) if p]
            hp = [p for p in (lh, rh) if p]
            if not sh or not hp:
                return False
            sx, sy = sum(p[0] for p in sh) / len(sh), sum(p[1] for p in sh) / len(sh)
            hx, hy = sum(p[0] for p in hp) / len(hp), sum(p[1] for p in hp) / len(hp)
            torso = ((sx - hx) ** 2 + (sy - hy) ** 2) ** 0.5
            if torso < 10:
                return False
            for k_i, a_i, h_p in ((13, 15, lh), (14, 16, rh)):
                k = pt(k_i)
                hpt = h_p or (hx, hy)
                if k is None:
                    continue
                dy, dx = k[1] - hpt[1], abs(k[0] - hpt[0])
                if dy < 0.5 * torso or dx > dy:          # thigh not hanging down
                    continue
                a_any = (float(kp[a_i][0]), float(kp[a_i][1])) if float(kp[a_i][2]) >= 0.1 else None
                if fh and a_any and a_any[1] >= 0.98 * fh:
                    return True                          # shin runs off the bottom
                a = pt(a_i)
                if a and (a[1] - k[1]) >= 0.4 * torso and abs(a[0] - k[0]) <= (a[1] - k[1]):
                    return True                          # shin hangs below the knee
            return False
        except Exception:
            return False

    def update(self, kp, frame_shape=None):
        now = time.time()
        try:
            pts = kp[kp[:, 2] >= CONF_THRESHOLD]
        except Exception:
            return False
        if len(pts) < self.MIN_PTS:
            return False
        w = float(pts[:, 0].max() - pts[:, 0].min())
        h = float(pts[:, 1].max() - pts[:, 1].min())
        if h < 10.0:
            return False
        aspect = w / h
        ang = self._torso_deg(kp)
        if ang is not None and frame_shape is not None:
            try:
                _fh, _fw = float(frame_shape[0]), float(frame_shape[1])
                for _i in (5, 6, 11, 12):
                    _x, _y = float(kp[_i][0]), float(kp[_i][1])
                    if _x <= 0.01 * _fw or _x >= 0.99 * _fw or _y <= 0.01 * _fh or _y >= 0.99 * _fh:
                        ang = None          # FALL_V43: clamped at the edge, not a real position
                        break
            except Exception:
                pass
        self.aspect = aspect
        if ang is not None:
            self.torso_angle = ang
        self.hip_drop_pct = float(min(1.0, aspect / 2.0))

        compact = (len(pts) >= self.MIN_PTS_COMPACT and aspect < self.UP_ASPECT
                   and (ang is None or ang < self.UP_TORSO_DEG))
        if ang is not None:
            down = ang >= self.DOWN_TORSO_DEG and aspect >= self.DOWN_ASPECT_WITH_TORSO
        else:
            down = aspect >= self.DOWN_ASPECT_NO_TORSO
        if down and self._legs_standing(kp, frame_shape):
            down = False            # FALL_V43: bending over, still on their feet
            self.last_up_t = now    # a fall is timed from the last moment on their feet
        in_zone = self._in_zone(pts, frame_shape)

        if compact:
            self.last_up_t = now
            if self.state == "falling":
                # One upright-looking frame while down is usually lost body
                # points, not recovery. Cancel only if it holds for a second.
                if self.compact_since is None:
                    self.compact_since = now
                if now - self.compact_since < self.CANCEL_COMPACT_S:
                    return False
            if self.state != "upright":
                self.state = "upright"
                self.down_since = None
            self.compact_since = None
            return False
        self.compact_since = None

        if self.state == "upright":
            if down:
                dt = now - self.last_up_t
                if in_zone:
                    self.state = "resting"
                elif dt <= self.MAX_DESCENT_S:
                    self.state = "falling"
                    self.down_since = now
                else:
                    self.state = "resting"      # lay down on purpose
                _dbg_print("[FALL_DBG] down a=%.2f torso=%s descent=%.2fs zone=%s -> %s" % (
                    aspect, ("%.0f" % ang) if ang is not None else "NA",
                    dt, in_zone, self.state))
            return False

        if self.state == "falling":
            # Furniture is judged at the moment of going down. A fall that
            # starts on the floor and ends against the couch is still a fall.
            if now - self.down_since >= self.HOLD_S:
                if in_zone and not down:
                    self.state = "resting"     # FALL_V43: ended up sitting on the bed/couch
                    _dbg_print("[FALL_DBG] ended seated in a furniture zone -> resting")
                    return False
                self.state = "fallen"
                if now - self.last_alert > FALL_COOLDOWN_SEC:
                    self.last_alert = now
                    return True
        return False


class ActivityTracker:
    def __init__(self, client):
        self.client = client
        self.state = "absent"
        self.state_start = time.time()
        self.candidate_state = None
        self.candidate_start = None
        self.last_present = 0
        self.stillness_alerted = False
        self._hip_hist = deque(maxlen=15)

    def _movement(self, kp):
        def get(idx):
            if kp[idx, 2] >= CONF_THRESHOLD: return np.array([kp[idx,0], kp[idx,1]])
            return None
        lh=get(KP_LH); rh=get(KP_RH)
        if lh is None or rh is None: return 0.0
        hip = (lh+rh)/2
        self._hip_hist.append(hip)
        if len(self._hip_hist) < 2: return 0.0
        deltas = [np.linalg.norm(self._hip_hist[i]-self._hip_hist[i-1]) for i in range(1, len(self._hip_hist))]
        return float(np.mean(deltas))

    def update(self, person_present, kp=None, body_scale_px=1.0):
        now = time.time()
        if person_present: self.last_present = now
        if person_present and kp is not None:
            mag = self._movement(kp)/max(body_scale_px, 1.0)
            desired = "active" if mag > 0.012 else "still"
        elif now-self.last_present > ABSENT_TIMEOUT_SEC:
            desired = "absent"
        else:
            desired = self.state
        if desired != self.state:
            if self.candidate_state != desired:
                self.candidate_state = desired; self.candidate_start = now
            elif now-self.candidate_start >= STATE_DEBOUNCE_SEC:
                self._transition(desired, now)
        else:
            self.candidate_state = None
        if self.state == "still":
            elapsed = now-self.state_start
            in_hours = ACTIVE_HOURS_START <= datetime.now().hour < ACTIVE_HOURS_END
            if elapsed >= STILLNESS_ALERT_MIN*60 and in_hours and not self.stillness_alerted:
                self.stillness_alerted = True
                self.client.alert_stillness(elapsed)
                print(f"[ALERT] Stillness {elapsed/60:.0f} min")

    def _transition(self, new_state, now):
        duration = now-self.state_start
        started_iso = datetime.fromtimestamp(self.state_start).isoformat()
        ended_iso = datetime.fromtimestamp(now).isoformat()
        self.client.activity_event(self.state, started_iso, ended_iso, duration)
        print(f"[ACTIVITY] {self.state} -> {new_state} ({duration/60:.1f} min)")
        self.state = new_state
        self.state_start = now
        self.candidate_state = None
        self.stillness_alerted = False

def compute_posture(kp):
    def get(idx):
        if kp[idx, 2] >= CONF_THRESHOLD: return np.array([kp[idx,0], kp[idx,1]])
        return None
    ls=get(KP_LS); rs=get(KP_RS); lh=get(KP_LH); rh=get(KP_RH); nose=get(KP_NOSE)
    if not (ls is not None and rs is not None and lh is not None and rh is not None): return {}
    smid=(ls+rs)/2; hmid=(lh+rh)/2
    sasym=float(abs(ls[1]-rs[1])/(abs(ls[0]-rs[0])+1e-6)*100); sasym=min(sasym,50.0)
    dx=smid[0]-hmid[0]; dy=smid[1]-hmid[1]
    trunk=float(np.degrees(np.arctan2(abs(dx),abs(dy)+1e-6)))
    hf=0.0
    if nose is not None:
        bw=abs(ls[0]-rs[0])+1e-6
        hf=float(abs(nose[0]-smid[0])/bw*100); hf=min(hf,50.0)
    score=float(max(0,100-trunk*1.5-sasym*0.8-hf*0.5))
    return {"posture_score":round(score,1),"trunk_lean_deg":round(trunk,1),"shoulder_asymmetry_pct":round(sasym,1),"head_forward_deg":round(hf,1)}

def select_all_persons(result, conf_threshold=0.4):
    """Returns list of all detected person keypoints with sufficient confidence."""
    if result.keypoints is None or len(result.keypoints.data) == 0:
        return []
    persons = []
    boxes = result.boxes
    for i, kp_data in enumerate(result.keypoints.data):
        kp = kp_data.cpu().numpy()
        valid_pts = np.sum(kp[:, 2] >= conf_threshold)
        if valid_pts >= 6:
            if boxes is not None and i < len(boxes.xyxy):
                b = boxes.xyxy[i]
                area = (b[2]-b[0]).item() * (b[3]-b[1]).item()
            else:
                area = 0
            persons.append((kp, area))
    # Sort by area descending (largest = closest person = resident)
    persons.sort(key=lambda x: x[1], reverse=True)
    return [p[0] for p in persons]

def select_all_persons_tracked(result, conf_threshold=0.4):
    """Like select_all_persons, but preserves ByteTrack IDs.

    Returns a list of (keypoints, track_id) sorted largest-area first.
    track_id is None when tracking data is unavailable for a detection.
    """
    if result.keypoints is None or len(result.keypoints.data) == 0:
        return []
    boxes = result.boxes
    ids = None
    if boxes is not None and getattr(boxes, "id", None) is not None:
        try:
            ids = boxes.id.cpu().numpy().astype(int)
        except Exception:
            ids = None
    out = []
    for i, kp_data in enumerate(result.keypoints.data):
        kp = kp_data.cpu().numpy()
        if np.sum(kp[:, 2] >= conf_threshold) < 6:
            continue
        # Drop low-confidence phantom boxes (furniture, reflections, shadows).
        # Observed 0.20 / 0.11 detections were spawning identities.
        if boxes is not None and getattr(boxes, "conf", None) is not None and i < len(boxes.conf):
            if float(boxes.conf[i]) < 0.5:
                continue
        if boxes is not None and i < len(boxes.xyxy):
            b = boxes.xyxy[i]
            area = (b[2]-b[0]).item() * (b[3]-b[1]).item()
        else:
            area = 0
        tid = int(ids[i]) if (ids is not None and i < len(ids)) else None
        # TRACK_DBG: real confidences + ids, throttled. Remove once validated.
        try:
            import time as _tt
            _now = _tt.time()
            if _now - getattr(select_all_persons_tracked, "_last_dbg", 0) > 5:
                select_all_persons_tracked._last_dbg = _now
                _confs = boxes.conf.cpu().numpy().round(2).tolist() if boxes is not None else []
                _dbg_print("[TRACK_DBG] dets=%d confs=%s ids=%s" % (
                    len(_confs), _confs,
                    ids.tolist() if ids is not None else None))
        except Exception:
            pass
        out.append((kp, area, tid))
    out.sort(key=lambda x: x[1], reverse=True)
    return [(p[0], p[2]) for p in out]


def select_main_person(result, conf_threshold=0.4):
    """Returns keypoints of the largest (closest) person — the resident."""
    persons = select_all_persons(result, conf_threshold)
    return persons[0] if persons else None

_VIGIL_DEBUG = os.environ.get("VIGIL_DEBUG", "0") == "1"


def _dbg_print(*args, **kwargs):
    """Diagnostic output, suppressed unless VIGIL_DEBUG=1."""
    if _VIGIL_DEBUG:
        print(*args, **kwargs)


class ThreadedCapture:
    """Drop-in replacement for cv2.VideoCapture, built for RTSP.

    A background thread reads continuously and keeps only the newest frame,
    so the main loop never blocks on decoding and never processes a stale
    frame. This replaces the old eight-grab flush, which measured 211 ms
    per iteration (74% of the loop) and held the pipeline near 3.5 fps.
    """

    def __init__(self, src):
        import threading
        self._cap = cv2.VideoCapture(src)
        try:
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        self._lock = threading.Lock()
        self._frame = None
        self._ok = False
        self._seq = 0
        self._last_seq = -1
        self._running = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        # wait up to 5 s for the first frame so startup does not look like
        # a dead stream and trigger an immediate reconnect
        import time as _tm
        end = _tm.time() + 5.0
        while _tm.time() < end:
            with self._lock:
                if self._frame is not None:
                    break
            _tm.sleep(0.02)

    def _loop(self):
        import time as _tm
        while self._running:
            try:
                ok, fr = self._cap.read()
            except Exception:
                ok, fr = False, None
            if not ok or fr is None:
                with self._lock:
                    self._ok = False
                _tm.sleep(0.05)
                continue
            with self._lock:
                self._frame = fr
                self._ok = True
                self._seq += 1

    def isOpened(self):
        try:
            return self._cap.isOpened()
        except Exception:
            return False

    def set(self, *a):
        try:
            return self._cap.set(*a)
        except Exception:
            return False

    def get(self, *a):
        try:
            return self._cap.get(*a)
        except Exception:
            return 0

    def grab(self):
        return True   # the reader thread always holds the newest frame

    def retrieve(self, *a, timeout=0.3):
        import time as _tm
        end = _tm.time() + timeout
        while True:
            with self._lock:
                fr, ok, seq = self._frame, self._ok, self._seq
                if fr is not None and seq != self._last_seq:
                    self._last_seq = seq
                    return ok, fr.copy()
            if _tm.time() >= end:
                if fr is None:
                    return False, None
                return ok, fr.copy()
            _tm.sleep(0.004)

    def read(self):
        return self.retrieve()

    def release(self):
        self._running = False
        try:
            self._t.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._cap.release()
        except Exception:
            pass


_PF = {"n": 0, "loop": 0.0, "inf": 0.0, "plot": 0.0, "prev": None, "t_inf": 0.0, "t_plot": 0.0}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",default=os.environ.get("VIGIL_RTSP_URL"),
                        help="Camera source (RTSP URL or device index). Falls back to VIGIL_RTSP_URL env var so credentials don't have to be passed on the command line.")
    parser.add_argument("--server",default=DEFAULT_SERVER)
    parser.add_argument("--model",default=MODEL_PATH)
    parser.add_argument("--debug",action="store_true")
    parser.add_argument("--room-id", default="room_1",
                        help="Room identifier for multi-camera setups")
    args=parser.parse_args()
    if not args.source:
        print("[FATAL] No --source given and VIGIL_RTSP_URL not set"); sys.exit(1)

    print(f"[INIT] Loading model: {args.model}")
    model=YOLO(args.model)
    src=int(args.source) if args.source.isdigit() else args.source
    print(f"[INIT] Opening source: {src}")
    cap=ThreadedCapture(src)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        print(f"[FATAL] Cannot open: {src}"); sys.exit(1)

    client=ServerClient(args.server)
    client.room_id = args.room_id
    gait=GaitTracker(TARGET_FPS)
    fall=FallDetector()
    bedexit = BedExitDetector(fall)
    activity=ActivityTracker(client)
    calib=Calibrator()
    tracker = PersonTracker() if PERSON_TRACKING else None
    last_tracker_report = 0
    last_sync_stride_count = 0
    frame_idx=0; last_hb=0; last_sync=0; last_snapshot=0; last_fps_t=time.time(); fps_frames=0; cur_fps=TARGET_FPS

    print("[INIT] VIGIL camera pipeline running. Ctrl+C to stop.")
    try:
        while True:
            person_present = False  # reset every frame
            # PERF_DBG: per-stage timing. Remove once frame rate is fixed.
            _pf_now = time.time()
            if _PF["prev"] is not None:
                _PF["loop"] += _pf_now - _PF["prev"]
                _PF["n"] += 1
            _PF["prev"] = _pf_now
            if _PF["n"] >= 40:
                _n = float(_PF["n"])
                _lp = _PF["loop"] / _n * 1000
                _in = _PF["inf"] / _n * 1000
                _pl = _PF["plot"] / _n * 1000
                _dbg_print("[PERF_DBG] fps=%.1f loop_ms=%.0f infer_ms=%.0f plot_ms=%.0f other_ms=%.0f"
                      % (1000.0 / _lp if _lp > 0 else 0, _lp, _in, _pl, _lp - _in - _pl))
                _PF.update(n=0, loop=0.0, inf=0.0, plot=0.0)
            # Flush stale RTSP buffer frames — keeps feed within 1-2s of live
            for _flush in range(8): cap.grab()
            ok, frame = cap.retrieve()
            if not ok:
                print("[WARN] Frame read failed — retrying"); time.sleep(2)
                cap.release(); cap=ThreadedCapture(src); cap.set(cv2.CAP_PROP_BUFFERSIZE, 1); continue

            try:
                frame_idx+=1; fps_frames+=1; now=time.time()
                if now-last_fps_t>=5:
                    cur_fps=fps_frames/(now-last_fps_t); globals()['cur_fps_dbg']=cur_fps; fps_frames=0; last_fps_t=now

                _PF["t_inf"] = time.time()
                results=model.track(frame,persist=True,tracker="vigil_bytetrack.yaml",verbose=False)
                _PF["inf"] += time.time() - _PF["t_inf"]
                person_present = False
                kp = None
                person_count = 0
                main_pid = None
                if len(results) > 0:
                    _tracked = select_all_persons_tracked(results[0], CONF_THRESHOLD)
                    all_persons = [t[0] for t in _tracked]
                    _track_ids = [t[1] for t in _tracked]
                    main_pid = vigil_track_persons(all_persons, frame=frame,
                                                   track_ids=_track_ids)
                    if all_persons:
                        globals().setdefault('_PIDS_WIN', set()).add(main_pid)
                    person_count = len(all_persons)
                    if person_count > 0:
                        kp = all_persons[0]  # Track the largest person (resident)
                        person_present = True
                        if person_count > 1:
                            print(f"[MULTI] {person_count} people detected — tracking resident (largest)")

                    # Multi-person tracking
                    if tracker and len(all_persons) > 0:
                        cadences = [gait.cadence_spm if i == 0 else None for i in range(len(all_persons))]
                        assignments = tracker.update(all_persons, cadences)

                        # Report new confident persons to server every 60 seconds
                        now_t = time.time()
                        if now_t - last_tracker_report >= 60:
                            to_report = tracker.get_persons_to_report()
                            if to_report:
                                last_tracker_report = now_t
                                persons_data = [tracker.get_signature_summary(pid) for pid in to_report]
                                client._post("/persons/detect", {
                                    "room_id": client.room_id,
                                    "persons": persons_data,
                                })
                                tracker.mark_reported(to_report)
                                if len(to_report) > 1 or tracker.get_active_count() > 1:
                                    print(f"[MULTI-PERSON] {tracker.get_active_count()} people detected in frame")

                # Write latest annotated frame to shared memory for MJPEG streaming
                _PF["t_plot"] = time.time()
                _PF["sk"] = _PF.get("sk", 0) + 1
                try:
                    # Encode the live-stream frame every 2nd iteration. The
                    # MJPEG view stays smooth and ~20 ms per frame is recovered.
                    if _PF["sk"] % 2 == 0:   # LIVE_VIDEO_V1 (was every 3rd)
                        annotated_frame = _vigil_annotate(results, frame)
                        cv2.imwrite('/dev/shm/vigil_latest.jpg', annotated_frame,
                                    [cv2.IMWRITE_JPEG_QUALITY, 70])
                        _write_skeleton_frame(results, frame.shape)
                    # Clean frame for the face service. The annotated frame
                    # above has skeleton dots over the eyes and nose, which
                    # would corrupt facial landmark measurement.
                    if _PF["sk"] % 3 == 0:
                        # Atomic write: a reader must never see a partial
                        # JPEG. os.replace is atomic on the same filesystem.
                        cv2.imwrite('/dev/shm/.vigil_raw.tmp.jpg', frame,
                                    [cv2.IMWRITE_JPEG_QUALITY, 80])
                        os.replace('/dev/shm/.vigil_raw.tmp.jpg',
                                   '/dev/shm/vigil_raw.jpg')
                except Exception:
                    pass
                _PF["plot"] += time.time() - _PF["t_plot"]

                client.refresh_config()
                fh=frame.shape[0]

                if person_present and kp is not None:
                    n_ok=kp[KP_NOSE,2]>=CONF_THRESHOLD
                    a_ok=kp[KP_LA,2]>=CONF_THRESHOLD or kp[KP_RA,2]>=CONF_THRESHOLD
                    if n_ok and a_ok:
                        ay=kp[KP_LA,1] if kp[KP_LA,2]>=CONF_THRESHOLD else kp[KP_RA,1]
                        calib.update(kp[KP_NOSE,1],ay,True)
                    bedexit.update(kp, frame.shape, client)
                    _replay_push(kp, frame.shape)
                    if fall.update(kp, frame.shape):
                        print(f"[FALL] hip={fall.hip_drop_pct:.2f} torso={fall.torso_angle:.0f} aspect={fall.aspect:.2f}")
                        client.alert_fall(fall.hip_drop_pct,fall.torso_angle)
                        try:
                            client._post("/alert/fall/replay", {"frames": [f for f in list(_REPLAY) if f[0] >= time.time() - 30]})
                        except Exception:
                            pass
                    gait.update(kp,frame_idx,fh)
                    # Head crop for the face service. MediaPipe downscales
                    # its input, so a face that is ~60 px in a 1920x1080
                    # frame is a few pixels by the time the detector sees
                    # it. Cropping the head and upscaling fixes that.
                    if _PF.get("sk", 0) % 3 == 0:
                        try:
                            _hk = kp[0:5]
                            _hv = _hk[_hk[:, 2] >= 0.3]
                            if len(_hv) >= 2:
                                _cx = float(_hv[:, 0].mean())
                                _cy = float(_hv[:, 1].mean())
                                _sh = kp[5:7]
                                _sv = _sh[_sh[:, 2] >= 0.3]
                                if len(_sv) == 2:
                                    _box = max(abs(float(_sv[0][0]) - float(_sv[1][0])), 50.0)
                                else:
                                    _box = max(float(_hv[:, 0].max() - _hv[:, 0].min()) * 3.0, 50.0)
                                _fh0, _fw0 = frame.shape[:2]
                                _x1 = max(0, int(_cx - _box))
                                _x2 = min(_fw0, int(_cx + _box))
                                _y1 = max(0, int(_cy - _box * 1.2))
                                _y2 = min(_fh0, int(_cy + _box * 0.9))
                                if (_x2 - _x1) > 30 and (_y2 - _y1) > 30:
                                    _crop = cv2.resize(frame[_y1:_y2, _x1:_x2], (256, 256),
                                                       interpolation=cv2.INTER_CUBIC)
                                    cv2.imwrite('/dev/shm/.vigil_face.tmp.jpg', _crop,
                                                [cv2.IMWRITE_JPEG_QUALITY, 92])
                                    os.replace('/dev/shm/.vigil_face.tmp.jpg',
                                               '/dev/shm/vigil_face.jpg')
                        except Exception:
                            pass
                    bscale=max(np.percentile(list(calib.max_body_px),90) if calib.max_body_px else 100,1)
                    _rp0 = getattr(client, 'resident_pid', None)
                    if _rp0 and main_pid and main_pid != _rp0:
                        activity.update(False)        # someone else is the main person
                    else:
                        activity.update(True,kp,bscale)
                else:
                    activity.update(False)

                if now-last_sync>=CAMERA_SYNC_SEC:
                    last_sync=now
                    # cadence_spm and gait_speed_ms are both derived from the same
                    # underlying stride timing, so they must share one freshness gate.
                    # gait_speed_ms previously had no gate at all: once cadence data
                    # went stale (e.g. the tracked person stopped walking or left
                    # frame) it kept computing from the last-known strides forever,
                    # silently repeating a stale reading into the DB every sync cycle.
                    cadence_fresh = gait.cadence_spm > 0 and (time.time()-getattr(gait,"_cadence_last_updated",0))<20
                    gait_speed_ms=None
                    if cadence_fresh and len(gait._strides)>0:
                        avg_stride=np.mean(gait._strides)
                        stride_len=client.step_length_ratio*(client.resident_height_cm/100.0)
                        # avg_stride is a full 2-step gait cycle duration; stride_len is a
                        # single-step length, so distance covered per cycle is 2*stride_len
                        gait_speed_ms=(2*stride_len)/avg_stride if avg_stride>0 else None
                    elif cadence_fresh:
                        # Fallback: estimate from cadence alone using average stride length
                        stride_len=client.step_length_ratio*(client.resident_height_cm/100.0)
                        stride_time=60.0/gait.cadence_spm
                        gait_speed_ms=stride_len/stride_time if stride_time>0 else None
                    posture=compute_posture(kp) if person_present and kp is not None else {}
                    payload={
                        "date":datetime.now().strftime("%Y-%m-%d"),
                        "camera_mode":"rtsp" if isinstance(src,str) else "usb",
                        "room_id": client.room_id,
                        "cadence_spm": round(gait.cadence_spm,1) if cadence_fresh else None,
                        "asymmetry_pct":round(gait.asymmetry_pct,1) if gait.asymmetry_pct else None,
                        "gait_speed_ms":round(gait_speed_ms,3) if gait_speed_ms else None,
                        "stride_variability":round(gait.stride_variability,2) if gait.stride_variability else None,
                        "arm_swing_asymmetry":round(gait.arm_swing_asymmetry,2) if gait.arm_swing_asymmetry else None,
                        "step_width":round(gait.step_width_px,2) if gait.step_width_px else None,
                        "cadence_variability":round(gait.cadence_variability,2) if gait.cadence_variability else None,
                        "stride_count":gait._stride_count,
                        **posture,
                    }

                    # Gait accuracy instrumentation — printed every sync cycle regardless
                    # of whether anything gets sent, so a frozen/None reading is visible
                    # alongside the raw numbers that produced it.
                    strides_this_cycle = gait._stride_count - last_sync_stride_count
                    last_sync_stride_count = gait._stride_count
                    _last_upd = getattr(gait, "_cadence_last_updated", None)
                    cadence_age_str = f"{time.time()-_last_upd:.1f}" if _last_upd else "never"
                    _dbg_print(f"[GAIT_DBG] heel_strikes_since_sync={strides_this_cycle} "
                          f"raw_stride_intervals_s={[round(s,3) for s in gait._strides]} "
                          f"cadence_recomputed_this_cycle={strides_this_cycle > 0} "
                          f"cadence_fresh={cadence_fresh} cadence_age_s={cadence_age_str} "
                          f"resident_height_cm_assumed={client.resident_height_cm} "
                          f"body_scale_px={locals().get('bscale')}")

                    if any(v is not None for k,v in payload.items() if k not in ("date","camera_mode","stride_count")):
                        _win = {p for p in globals().get('_PIDS_WIN', set()) if p}
                        globals()['_PIDS_WIN'] = set()
                        _rp = getattr(client, 'resident_pid', None)
                        _mixed = len(_win) > 1
                        if not _mixed and (_rp is None or _win == {_rp}):
                            client.sync_camera(payload)
                        print(f"[SYNC] who={sorted(_win)} mixed={_mixed} cadence={payload.get('cadence_spm')} speed={payload.get('gait_speed_ms')} posture={payload.get('posture_score')}")

                        if main_pid and payload.get('cadence_spm') and not _mixed and main_pid in _win:
                            _gs = payload.get('gait_speed_ms')
                            if _gs and _rp and main_pid != _rp:
                                try:
                                    import person_hook as _ph2
                                    _a, _b = _ph2._body_ratio(main_pid), _ph2._body_ratio(_rp)
                                    _gs = round(_gs * _a / _b, 3) if (_a and _b) else None
                                except Exception:
                                    _gs = None
                            try:
                                client._post(f"/persons/{main_pid}/record", {
                                    'gait_speed_ms': _gs,
                                    'cadence_spm': payload.get('cadence_spm'),
                                    'posture_score': payload.get('posture_score'),
                                    'asymmetry_pct': payload.get('asymmetry_pct'),
                                })
                            except Exception:
                                pass

                if now-last_hb>=HEARTBEAT_SEC:
                    last_hb=now
                    client.heartbeat(person_present, cur_fps,
                                     "rtsp" if isinstance(src,str) else "usb",
                                     person_count=person_count)

                if now - last_snapshot >= 2.0 and frame is not None:
                    last_snapshot = now
                    client.post_snapshot(frame)

                if args.debug:
                    disp=frame.copy()
                    if kp is not None:
                        for i in range(len(kp)):
                            if kp[i,2]>=CONF_THRESHOLD:
                                cv2.circle(disp,(int(kp[i,0]),int(kp[i,1])),4,(0,255,0),-1)
                    cv2.putText(disp,f"{activity.state.upper()} | fall:{fall.state} | fps:{cur_fps:.0f}",(10,30),cv2.FONT_HERSHEY_SIMPLEX,0.7,(0,255,255),2)
                    cv2.imshow("VIGIL",disp)
                    if cv2.waitKey(1)&0xFF==ord('q'): break

                elapsed=time.time()-now
                time.sleep(max(0,(1.0/TARGET_FPS)-elapsed))
            except Exception as e:
                print(f"[ERROR] Frame processing failed: {e}")
                time.sleep(0.5)
                continue

    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Stopping")
    finally:
        cap.release()
        if args.debug: cv2.destroyAllWindows()

if __name__=="__main__":
    main()
