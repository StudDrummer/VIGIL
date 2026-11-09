#!/bin/bash
# AUTOUPDATE_V1: turn on automatic updates from the 'stable' branch (see autoupdate.sh).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
command -v git >/dev/null || apt-get install -y -qq git >/dev/null
install -d /opt/vigil-relay
install -m 755 "$HERE/autoupdate.sh" /opt/vigil-relay/
ln -sf /opt/vigil-relay/autoupdate.sh /usr/local/bin/vigil-update
cat > /etc/systemd/system/vigil-update.service <<'U'
[Unit]
Description=VIGIL box update check
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/bin/bash /opt/vigil-relay/autoupdate.sh
TimeoutStartSec=15min
U
cat > /etc/systemd/system/vigil-update.timer <<'U'
[Unit]
Description=VIGIL box update check every 15 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
RandomizedDelaySec=3min
[Install]
WantedBy=timers.target
U
systemctl daemon-reload
systemctl enable --now vigil-update.timer >/dev/null
echo "Automatic updates on (checks every 15 minutes)."
