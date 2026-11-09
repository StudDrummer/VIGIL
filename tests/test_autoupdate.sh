#!/bin/bash
# AUTOUPDATE_V1: the box updater installs a new 'stable', skips what it has,
# rolls back a version that crashes, and can undo.
set -u
T=$(mktemp -d); HERE="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p $T/bin $T/dst $T/state
cat > $T/bin/systemctl <<'S'
#!/bin/bash
echo "systemctl $*" >> "$SYSLOG"
case "$1" in
  is-active) [ "$(cat "$CRASH" 2>/dev/null)" != 1 ];;
  is-enabled) exit 0;;
  show) [ "$(cat "$CRASH" 2>/dev/null)" = 1 ] && [ -n "$(grep -c restart "$SYSLOG")" ] && echo 3 || echo 0;;
  *) exit 0;;
esac
S
printf '#!/bin/bash\nexit 0\n' > $T/bin/sleep
chmod +x $T/bin/*
export PATH=$T/bin:$PATH SYSLOG=$T/sys.log CRASH=$T/crash VIGIL_SRC=$T/src VIGIL_DST=$T/dst VIGIL_STATE=$T/state VIGIL_REPO=$T/origin.git
git init -q --bare $T/origin.git
git clone -q $T/origin.git $T/work 2>/dev/null; cd $T/work
git config user.email t@t; git config user.name t
mkdir relay; cp "$HERE/relay/vigil_relay.py" "$HERE/relay/wifi_setup.py" "$HERE/relay/autoupdate.sh" relay/
git add -A; git commit -qm v1; git push -q origin HEAD:stable
V1=$(git rev-parse HEAD)
F=0; chk(){ if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; F=1; fi; }
U="$HERE/relay/autoupdate.sh"
bash $U >/dev/null 2>&1
chk "first run installs stable" '[ "$(cat $T/dst/VERSION)" = "$V1" ] && [ -f $T/dst/vigil_relay.py ]'
: > $T/sys.log; bash $U >/dev/null 2>&1
chk "nothing new: no restart" '! grep -q restart $T/sys.log'
echo "# v2" >> relay/vigil_relay.py; git commit -qam v2; git push -q origin HEAD:stable; V2=$(git rev-parse HEAD)
bash $U >/dev/null 2>&1
chk "new version installed, old kept" '[ "$(cat $T/dst/VERSION)" = "$V2" ] && [ "$(cat $T/dst/prev/VERSION)" = "$V1" ]'
chk "wifi helper not restarted when it did not change" '! grep -q "restart vigil-wifi-setup" $T/sys.log'
echo "x = (" >> relay/vigil_relay.py; git commit -qam broken; git push -q origin HEAD:stable; V3=$(git rev-parse HEAD)
bash $U >/dev/null 2>&1
chk "code that does not compile is never installed" '[ "$(cat $T/dst/VERSION)" = "$V2" ] && [ "$(cat $T/state/bad_version)" = "$V3" ]'
git revert --no-edit HEAD >/dev/null; echo "# v4" >> relay/vigil_relay.py; git commit -qam v4; git push -q origin HEAD:stable; V4=$(git rev-parse HEAD)
echo 1 > $T/crash; : > $T/sys.log
bash $U >/dev/null 2>&1
chk "a version that crashes is rolled back" '[ "$(cat $T/dst/VERSION)" = "$V2" ] && [ "$(cat $T/state/bad_version)" = "$V4" ]'
: > $T/sys.log; bash $U >/dev/null 2>&1
chk "a failed version is not retried" '! grep -q restart $T/sys.log'
rm $T/crash; echo "# v5" >> relay/vigil_relay.py; git commit -qam v5; git push -q origin HEAD:stable; V5=$(git rev-parse HEAD)
bash $U >/dev/null 2>&1
chk "a newer fixed version installs" '[ "$(cat $T/dst/VERSION)" = "$V5" ]'
bash $U --undo >/dev/null 2>&1
chk "undo goes back one version" '[ "$(cat $T/dst/VERSION)" = "$V2" ]'
rm -rf $T; [ $F = 0 ] && echo "ALL PASS"; exit $F
