"""ALWAYS_ON_V1: the relay box repairs its own connection and never strands
itself in setup mode after a hiccup."""
import os
import sys
import tempfile

d = tempfile.mkdtemp()
env = os.path.join(d, "vigil-relay.env")
with open(env, "w") as f:
    f.write("VIGIL_CAMERA_ID=home01\nVIGIL_SETUP_CODE=K7Q2-M9XD\nVIGIL_TAPO_USER=vigil\nVIGIL_TAPO_PASS=x\n")
os.environ["VIGIL_RELAY_ENV"] = env
os.environ["VIGIL_LAST_REBOOT"] = os.path.join(d, "last_reboot")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
import wifi_setup as ws  # noqa: E402

W = {"assoc": True, "internet": True, "gateway": True, "saved": ["LalsT"], "visible": ["LalsT"], "clients": 0}
calls = []


def fake_sh(*cmd, timeout=30):
    calls.append(cmd)
    if cmd[:4] == ("nmcli", "-t", "-f", "TYPE,STATE"):
        return 0, ("wifi:connected\n" if W["assoc"] else "wifi:disconnected\n") + \
            ("ethernet:connected\n" if W.get("eth") else "ethernet:unavailable\n")
    if cmd[:4] == ("nmcli", "-t", "-f", "NAME,TYPE"):
        return 0, "".join("%s:802-11-wireless\n" % n for n in W["saved"])
    if cmd[0] == "ping":
        tgt = cmd[-1]
        if tgt in ("1.1.1.1", "8.8.8.8"):
            return (0 if (W["assoc"] or W.get("eth")) and W["internet"] else 1), ""
        return (0 if (W["assoc"] or W.get("eth")) and W["gateway"] else 1), ""
    if cmd[:3] == ("ip", "route", "show"):
        return 0, "default via 192.168.12.1 dev wlan0"
    if cmd[:4] == ("nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY"):
        return 0, "".join("%s:70:WPA2\n" % n for n in W["visible"])
    if cmd[:4] == ("iw", "dev", "wlan0", "station"):
        return 0, "Station aa:bb\n" * W["clients"]
    return 0, ""


ws.sh = fake_sh
ws.time = type("T", (), {"sleep": staticmethod(lambda s: None), "time": staticmethod(lambda: 0)})
ws.FLAG = os.path.join(d, "setup-mode")


def did(prefix):
    return [c for c in calls if c[:len(prefix)] == prefix]


def reset():
    calls.clear()
    ws.NET.update(offline_since=None, level=0, hotspot_since=None)
    ws.NET["retries_fixed"].clear()
    W.update(assoc=True, internet=True, gateway=True, saved=["LalsT"], visible=["LalsT"], clients=0, eth=False)
    if os.path.exists(ws.FLAG):
        os.remove(ws.FLAG)


def test_online_and_retry_forever():
    reset()
    assert ws.tick(1000) == "internet"
    mod = did(("nmcli", "connection", "modify", "LalsT"))
    assert mod and "connection.autoconnect-retries" in mod[0] and mod[0][mod[0].index("connection.autoconnect-retries") + 1] == "0"


def test_internet_outage_is_not_the_box():
    reset()
    W.update(internet=False)                       # router fine, ISP down
    for t in range(0, 3600, 15):
        assert ws.tick(1000 + t) == "local"
    assert not did(("systemctl", "reboot")) and not did(("modprobe",)) and not os.path.exists(ws.FLAG)


def test_wifi_down_escalates_then_reboots_once():
    reset()
    W.update(assoc=False)
    got = [ws.tick(t) for t in range(1000, 1000 + 660, 15)]
    assert got.index("reconnect") < got.index("radio restarted") < got.index("driver reloaded") < got.index("reboot")
    assert got.count("reboot") == 1 and not os.path.exists(ws.FLAG)
    # it rebooted 1 minute ago and is still down: repairs repeat, no second reboot for 20 minutes
    ws.NET.update(offline_since=None, level=0)
    got = [ws.tick(t) for t in range(1700, 1700 + 900, 15)]
    assert "reboot" not in got and "driver reloaded" in got and "repairs again" in got
    W.update(assoc=True)
    assert ws.tick(2700) == "internet" and ws.NET["level"] == 0


def test_never_stranded_in_setup_after_a_hiccup():
    reset()
    W.update(assoc=False)
    got = [ws.tick(t) for t in range(1000, 1000 + 300, 15)]
    assert not any(g.startswith("setup") for g in got) and not os.path.exists(ws.FLAG)


def test_moved_house_opens_setup_then_keeps_looking():
    reset()
    open(os.environ["VIGIL_LAST_REBOOT"], "w").write("0")
    W.update(assoc=False, visible=["Neighbour"])
    got = [ws.tick(t) for t in range(10000, 10000 + 660, 15)]
    assert "setup: saved wifi gone" in got and os.path.exists(ws.FLAG)
    ws.NET["hotspot_since"] = 20000
    W.update(clients=1)                            # someone is setting it up: stay
    assert ws.tick(20400) == "hotspot"
    W.update(clients=0)
    assert ws.tick(20400) == "left hotspot to look for saved Wi-Fi" and not os.path.exists(ws.FLAG)


def test_setup_needed():
    reset()
    W.update(saved=[])
    assert ws.tick(1000) == "setup: no wifi" and os.path.exists(ws.FLAG)
    assert ws.tick(1015) == "hotspot"


def test_cable_only_needs_no_wifi():
    """ETHERNET_V1: a box on a network cable with no Wi-Fi saved just works."""
    reset()
    W.update(saved=[], assoc=False, eth=True)
    got = [ws.tick(t) for t in range(1000, 1000 + 900, 15)]
    assert set(got) == {"internet"}, set(got)
    assert not os.path.exists(ws.FLAG) and not did(("modprobe",)) and not did(("systemctl", "reboot"))


def test_cable_box_leaves_setup_hotspot():
    reset()
    W.update(saved=[], assoc=False, eth=True)
    open(ws.FLAG, "w").close(); ws.NET["hotspot_since"] = 1000
    assert ws.tick(1400) == "left hotspot to look for saved Wi-Fi" and not os.path.exists(ws.FLAG)
    assert ws.tick(1415) == "internet"


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
