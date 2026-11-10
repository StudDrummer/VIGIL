#!/bin/bash
# Pull the newest VIGIL code and restart every camera with it. Data is kept.
set -euo pipefail
{
cd "$(dirname "$0")"
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
git -C .. pull --ff-only
bash build_images.sh
VIGILCTL_NO_DOCKER=1 python3 vigilctl.py apply
$DOCKER compose up -d --remove-orphans --force-recreate
mkdir -p "$HOME/.vigil-autoupdate" && git -C .. rev-parse HEAD > "$HOME/.vigil-autoupdate/deployed"   # DEPLOYED_V1
echo "Updated."
exit 0
}
