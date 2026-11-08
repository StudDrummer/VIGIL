#!/usr/bin/env python3
"""
VIGIL relay Wi-Fi setup, for a home whose Wi-Fi the box does not know yet.

If the box has no working Wi-Fi 90 seconds after starting, or does not have
the camera account yet, it opens its own Wi-Fi network called "VIGIL-<camera>"
(for example VIGIL-home01). Its password is the setup code on the box's card,
without the dash. The VIGIL app joins it and sends the home Wi-Fi and the
camera account in one step (APP_SETUP_V1, JSON at http://10.42.0.1/api/...).
A browser page at http://10.42.0.1 still works as a fallback.

Uses NetworkManager (Raspberry Pi OS Bookworm and later).
"""
import html
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

FLAG = "/run/vigil-relay/setup-mode"
ENV_FILE = os.environ.get("VIGIL_RELAY_ENV", "/etc/vigil-relay.env")
NETWORKS = []
MSG = [""]
STATE = {"joining": None, "last_error": "", "joined": ""}


def load_env(path=None):
    cfg = {}
    try:
        with open(path or ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg


def _code(cfg=None):
    return "".join(ch for ch in (cfg or load_env()).get("VIGIL_SETUP_CODE", "") if ch.isalnum()).upper()


def ssid_name(cfg=None):
    cid = (cfg or load_env()).get("VIGIL_CAMERA_ID", "")
    return ("VIGIL-%s" % cid) if cid else "VIGIL-Setup"


def hotspot_password(cfg=None):
    c = _code(cfg)
    return c if len(c) >= 8 else "vigilsetup"


def has_camera_account(cfg=None):
    cfg = cfg or load_env()
    return bool(cfg.get("VIGIL_TAPO_USER") and cfg.get("VIGIL_TAPO_PASS"))


def save_camera_account(user, password, path=None):
    """Writes the Tapo camera account into the relay settings, keeping every other line."""
    path = path or ENV_FILE
    try:
        with open(path) as f:
            lines = [l for l in f.read().splitlines()
                     if not l.startswith("VIGIL_TAPO_USER=") and not l.startswith("VIGIL_TAPO_PASS=")]
    except OSError:
        lines = []
    lines += ["VIGIL_TAPO_USER=%s" % user, "VIGIL_TAPO_PASS=%s" % password]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def sh(*cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def online():
    rc, out = sh("nmcli", "-t", "-f", "TYPE,STATE", "device")
    if "wifi:connected" not in out:
        return False
    rc, _ = sh("ping", "-c", "1", "-W", "3", "1.1.1.1", timeout=6)
    return rc == 0


def scan():
    sh("nmcli", "device", "wifi", "rescan", timeout=15)
    time.sleep(3)
    rc, out = sh("nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list")
    seen = {}
    for line in out.splitlines():
        parts = line.rsplit(":", 2)
        if len(parts) == 3 and parts[0] and not parts[0].startswith("VIGIL-"):
            name, sig = parts[0].replace("\\:", ":"), int(parts[1] or 0)
            seen[name] = max(sig, seen.get(name, 0))
    return sorted(seen.items(), key=lambda t: -t[1])


def hotspot_on():
    os.makedirs(os.path.dirname(FLAG), exist_ok=True)
    open(FLAG, "w").close()
    sh("nmcli", "device", "wifi", "hotspot", "ifname", "wlan0", "con-name", "vigil-setup",
       "ssid", ssid_name(), "password", hotspot_password())


def hotspot_off():
    sh("nmcli", "connection", "down", "vigil-setup")
    sh("nmcli", "connection", "delete", "vigil-setup")
    try:
        os.remove(FLAG)
    except OSError:
        pass


PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>VIGIL setup</title><style>
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0b0f0d;color:#f2f6f3;margin:0;padding:24px;max-width:480px}
h1{font-size:26px}p{color:#a3b0a9;font-size:17px;line-height:1.5}
select,input,button{width:100%%;font-size:19px;padding:14px;margin:8px 0;border-radius:12px;border:1px solid #2c3a33;box-sizing:border-box}
select,input{background:#141d19;color:#f2f6f3}button{background:#32d74b;color:#000;font-weight:700;border:0}
.msg{background:#3a2a10;color:#ffcf7a;padding:12px;border-radius:10px}</style></head><body>
<h1>Connect VIGIL to Wi-Fi</h1>
<p>Choose the home Wi-Fi and type its password.</p>%s
<form method=post><select name=ssid>%s</select>
<input name=password type=password placeholder="Wi-Fi password" autocomplete=off>
<button>Connect</button></form>
<p>After you tap Connect, this page will stop working. That is normal. Wait 3 minutes:
when the green light on the VIGIL box stays on, it is working.</p></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _page(self):
        opts = "".join("<option>%s</option>" % html.escape(n) for n, _s in NETWORKS) or "<option>(no networks found)</option>"
        msg = ('<p class=msg>%s</p>' % html.escape(MSG[0])) if MSG[0] else ""
        body = (PAGE % (msg, opts)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/info"):
            cfg = load_env()
            return self._json(200, {"box": "vigil-relay", "camera_id": cfg.get("VIGIL_CAMERA_ID", ""),
                                    "has_camera_account": has_camera_account(cfg),
                                    "camera_mac_known": bool(cfg.get("VIGIL_TAPO_MAC")),
                                    "joining": STATE["joining"], "last_error": STATE["last_error"] or MSG[0],
                                    "known_wifi": known_wifi()})
        if self.path.startswith("/api/networks"):
            return self._json(200, {"networks": [{"ssid": n, "signal": s_} for n, s_ in NETWORKS]})
        self._page()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if self.path.startswith("/api/setup"):
            try:
                body = json.loads(self.rfile.read(n).decode() or "{}")
            except ValueError:
                return self._json(400, {"status": "error", "reason": "bad request"})
            code = "".join(ch for ch in str(body.get("code", "")) if ch.isalnum()).upper()
            if _code() and code != _code():
                return self._json(403, {"status": "error", "reason": "that setup code is not this box's"})
            user, pw = (body.get("tapo_user") or "").strip(), body.get("tapo_pass") or ""
            if user and pw:
                save_camera_account(user, pw)
            elif not has_camera_account():
                return self._json(400, {"status": "error", "reason": "the camera account is missing"})
            ssid = (body.get("ssid") or "").strip()
            if not ssid and not known_wifi():
                return self._json(400, {"status": "error", "reason": "choose the home Wi-Fi"})
            STATE.update(joining=ssid or "(known)", last_error="")
            self._json(200, {"status": "ok", "message": "joining"})
            threading.Thread(target=try_join, args=(ssid, body.get("wifi_password") or ""), daemon=True).start()
            return
        q = parse_qs(self.rfile.read(n).decode())
        ssid = (q.get("ssid") or [""])[0]
        pw = (q.get("password") or [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<meta name=viewport content='width=device-width'><h2>Connecting...</h2>"
                         b"<p>Wait 3 minutes. If the green light stays on, VIGIL is working.</p>")
        threading.Thread(target=try_join, args=(ssid, pw), daemon=True).start()


def known_wifi():
    rc, out = sh("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")
    return [l.split(":")[0] for l in out.splitlines() if ":802-11-wireless" in l and not l.startswith("vigil-setup:")]


def try_join(ssid, pw):
    time.sleep(2)
    hotspot_off()
    if ssid:
        args = ["nmcli", "device", "wifi", "connect", ssid] + (["password", pw] if pw else [])
        rc, out = sh(*args, timeout=60)
    else:
        rc, out = 0, ""
        time.sleep(20)                     # let NetworkManager rejoin a Wi-Fi it already knows
    time.sleep(10)
    if rc == 0 and online():
        MSG[0] = ""
        STATE.update(joining=None, last_error="", joined=ssid)
        print("[wifi-setup] joined %s" % (ssid or "known Wi-Fi"), flush=True)
        return
    print("[wifi-setup] could not join %s: %s" % (ssid, out.strip()[:200]), flush=True)
    if ssid:
        sh("nmcli", "connection", "delete", ssid)
    MSG[0] = ("Could not join %s. Check the Wi-Fi password and try again." % ssid) if ssid else \
        "The box could not get online. Choose the home Wi-Fi."
    STATE.update(joining=None, last_error=MSG[0])
    hotspot_on()


def main():
    time.sleep(int(os.environ.get("VIGIL_WIFI_WAIT_S", "90")))
    srv = None
    while True:
        ready = has_camera_account()
        if STATE["joining"]:
            time.sleep(5)
            continue
        if online() and ready:
            if os.path.exists(FLAG):
                hotspot_off()
            time.sleep(30)
            continue
        if not os.path.exists(FLAG):
            NETWORKS[:] = scan()
            print("[wifi-setup] %s; opening %s" % ("no Wi-Fi" if ready else "no camera account yet", ssid_name()),
                  flush=True)
            hotspot_on()
            if srv is None:
                srv = HTTPServer(("0.0.0.0", 80), H)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(20)


if __name__ == "__main__":
    main()
