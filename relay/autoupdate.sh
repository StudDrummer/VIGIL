#!/bin/bash
# AUTOUPDATE_V1: keeps this box on the newest tested VIGIL code.
#
# Every 15 minutes (vigil-update.timer) it checks the 'stable' branch on
# GitHub. Code only reaches 'stable' after it ran at home (tools/ship.sh).
# A new version is installed only if it compiles, and is rolled back at once
# if the relay then fails to run. No internet: it simply tries again later.
#
#   sudo vigil-update        check now        sudo vigil-update --undo   go back one version
{
set -uo pipefail
SRC=${VIGIL_SRC:-/opt/vigil-src}
DST=${VIGIL_DST:-/opt/vigil-relay}
STATE=${VIGIL_STATE:-/var/lib/vigil-relay}
BRANCH=${VIGIL_BRANCH:-stable}
REPO=${VIGIL_REPO:-https://github.com/StudDrummer/VIGIL.git}
say() { echo "[vigil-update] $*"; }
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
mkdir -p "$STATE"

restart_all() {
  systemctl restart vigil-relay
  if [ "${1:-}" = wifi ] && systemctl is-enabled -q vigil-wifi-setup 2>/dev/null; then
    # never cut off a family that is in the middle of setting the box up
    if [ -e /run/vigil-relay/setup-mode ]; then say "setup in progress: Wi-Fi helper restarts at the next reboot"
    else systemctl restart vigil-wifi-setup; fi
  fi
}

if [ "${1:-}" = "--undo" ]; then
  [ -f "$DST/prev/vigil_relay.py" ] || { say "no previous version kept"; exit 1; }
  cat "$DST/VERSION" > "$STATE/bad_version" 2>/dev/null || true
  cp -a "$DST/prev/." "$DST/"
  restart_all wifi
  say "back on $(cat "$DST/VERSION" 2>/dev/null)"
  exit 0
fi

if [ ! -d "$SRC/.git" ]; then
  rm -rf "$SRC"
  git clone -q --depth 10 -b "$BRANCH" "$REPO" "$SRC" 2>/dev/null || { say "GitHub not reachable; will try later"; exit 0; }
fi
cd "$SRC"
git fetch -q --depth 10 origin "$BRANCH" 2>/dev/null || { say "GitHub not reachable; will try later"; exit 0; }
NEW=$(git rev-parse FETCH_HEAD)
OLD=$(cat "$DST/VERSION" 2>/dev/null || echo none)
[ "$NEW" = "$OLD" ] && { [ -t 1 ] && say "up to date (${NEW:0:7})"; exit 0; }
[ "$NEW" = "$(cat "$STATE/bad_version" 2>/dev/null)" ] && { say "${NEW:0:7} failed here before; waiting for a newer one"; exit 0; }
git reset -q --hard "$NEW"
python3 -m py_compile relay/vigil_relay.py relay/wifi_setup.py || { echo "$NEW" > "$STATE/bad_version"; say "new code does not compile; kept the old"; exit 0; }

say "installing ${NEW:0:7} (was ${OLD:0:7})"
rm -rf "$DST/prev"; mkdir -p "$DST/prev"
cp -a "$DST"/*.py "$DST"/*.sh "$DST/VERSION" "$DST/prev/" 2>/dev/null || true
WIFI_CHANGED=""
cmp -s relay/wifi_setup.py "$DST/wifi_setup.py" || WIFI_CHANGED=wifi
install -m 755 relay/vigil_relay.py relay/wifi_setup.py relay/autoupdate.sh "$DST/"
echo "$NEW" > "$DST/VERSION"
restart_all $WIFI_CHANGED
sleep 2
R0=$(systemctl show -p NRestarts --value vigil-relay 2>/dev/null || echo 0)   # a manual restart resets the count

sleep 60
R1=$(systemctl show -p NRestarts --value vigil-relay 2>/dev/null || echo 0)
if ! systemctl is-active -q vigil-relay || [ "$R1" -gt "$R0" ]; then
  say "relay failed on ${NEW:0:7}; going back to ${OLD:0:7}"
  echo "$NEW" > "$STATE/bad_version"
  cp -a "$DST/prev/." "$DST/"
  restart_all $WIFI_CHANGED
  exit 1
fi
say "running ${NEW:0:7}"
exit 0
}
