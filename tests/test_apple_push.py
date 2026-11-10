"""APPLE_PUSH_V1: phones register for alerts; a camera's alert and the gateway's
own offline alerts reach every phone of everyone on that camera through Apple
(mocked), signed with a valid ES256 token; dead tokens are dropped."""
import json
import os
import sys
import tempfile

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.makedirs(os.path.join(TMP, "config"))
json.dump({"sites": [{"id": "home01", "name": "Nani", "api_key": "sitekey1", "ntfy_topic": "", "setup_code": "K7Q2-M9XD",
                      "internal_url": "http://127.0.0.1:1"},
                     {"id": "home02", "name": "Dada", "api_key": "sitekey2", "ntfy_topic": "", "setup_code": "P3R8-T6YB",
                      "internal_url": "http://127.0.0.1:1"}]},
          open(os.path.join(TMP, "config", "sites.json"), "w"))
os.environ.update(VIGIL_CONFIG=os.path.join(TMP, "config"), VIGIL_DATA=os.path.join(TMP, "data"), VIGIL_WATCHDOG="0")
sys.path.insert(0, os.path.join(HERE, "..", "cloud", "gateway"))
import gateway  # noqa: E402

C = gateway.app.test_client()
key = ec.generate_private_key(ec.SECP256R1())
pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
SENT = []


def apple(req):
    SENT.append(req)
    tok = req.url.path.rsplit("/", 1)[1]
    if tok.startswith("dead"):
        return httpx.Response(410, json={"reason": "Unregistered"})
    if tok.startswith("5a") and req.url.host == "api.push.apple.com":     # an Xcode build's phone
        return httpx.Response(400, json={"reason": "BadDeviceToken"})
    return httpx.Response(200)


gateway._APNS["client"] = httpx.Client(transport=httpx.MockTransport(apple))


def user(email, code):
    t = C.post("/auth/signup", json={"email": email, "password": "good password", "name": email}).json["token"]
    C.post("/auth/claim", json={"code": code}, headers={"X-VIGIL-Key": t})
    return {"X-VIGIL-Key": t}


def test_push():
    dad, mom, other = user("dad@x.com", "K7Q2M9XD"), user("mom@x.com", "K7Q2M9XD"), user("o@x.com", "P3R8T6YB")
    assert C.post("/auth/push-token", json={"token": "ab" * 32, "env": "sandbox"}, headers=dad).json["push_ready"] is False
    C.post("/auth/push-token", json={"token": "cd" * 32}, headers=mom)
    C.post("/auth/push-token", json={"token": "dead" + "0" * 60}, headers=mom)
    C.post("/auth/push-token", json={"token": "ef" * 32}, headers=other)
    assert C.post("/auth/push-token", json={"token": "zz"}, headers=dad).status_code == 400
    # not switched on yet: nothing sent, nothing breaks
    assert C.post("/internal/push/home01", json={"title": "t", "message": "m"}, headers={"X-VIGIL-Key": "sitekey1"}).json["phones"] == 0
    open(os.path.join(TMP, "config", "apns_key.p8"), "w").write(pem)
    json.dump({"key_id": "KEY1234567", "team_id": "TEAM123456", "bundle_id": "com.rushilmohan.vigilapp"},
              open(os.path.join(TMP, "config", "apns.json"), "w"))
    # wrong key refused; right key reaches only home01's family
    assert C.post("/internal/push/home01", json={"title": "x"}, headers={"X-VIGIL-Key": "sitekey2"}).status_code == 403
    r = C.post("/internal/push/home01", json={"title": "VIGIL: possible fall", "message": "Open VIGIL", "priority": "urgent", "kind": "fall"},
               headers={"X-VIGIL-Key": "sitekey1"})
    assert r.json["phones"] == 2, r.json
    hosts = sorted(q.url.host for q in SENT)
    assert hosts == ["api.push.apple.com", "api.push.apple.com", "api.sandbox.push.apple.com"], hosts
    q = SENT[-1]
    assert q.headers["apns-topic"] == "com.rushilmohan.vigilapp" and q.headers["apns-push-type"] == "alert"
    claims = jwt.decode(q.headers["authorization"].split()[1], key.public_key(), algorithms=["ES256"])
    assert claims["iss"] == "TEAM123456" and jwt.get_unverified_header(q.headers["authorization"].split()[1])["kid"] == "KEY1234567"
    body = json.loads(q.content)
    assert body["aps"]["alert"]["title"] == "VIGIL: possible fall" and body["aps"]["interruption-level"] == "time-sensitive"
    assert not any(t.url.path.endswith("ef" * 32) for t in SENT)          # other family's phone not told
    left = [r[0] for r in gateway.sqlite3.connect(gateway.DB_PATH).execute("SELECT token FROM push_tokens")]
    assert not any(t.startswith("dead") for t in left)                    # dead phone dropped
    # the gateway's own offline alert goes the same way
    SENT.clear()
    gateway._alert_site(gateway.sites()["home02"], "VIGIL: monitoring offline", "check power", "urgent")
    assert [t.url.path[-64:] for t in SENT] == ["ef" * 32]
    # a phone registered as production that is really an Xcode (sandbox) build still gets it
    SENT.clear()
    C.post("/auth/push-token", json={"token": "5a" * 32}, headers=other)
    gateway._alert_site(gateway.sites()["home02"], "t", "m", "high")
    assert sorted(t.url.host for t in SENT if t.url.path.endswith("5a" * 32)) == ["api.push.apple.com", "api.sandbox.push.apple.com"]
    env = gateway.sqlite3.connect(gateway.DB_PATH).execute("SELECT env FROM push_tokens WHERE token=?", ("5a" * 32,)).fetchone()[0]
    assert env == "sandbox", env
    # sign-out removes the phone
    C.delete("/auth/push-token", json={"token": "ab" * 32}, headers=dad)
    assert ("ab" * 32) not in [r[0] for r in gateway.sqlite3.connect(gateway.DB_PATH).execute("SELECT token FROM push_tokens")]
    print("ALL PASS")


if __name__ == "__main__":
    test_push()
