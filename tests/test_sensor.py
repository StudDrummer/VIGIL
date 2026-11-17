"""SENSOR_V1: a bathroom radar's fall becomes one ordinary fall alert; while the same fall
is still reported nothing more is sent; after "clear" a new fall alerts again."""
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
Server.FALL_GAP_SEC = 0
C = Server.app.test_client()
H = {"X-VIGIL-Key": os.environ["VIGIL_API_KEY"]}


def falls():
    return [a for a in C.get("/alerts/all", headers=H).get_json() if a["alert_type"] == "fall"]


def test_sensor():
    ev = lambda e: C.post("/sensor/event", json={"sensor_id": "bath1", "event": e, "room": "Bathroom"}, headers=H).get_json()
    assert C.post("/sensor/event", json={"sensor_id": "bath1", "event": "boom"}, headers=H).status_code == 400
    assert C.post("/sensor/event", json={"sensor_id": "bath1", "event": "fall"}).status_code in (401, 403)
    assert ev("heartbeat")["alerted"] is False and falls() == []
    assert ev("fall")["alerted"] is True
    for _ in range(20):
        assert ev("fall")["alerted"] is False                       # the radar keeps saying it: one alert
    assert len(falls()) == 1 and falls()[0]["payload"]["where"] == "Bathroom", falls()
    ev("clear"); Server._FALL_BURST.clear()
    assert ev("fall")["alerted"] is True and len(falls()) == 2
    s = C.get("/sensors", headers=H).get_json()
    assert s[0]["sensor_id"] == "bath1" and s[0]["last_event"] == "fall", s


if __name__ == "__main__":
    test_sensor(); print("ok")
