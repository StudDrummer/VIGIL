#!/bin/bash
# Turns a Raspberry Pi (Pi 3 Model B, Zero 2 W or newer, Raspberry Pi OS Lite,
# Bookworm or later) into a VIGIL relay box. Run on the Pi:
#
#   sudo bash install.sh relay_home01.env
#
# relay_home01.env comes from the cloud server (vigilctl.py add-camera writes
# it). The script asks for the Tapo camera account (Tapo app > camera >
# Settings > Advanced settings > Camera account) and, optionally, a Tailscale
# auth key so you can reach the box from anywhere, and the Wi-Fi of the home
# it is going to (it can also be set up there with a phone; see wifi_setup.py).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
SETTINGS="${1:?usage: sudo bash install.sh relay_<camera>.env}"
HERE="$(cd "$(dirname "$0")" && pwd)"

read -rp "Tapo camera account username: " TAPO_USER
read -rsp "Tapo camera account password: " TAPO_PASS; echo
read -rp "Tailscale auth key (press Enter to skip): " TS_KEY
read -rp "Wi-Fi name of the home it goes to (Enter to skip): " WIFI_SSID
WIFI_PASS=""
[ -n "$WIFI_SSID" ] && { read -rsp "That Wi-Fi's password: " WIFI_PASS; echo; }

echo "== Installing ffmpeg"
apt-get update -qq
apt-get install -y -qq ffmpeg python3 network-manager dnsmasq-base >/dev/null

echo "== Installing VIGIL relay"
install -d /opt/vigil-relay /var/lib/vigil-relay
install -m 755 "$HERE/vigil_relay.py" "$HERE/wifi_setup.py" /opt/vigil-relay/
{ cat "$SETTINGS"; echo "VIGIL_TAPO_USER=$TAPO_USER"; echo "VIGIL_TAPO_PASS=$TAPO_PASS"; } > /etc/vigil-relay.env
chmod 600 /etc/vigil-relay.env

cat > /etc/systemd/system/vigil-relay.service <<'EOF'
[Unit]
Description=VIGIL relay (camera to VIGIL Cloud)
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/bin/python3 /opt/vigil-relay/vigil_relay.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/vigil-wifi-setup.service <<'EOF'
[Unit]
Description=VIGIL Wi-Fi setup hotspot (only when there is no Wi-Fi)
After=NetworkManager.service
[Service]
ExecStart=/usr/bin/python3 /opt/vigil-relay/wifi_setup.py
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

echo "== Making it run unattended for months"
# Restart the whole box if it ever hangs.
mkdir -p /etc/systemd/system.conf.d
printf "[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n" > /etc/systemd/system.conf.d/vigil-watchdog.conf
# Wi-Fi power saving drops video on the Pi: turn it off.
printf "[connection]\nwifi.powersave=2\n" > /etc/NetworkManager/conf.d/vigil-wifi-powersave.conf
# Logs in memory only: saves the SD card.
mkdir -p /etc/systemd/journald.conf.d
printf "[Journal]\nStorage=volatile\nRuntimeMaxUse=20M\n" > /etc/systemd/journald.conf.d/vigil.conf
# A weekly fresh start, Sunday 4:00 local time (a systemd timer: newer
# Raspberry Pi OS Lite images do not always include cron).
cat > /etc/systemd/system/vigil-weekly-reboot.service <<'EOF2'
[Unit]
Description=VIGIL weekly fresh start
[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl reboot
EOF2
cat > /etc/systemd/system/vigil-weekly-reboot.timer <<'EOF2'
[Unit]
Description=VIGIL weekly fresh start, Sunday 4:00
[Timer]
OnCalendar=Sun *-*-* 04:00:00
Persistent=false
[Install]
WantedBy=timers.target
EOF2

if [ -n "$WIFI_SSID" ]; then
  echo "== Saving Wi-Fi '$WIFI_SSID' for when it arrives"
  nmcli connection delete "$WIFI_SSID" >/dev/null 2>&1 || true
  nmcli connection add type wifi ifname wlan0 con-name "$WIFI_SSID" ssid "$WIFI_SSID" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PASS" connection.autoconnect yes >/dev/null
fi

if [ -n "$TS_KEY" ]; then
  echo "== Tailscale (remote access from anywhere)"
  command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
  CAM=$(grep '^VIGIL_CAMERA_ID=' /etc/vigil-relay.env | cut -d= -f2)
  tailscale up --authkey="$TS_KEY" --hostname="vigil-relay-$CAM" --ssh
fi

systemctl daemon-reload
systemctl enable --now vigil-relay.service vigil-wifi-setup.service vigil-weekly-reboot.timer
echo
echo "Done. Within a minute the green light should go steady (sending video)."
echo "Check:  journalctl -u vigil-relay -n 20     and     cat /var/lib/vigil-relay/status.json"
