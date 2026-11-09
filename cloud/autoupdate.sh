#!/bin/bash
# AUTOUPDATE_V1: keeps VIGIL Cloud on the newest tested code.
#
# Every 10 minutes (vigil-autoupdate.timer) it checks the 'stable' branch.
# A new version is built next to the running one; the cameras switch over
# only if the build succeeds, and switch back at once if the new version
# does not come up healthy. Data is never touched.
#
#   bash ~/VIGIL/cloud/autoupdate.sh           check now
#   bash ~/VIGIL/cloud/autoupdate.sh --undo    go back one version
{
set -uo pipefail
cd "$(dirname "$0")"
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
BRANCH=${VIGIL_BRANCH:-stable}
ST="$HOME/.vigil-autoupdate"; mkdir -p "$ST"
say() { echo "[vigil-autoupdate] $(date '+%F %T') $*" | tee -a "$ST/log"; }

images() {   # $1 = from-tag, $2 = to-tag
  $DOCKER tag vigil-site:$1 vigil-site:$2 && $DOCKER tag vigil-gateway:$1 vigil-gateway:$2
}
up() { VIGILCTL_NO_DOCKER=1 python3 vigilctl.py apply >/dev/null && $DOCKER compose up -d --remove-orphans --force-recreate >/dev/null 2>&1; }
healthy() {
  sleep 75
  local bad
  bad=$($DOCKER compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk '$2!="running"{print $1}')
  [ -z "$bad" ] || { say "not running: $bad"; return 1; }
  $DOCKER compose exec -T gateway python -c "import urllib.request;urllib.request.urlopen('http://localhost:8080/health',timeout=10)" >/dev/null 2>&1 \
    || { say "gateway health check failed"; return 1; }
  return 0
}

if [ "${1:-}" = "--undo" ]; then
  PREV=$(cat "$ST/prev_commit" 2>/dev/null) || { say "no previous version kept"; exit 1; }
  git -C .. rev-parse HEAD > "$ST/bad_version"
  git -C .. checkout -q -B "$BRANCH" "$PREV" && images prev latest && up && say "back on ${PREV:0:7}"
  exit 0
fi

git -C .. fetch -q origin "$BRANCH" 2>/dev/null || { say "GitHub not reachable; will try later"; exit 0; }
NEW=$(git -C .. rev-parse FETCH_HEAD); OLD=$(git -C .. rev-parse HEAD)
[ "$NEW" = "$OLD" ] && { [ -t 1 ] && echo "Up to date (${NEW:0:7})."; exit 0; }
[ "$NEW" = "$(cat "$ST/bad_version" 2>/dev/null)" ] && exit 0

say "updating ${OLD:0:7} -> ${NEW:0:7}"
images latest prev 2>/dev/null
echo "$OLD" > "$ST/prev_commit"
if ! git -C .. checkout -q -B "$BRANCH" "$NEW"; then
  say "files changed by hand on this server block the update (git -C ~/VIGIL status)"; exit 1
fi
git -C .. branch -q -u "origin/$BRANCH" "$BRANCH" 2>/dev/null
if ! bash build_images.sh >> "$ST/log" 2>&1; then
  say "build failed; still running ${OLD:0:7}"
  echo "$NEW" > "$ST/bad_version"; git -C .. checkout -q -B "$BRANCH" "$OLD"; images prev latest; exit 1
fi
up
if healthy; then say "running ${NEW:0:7}"; exit 0; fi
say "new version unhealthy; going back to ${OLD:0:7}"
echo "$NEW" > "$ST/bad_version"
git -C .. checkout -q -B "$BRANCH" "$OLD"; images prev latest; up
exit 1
}
