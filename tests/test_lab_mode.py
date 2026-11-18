"""LAB_MODE_V1: during the fall lab, falls are stored as lab tests, every trial is kept (no cap),
nobody is notified, they never count in accuracy, and a forgotten flag expires by itself."""
import json
import os
import sys
import tempfile
import time

home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.makedirs(os.path.join(home, "passive-health-moniter"))
os.environ.setdefault("VIGIL_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import Server  # noqa: E402

Server.init_db()
SENT = []
Server.send_sms = lambda *a, **k: SENT.append(a)
Server.LAB_FLAG = os.path.join(home, "vigil_lab")
C = Server.app.test_client()
H = {"X-VIGIL-Key": os.environ["VIGIL_API_KEY"]}


def rows():
    c = Server.get_db()
    r = [dict(x) for x in c.execute("SELECT * FROM alerts WHERE alert_type='fall' ORDER BY id")]
    c.close()
    return r


def test_lab_falls_are_quiet_and_all_kept():
    open(Server.LAB_FLAG, "w").write("1")
    codes = [C.post("/alert/fall", json={"message": "Fall detected"}, headers=H).status_code for _ in range(7)]
    assert codes == [200] * 7, codes                       # no 30 s gap, no half-hour cap in the lab
    r = rows()
    assert len(r) == 7 and all(x["seen"] == 1 for x in r)  # seen: never escalated
    assert all(json.loads(x["payload"])["lab"] is True for x in r)
    assert all("TEST" in x["payload"].upper() for x in r)  # the push loop skips TEST payloads
    assert SENT == []
    acc = C.get("/alerts/accuracy", headers=H).get_json()
    assert acc["fall_alerts"] == 0, acc                    # lab trials never count


def test_stale_flag_expires():
    os.utime(Server.LAB_FLAG, (time.time() - 21 * 60,) * 2)
    assert not Server._lab_mode()
    C.post("/alert/fall", json={"message": "Fall detected"}, headers=H)
    assert "lab" not in json.loads(rows()[-1]["payload"])   # a real fall after a forgotten lab is a real alert
    assert len(SENT) == 1


if __name__ == "__main__":
    test_lab_falls_are_quiet_and_all_kept()
    test_stale_flag_expires()
    print("lab mode: OK")
