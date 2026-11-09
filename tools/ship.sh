#!/bin/bash
# SHIP_V1: send what you tested at home to every home in India, in one step.
#
#   bash tools/ship.sh           show what is new and ship it
#   bash tools/ship.sh status    what is tested (main) vs what India runs (stable)
#   bash tools/ship.sh undo      put India back on the previous shipped version
#
# How it works: 'main' is what runs on the Jetson at home. Shipping moves the
# 'stable' branch up to it. VIGIL Cloud picks that up within 10 minutes and
# every relay box within 15, each checking itself and going back on its own
# if the new version does not run. The iPhone app is shipped separately,
# through TestFlight.
set -euo pipefail
cd "$(dirname "$0")/.."
git fetch -q origin
main=$(git rev-parse origin/main)
stable=$(git rev-parse origin/stable 2>/dev/null || echo "")

case "${1:-ship}" in
status)
  echo "Tested at home (main):  $(git log -1 --format='%h  %s  (%cr)' "$main")"
  [ -n "$stable" ] && echo "India runs (stable):    $(git log -1 --format='%h  %s  (%cr)' "$stable")"
  n=$( [ -n "$stable" ] && git rev-list --count "$stable..$main" || echo all)
  echo "Waiting to ship: $n change(s)"
  ;;
undo)
  prev=$(git tag -l 'ship-*' --sort=-creatordate | sed -n 2p)
  [ -n "$prev" ] || { echo "No earlier shipped version to go back to."; exit 1; }
  echo "India goes back to $(git log -1 --format='%h  %s' "$prev")"
  read -rp "Continue? (y/N) " a; [ "${a,,}" = y ] || exit 0
  git push -q --force origin "$prev^{commit}:refs/heads/stable"
  git push -q origin ":refs/tags/$(git tag -l 'ship-*' --sort=-creatordate | sed -n 1p)" || true
  git tag -d "$(git tag -l 'ship-*' --sort=-creatordate | sed -n 1p)" >/dev/null
  echo "Done. The cloud and boxes switch back within 15 minutes."
  ;;
ship)
  if [ -n "$stable" ] && [ "$main" = "$stable" ]; then echo "India already runs the newest tested version."; exit 0; fi
  echo "These changes go to India:"
  if [ -n "$stable" ]; then git log --format='  %h  %s' "$stable..$main"; else git log -5 --format='  %h  %s' "$main"; fi
  read -rp "Ship them? (y/N) " a; [ "${a,,}" = y ] || { echo "Not shipped."; exit 0; }
  git push -q origin "$main:refs/heads/stable"
  t="ship-$(date +%Y%m%d-%H%M)"; git tag "$t" "$main"; git push -q origin "$t"
  echo "Shipped ($t). The cloud updates within 10 minutes, every box within 15."
  echo "Check from the cloud window:  cd ~/VIGIL/cloud && python3 vigilctl.py boxes"
  ;;
*) echo "usage: bash tools/ship.sh [status|undo]"; exit 1;;
esac
