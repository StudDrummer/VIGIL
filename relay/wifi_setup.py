#!/usr/bin/env python3
"""
VIGIL relay Wi-Fi setup, for a home whose Wi-Fi the box does not know yet.

If the box has no working Wi-Fi 90 seconds after starting, it opens its own
Wi-Fi network called "VIGIL-Setup" (password: vigilsetup). Join it with any
phone; a page opens (or go to http://10.42.0.1) listing nearby networks. Pick
the home Wi-Fi, type its password, tap Connect. The box joins it and starts
sending video; the green light goes steady within a few minutes.

Uses NetworkManager (Raspberry Pi OS Bookworm and later).
"""
import html
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

FLAG = "/run/vigil-relay/setup-mode"
SSID = "VIGIL-Setup"
PASSWORD = "vigilsetup"
NETWORKS = []
MSG = [""]


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
        if len(parts) == 3 and parts[0] and parts[0] != SSID:
            name, sig = parts[0].replace("\\:", ":"), int(parts[1] or 0)
            seen[name] = max(sig, seen.get(name, 0))
    return sorted(seen.items(), key=lambda t: -t[1])


def hotspot_on():
    os.makedirs(os.path.dirname(FLAG), exist_ok=True)
    open(FLAG, "w").close()
    sh("nmcli", "device", "wifi", "hotspot", "ifname", "wlan0", "con-name", "vigil-setup",
       "ssid", SSID, "password", PASSWORD)


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

    def do_GET(self):
        self._page()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        q = parse_qs(self.rfile.read(n).decode())
        ssid = (q.get("ssid") or [""])[0]
        pw = (q.get("password") or [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<meta name=viewport content='width=device-width'><h2>Connecting...</h2>"
                         b"<p>Wait 3 minutes. If the green light stays on, VIGIL is working.</p>")
        threading.Thread(target=try_join, args=(ssid, pw), daemon=True).start()


def try_join(ssid, pw):
    time.sleep(2)
    hotspot_off()
    args = ["nmcli", "device", "wifi", "connect", ssid] + (["password", pw] if pw else [])
    rc, out = sh(*args, timeout=60)
    time.sleep(10)
    if rc == 0 and online():
        MSG[0] = ""
        print("[wifi-setup] joined %s" % ssid, flush=True)
        return
    print("[wifi-setup] could not join %s: %s" % (ssid, out.strip()[:200]), flush=True)
    sh("nmcli", "connection", "delete", ssid)
    MSG[0] = "Could not join %s. Check the password and try again." % ssid
    hotspot_on()


def main():
    time.sleep(int(os.environ.get("VIGIL_WIFI_WAIT_S", "90")))
    srv = None
    while True:
        if online():
            if os.path.exists(FLAG):
                hotspot_off()
            time.sleep(30)
            continue
        if not os.path.exists(FLAG):
            NETWORKS[:] = scan()
            print("[wifi-setup] no Wi-Fi; opening %s" % SSID, flush=True)
            hotspot_on()
            if srv is None:
                srv = HTTPServer(("0.0.0.0", 80), H)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(20)


if __name__ == "__main__":
    main()
