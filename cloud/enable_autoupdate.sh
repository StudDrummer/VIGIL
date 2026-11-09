#!/bin/bash
# AUTOUPDATE_V1: the cloud follows the 'stable' branch by itself (see autoupdate.sh).
#   bash ~/VIGIL/cloud/enable_autoupdate.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ME="$(id -un)"
git -C "$HERE/.." fetch -q origin stable
git -C "$HERE/.." checkout -q -B stable origin/stable
sudo tee /etc/systemd/system/vigil-autoupdate.service >/dev/null <<U
[Unit]
Description=VIGIL Cloud update check
After=network-online.target docker.service
[Service]
Type=oneshot
User=$ME
ExecStart=/bin/bash $HERE/autoupdate.sh
TimeoutStartSec=30min
U
sudo tee /etc/systemd/system/vigil-autoupdate.timer >/dev/null <<'U'
[Unit]
Description=VIGIL Cloud update check every 10 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
U
sudo systemctl daemon-reload
sudo systemctl enable --now vigil-autoupdate.timer >/dev/null
echo "VIGIL Cloud now follows 'stable' and updates itself within 10 minutes of each ship."
