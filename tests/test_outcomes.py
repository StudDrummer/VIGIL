"""OUTCOME_V1: caregivers record what each fall alert turned out to be, and falls
VIGIL missed, so false alarms and misses in real use are counted, not guessed."""
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


def fall(minutes_ago, ack_after_s=None):
    c = Server.get_db()
    Server._ensure_ack_columns(c)
    t = datetime.now() - timedelta(minutes=minutes_ago)
    cur = c.execute("INSERT INTO alerts (alert_type, detected_at, payload) VALUES ('fall', ?, '{}')", (t.isoformat(),))
    if ack_after_s is not None:
        c.execute("UPDATE alerts SET seen=1, acknowledged_by='Ana', acknowledged_at=? WHERE id=?",
                  ((t + timedelta(seconds=ack_after_s)).strftime("%Y-%m-%d %H:%M:%S"), cur.lastrowid))
    c.commit(); c.close()
    return cur.lastrowid


def test_outcomes_and_accuracy():
    a = fall(300, ack_after_s=40); b = fall(200, ack_after_s=100); c = fall(100, ack_after_s=70); fall(50)
    assert C.post("/alerts/%d/outcome" % a, json={"outcome": "real", "by": "Ana"}, headers=H).status_code == 200
    assert C.post("/alerts/%d/outcome" % b, json={"outcome": "false_alarm", "note": "sat down fast"}, headers=H).status_code == 200
    assert C.post("/alerts/%d/outcome" % c, json={"outcome": "unsure"}, headers=H).status_code == 200
    assert C.post("/alerts/%d/outcome" % a, json={"outcome": "maybe"}, headers=H).status_code == 400
    assert C.post("/alerts/99999/outcome", json={"outcome": "real"}, headers=H).status_code == 404
    assert C.post("/alerts/%d/outcome" % a, json={"outcome": "real"}).status_code in (401, 403)      # no key
    r = C.get("/alerts/accuracy?days=30", headers=H).get_json()
    assert (r["fall_alerts"], r["confirmed_real"], r["false_alarms"], r["unsure"], r["not_reviewed"]) == (4, 1, 1, 1, 1), r
    assert r["median_response_s"] == 70, r
    rows = {x["id"]: x for x in C.get("/alerts/all", headers=H).get_json()}
    assert rows[b]["outcome"] == "false_alarm" and rows[b]["outcome_note"] == "sat down fast"


def test_missed_fall_is_a_record_not_an_alert():
    sent = []
    Server.send_push = lambda *a, **k: sent.append(a)
    before = C.get("/alerts/unseen", headers=H).get_json()["unseen"]
    assert C.post("/alerts/missed", json={"when": datetime.now().isoformat(), "by": "Ana", "note": "bathroom door"},
                  headers=H).status_code == 200
    assert C.post("/alerts/missed", json={"when": "yesterday-ish"}, headers=H).status_code == 400
    assert C.get("/alerts/unseen", headers=H).get_json()["unseen"] == before      # nothing new to acknowledge
    assert not sent                                                                 # and nobody is paged
    r = C.get("/alerts/accuracy", headers=H).get_json()
    assert r["missed_reported"] == 1 and r["fall_alerts"] == 4, r


def test_old_alerts_fall_out_of_the_window():
    c = Server.get_db()
    c.execute("INSERT INTO alerts (alert_type, detected_at, payload) VALUES ('fall', ?, '{}')",
              ((datetime.now() - timedelta(days=45)).isoformat(),))
    c.commit(); c.close()
    assert C.get("/alerts/accuracy?days=30", headers=H).get_json()["fall_alerts"] == 4
    assert C.get("/alerts/accuracy?days=60", headers=H).get_json()["fall_alerts"] == 5


if __name__ == "__main__":
    test_outcomes_and_accuracy(); test_missed_fall_is_a_record_not_an_alert(); test_old_alerts_fall_out_of_the_window()
    print("ok")
