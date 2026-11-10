#!/bin/bash
# Give the box the camera account by hand (same as the VIGIL app's setup does).
#   sudo bash ~/VIGIL/relay/set_camera_account.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
read -rp "Camera account username [vigilcam]: " U; U=${U:-vigilcam}
read -rsp "Camera account password (as typed in Tapo): " P; echo
[ -n "$P" ] || { echo "No password given."; exit 1; }
ENV=/etc/vigil-relay.env
sed -i '/^VIGIL_TAPO_USER=/d;/^VIGIL_TAPO_PASS=/d' $ENV
printf 'VIGIL_TAPO_USER=%s\nVIGIL_TAPO_PASS=%s\n' "$U" "$P" >> $ENV
rm -f /run/vigil-relay/setup-mode
systemctl restart vigil-wifi-setup vigil-relay
echo "Saved. Looking for the camera; give it about a minute, then:  cat /var/lib/vigil-relay/status.json"
