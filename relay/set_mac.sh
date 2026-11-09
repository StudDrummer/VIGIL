#!/bin/bash
# Save the MAC of the camera that ships with this box (on the camera's label,
# or Tapo app > camera > Settings > Device info).
#   sudo bash ~/VIGIL/relay/set_mac.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
read -rp "Camera MAC (like 58:D8:12:20:85:2F): " M
M=$(echo "$M" | tr 'a-f-' 'A-F:' | tr -d ' ')
echo "$M" | grep -Eq '^([0-9A-F]{2}:){5}[0-9A-F]{2}$' || { echo "That is not a MAC address (6 pairs like 58:D8:12:20:85:2F)."; exit 1; }
ENV=/etc/vigil-relay.env
sed -i '/^VIGIL_TAPO_MAC=/d' $ENV; echo "VIGIL_TAPO_MAC=$M" >> $ENV
rm -f /var/lib/vigil-relay/camera.json /var/lib/vigil-relay/camera_ip
systemctl restart vigil-relay
echo "Saved $M. The box now looks for that camera."
