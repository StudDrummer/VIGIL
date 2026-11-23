"""STALL_WATCHDOG_V1: a camera pipeline that stops moving (stuck main loop, or a camera reader
that hands back the same old frame) exits so systemd starts it fresh, instead of sitting for
hours as "running" while the app shows the camera offline."""
import os
import sys
import tempfile
import time
import types

home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.makedirs(os.path.join(home, "passive-health-moniter"))
os.environ.setdefault("VIGIL_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
sys.modules.setdefault("ultralytics", types.SimpleNamespace(YOLO=object))
import activity_monitor as am  # noqa: E402

now = 10_000.0
# healthy: loop moved 1 s ago, new frame 0.1 s ago
assert am._stall_reason(now, now - 1, now - 0.1, True) is None
# the main loop is stuck (the 10-hour hang)
assert "not moved" in am._stall_reason(now, now - 600, now - 600, True)
# the loop keeps going but the camera reader is stuck on an old frame
assert "no new picture" in am._stall_reason(now, now - 1, now - 300, True)
# a USB camera or a video file is not held to the stream rule; startup (no beat yet) is never a stall
assert am._stall_reason(now, now - 1, now - 300, False) is None
assert am._stall_reason(now, 0.0, 0.0, True) is None

# the reader records when each new frame arrives
class Fake:
    def __init__(self):
        self.n = 0
    def read(self):
        self.n += 1
        time.sleep(0.01)
        return (self.n < 5), (object() if self.n < 5 else None)
    def set(self, *a):
        return True
    def isOpened(self):
        return True
    def release(self):
        pass

real = am.cv2.VideoCapture
am.cv2.VideoCapture = lambda src: Fake()
try:
    t0 = time.time()
    cap = am.ThreadedCapture("rtsp://camera")
    time.sleep(0.3)
    first = cap.last_new
    assert t0 <= first <= time.time(), first
    time.sleep(0.3)
    assert cap.last_new == first, "no new frames after the camera stopped: last_new must stand still"
    cap.release()
finally:
    am.cv2.VideoCapture = real
print("ALL PASS")
