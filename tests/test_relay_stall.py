"""STALL_V1: a stream that stops moving is restarted; a chatty ffmpeg never freezes."""
import os
import sys
import tempfile
import time

os.environ.update(VIGIL_RELAY_STATE=tempfile.mkdtemp(), VIGIL_STALL_S="2", VIGIL_STALL_GRACE_S="1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
import vigil_relay as vr  # noqa: E402

d = tempfile.mkdtemp()
FAKE = os.path.join(d, "fake_ffmpeg.py")
open(FAKE, "w").write("""import sys, time
for i in range(6):                       # 3 s of video, plus lots of warnings
    print("total_size=%d" % (i * 1000), flush=True)
    for _ in range(400):
        sys.stderr.write("[warning] non-monotonic DTS, this line is long enough to fill a pipe quickly\\n")
    sys.stderr.flush()
    time.sleep(0.5)
time.sleep(600)                          # then hangs, like a dead SRT connection
""")
vr.report = lambda kw: None


class L:
    state = ""


def test_stall_is_detected():
    vr.ffmpeg_cmd = lambda cfg, ip, mode: [sys.executable, FAKE]
    t0 = time.time()
    ran = vr.stream({}, "1.2.3.4", "srt", L())
    took = time.time() - t0
    assert 3 <= took < 15, took                 # did not hang for 600 s, and did not stop while video moved
    assert ran >= 8                             # counted as "worked, then stalled", not "SRT blocked"


if __name__ == "__main__":
    test_stall_is_detected(); print("ok")
