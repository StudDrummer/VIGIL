#!/bin/bash
# Pull the newest VIGIL code and restart every camera with it. Data is kept.
set -euo pipefail
cd "$(dirname "$0")"
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
git -C .. pull --ff-only
$DOCKER build -f site/Dockerfile -t vigil-site:latest ..
$DOCKER build -t vigil-gateway:latest gateway
VIGILCTL_NO_DOCKER=1 python3 vigilctl.py apply
$DOCKER compose up -d --remove-orphans --force-recreate
echo "Updated."
