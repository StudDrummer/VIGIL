#!/bin/bash
# Build the two VIGIL images from a clean copy of only the files they need,
# so camera data and certificates in cloud/data are never read by Docker.
set -euo pipefail
cd "$(dirname "$0")"
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
CTX=$(mktemp -d); trap 'rm -rf "$CTX"' EXIT
mkdir -p "$CTX/jetson-backend" "$CTX/cloud/site"
cp ../jetson-backend/*.py ../jetson-backend/vigil_bytetrack.yaml "$CTX/jetson-backend/"
cp site/site_entry.sh site/site_jobs.py "$CTX/cloud/site/"
$DOCKER build -f site/Dockerfile -t vigil-site:latest "$CTX"
$DOCKER build -t vigil-gateway:latest gateway
