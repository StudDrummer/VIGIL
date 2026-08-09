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

def _post_person_photo(person_id: str, frame_bgr) -> None:
    """Encode the current camera frame and POST it as this person's identification photo."""
    try:
        import cv2, base64
        # Crop to a reasonable size, keep aspect ratio
        h, w = frame_bgr.shape[:2]
        small = cv2.resize(frame_bgr, (min(w, 480), min(h, 360)))
        ok, buf = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok:
            return
        b64 = base64.b64encode(buf.tobytes()).decode('utf-8')
        requests.post(
            f"{SERVER_URL}/persons/{person_id}/photo",
            json={"photo_b64": b64},
            headers={"X-VIGIL-Key": API_KEY},
            timeout=5,
        )
    except Exception:
        pass




class _PersonSignature:
    def __init__(self, pid):
        self.id = pid
        self.height_samples = deque(maxlen=50)
        self.first_seen = time.time()
        self.last_seen = time.time()
        self.frames_seen = 0
        self.reported = False
        self.name = None          # filled in once the app identifies them
        self.is_resident = False

    @property
    def height_est(self):
        if len(self.height_samples) < 5:
            return None
        return float(np.percentile(list(self.height_samples), 75))

    @property
    def confidence(self):
        h = len(self.height_samples) / 50.0
        f = min(1.0, self.frames_seen / 100.0)
        return (h + f) / 2.0

    def matches(self, height_px, threshold=0.18):
        h = self.height_est
        if h is None or h <= 0:
            return False
        return abs(height_px - h) / h <= threshold

    def update(self, height_px):
        self.height_samples.append(height_px)
        self.last_seen = time.time()
        self.frames_seen += 1


_signatures = {}
_id_counter = [0]
_last_report = [0.0]

def reload_known_persons() -> None:
    """Load named persons from DB at startup so IDs persist across restarts."""
    import sqlite3
    try:
        conn = sqlite3.connect('/home/rushil-mohan/passive-health-moniter/vigil.db')
        rows = conn.execute(
            'SELECT id, height_px, confidence, frames_seen FROM persons '
            'WHERE name IS NOT NULL AND height_px IS NOT NULL'
        ).fetchall()
        conn.close()
        for pid, height_px, conf, frames_seen in rows:
            if pid not in _signatures:
                sig = _PersonSignature(pid)
                count = min(50, max(5, int((frames_seen or 100) / 60)))
                for _ in range(count):
                    sig.height_samples.append(height_px)
                sig.frames_seen = frames_seen or 100
                sig.reported = True
                _signatures[pid] = sig
                print(f'[person_hook] Restored: {pid} height={height_px}')
    except Exception as e:
        print(f'[person_hook] reload_known_persons error: {e}')

_last_main_id = [None]   # person_id
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


def vigil_track_persons(all_persons, room_id=None, frame=None):
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

        for kp in all_persons:
            height_px = _estimate_height(kp)
            if height_px is None:
                frame_assignments.append((None, None))
                continue

            matched = None
            best_diff = float('inf')
            for pid, sig in _signatures.items():
                if sig.height_est is None:
                    continue
                diff = abs(height_px - sig.height_est) / sig.height_est
                if diff < best_diff and sig.matches(height_px):
                    best_diff = diff
                    matched = pid

            if matched:
                _signatures[matched].update(height_px)
                frame_assignments.append((matched, height_px))
            else:
                new_id = _next_id()
                sig = _PersonSignature(new_id)
                sig.update(height_px)
                _signatures[new_id] = sig
                frame_assignments.append((new_id, height_px))

        # all_persons[0] is the largest = the resident being measured for gait
        main_id = frame_assignments[0][0] if frame_assignments else None
        _last_main_id[0] = main_id

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
                    # Post identification photo if we have a current frame
                    if _current_frame is not None:
                        _post_person_photo(pid, _current_frame)
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
