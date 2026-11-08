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


# Some home routers (and some cameras in power save) drop the "who has this
# address?" question (ARP), so a device on Wi-Fi gets "No route to host" even
# though the camera is right there. The camera still announces itself now and
# then (its own ARP and multicast), so the relay listens for that, learns the
# camera's hardware address, and tells Linux the answer directly ("pins" it).
ARP_LISTEN_S = float(os.environ.get("VIGIL_ARP_LISTEN_S", "90"))
_pinned = set()


def iface_for(ip):
    try:
        out = subprocess.run(["ip", "route", "get", str(ip)], capture_output=True, text=True, timeout=5).stdout.split()
        return out[out.index("dev") + 1]
    except Exception:
        return None


def neigh_mac(ip):
    """The hardware address Linux has for ip right now, or None."""
    try:
        out = subprocess.run(["ip", "neigh", "show", str(ip)], capture_output=True, text=True, timeout=5).stdout.split()
        if "lladdr" in out and "FAILED" not in out and "INCOMPLETE" not in out:
            return out[out.index("lladdr") + 1].lower()
    except Exception:
        pass
    return None


def neigh_table():
    """{ip: mac} for every address Linux currently has a good answer for."""
    table = {}
    try:
        out = subprocess.run(["ip", "-4", "neigh", "show"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            w = line.split()
            if w and "lladdr" in w and "FAILED" not in w and "INCOMPLETE" not in w:
                table[w[0]] = w[w.index("lladdr") + 1].lower()
    except Exception:
        pass
    return table


def pin(ip, mac):
    dev = iface_for(ip)
    if not dev or not mac:
        return False
    r = subprocess.run(["ip", "neigh", "replace", str(ip), "lladdr", mac, "dev", dev, "nud", "permanent"],
                       capture_output=True, text=True, timeout=5)
    if r.returncode == 0:
        _pinned.add(str(ip))
        return True
    return False


def unpin(ip):
    if str(ip) in _pinned:
        dev = iface_for(ip)
        if dev:
            subprocess.run(["ip", "neigh", "del", str(ip), "dev", dev], capture_output=True, timeout=5)
        _pinned.discard(str(ip))


def parse_frame(frame, nets):
    """(ip, mac) of the sender of one Ethernet frame, if it is an ARP packet or an
    IPv4 broadcast/multicast from a device on our networks."""
    if len(frame) < 34:
        return None
    etype = int.from_bytes(frame[12:14], "big")
    mac = None
    ip = None
    if etype == 0x0806 and len(frame) >= 42:
        mac = frame[22:28]
        ip = ipaddress.IPv4Address(frame[28:32])
    elif etype == 0x0800 and (frame[0] & 1):          # multicast or broadcast destination
        mac = frame[6:12]
        ip = ipaddress.IPv4Address(frame[26:30])
    if ip is None or mac == b"\x00" * 6 or (mac[0] & 1) or ip.is_unspecified:
        return None
    if not any(ip in n for n in nets):
        return None
    return str(ip), ":".join("%02x" % b for b in mac)


def listen_for_devices(seconds, nets):
    """Listen quietly and return {ip: mac} for every device heard announcing itself."""
    seen = {}
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
    except (OSError, AttributeError) as e:
        log("cannot listen for devices (%s)" % e)
        return seen
    s.settimeout(1.0)
    end = time.time() + seconds
    try:
        while time.time() < end:
            try:
                frame, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            if len(addr) > 2 and addr[2] == 4:          # our own outgoing packet
                continue
            got = parse_frame(frame, nets)
            if got:
                seen[got[0]] = got[1]
    finally:
        s.close()
    return seen


def remember(ip, mac):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "camera.json"), "w") as f:
            json.dump({"ip": ip, "mac": mac}, f)
        with open(os.path.join(STATE_DIR, "camera_ip"), "w") as f:
            f.write(ip)
    except OSError:
        pass


def remembered():
    try:
        with open(os.path.join(STATE_DIR, "camera.json")) as f:
            d = json.load(f)
        return d.get("ip"), (d.get("mac") or "").lower() or None
    except (OSError, ValueError):
        return _read(os.path.join(STATE_DIR, "camera_ip")), None


def _try(cfg, ip, mac=None):
    """Is the camera at ip? With a mac, pin it first so a lost ARP answer cannot stop us."""
    pinned_now = False
    if mac and neigh_mac(ip) != mac:
        pinned_now = pin(ip, mac)
    if port_open(ip):
        ok, info = is_camera(cfg, ip)
        if ok:
            remember(ip, mac or neigh_mac(ip))
            return True, info
    if pinned_now:
        unpin(ip)
    return False, {}


def own_ips():
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
        return [l.split()[3].split("/")[0] for l in out.splitlines() if len(l.split()) >= 4]
    except Exception:
        return []


def gateway():
    try:
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5).stdout.split()
        return out[out.index("via") + 1]
    except Exception:
        return None


def sweep_for_mac(cfg, mac, nets, skip=()):
    """The camera's hardware address is known but not its address (it may have
    changed). Point every address that has no owner at the camera's hardware
    address for a moment: only the camera's real address will answer."""
    hosts = [str(h) for n in nets for h in n.hosts()]
    known = neigh_table()
    free = [h for h in hosts if h not in skip and not known.get(h)]
    dev = iface_for(free[0]) if free else None
    if not dev:
        return None, {}
    cmds = "".join("neigh replace %s lladdr %s dev %s nud permanent\n" % (h, mac, dev) for h in free)
    subprocess.run(["ip", "-batch", "-"], input=cmds, capture_output=True, text=True, timeout=30)
    _pinned.update(free)
    try:
        with ThreadPoolExecutor(max_workers=64) as ex:
            open_ips = [h for h, ok in zip(free, ex.map(lambda h: port_open(h, timeout=1.5), free)) if ok]
        for ip in open_ips:
            ok, info = is_camera(cfg, ip)
            if ok:
                _pinned.discard(ip)                  # keep this one
                remember(ip, mac)
                return ip, info
        return None, {}
    finally:
        rest = [h for h in free if h in _pinned]
        cmds = "".join("neigh del %s dev %s\n" % (h, dev) for h in rest)
        subprocess.run(["ip", "-batch", "-"], input=cmds, capture_output=True, text=True, timeout=30)
        _pinned.difference_update(rest)


def find_camera(cfg):
    r_ip, r_mac = remembered()
    mac_cfg = (cfg.get("VIGIL_TAPO_MAC") or "").lower().replace("-", ":") or None
    known_mac = mac_cfg or r_mac
    candidates = []
    for ip in (cfg.get("VIGIL_TAPO_IP"), r_ip):
        if ip and ip not in candidates:
            candidates.append(ip)
    # 1. the address we know, pinned to the camera's hardware address if we know it
    for ip in candidates:
        ok, info = _try(cfg, ip, known_mac)
        if ok:
            return ip, info
    for ip in candidates:                       # a stale pin must not block normal lookup
        unpin(ip)
    # 2. ordinary scan of the home network
    nets = local_networks()
    hosts = [str(h) for n in nets for h in n.hosts()]
    with ThreadPoolExecutor(max_workers=64) as ex:
        open_ips = [h for h, ok in zip(hosts, ex.map(port_open, hosts)) if ok]
    for ip in open_ips:
        ok, info = _try(cfg, ip)
        if ok:
            return ip, info
    # 3. we know the camera's hardware address: find which address it has now
    if known_mac:
        log("camera not answering address lookups; searching by its hardware address %s" % known_mac)
        mine = set(own_ips())
        ip, info = sweep_for_mac(cfg, known_mac, nets, skip=mine | {gateway()})
        if ip:
            log("found the camera at %s by its hardware address; address pinned" % ip)
            return ip, info
    # 4. listen for the camera announcing itself
    log("camera not answering address lookups; listening for it for %.0f s" % ARP_LISTEN_S)
    heard = listen_for_devices(ARP_LISTEN_S, nets)
    mine = set(own_ips()) | {gateway()}
    order = sorted(heard.items(), key=lambda kv: (kv[1] != known_mac, kv[0]))   # known camera first
    for ip, mac in order:
        if ip in mine or ip in open_ips:
            continue
        ok, info = _try(cfg, ip, mac)
        if ok:
            log("found the camera at %s (%s) by listening; address pinned" % (ip, mac))
            return ip, info
    log("heard %d devices, none was the camera" % len(heard))
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
