#!/bin/bash
# PACK_V1: get a box ready to travel to its new home, then shut it down.
#
#   sudo bash ~/VIGIL/relay/pack.sh
#
# Installs the newest tested code, forgets this house's Wi-Fi and the camera
# account (both are set again in the VIGIL app at the new home), and keeps the
# camera's MAC so it finds its own camera anywhere. When it powers on at the
# new home it opens its setup Wi-Fi by itself.
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo:  sudo bash ~/VIGIL/relay/pack.sh"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
bash "$HERE/ready.sh" || true
echo
read -rp "Pack $(hostname) now? It forgets this Wi-Fi and shuts down. (y/N) " A
[ "${A,,}" = y ] || { echo "Not packed."; exit 0; }
ENV=/etc/vigil-relay.env
sed -i '/^VIGIL_TAPO_USER=/d;/^VIGIL_TAPO_PASS=/d' $ENV
rm -f /var/lib/vigil-relay/camera.json /var/lib/vigil-relay/camera_ip /var/lib/vigil-relay/status.json \
      /var/lib/vigil-relay/bad_version /var/lib/vigil-relay/last_reboot
systemctl enable vigil-relay vigil-wifi-setup vigil-update.timer >/dev/null 2>&1
grep -q '^VIGIL_TAPO_MAC=.\+' $ENV && echo "Camera MAC kept: $(grep '^VIGIL_TAPO_MAC=' $ENV | cut -d= -f2)" \
  || echo "WARNING: no camera MAC saved. Add it before packing (see ready.sh report)."
echo "Shutting down in a few seconds. Unplug it once the green light has stopped blinking."
# runs on its own so it finishes even though this connection drops with the Wi-Fi
systemd-run --quiet --no-block --unit=vigil-pack bash -c '
  sleep 3
  nmcli -t -f UUID,TYPE connection show | awk -F: "\$2==\"802-11-wireless\"{print \$1}" | while read -r u; do
    n=$(nmcli -g connection.id connection show "$u")
    [ "$n" = vigil-setup ] || nmcli connection delete "$u" >/dev/null
  done
  sync; systemctl poweroff'
