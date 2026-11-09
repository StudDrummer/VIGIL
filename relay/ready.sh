#!/bin/bash
# READY_V1: one command brings a box fully up to date and checks it.
#
#   sudo bash ~/VIGIL/relay/ready.sh
#
# Gets the newest tested code, turns on automatic updates and the always-on
# Wi-Fi helper, restarts the relay, then prints a short health report.
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo:  sudo bash ~/VIGIL/relay/ready.sh"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
U="${SUDO_USER:-root}"
ok()  { printf "  [ OK ]  %s\n" "$*"; }
bad() { printf "  [FIX ]  %s\n" "$*"; ISSUES=$((ISSUES+1)); }
ISSUES=0

echo "== Getting the newest code"
sudo -u "$U" git -C "$REPO" pull -q --ff-only 2>/dev/null || echo "  (could not reach GitHub; using the copy on this box)"
bash "$REPO/relay/enable_autoupdate.sh"
echo "== Installing the tested version (about a minute)"
/opt/vigil-relay/autoupdate.sh || true
if [ ! -s /opt/vigil-relay/VERSION ]; then        # GitHub unreachable on a first run
  install -m 755 "$REPO/relay/vigil_relay.py" "$REPO/relay/wifi_setup.py" /opt/vigil-relay/
  git -C "$REPO" rev-parse HEAD > /opt/vigil-relay/VERSION
  systemctl restart vigil-relay
fi
systemctl enable --now vigil-wifi-setup >/dev/null 2>&1
sleep 15

echo
echo "== Report for $(hostname)"
ENV=/etc/vigil-relay.env
CAM=$(grep '^VIGIL_CAMERA_ID=' $ENV 2>/dev/null | cut -d= -f2)
[ -n "$CAM" ] && ok "camera name: $CAM" || bad "no camera settings: run install.sh with this box's relay_<camera>.env"
MAC=$(grep '^VIGIL_TAPO_MAC=' $ENV 2>/dev/null | cut -d= -f2)
if echo "$MAC" | grep -Eiq '^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$'; then ok "camera MAC: $MAC"
else bad "camera MAC missing or not real ('$MAC'): sudo bash ~/VIGIL/relay/set_mac.sh"; fi
grep -q '^VIGIL_TAPO_USER=.\+' $ENV 2>/dev/null && ok "camera account saved" || echo "  [ -- ]  no camera account yet (set in the VIGIL app during setup)"
systemctl is-active -q vigil-relay && ok "relay running" || bad "relay not running: journalctl -u vigil-relay -n 30"
systemctl is-active -q vigil-wifi-setup && ok "Wi-Fi helper running (always on)" || bad "Wi-Fi helper not running: journalctl -u vigil-wifi-setup -n 30"
systemctl is-active -q vigil-update.timer && ok "automatic updates on" || bad "automatic updates off"
ok "code version: $(cut -c1-7 /opt/vigil-relay/VERSION 2>/dev/null)"
ST=$(python3 -c "import json;d=json.load(open('/var/lib/vigil-relay/status.json'));print(d.get('state',''),'|',d.get('camera') or '')" 2>/dev/null)
case "$ST" in
  sending*) ok "state: sending video ($ST)";;
  "") echo "  [ -- ]  state: starting";;
  *) echo "  [ -- ]  state: $ST";;
esac
nmcli -t -f TYPE,STATE device 2>/dev/null | grep -q '^ethernet:connected' && ok "network cable plugged in (most reliable)"
SSID=$(nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2; exit}')
SIG=$(awk 'NR==3{gsub(/\./,"",$4); print $4}' /proc/net/wireless 2>/dev/null)
if [ -n "$SSID" ]; then
  if [ -n "$SIG" ] && [ "$SIG" -lt -75 ]; then bad "Wi-Fi '$SSID' is weak ($SIG dBm): move the box closer to the router"
  else ok "Wi-Fi '$SSID' ${SIG:+($SIG dBm)}"; fi
else echo "  [ -- ]  not on Wi-Fi"; fi
T=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
if [ -n "$T" ] && [ "$T" != "0x0" ]; then bad "power supply too weak ($T): use a 5 V 2.5 A supply and a short cable"
elif [ -n "$T" ]; then ok "power supply fine"; fi
command -v tailscale >/dev/null && { tailscale status >/dev/null 2>&1 && ok "Tailscale connected" || echo "  [ -- ]  Tailscale not connected (needs internet that allows it)"; }
echo
[ $ISSUES = 0 ] && echo "Ready." || echo "$ISSUES thing(s) to fix above."
