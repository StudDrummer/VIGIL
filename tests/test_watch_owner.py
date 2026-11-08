"""WATCH_OWNER_V2: Apple Watch numbers are about the person who wears the watch,
never the resident by default; camera activity is about everyone at home."""
import os
import sys
import tempfile
from datetime import datetime, timedelta

home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.makedirs(os.path.join(home, "passive-health-moniter"))
os.environ.setdefault("VIGIL_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import Server  # noqa: E402

Server.init_db()
C = Server.app.test_client()
H = {"X-VIGIL-Key": os.environ["VIGIL_API_KEY"]}
sms = []
Server.send_sms = lambda m, *a, **k: sms.append(m)


def setup_days():
    c = Server.get_db()
    c.execute("UPDATE config SET resident_name='Shivani' WHERE id=1")
    for pid, name, res in (("p_shivani", "Shivani", 1), ("p_rushil", "Rushil", 0)):
        c.execute("INSERT INTO persons (id, name, is_resident, room_id) VALUES (?,?,?,'room_1')", (pid, name, res))
    for i in range(30):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        rhr = 90 if i < 3 else 60 + (i % 3)        # last 3 days clearly up
        c.execute("INSERT INTO daily_summary (date, resting_hr, activity_level_pct) VALUES (?,?,?)",
                  (d, rhr, 40 + (i % 4)))
    c.commit(); c.close()


def test_watch_signals_follow_the_owner():
    setup_days()
    sig = C.get("/signals/latest", headers=H).json["signals"]
    # nobody said whose watch it is: no heart-rate signal at all, activity is for the home
    assert not [s for s in sig if s["metric"] == "resting_hr"], sig
    act = [s for s in sig if s["metric"] == "activity_level_pct"]
    assert act and act[0]["who"] == "Everyone at home"
    r = C.post("/config/watch-owner", json={"person_id": "p_rushil"}, headers=H).json
    assert r["watch_owner"] == "Rushil"
    sig = C.get("/signals/latest", headers=H).json["signals"]
    hr = [s for s in sig if s["metric"] == "resting_hr"]
    assert hr and hr[0]["who"] == "Rushil", hr


def test_daily_alert_names_the_wearer():
    Server._check_anomalies(Server.get_db(), datetime.now().strftime("%Y-%m-%d"))
    assert sms and all("Shivani" not in m for m in sms), sms
    assert any("Rushil's resting hr" in m for m in sms), sms


if __name__ == "__main__":
    test_watch_signals_follow_the_owner(); test_daily_alert_names_the_wearer(); print("ok")
