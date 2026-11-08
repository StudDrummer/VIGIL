"""APP_SETUP_V1: the VIGIL app sets up the box over its own Wi-Fi: it checks the
setup code, saves the camera account into the relay settings (keeping every
other line), and joins the home Wi-Fi."""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import HTTPServer

d = tempfile.mkdtemp()
env = os.path.join(d, "vigil-relay.env")
with open(env, "w") as f:
    f.write("VIGIL_CAMERA_ID=home01\nVIGIL_CLOUD_HOST=api.vigilhealth.me\nVIGIL_SETUP_CODE=K7Q2-M9XD\nVIGIL_TAPO_MAC=ac:a7:f1:fd:42:92\n")
os.environ["VIGIL_RELAY_ENV"] = env
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
import wifi_setup as ws  # noqa: E402

calls = []
NET = {"online": False}


def fake_sh(*cmd, timeout=30):
    calls.append(cmd)
    if cmd[:4] == ("nmcli", "-t", "-f", "NAME,TYPE"):
        return 0, "vigil-setup:802-11-wireless\n"
    if cmd[:4] == ("nmcli", "device", "wifi", "connect"):
        ok = "password" in cmd and cmd[cmd.index("password") + 1] == "right"
        NET["online"] = ok
        return (0, "ok") if ok else (4, "Secrets were required")
    return 0, ""


ws.sh = fake_sh
ws.online = lambda: NET["online"]
ws.time.sleep = lambda s: None
srv = HTTPServer(("127.0.0.1", 0), ws.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_port


def req(path, body=None):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_join():
    for _ in range(100):
        if not ws.STATE["joining"]:
            return
        time.sleep(0.02)


def test_names_and_password():
    assert ws.ssid_name() == "VIGIL-home01" and ws.hotspot_password() == "K7Q2M9XD"


def test_setup_flow():
    ws.NETWORKS[:] = [("Sharma Home", 80), ("Neighbour", 30)]
    st, info = req("/api/info")
    assert st == 200 and info["camera_id"] == "home01" and not info["has_camera_account"] and info["camera_mac_known"]
    assert req("/api/networks")[1]["networks"][0]["ssid"] == "Sharma Home"
    assert req("/api/setup", {"code": "WRONG123", "ssid": "Sharma Home"})[0] == 403
    assert req("/api/setup", {"code": "k7q2-m9xd", "ssid": "Sharma Home", "wifi_password": "x"})[0] == 400   # no camera account
    st, r = req("/api/setup", {"code": "k7q2-m9xd", "ssid": "Sharma Home", "wifi_password": "wrong",
                               "tapo_user": "vigil", "tapo_pass": "Cam-pass-123"})
    assert st == 200
    wait_join()
    cfg = ws.load_env()
    assert cfg["VIGIL_TAPO_USER"] == "vigil" and cfg["VIGIL_TAPO_PASS"] == "Cam-pass-123"
    assert cfg["VIGIL_TAPO_MAC"] == "ac:a7:f1:fd:42:92" and cfg["VIGIL_CLOUD_HOST"] == "api.vigilhealth.me"
    assert "Check the Wi-Fi password" in req("/api/info")[1]["last_error"]           # wrong password reported
    st, r = req("/api/setup", {"code": "K7Q2M9XD", "ssid": "Sharma Home", "wifi_password": "right"})
    assert st == 200                                                                  # account already saved
    wait_join()
    info = req("/api/info")[1]
    assert info["last_error"] == "" and info["has_camera_account"] and NET["online"]
    assert oct(os.stat(env).st_mode)[-3:] == "600"


if __name__ == "__main__":
    test_names_and_password(); test_setup_flow(); print("ok")
