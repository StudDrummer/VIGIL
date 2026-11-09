"""LIVE_SMOOTH_V1: the live view is drawn from the camera's own frames about
10 times a second (only while someone watches), and the server streams each
new frame to the app as one base64 line."""
import base64
import os
import sys
import tempfile
import threading
import time

import cv2
import numpy as np

home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.makedirs(os.path.join(home, "passive-health-moniter"))
os.environ.setdefault("VIGIL_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import types  # noqa: E402
sys.modules.setdefault("ultralytics", types.SimpleNamespace(YOLO=object))
import activity_monitor as am  # noqa: E402
import Server  # noqa: E402

for f in os.listdir("/dev/shm"):
    if f.startswith("vigil_live"):
        os.remove(os.path.join("/dev/shm", f))


class FakeCap:
    """Stands in for ThreadedCapture: a reader thread swapping in new 2K frames at 15 fps."""
    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._seq = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            fr = np.full((1296, 2304, 3), (self._seq * 7) % 255, np.uint8)
            with self._lock:
                self._frame = fr
                self._seq += 1
            time.sleep(1 / 15)


class T:
    def __init__(self, a): self.a = np.asarray(a)
    def cpu(self): return self
    def numpy(self): return self.a


class Boxes:
    xyxy = T([[400, 200, 900, 1200]])
    id = T([3])


class KP:
    data = T(np.dstack([np.linspace(450, 850, 17), np.linspace(250, 1150, 17), np.full(17, 0.9)]))


class R:
    boxes = Boxes()
    keypoints = KP()


am._vigil_names = lambda: {}
fails = 0
def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + (" " + detail if detail else ""))
    fails += 0 if ok else 1


# 1. nothing drawn while no one watches
am._LIVE["cap"] = FakeCap()
am._live_start()
time.sleep(0.8)
check("idle: no live frames without a viewer", not os.path.exists("/dev/shm/vigil_live_video.jpg"))

# 2. watching video: ~10 fps of 640-wide frames with the overlay
open("/dev/shm/vigil_live_want_video", "a").close(); os.utime("/dev/shm/vigil_live_want_video", None)
am._LIVE["want_chk"] = 0
am._live_update([R()], (1296, 2304, 3))
check("overlay recorded", am._LIVE["ov"] and len(am._LIVE["ov"]["items"]) == 1)
seen = set(); t0 = time.time()
while time.time() - t0 < 2.0:
    try:
        seen.add(os.stat("/dev/shm/vigil_live_video.jpg").st_mtime_ns)
    except OSError:
        pass
    am._live_update([R()], (1296, 2304, 3))
    time.sleep(0.01)
check("about 10 frames a second", len(seen) >= 16, "(%d in 2 s)" % len(seen))
img = cv2.imread("/dev/shm/vigil_live_video.jpg")
check("640 wide", img is not None and img.shape[1] == 640, str(None if img is None else img.shape))
sz = os.path.getsize("/dev/shm/vigil_live_video.jpg")
check("small enough to stream", sz < 60000, "(%d bytes)" % sz)

# 3. the server streams each new frame as one line
c = Server.app.test_client()
r = c.get("/camera/stream64?view=video&max_s=5", headers={"X-VIGIL-Key": os.environ["VIGIL_API_KEY"]}, buffered=False)
check("stream answers 200", r.status_code == 200, str(r.status_code))
buf = b""; frames = 0; t0 = time.time()
for chunk in r.response:
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        if line:
            jpg = base64.b64decode(line)
            assert jpg[:2] == b"\xff\xd8" and jpg[-2:] == b"\xff\xd9"
            frames += 1
    am._live_update([R()], (1296, 2304, 3))
    if time.time() - t0 > 3:
        break
r.close()
rate = frames / max(0.1, time.time() - t0)
check("stream delivers ~10 frames a second", rate >= 7, "(%.1f/s)" % rate)

# 4. skeleton view on request, snapshot uses the live frame
r = c.get("/camera/stream64?view=skeleton&max_s=5", headers={"X-VIGIL-Key": os.environ["VIGIL_API_KEY"]}, buffered=False)
got = None; buf = b""; t0 = time.time()
for chunk in r.response:
    buf += chunk
    if b"\n" in buf:
        line = buf.split(b"\n", 1)[0]
        if line:
            got = line; break
        buf = buf.split(b"\n", 1)[1]
    if time.time() - t0 > 3:
        break
r.close()
sk = cv2.imdecode(np.frombuffer(base64.b64decode(got), np.uint8), cv2.IMREAD_COLOR) if got else None
check("skeleton view streams a dark stick-figure frame", sk is not None and sk.mean() < 60,
      "" if sk is None else "(mean %.0f)" % sk.mean())
s = c.get("/camera/snapshot?w=800", headers={"X-VIGIL-Key": os.environ["VIGIL_API_KEY"]}).get_json()
check("snapshot serves the live frame", s.get("available") and s.get("view") == "video")
print("ALL PASS" if not fails else "%d FAILED" % fails)
sys.exit(1 if fails else 0)
