"""SITE_NAMES_V1 / COPY_PEOPLE_V1 / PERSON_DETAILS_V1: an owner renames cameras and
groups them into one home; the same family is copied from one camera to another
with their details and photo; a caregiver cannot do either."""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
home = tempfile.mkdtemp(); os.environ["HOME"] = home; os.makedirs(os.path.join(home, "passive-health-moniter"))
os.environ.setdefault("VIGIL_API_KEY", "sitekey")
sys.path.insert(0, os.path.join(HERE, "..", "jetson-backend"))
import Server  # noqa: E402  the real site server
Server.init_db()

SITE_A = Server.app.test_client()


class Proxy(BaseHTTPRequestHandler):
    """Puts the real Flask site behind an HTTP port, as the gateway expects."""
    def log_message(self, *a): pass

    def _go(self, m):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        r = getattr(SITE_A, m)(self.path, data=body, headers={"X-VIGIL-Key": self.headers.get("X-VIGIL-Key", ""),
                                                             "Content-Type": "application/json"})
        self.send_response(r.status_code); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(r.data)

    def do_GET(self): self._go("get")
    def do_POST(self): self._go("post")


srv = HTTPServer(("127.0.0.1", 5097), Proxy)
threading.Thread(target=srv.serve_forever, daemon=True).start()

os.makedirs(os.path.join(TMP, "config"))
json.dump({"sites": [{"id": "home01", "name": "home01", "api_key": "sitekey", "setup_code": "K7Q2-M9XD", "internal_url": "http://127.0.0.1:5097"},
                     {"id": "home02", "name": "home02", "api_key": "sitekey", "setup_code": "P3R8-T6YB", "internal_url": "http://127.0.0.1:5097"}]},
          open(os.path.join(TMP, "config", "sites.json"), "w"))
os.environ.update(VIGIL_CONFIG=os.path.join(TMP, "config"), VIGIL_DATA=os.path.join(TMP, "data"), VIGIL_WATCHDOG="0")
sys.path.insert(0, os.path.join(HERE, "..", "cloud", "gateway"))
import gateway  # noqa: E402
C = gateway.app.test_client()


def test_all():
    t = C.post("/auth/signup", json={"email": "r@x.com", "password": "good password", "name": "R"}).json["token"]
    H = {"X-VIGIL-Key": t}
    C.post("/auth/claim", json={"code": "K7Q2M9XD"}, headers=H); C.post("/auth/claim", json={"code": "P3R8T6YB"}, headers=H)
    m = C.post("/auth/signup", json={"email": "m@x.com", "password": "good password", "name": "M"}).json["token"]
    C.post("/auth/claim", json={"code": "K7Q2M9XD"}, headers={"X-VIGIL-Key": m})
    # rename + same home
    r = C.post("/auth/site/home01/settings", json={"name": "Nani's bedroom", "home": "Nani's flat"}, headers=H)
    assert r.json["site"]["name"] == "Nani's bedroom" and r.json["site"]["home"] == "Nani's flat", r.json
    r = C.post("/auth/site/home02/settings", json={"name": "Living room", "home": "Nani's flat"}, headers=H)
    homes = {s["id"]: s["home"] for s in r.json["sites"]}
    assert homes == {"home01": "Nani's flat", "home02": "Nani's flat"}, homes
    assert C.post("/auth/site/home01/settings", json={"name": "  "}, headers=H).status_code == 400
    assert C.post("/auth/site/home01/settings", json={"name": "x"}, headers={"X-VIGIL-Key": m}).status_code == 403
    assert C.post("/auth/login", json={"email": "m@x.com", "password": "good password"}).json["sites"][0]["name"] == "Nani's bedroom"
    # details: validated, stored
    K = {"X-VIGIL-Key": "sitekey"}
    assert SITE_A.post("/persons/new", json={"name": "Nani", "birth_year": 1800}, headers=K).status_code == 400
    r = SITE_A.post("/persons/new", json={"name": "Nani", "is_resident": True, "height_cm": 152, "birth_year": 1948, "sex": "female",
                                          "weight_kg": 58, "emergency_name": "Ravi", "emergency_phone": "+91 98 000", "photo_b64": "aGVsbG8="}, headers=K)
    pid = r.json["person_id"]
    p = [x for x in SITE_A.get("/persons", headers=K).json if x["id"] == pid][0]
    assert (p["birth_year"], p["sex"], p["weight_kg"], p["emergency_phone"], p["photo_b64"]) == (1948, "female", 58.0, "+91 98 000", "aGVsbG8="), p
    assert SITE_A.post("/persons/%s/details" % pid, json={"weight_kg": 60, "height_cm": 999}, headers=K).status_code == 400
    assert SITE_A.post("/persons/%s/details" % pid, json={"weight_kg": 60}, headers=K).json["status"] == "ok"
    # copy people: both "cameras" share one test server here, so Nani is already there: nothing duplicated
    r = C.post("/auth/site/home02/copy-people", json={"from": "home01"}, headers=H)
    assert r.json["status"] == "ok" and r.json["added"] == [] and r.json["already_there"] == ["Nani"], r.json
    assert C.post("/auth/site/home02/copy-people", json={"from": "home01"}, headers={"X-VIGIL-Key": m}).status_code == 403
    assert C.post("/auth/site/home01/copy-people", json={"from": "home01"}, headers=H).status_code == 400
    # a fresh camera gets her with every detail and her photo
    SITE_A.post("/persons/%s/rename" % pid, json={"name": "Nani"}, headers=K)
    Server.get_db().execute("DELETE FROM persons").connection.commit()
    srcp = [{"name": "Nani", "is_resident": 1, "height_cm": 152, "birth_year": 1948, "sex": "female", "weight_kg": 60,
             "emergency_name": "Ravi", "emergency_phone": "+91 98 000", "photo_b64": "aGVsbG8=", "consent_at": "x"}]
    real_get = gateway.requests.get
    gateway.requests.get = lambda *a, **k: type("R", (), {"json": lambda self: srcp})()
    try:
        r = C.post("/auth/site/home02/copy-people", json={"from": "home01"}, headers=H)
    finally:
        gateway.requests.get = real_get
    assert r.json["added"] == ["Nani"], r.json
    p = SITE_A.get("/persons", headers=K).json[0]
    assert (p["name"], p["is_resident"], p["birth_year"], p["weight_kg"], p["photo_b64"]) == ("Nani", 1, 1948, 60.0, "aGVsbG8="), p
    assert p["consent_at"], p
    print("ALL PASS")


if __name__ == "__main__":
    test_all()
