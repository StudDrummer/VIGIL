"""
VIGIL Person Tracker
Identifies and distinguishes multiple people using height + gait signatures.
No face recognition needed — uses body proportions and movement patterns.
"""

import time
import numpy as np
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

@dataclass
class PersonSignature:
    person_id: str
    height_px_samples: deque = field(default_factory=lambda: deque(maxlen=50))
    cadence_samples: deque = field(default_factory=lambda: deque(maxlen=20))
    shoulder_width_samples: deque = field(default_factory=lambda: deque(maxlen=30))
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    frames_seen: int = 0
    reported: bool = False  # whether server was notified

    @property
    def height_est(self) -> Optional[float]:
        if len(self.height_px_samples) < 5: return None
        return float(np.percentile(list(self.height_px_samples), 75))

    @property
    def cadence_est(self) -> Optional[float]:
        if len(self.cadence_samples) < 3: return None
        return float(np.mean(self.cadence_samples))

    @property
    def shoulder_width_est(self) -> Optional[float]:
        if len(self.shoulder_width_samples) < 5: return None
        return float(np.median(self.shoulder_width_samples))

    @property
    def confidence(self) -> float:
        """How confident we are this is a stable, identifiable person."""
        h = len(self.height_px_samples) / 50.0
        f = min(1.0, self.frames_seen / 100.0)
        return (h + f) / 2.0

    def matches(self, height_px: float, shoulder_w: Optional[float] = None,
                threshold: float = 0.18) -> bool:
        """Check if a detected person matches this signature."""
        my_h = self.height_est
        if my_h is None or my_h <= 0: return False
        height_diff = abs(height_px - my_h) / my_h
        if height_diff > threshold: return False
        if shoulder_w is not None and self.shoulder_width_est is not None:
            sw_diff = abs(shoulder_w - self.shoulder_width_est) / max(self.shoulder_width_est, 1)
            if sw_diff > 0.25: return False
        return True

    def update(self, height_px: float, shoulder_w: Optional[float],
               cadence: Optional[float]):
        self.height_px_samples.append(height_px)
        if shoulder_w: self.shoulder_width_samples.append(shoulder_w)
        if cadence: self.cadence_samples.append(cadence)
        self.last_seen = time.time()
        self.frames_seen += 1


class PersonTracker:
    """
    Tracks multiple people across frames using appearance signatures.
    Does not use face recognition — uses height, build, and gait patterns.
    """
    MIN_CONFIDENCE_TO_REPORT = 0.4   # report to server when this confident
    MIN_FRAMES_TO_TRACK = 30          # must be seen 30+ frames before creating signature
    ABSENCE_TIMEOUT = 120             # seconds before removing person from active set
    NEW_PERSON_COOLDOWN = 300         # don't report same new-person event within 5 min

    def __init__(self):
        self.signatures: Dict[str, PersonSignature] = {}
        self._id_counter = 0
        self._last_new_person_report = 0

    def _next_id(self) -> str:
        self._id_counter += 1
        return f"person_{self._id_counter}"

    def _estimate_height(self, kp: np.ndarray, conf_threshold: float = 0.25) -> Optional[float]:
        """Estimate person height in pixels from keypoints."""
        nose_ok = kp[0, 2] >= conf_threshold
        lankle = kp[15, 2] >= conf_threshold
        rankle = kp[16, 2] >= conf_threshold
        if not nose_ok or not (lankle or rankle): return None
        nose_y = kp[0, 1]
        ankle_y = kp[15, 1] if lankle else kp[16, 1]
        if ankle_y <= nose_y: return None
        return float(ankle_y - nose_y)

    def _estimate_shoulder_width(self, kp: np.ndarray,
                                  conf_threshold: float = 0.25) -> Optional[float]:
        ls_ok = kp[5, 2] >= conf_threshold
        rs_ok = kp[6, 2] >= conf_threshold
        if not (ls_ok and rs_ok): return None
        return float(abs(kp[5, 0] - kp[6, 0]))

    def _cleanup_absent(self):
        now = time.time()
        to_remove = [pid for pid, sig in self.signatures.items()
                     if now - sig.last_seen > self.ABSENCE_TIMEOUT]
        for pid in to_remove:
            del self.signatures[pid]

    def update(self, all_keypoints: List[np.ndarray],
               cadences: Optional[List[float]] = None) -> Dict[str, str]:
        """
        Update tracker with new frame detections.
        Returns dict mapping detection_index -> person_id
        """
        self._cleanup_absent()
        assignments = {}

        for i, kp in enumerate(all_keypoints):
            h = self._estimate_height(kp)
            sw = self._estimate_shoulder_width(kp)
            cad = cadences[i] if cadences and i < len(cadences) else None

            if h is None: continue

            matched_id = None
            best_diff = float('inf')
            for pid, sig in self.signatures.items():
                if sig.height_est is None: continue
                diff = abs(h - sig.height_est) / max(sig.height_est, 1)
                if diff < best_diff and sig.matches(h, sw):
                    best_diff = diff
                    matched_id = pid

            if matched_id:
                self.signatures[matched_id].update(h, sw, cad)
                assignments[i] = matched_id
            else:
                new_id = self._next_id()
                sig = PersonSignature(person_id=new_id)
                sig.update(h, sw, cad)
                self.signatures[new_id] = sig
                assignments[i] = new_id

        return assignments

    def get_persons_to_report(self) -> List[str]:
        """Return IDs of newly confident persons that haven't been reported yet."""
        now = time.time()
        if now - self._last_new_person_report < self.NEW_PERSON_COOLDOWN:
            return []
        to_report = [
            pid for pid, sig in self.signatures.items()
            if not sig.reported
            and sig.confidence >= self.MIN_CONFIDENCE_TO_REPORT
            and sig.frames_seen >= self.MIN_FRAMES_TO_TRACK
        ]
        if to_report:
            self._last_new_person_report = now
        return to_report

    def mark_reported(self, person_ids: List[str]):
        for pid in person_ids:
            if pid in self.signatures:
                self.signatures[pid].reported = True

    def get_active_count(self) -> int:
        now = time.time()
        return sum(1 for sig in self.signatures.values()
                   if now - sig.last_seen < 30)

    def get_signature_summary(self, person_id: str) -> dict:
        sig = self.signatures.get(person_id)
        if not sig: return {}
        return {
            "person_id": person_id,
            "height_px": round(sig.height_est, 1) if sig.height_est else None,
            "shoulder_width_px": round(sig.shoulder_width_est, 1) if sig.shoulder_width_est else None,
            "cadence_est": round(sig.cadence_est, 1) if sig.cadence_est else None,
            "frames_seen": sig.frames_seen,
            "confidence": round(sig.confidence, 3),
            "minutes_present": round((sig.last_seen - sig.first_seen) / 60, 1),
        }
