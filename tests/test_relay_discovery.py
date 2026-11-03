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


if __name__ == "__main__":
    test_scan_then_remember(); test_srt_target_has_encryption(); print("ok")
