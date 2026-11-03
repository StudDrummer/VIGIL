"""TODAY_ROBUST_V1: a person's day uses the median of believable readings, so the
frozen and impossible readings seen on Nov 2 (posture 0, asymmetry 94 %) do not
decide the numbers, and too few readings show nothing rather than a guess."""
import os
import sys
import tempfile

home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.makedirs(os.path.join(home, "passive-health-moniter"))
os.environ.setdefault("VIGIL_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import Server  # noqa: E402

Server.init_db()
C = Server.app.test_client()
H = {"X-VIGIL-Key": os.environ["VIGIL_API_KEY"]}


def add(pid, sp, cad, post, asym):
    c = Server.get_db()
    c.execute("INSERT INTO person_detections (person_id, detected_at, gait_speed_ms, cadence_spm, posture_score, asymmetry_pct)"
              " VALUES (?, datetime('now'), ?, ?, ?, ?)", (pid, sp, cad, post, asym))
    c.commit(); c.close()


def test_median_and_filters():
    # Myra's real readings from Nov 2, 17:38 to 17:45
    for r in [(0.675, 83.3, 0.0, 7.3), (0.726, 89.6, 0.0, 31.6), (0.89, 89.9, 0.0, 9.4),
              (0.812, 80.6, 0.0, 17.8), (1.867, 97.6, 61.8, 9.8), (0.547, 104.1, 92.2, 94.1)]:
        add("myra", *r)
    d = C.get("/persons/myra/today", headers=H).json
    assert d["camera_gait_speed_ms"] == 0.77, d          # median of 0.547..1.867
    assert d["camera_cadence_spm"] == 89.8, d
    assert d["posture_score"] is None, d                 # only 2 real postures: not enough
    assert d["camera_asymmetry_pct"] == 9.8, d           # 94.1 dropped
    e = C.get("/persons/nobody/today", headers=H).json
    assert e["camera_gait_speed_ms"] is None and e["camera_data_points"] == 0, e


if __name__ == "__main__":
    test_median_and_filters()
    print("ok")
