"""FLOOR_V1: a person lying on the floor outside furniture zones alerts once,
after the configured time; standing up or lying in a bed zone does not."""
import os
import sys
import types

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
sys.modules.setdefault("ultralytics", types.SimpleNamespace(YOLO=None))
import activity_monitor as am  # noqa: E402

T = [1000.0]
am.time.time = lambda: T[0]


def lying(x0=300, y=600):
    kp = np.zeros((17, 3), np.float32)
    for i in range(17):
        kp[i] = (x0 + i * 20, y + (i % 3) * 4, 0.9)
    kp[5] = (x0 + 40, y, 0.9); kp[6] = (x0 + 40, y + 20, 0.9)        # shoulders
    kp[11] = (x0 + 200, y, 0.9); kp[12] = (x0 + 200, y + 20, 0.9)    # hips
    return kp


def standing(x=600, y0=200):
    kp = np.zeros((17, 3), np.float32)
    for i in range(17):
        kp[i] = (x + (i % 2) * 30, y0 + i * 25, 0.9)
    kp[5] = (x, y0 + 100, 0.9); kp[6] = (x + 30, y0 + 100, 0.9)
    kp[11] = (x, y0 + 250, 0.9); kp[12] = (x + 30, y0 + 250, 0.9)
    return kp


def run(frames, zones=None):
    fd = am.FallDetector()
    fd._zones = zones or []
    fd._zchk = 1e18
    alerts = 0
    for kp in frames:
        T[0] += 0.2
        fd.update(kp, (1080, 1920))
        alerts += fd.floor_alert_due(60)
    return alerts


def test_floor_alerts_once_after_60s():
    assert run([lying()] * 400) == 1          # 80 s on the floor


def test_short_lie_no_alert():
    assert run([lying()] * 200) == 0          # 40 s


def test_bed_zone_never_alerts():
    assert run([lying()] * 400, zones=[(0.0, 0.0, 1.0, 1.0)]) == 0


def test_getting_up_resets():
    frames = [lying()] * 200 + [standing()] * 30 + [lying()] * 200
    assert run(frames) == 0


def test_brief_dropout_does_not_reset():
    frames = [lying()] * 150 + [np.zeros((17, 3), np.float32)] * 10 + [lying()] * 200
    assert run(frames) == 1


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
