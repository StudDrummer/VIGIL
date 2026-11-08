#!/bin/bash
# One-time setup of a fresh Ubuntu 22.04/24.04 server for VIGIL Cloud.
#   bash install_server.sh api.vigilhealth.me you@example.com
# Safe to run again: it skips what is already done.
set -euo pipefail
DOMAIN="${1:?usage: bash install_server.sh <domain> <your email>}"
EMAIL="${2:?usage: bash install_server.sh <domain> <your email>}"
cd "$(dirname "$0")"

if ! command -v docker >/dev/null; then
  echo "== Installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi
sudo apt-get update -qq && sudo apt-get install -y -qq python3-flask python3-requests >/dev/null

DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"

echo "== Building the camera pipeline image (10-20 minutes the first time)"
bash build_images.sh

if [ ! -f config/vigil_cloud.json ]; then
  python3 vigilctl.py init --domain "$DOMAIN"
fi
if ! python3 vigilctl.py users 2>/dev/null | grep -qi "$EMAIL"; then
  python3 vigilctl.py add-user --email "$EMAIL" --name "Admin" --admin
fi
VIGILCTL_NO_DOCKER=1 python3 vigilctl.py apply
$DOCKER compose up -d --remove-orphans
echo
echo "VIGIL Cloud is running. Check: curl https://$DOMAIN/health"
echo "Add a camera:  python3 vigilctl.py add-camera --name \"<home> - <room>\" --home \"<home>\" --tz <Region/City>"
