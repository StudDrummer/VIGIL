"""HOTSPOT_VISIBLE_V1: the box's setup Wi-Fi is made plain WPA2, not hidden, with power saving
off, so it shows in the iPhone's Wi-Fi list; if those settings will not start, the plain
hotspot is made again instead of leaving the box with no setup Wi-Fi at all."""
import os
import sys
import tempfile

d = tempfile.mkdtemp()
env = os.path.join(d, "vigil-relay.env")
with open(env, "w") as f:
    f.write("VIGIL_CAMERA_ID=home01\nVIGIL_SETUP_CODE=K7Q2-M9XD\n")
os.environ["VIGIL_RELAY_ENV"] = env
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
import wifi_setup as ws  # noqa: E402

ws.FLAG = os.path.join(d, "hotspot.flag")
calls, fail_up = [], [False]


def fake_sh(*cmd, timeout=30):
    calls.append(cmd)
    if cmd[:3] == ("nmcli", "connection", "up") and fail_up[0]:
        return 4, "Error: Connection activation failed"
    return 0, ""


ws.sh = fake_sh
ws.hotspot_on()
hs = [c for c in calls if c[:4] == ("nmcli", "device", "wifi", "hotspot")]
assert hs and "VIGIL-home01" in hs[0] and "K7Q2M9XD" in hs[0] and "6" in hs[0], hs
mod = next(c for c in calls if c[:3] == ("nmcli", "connection", "modify"))
for k, v in (("802-11-wireless.hidden", "no"), ("802-11-wireless.powersave", "2"),
             ("802-11-wireless-security.key-mgmt", "wpa-psk"), ("802-11-wireless-security.pmf", "1")):
    assert mod[mod.index(k) + 1] == v, (k, mod)
assert ("nmcli", "connection", "up", "vigil-setup") in calls
assert ("iw", "dev", "wlan0", "set", "power_save", "off") in calls
assert ("nmcli", "connection", "delete", "vigil-setup") not in calls

# the tuned hotspot will not start on this box: the plain one is made again
calls.clear(); fail_up[0] = True
ws.hotspot_on()
assert ("nmcli", "connection", "delete", "vigil-setup") in calls
assert len([c for c in calls if c[:4] == ("nmcli", "device", "wifi", "hotspot")]) == 2
print("ALL PASS")
