"""ACCOUNTS_V1 / ALERT_EMAIL_V1, end to end against a real SMTP sink and a
fake camera site: sign up, add a camera by setup code, profile, password
reset by emailed code, and an alert email for a new fall."""
import json
import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiosmtpd.controller import Controller

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
MAIL = []


class Sink:
    async def handle_DATA(self, server, session, envelope):
        MAIL.append((envelope.rcpt_tos[0], envelope.content.decode("utf-8", "replace")))
        return "250 OK"


smtp = Controller(Sink(), hostname="127.0.0.1", port=8025)
smtp.start()

ALERTS = [{"id": 1, "alert_type": "fall", "detected_at": "2026-11-03T10:00:00", "payload": {}}]


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("X-VIGIL-Key") != "sitekey":
            self.send_response(401); self.end_headers(); return
        body = json.dumps(ALERTS if self.path.startswith("/alerts/all") else {"online": True, "last_seen_minutes_ago": 0.1})
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(body.encode())


site = HTTPServer(("127.0.0.1", 5099), Site)
threading.Thread(target=site.serve_forever, daemon=True).start()

os.makedirs(os.path.join(TMP, "config"))
json.dump({"sites": [{"id": "home01", "name": "Nani - bedroom", "api_key": "sitekey", "ntfy_topic": "",
                      "setup_code": "K7Q2-M9XD", "internal_url": "http://127.0.0.1:5099"}]},
          open(os.path.join(TMP, "config", "sites.json"), "w"))
os.environ.update(VIGIL_CONFIG=os.path.join(TMP, "config"), VIGIL_DATA=os.path.join(TMP, "data"),
                  VIGIL_WATCHDOG="0", VIGIL_SMTP_HOST="127.0.0.1", VIGIL_SMTP_PORT="8025",
                  VIGIL_SMTP_FROM="VIGIL <alerts@vigilhealth.me>")
sys.path.insert(0, os.path.join(HERE, "..", "cloud", "gateway"))
import gateway  # noqa: E402

C = gateway.app.test_client()


def wait_mail(n, t=10):
    end = time.time() + t
    while len(MAIL) < n and time.time() < end:
        time.sleep(0.1)
    return len(MAIL) >= n


def test_everything():
    r = C.post("/auth/signup", json={"email": "Dad@Example.com", "password": "short", "name": "Dad"})
    assert r.status_code == 400
    r = C.post("/auth/signup", json={"email": "Dad@Example.com", "password": "good password", "name": "Dad",
                                     "phone": "+91 98000 00000"})
    assert r.status_code == 200, r.json
    tok = r.json["token"]
    H = {"X-VIGIL-Key": tok}
    assert wait_mail(1) and "Welcome to VIGIL" in MAIL[-1][1]
    assert C.post("/auth/signup", json={"email": "dad@example.com", "password": "another one", "name": "x"}).status_code == 409
    # no camera yet; wrong code; right code (spaces, lower case, no dash all fine)
    assert C.get("/s/home01/camera/status", headers=H).status_code == 403
    assert C.post("/auth/claim", json={"code": "AAAA-BBBB"}, headers=H).status_code == 404
    r = C.post("/auth/claim", json={"code": "k7q2 m9xd"}, headers=H)
    assert r.status_code == 200 and r.json["sites"][0]["id"] == "home01", r.json
    assert r.json["role"] == "owner", r.json
    assert C.get("/s/home01/camera/status", headers=H).json["online"] is True
    # second family member with the same code becomes a caregiver
    r2 = C.post("/auth/signup", json={"email": "mom@example.com", "password": "good password", "name": "Mom"})
    r3 = C.post("/auth/claim", json={"code": "K7Q2M9XD"}, headers={"X-VIGIL-Key": r2.json["token"]})
    assert r3.status_code == 200 and r3.json["role"] == "caregiver", r3.json
    roles = dict(gateway.sqlite3.connect(gateway.DB_PATH).execute(
        "SELECT u.email, g.role FROM grants g JOIN users u ON u.id=g.user_id").fetchall())
    assert roles == {"dad@example.com": "owner", "mom@example.com": "caregiver"}, roles
    # profile saved and returned on every sign-in
    C.post("/auth/profile", json={"name": "Papa", "email_alerts": True}, headers=H)
    r = C.post("/auth/login", json={"email": "dad@example.com", "password": "good password"})
    assert r.json["user"]["name"] == "Papa" and r.json["user"]["phone"] == "+91 98000 00000" and r.json["sites"]
    # forgot password: code by email, wrong code refused, right code works
    n = len(MAIL)
    assert C.post("/auth/forgot", json={"email": "dad@example.com"}).json["status"] == "ok"
    assert C.post("/auth/forgot", json={"email": "nobody@example.com"}).json["status"] == "ok"
    assert wait_mail(n + 1)
    code = re.search(r"code is (\d{6})", MAIL[-1][1]).group(1)
    assert C.post("/auth/reset", json={"email": "dad@example.com", "code": "000000",
                                       "new_password": "new password 1"}).status_code == 400
    r = C.post("/auth/reset", json={"email": "dad@example.com", "code": code, "new_password": "new password 1"})
    assert r.status_code == 200 and r.json["token"]
    assert C.post("/auth/login", json={"email": "dad@example.com", "password": "good password"}).status_code == 401
    assert C.post("/auth/login", json={"email": "dad@example.com", "password": "new password 1"}).status_code == 200
    assert C.get("/auth/me", headers=H).status_code == 401          # old sessions ended by the reset
    # alerts: first pass only records where we are; a new fall is emailed to both
    gateway._alert_email_once()
    n = len(MAIL)
    ALERTS.insert(0, {"id": 2, "alert_type": "fall", "detected_at": "2026-11-03T10:05:00", "payload": {"message": "x"}})
    gateway._alert_email_once()
    assert wait_mail(n + 2)
    got = {to: body for to, body in MAIL[n:]}
    assert set(got) == {"dad@example.com", "mom@example.com"}
    assert "Possible fall" in got["dad@example.com"] and "Nani - bedroom" in got["dad@example.com"]
    # turning email alerts off stops them for that person only
    tok3 = C.post("/auth/login", json={"email": "mom@example.com", "password": "good password"}).json["token"]
    C.post("/auth/profile", json={"email_alerts": False}, headers={"X-VIGIL-Key": tok3})
    n = len(MAIL)
    ALERTS.insert(0, {"id": 3, "alert_type": "stillness", "detected_at": "2026-11-03T11:00:00", "payload": {}})
    gateway._alert_email_once()
    assert wait_mail(n + 1)
    time.sleep(1)
    assert [to for to, _ in MAIL[n:]] == ["dad@example.com"]


if __name__ == "__main__":
    test_everything()
    print("ok: %d emails sent" % len(MAIL))
    smtp.stop()
