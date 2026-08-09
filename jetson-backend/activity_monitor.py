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
        self.resident_name = ""
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
                self.resident_name = cfg.get("resident_name") or ""
        except Exception as e:
            print(f"[SERVER] config fetch failed: {e}")

class Calibrator:
    def __init__(self):
        self.max_body_px = deque(maxlen=300)

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

        if la and lh:
            d = abs(la[1]-lh[1])
            contact = la[1] > lh[1]+d*0.85
            if contact and not self._lin:
                self._lc.append(frame_idx)
                if len(self._lc) >= 2:
                    t = (self._lc[-1]-self._lc[-2])/self.fps
                    if 0.4 <= t <= 2.5:
                        self._strides.append(t); self._stride_count += 1
                if len(self._lc) > 20:
                    self._lc = self._lc[-10:]
            self._lin = contact

        if ra and rh:
            d = abs(ra[1]-rh[1])
            contact = ra[1] > rh[1]+d*0.85
            if contact and not self._rin:
                self._rc.append(frame_idx)
                if len(self._rc) >= 2:
                    t = (self._rc[-1]-self._rc[-2])/self.fps
                    if 0.4 <= t <= 2.5:
                        self._strides.append(t); self._stride_count += 1
                if len(self._rc) > 20:
                    self._rc = self._rc[-10:]
            self._rin = contact

        if len(self._strides) >= 6:
            arr = np.array(self._strides)
            m = np.mean(arr)
            self.stride_variability = float(np.std(arr, ddof=1)/m*100) if m > 0 else 0.0
            self.cadence_spm = 120.0/m if m > 0 else 0.0
            self._cadence_last_updated = __import__('time').time()
            self._hip_peaks = []  # primary method succeeded — clear stale fallback state
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
                            if 0.5 <= (self._hip_peaks[i] - self._hip_peaks[i-2]) / self.fps <= 3.0
                        ]
                        if intervals and self.cadence_spm == 0.0 and len(self._hip_peaks) >= 3:
                            avg_stride = sum(intervals) / len(intervals)
                            self.cadence_spm = round(120.0 / avg_stride, 1) if avg_stride > 0 else 0.0
                        if len(self._hip_peaks) > 20:
                            self._hip_peaks = self._hip_peaks[-10:]

class FallDetector:
    def __init__(self):
        self.state = "upright"
        self.candidate_start = None
        self.last_alert = 0
        self.hip_drop_pct = 0.0
        self.torso_angle = 0.0

    def update(self, kp):
        def get(idx):
            if kp[idx, 2] >= CONF_THRESHOLD: return kp[idx, 0], kp[idx, 1]
            return None

        # Confidence gate: require both shoulders and both hips at >= 0.3
        # before computing any fall metrics. A corrupted frame resets the
        # confirmation timer rather than counting toward it.
        KP_CONF_GATE = 0.3
        shoulders_ok = (kp[KP_LS, 2] >= KP_CONF_GATE and kp[KP_RS, 2] >= KP_CONF_GATE)
        hips_ok      = (kp[KP_LH, 2] >= KP_CONF_GATE and kp[KP_RH, 2] >= KP_CONF_GATE)
        if not (shoulders_ok and hips_ok):
            # Bad frame — reset timer so it never contributes to confirmation
            if self.state == "descending":
                self.state = "upright"; self.candidate_start = None
            return False

        lh=get(KP_LH); rh=get(KP_RH); ls=get(KP_LS); rs=get(KP_RS)
        la=get(KP_LA); ra=get(KP_RA)
        if not (lh and rh and ls and rs): return False
        hip_y = (lh[1]+rh[1])/2
        shoulder_y = (ls[1]+rs[1])/2
        shoulder_x = (ls[0]+rs[0])/2
        hip_x = (lh[0]+rh[0])/2
        ankle_y = ((la[1] if la else hip_y)+(ra[1] if ra else hip_y))/2
        body_span = abs(hip_y-shoulder_y)
        if body_span < 5: return False
        dx = shoulder_x-hip_x; dy = shoulder_y-hip_y
        torso_angle = float(np.degrees(np.arctan2(abs(dx), abs(dy)+1e-6)))
        self.torso_angle = torso_angle
        hip_ratio = (ankle_y-hip_y)/body_span if body_span > 0 else 1.0
        self.hip_drop_pct = float(max(0, min(1, 1.0-hip_ratio/1.5)))
        is_low = torso_angle > 55 and self.hip_drop_pct > 0.45
        now = time.time()
        if is_low:
            if self.state == "upright":
                self.state = "descending"; self.candidate_start = now
            elif self.state == "descending" and now-self.candidate_start >= FALL_CONFIRM_SEC:
                self.state = "fallen_confirmed"
                if now-self.last_alert > FALL_COOLDOWN_SEC:
                    self.last_alert = now; return True
        else:
            self.state = "upright"; self.candidate_start = None
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

def select_main_person(result, conf_threshold=0.4):
    """Returns keypoints of the largest (closest) person — the resident."""
    persons = select_all_persons(result, conf_threshold)
    return persons[0] if persons else None

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
    cap=cv2.VideoCapture(src)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        print(f"[FATAL] Cannot open: {src}"); sys.exit(1)

    client=ServerClient(args.server)
    client.room_id = args.room_id
    gait=GaitTracker(TARGET_FPS)
    fall=FallDetector()
    activity=ActivityTracker(client)
    calib=Calibrator()
    tracker = PersonTracker() if PERSON_TRACKING else None
    last_tracker_report = 0
    frame_idx=0; last_hb=0; last_sync=0; last_snapshot=0; last_fps_t=time.time(); fps_frames=0; cur_fps=TARGET_FPS

    print("[INIT] VIGIL camera pipeline running. Ctrl+C to stop.")
    try:
        while True:
            person_present = False  # reset every frame
            # Flush stale RTSP buffer frames — keeps feed within 1-2s of live
            for _flush in range(8): cap.grab()
            ok, frame = cap.retrieve()
            if not ok:
                print("[WARN] Frame read failed — retrying"); time.sleep(2)
                cap.release(); cap=cv2.VideoCapture(src); cap.set(cv2.CAP_PROP_BUFFERSIZE, 1); continue

            try:
                frame_idx+=1; fps_frames+=1; now=time.time()
                if now-last_fps_t>=5:
                    cur_fps=fps_frames/(now-last_fps_t); fps_frames=0; last_fps_t=now

                results=model(frame,verbose=False)
                person_present = False
                kp = None
                person_count = 0
                main_pid = None
                if len(results) > 0:
                    all_persons = select_all_persons(results[0], CONF_THRESHOLD)
                    main_pid = vigil_track_persons(all_persons, frame=frame)
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

                client.refresh_config()
                fh=frame.shape[0]

                if person_present and kp is not None:
                    n_ok=kp[KP_NOSE,2]>=CONF_THRESHOLD
                    a_ok=kp[KP_LA,2]>=CONF_THRESHOLD or kp[KP_RA,2]>=CONF_THRESHOLD
                    if n_ok and a_ok:
                        ay=kp[KP_LA,1] if kp[KP_LA,2]>=CONF_THRESHOLD else kp[KP_RA,1]
                        calib.update(kp[KP_NOSE,1],ay,True)
                    if fall.update(kp):
                        print(f"[FALL] hip={fall.hip_drop_pct:.2f} torso={fall.torso_angle:.0f}")
                        client.alert_fall(fall.hip_drop_pct,fall.torso_angle)
                    gait.update(kp,frame_idx,fh)
                    bscale=max(np.percentile(list(calib.max_body_px),90) if calib.max_body_px else 100,1)
                    activity.update(True,kp,bscale)
                else:
                    activity.update(False)

                if now-last_sync>=CAMERA_SYNC_SEC:
                    last_sync=now
                    gait_speed_ms=None
                    if gait.cadence_spm>0 and len(gait._strides)>0:
                        avg_stride=np.mean(gait._strides)
                        stride_len=0.41*(client.resident_height_cm/100.0)
                        # avg_stride is a full 2-step gait cycle duration; stride_len is a
                        # single-step length, so distance covered per cycle is 2*stride_len
                        gait_speed_ms=(2*stride_len)/avg_stride if avg_stride>0 else None
                    elif gait.cadence_spm>0:
                        # Fallback: estimate from cadence alone using average stride length
                        stride_len=0.41*(client.resident_height_cm/100.0)
                        stride_time=60.0/gait.cadence_spm
                        gait_speed_ms=stride_len/stride_time if stride_time>0 else None
                    posture=compute_posture(kp) if person_present and kp is not None else {}
                    payload={
                        "date":datetime.now().strftime("%Y-%m-%d"),
                        "camera_mode":"rtsp" if isinstance(src,str) else "usb",
                        "room_id": client.room_id,
                        "cadence_spm":(round(gait.cadence_spm,1)
                            if gait.cadence_spm and
                            (__import__("time").time()-getattr(gait,"_cadence_last_updated",0))<20
                            else None),
                        "asymmetry_pct":round(gait.asymmetry_pct,1) if gait.asymmetry_pct else None,
                        "gait_speed_ms":round(gait_speed_ms,3) if gait_speed_ms else None,
                        "stride_variability":round(gait.stride_variability,2) if gait.stride_variability else None,
                        "arm_swing_asymmetry":round(gait.arm_swing_asymmetry,2) if gait.arm_swing_asymmetry else None,
                        "step_width":round(gait.step_width_px,2) if gait.step_width_px else None,
                        "cadence_variability":round(gait.cadence_variability,2) if gait.cadence_variability else None,
                        "stride_count":gait._stride_count,
                        **posture,
                    }
                    if any(v is not None for k,v in payload.items() if k not in ("date","camera_mode","stride_count")):
                        client.sync_camera(payload)
                        print(f"[SYNC] cadence={payload.get('cadence_spm')} speed={payload.get('gait_speed_ms')} posture={payload.get('posture_score')}")

                        if main_pid and payload.get('cadence_spm'):
                            try:
                                client._post(f"/persons/{main_pid}/record", {
                                    'gait_speed_ms': payload.get('gait_speed_ms'),
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

                if now - last_snapshot >= 2.0 and person_present and frame is not None:
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
