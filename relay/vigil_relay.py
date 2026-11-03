#!/usr/bin/env python3
"""
VIGIL relay box: forwards one Tapo camera's video to VIGIL Cloud.

Runs on a Raspberry Pi Zero 2 W on the same Wi-Fi as the camera. It does no
AI and stores no video: it finds the camera, then copies its stream to the
cloud, encrypted (SRT with a per-camera passphrase). If SRT cannot get out of
the network, it falls back to RTMP. Anything that fails is retried forever.

Settings: /etc/vigil-relay.env (written by install.sh from the cloud's
relay_<camera>.env plus the Tapo camera account).

The green light on the board tells a non-technical person what is happening:
  steady on        sending video to VIGIL
  slow blink       cannot find the camera on the Wi-Fi
  fast blink       found the camera but cannot reach VIGIL Cloud (internet)
  double blink     Wi-Fi setup mode: join "VIGIL-Setup" on a phone
"""
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ENV_FILE = os.environ.get("VIGIL_RELAY_ENV", "/etc/vigil-relay.env")
STATE_DIR = os.environ.get("VIGIL_RELAY_STATE", "/var/lib/vigil-relay")
STATUS_FILE = os.path.join(STATE_DIR, "status.json")


def load_env(path=ENV_FILE):
    cfg = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("VIGIL_")})
    return cfg


def log(msg):
    print("[relay] %s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


# ------------------------------------------------------------------ status light
class Light:
    PATTERNS = {"ok": None, "no_camera": (1.0, 1.0), "no_cloud": (0.15, 0.15),
                "setup": "double", "starting": (0.5, 0.5)}

    def __init__(self):
        self.state = "starting"
        self.path = None
        for name in ("ACT", "led0"):
            p = "/sys/class/leds/%s" % name
            if os.path.isdir(p):
                self.path = p
                break
        if self.path:
            try:
                with open(self.path + "/trigger", "w") as f:
                    f.write("none")
            except OSError:
                self.path = None
        threading.Thread(target=self._run, daemon=True).start()

    def _set(self, on):
        if not self.path:
            return
        try:
            with open(self.path + "/brightness", "w") as f:
                # The Zero 2 W's ACT LED is active-low on some kernels; "on" means lit either way here.
                f.write("1" if on else "0")
        except OSError:
            pass

    def _run(self):
        while True:
            p = self.PATTERNS.get(self.state)
            if p is None:
                self._set(True); time.sleep(0.5)
            elif p == "double":
                for _ in range(2):
                    self._set(True); time.sleep(0.12); self._set(False); time.sleep(0.12)
                time.sleep(0.8)
            else:
                self._set(True); time.sleep(p[0]); self._set(False); time.sleep(p[1])


def write_status(**kw):
    kw["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATUS_FILE + ".tmp", "w") as f:
            json.dump(kw, f)
        os.replace(STATUS_FILE + ".tmp", STATUS_FILE)
    except OSError:
        pass


# ------------------------------------------------------------------ finding the camera
def local_networks():
    """IPv4 /24 networks this box is on (skips loopback and the setup hotspot)."""
    nets = set()
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[1] != "lo":
                iface = ipaddress.ip_interface(parts[3])
                net = ipaddress.ip_network("%s/24" % iface.ip, strict=False)
                if not str(net).startswith("10.42.0."):       # our own setup hotspot
                    nets.add(net)
    except Exception:
        pass
    return sorted(nets)


def port_open(ip, port=554, timeout=0.6):
    try:
        with socket.create_connection((str(ip), port), timeout=timeout):
            return True
    except OSError:
        return False


def rtsp_url(cfg, ip):
    return "rtsp://%s:%s@%s:554/%s" % (cfg.get("VIGIL_TAPO_USER", ""), cfg.get("VIGIL_TAPO_PASS", ""), ip,
                                       cfg.get("VIGIL_TAPO_STREAM", "stream1"))


def is_camera(cfg, ip):
    """True if this address answers as an RTSP camera with our camera account."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-rw_timeout", "6000000",
                            "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
                            "-of", "json", rtsp_url(cfg, ip)], capture_output=True, text=True, timeout=15)
        info = json.loads(r.stdout or "{}").get("streams") or []
        return bool(info), (info[0] if info else {})
    except Exception:
        return False, {}


def find_camera(cfg):
    remembered = os.path.join(STATE_DIR, "camera_ip")
    candidates = []
    for ip in (cfg.get("VIGIL_TAPO_IP"), _read(remembered)):
        if ip and ip not in candidates:
            candidates.append(ip)
    for ip in candidates:
        if port_open(ip):
            ok, info = is_camera(cfg, ip)
            if ok:
                return ip, info
    hosts = [str(h) for n in local_networks() for h in n.hosts()]
    with ThreadPoolExecutor(max_workers=64) as ex:
        open_ips = [h for h, ok in zip(hosts, ex.map(port_open, hosts)) if ok]
    for ip in open_ips:
        ok, info = is_camera(cfg, ip)
        if ok:
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                with open(remembered, "w") as f:
                    f.write(ip)
            except OSError:
                pass
            return ip, info
    return None, {}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# ------------------------------------------------------------------ sending
def srt_target(cfg):
    sid = "publish:%s:%s:%s" % (cfg["VIGIL_CAMERA_ID"], cfg["VIGIL_STREAM_USER"], cfg["VIGIL_STREAM_PASS"])
    return ("srt://%s:%s?streamid=%s&passphrase=%s&pbkeylen=16&latency=400000&mode=caller"
            % (cfg["VIGIL_CLOUD_HOST"], cfg.get("VIGIL_SRT_PORT", "8890"), sid, cfg["VIGIL_SRT_PASSPHRASE"]))


def ffmpeg_cmd(cfg, ip, mode):
    src = cfg.get("VIGIL_SOURCE_URL") or rtsp_url(cfg, ip)
    inp = ["-rtsp_transport", "tcp", "-rw_timeout", "10000000"] if src.startswith("rtsp") else ["-re", "-stream_loop", "-1"]
    base = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"] + inp + ["-i", src,
            "-map", "0:v:0", "-c:v", "copy", "-an"]
    if mode == "srt":
        return base + ["-f", "mpegts", srt_target(cfg)]
    return base + ["-f", "flv", cfg["VIGIL_RTMP_FALLBACK"]]


def stream(cfg, ip, mode, light):
    """Run one ffmpeg until it stops. Returns seconds it ran."""
    t0 = time.time()
    log("sending camera %s to VIGIL Cloud by %s" % (ip, mode.upper()))
    p = subprocess.Popen(ffmpeg_cmd(cfg, ip, mode), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    marked = False
    last_err = ""
    while p.poll() is None:
        time.sleep(1)
        if not marked and time.time() - t0 > 8:
            marked = True                      # still running after 8 s: the cloud accepted it
            light.state = "ok"
            write_status(state="sending", camera=ip, mode=mode, since=time.strftime("%H:%M:%S"))
    try:
        last_err = (p.stderr.read() or "").strip().splitlines()[-1:] or [""]
        last_err = last_err[0][:200]
    except Exception:
        pass
    ran = time.time() - t0
    log("stream stopped after %.0f s%s" % (ran, (": " + last_err) if last_err else ""))
    return ran


def internet_ok(cfg):
    try:
        socket.getaddrinfo(cfg["VIGIL_CLOUD_HOST"], 443)
        with socket.create_connection((cfg["VIGIL_CLOUD_HOST"], 443), timeout=5):
            return True
    except OSError:
        return False


def main():
    cfg = load_env()
    need = ["VIGIL_CAMERA_ID", "VIGIL_CLOUD_HOST", "VIGIL_STREAM_USER", "VIGIL_STREAM_PASS",
            "VIGIL_SRT_PASSPHRASE", "VIGIL_TAPO_USER", "VIGIL_TAPO_PASS"]
    missing = [k for k in need if not cfg.get(k)]
    if missing:
        log("settings missing in %s: %s" % (ENV_FILE, ", ".join(missing)))
        sys.exit(2)
    light = Light()
    mode = cfg.get("VIGIL_TRANSPORT", "srt")
    srt_fails = 0
    fallback_t = 0.0
    while True:
        cfg = load_env()
        if os.path.exists("/run/vigil-relay/setup-mode"):
            light.state = "setup"
            write_status(state="wifi_setup")
            time.sleep(5)
            continue
        if cfg.get("VIGIL_SOURCE_URL"):       # any other camera or a test file: no search needed
            ip, info = "configured", {}
        else:
            ip, info = find_camera(cfg)
        if not ip:
            light.state = "no_camera"
            write_status(state="no_camera")
            log("camera not found on the Wi-Fi; looking again in 20 s")
            time.sleep(20)
            continue
        if not internet_ok(cfg):
            light.state = "no_cloud"
            write_status(state="no_internet", camera=ip)
            log("camera found at %s but VIGIL Cloud is unreachable; retrying in 15 s" % ip)
            time.sleep(15)
            continue
        ran = stream(cfg, ip, mode, light)
        if ran < 8:
            light.state = "no_cloud"
            if mode == "srt":
                srt_fails += 1
                if srt_fails >= 3 and cfg.get("VIGIL_RTMP_FALLBACK"):
                    log("SRT is blocked on this network; switching to RTMP")
                    mode = "rtmp"
                    fallback_t = time.time()
            time.sleep(5)
        else:
            srt_fails = 0
            time.sleep(2)
        if mode == "rtmp" and time.time() - fallback_t > 3600:
            mode = cfg.get("VIGIL_TRANSPORT", "srt")       # try encrypted again every hour
            srt_fails = 0


if __name__ == "__main__":
    main()
