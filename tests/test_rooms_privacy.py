"""ROOMS_V1 / PRIVACY_LOCK_V1 / VIEW_LOG_V1 / PLAN_V1: one call lists every room with
what needs attention; an owner can lock a camera to stick figures; opening video is
written down for everyone with access to see; each home has a plan that is only a record."""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
SEEN = []            # (path, view) each fake camera was asked for
STATE = {"home01": {"unseen": 0, "unseen_falls": 0, "present": True},
         "home02": {"unseen": 2, "unseen_falls": 1, "present": False},
         "home03": {"unseen": 1, "unseen_falls": 0, "present": False}}


def make(site):
    class Cam(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            u = urlparse(self.path); q = parse_qs(u.query)
            SEEN.append((site, u.path, (q.get("view") or [None])[0]))
            s = STATE[site]
            body = {"/alerts/unseen": {"unseen": s["unseen"], "unseen_falls": s["unseen_falls"], "oldest_unseen_at": None},
                    "/camera/status": {"online": True, "person_present": s["present"]}}.get(u.path, {"ok": True})
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(body).encode())
    return Cam


PORTS = {"home01": 5191, "home02": 5192, "home03": 5193}
for sid, port in PORTS.items():
    threading.Thread(target=HTTPServer(("127.0.0.1", port), make(sid)).serve_forever, daemon=True).start()

os.makedirs(os.path.join(TMP, "config"))
json.dump({"sites": [{"id": sid, "name": sid, "api_key": "k", "setup_code": code, "internal_url": "http://127.0.0.1:%d" % PORTS[sid]}
                     for sid, code in (("home01", "K7Q2-M9XD"), ("home02", "P3R8-T6YB"), ("home03", "W4N6-H2ZC"))]},
          open(os.path.join(TMP, "config", "sites.json"), "w"))
os.environ.update(VIGIL_CONFIG=os.path.join(TMP, "config"), VIGIL_DATA=os.path.join(TMP, "data"), VIGIL_WATCHDOG="0")
sys.path.insert(0, os.path.join(HERE, "..", "cloud", "gateway"))
import gateway  # noqa: E402
C = gateway.app.test_client()


def user(email, name, codes):
    t = C.post("/auth/signup", json={"email": email, "password": "good password", "name": name}).json["token"]
    for c in codes:
        C.post("/auth/claim", json={"code": c}, headers={"X-VIGIL-Key": t})
    return {"X-VIGIL-Key": t}


def test_all():
    owner = user("o@x.com", "Olga", ["K7Q2M9XD", "P3R8T6YB", "W4N6H2ZC"])
    carer = user("c@x.com", "Carl", ["K7Q2M9XD"])
    other = user("z@x.com", "Zed", [])

    # --- rooms board: the fall first, then other alerts, then quiet rooms; only your own cameras
    r = C.get("/overview", headers=owner).json
    assert [x["id"] for x in r["rooms"]] == ["home02", "home03", "home01"], [x["id"] for x in r["rooms"]]
    assert r["open_falls"] == 1 and r["rooms"][0]["unseen_falls"] == 1 and r["rooms"][2]["person_present"] is True, r
    assert [x["id"] for x in C.get("/overview", headers=carer).json["rooms"]] == ["home01"]
    assert C.get("/overview", headers=other).json["rooms"] == []
    assert C.get("/overview").status_code == 401

    # --- video views are written down once per person per 10 minutes, figures are not
    SEEN.clear()
    C.get("/s/home01/camera/stream64?view=skeleton", headers=carer)
    assert C.get("/auth/site/home01/views", headers=owner).json["views"] == []
    C.get("/s/home01/camera/stream64?view=both", headers=carer)
    C.get("/s/home01/camera/snapshot", headers=carer)
    v = C.get("/auth/site/home01/views", headers=owner).json
    assert [x["name"] for x in v["views"]] == ["Carl"] and v["video_locked"] is False, v
    assert C.get("/auth/site/home01/views", headers=carer).json["views"][0]["name"] == "Carl"     # carers see it too
    assert C.get("/auth/site/home01/views", headers=other).status_code == 403

    # --- lock: only the owner can; after it the camera is only ever asked for figures
    assert C.post("/auth/site/home01/settings", json={"video_locked": True}, headers=carer).status_code == 403
    r = C.post("/auth/site/home01/settings", json={"video_locked": True}, headers=owner).json
    assert r["site"]["video_locked"] is True and r["site"]["name"] == "home01", r
    SEEN.clear()
    C.get("/s/home01/camera/stream64?view=both", headers=owner)
    C.get("/s/home01/camera/snapshot?w=800", headers=owner)
    assert [x[2] for x in SEEN] == ["skeleton", "skeleton"], SEEN
    assert len(C.get("/auth/site/home01/views", headers=owner).json["views"]) == 1                 # nothing new: no video was sent
    assert C.post("/auth/login", json={"email": "c@x.com", "password": "good password"}).json["sites"][0]["video_locked"] is True
    # other cameras are untouched, and it can be unlocked
    C.get("/s/home02/camera/stream64?view=both", headers=owner)
    assert SEEN[-1] == ("home02", "/camera/stream64", "both"), SEEN[-1]
    assert C.post("/auth/site/home01/settings", json={"video_locked": False}, headers=owner).json["site"]["video_locked"] is False

    # --- plan: pilot unless set; shown to the app; never blocks anything
    assert C.get("/sites/status", headers=owner).json["sites"][0]["plan"] == "pilot"
    db = gateway.sqlite3.connect(gateway.DB_PATH)
    db.execute("INSERT OR REPLACE INTO site_prefs VALUES ('home02','plan','past_due')"); db.commit(); db.close()
    s2 = [x for x in C.get("/sites/status", headers=owner).json["sites"] if x["id"] == "home02"][0]
    assert s2["plan"] == "past_due", s2
    assert C.get("/s/home02/alerts/unseen", headers=owner).json["unseen"] == 2                     # still monitored
    print("ALL PASS")


if __name__ == "__main__":
    test_all()
