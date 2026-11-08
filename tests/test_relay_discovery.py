"""Relay box: finds the Tapo by scanning the home network, remembers it, and
uses the remembered address first next time."""
import ipaddress
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
os.environ["VIGIL_RELAY_STATE"] = tempfile.mkdtemp()
import vigil_relay as vr  # noqa: E402

CAM = "192.168.1.57"
probed = []
vr.local_networks = lambda: [ipaddress.ip_network("192.168.1.0/24")]
vr.port_open = lambda ip, port=554, timeout=0.6: str(ip) in (CAM, "192.168.1.1")   # router also has 554 open
def fake_is_camera(cfg, ip):
    probed.append(ip)
    return (ip == CAM), ({"codec_name": "h264"} if ip == CAM else {})
vr.is_camera = fake_is_camera


def test_scan_then_remember():
    cfg = {"VIGIL_TAPO_USER": "u", "VIGIL_TAPO_PASS": "p"}
    ip, _ = vr.find_camera(cfg)
    assert ip == CAM
    assert set(probed) == {"192.168.1.1", CAM}           # only hosts with RTSP open were tried
    probed.clear()
    ip, _ = vr.find_camera(cfg)
    assert ip == CAM and probed == [CAM]                  # remembered address tried first, no scan


def test_srt_target_has_encryption():
    t = vr.srt_target({"VIGIL_CAMERA_ID": "home01", "VIGIL_STREAM_USER": "cam_home01", "VIGIL_STREAM_PASS": "x",
                       "VIGIL_SRT_PASSPHRASE": "abc", "VIGIL_CLOUD_HOST": "api.vigilhealth.me"})
    assert "passphrase=abc" in t and "streamid=publish:home01:cam_home01:x" in t


def _arp_frame(ip, mac):
    m = bytes(int(x, 16) for x in mac.split(":"))
    return (b"\xff" * 6 + m + b"\x08\x06" + b"\x00\x01\x08\x00\x06\x04\x00\x01" + m
            + ipaddress.IPv4Address(ip).packed + b"\x00" * 6 + ipaddress.IPv4Address("192.168.1.1").packed)


def test_parse_frames():
    nets = [ipaddress.ip_network("192.168.1.0/24")]
    assert vr.parse_frame(_arp_frame(CAM, "ac:a7:f1:fd:42:92"), nets) == (CAM, "ac:a7:f1:fd:42:92")
    assert vr.parse_frame(_arp_frame("10.0.0.5", "ac:a7:f1:fd:42:92"), nets) is None     # other network
    m = bytes.fromhex("aca7f1fd4292")
    mdns = (b"\x01\x00\x5e\x00\x00\xfb" + m + b"\x08\x00" + b"\x45" + b"\x00" * 11
            + ipaddress.IPv4Address(CAM).packed + ipaddress.IPv4Address("224.0.0.251").packed)
    assert vr.parse_frame(mdns, nets) == (CAM, "ac:a7:f1:fd:42:92")
    unicast = b"\x02" + mdns[1:]
    assert vr.parse_frame(unicast, nets) is None


def test_finds_camera_when_address_lookups_fail():
    """Like the T-Mobile house: the camera never answers ARP, so the scan finds
    nothing, but it announces itself and pinning its address makes it reachable."""
    import shutil
    shutil.rmtree(vr.STATE_DIR, ignore_errors=True)
    mac = "ac:a7:f1:fd:42:92"
    pins = {}
    old = (vr.port_open, vr.pin, vr.unpin, vr.neigh_mac, vr.listen_for_devices)
    vr.port_open = lambda ip, port=554, timeout=0.6: str(ip) == "192.168.1.1" or pins.get(str(ip)) == mac
    vr.neigh_mac = lambda ip: pins.get(str(ip))
    vr.pin = lambda ip, m: pins.__setitem__(str(ip), m) or True
    vr.unpin = lambda ip: pins.pop(str(ip), None)
    vr.listen_for_devices = lambda s, nets: {"192.168.1.20": "11:22:33:44:55:66", CAM: mac}
    try:
        probed.clear()
        ip, _ = vr.find_camera({"VIGIL_TAPO_USER": "u", "VIGIL_TAPO_PASS": "p"})
        assert ip == CAM and pins == {CAM: mac}, (ip, pins)        # the phone's pin was taken back off
        assert vr.remembered() == (CAM, mac)
        # after a reboot the pin is gone; the remembered hardware address puts it straight back
        pins.clear()
        vr.listen_for_devices = lambda s, nets: (_ for _ in ()).throw(AssertionError("should not need to listen"))
        ip, _ = vr.find_camera({"VIGIL_TAPO_USER": "u", "VIGIL_TAPO_PASS": "p"})
        assert ip == CAM and pins == {CAM: mac}
    finally:
        vr.port_open, vr.pin, vr.unpin, vr.neigh_mac, vr.listen_for_devices = old


def test_finds_camera_by_hardware_address_alone():
    """The camera never announces itself and its address changed, but the box
    was told its hardware address (VIGIL_TAPO_MAC)."""
    import shutil
    import types
    shutil.rmtree(vr.STATE_DIR, ignore_errors=True)
    mac = "ac:a7:f1:fd:42:92"
    new_ip = "192.168.1.88"
    pins = {"192.168.1.1": "aa:aa:aa:aa:aa:aa", "192.168.1.30": "11:22:33:44:55:66"}   # router, a phone
    real_run = vr.subprocess.run

    def fake_run(cmd, input=None, **kw):
        if cmd[:2] == ["ip", "-batch"]:
            for line in input.splitlines():
                w = line.split()
                if w[1] == "replace":
                    pins[w[2]] = w[4]
                elif w[1] == "del":
                    pins.pop(w[2], None)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    old = (vr.neigh_table, vr.port_open, vr.neigh_mac, vr.iface_for, vr.own_ips, vr.gateway, vr.listen_for_devices, vr.is_camera)
    vr.subprocess.run = fake_run
    vr.port_open = lambda ip, port=554, timeout=0.6: str(ip) == new_ip and pins.get(new_ip) == mac
    vr.neigh_mac = lambda ip: pins.get(str(ip))
    vr.neigh_table = lambda: dict(pins)
    vr.iface_for = lambda ip: "wlan0"
    vr.own_ips = lambda: ["192.168.1.40"]
    vr.gateway = lambda: "192.168.1.1"
    vr.listen_for_devices = lambda s, nets: {}
    vr.is_camera = lambda cfg, ip: (ip == new_ip, {"width": 1920})
    try:
        ip, info = vr.find_camera({"VIGIL_TAPO_USER": "u", "VIGIL_TAPO_PASS": "p", "VIGIL_TAPO_MAC": mac,
                                   "VIGIL_TAPO_IP": CAM})
        assert ip == new_ip, ip
        # only the camera stays pinned; the router and phone entries were never touched
        assert pins == {"192.168.1.1": "aa:aa:aa:aa:aa:aa", "192.168.1.30": "11:22:33:44:55:66", new_ip: mac}, pins
        assert vr.remembered() == (new_ip, mac)
    finally:
        vr.subprocess.run = real_run
        (vr.neigh_table, vr.port_open, vr.neigh_mac, vr.iface_for, vr.own_ips, vr.gateway, vr.listen_for_devices, vr.is_camera) = old


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
