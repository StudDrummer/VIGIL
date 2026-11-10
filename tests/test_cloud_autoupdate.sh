#!/bin/bash
# DEPLOYED_V1: the cloud updater builds a new 'stable', finishes an update that
# was cut short, rolls back a version that is unhealthy, and skips what it has.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; T=$(mktemp -d)
mkdir -p $T/bin; export HOME=$T/home; mkdir -p $HOME
cat > $T/bin/docker <<'S'
#!/bin/bash
echo "docker $*" >> "$LOG"
case "$*" in
  info*) exit 0;;
  "compose ps"*) [ -f "$SICK" ] && echo "site-home01 restarting" || echo "site-home01 running"; exit 0;;
  "compose exec"*) [ -f "$SICK" ] && exit 1 || exit 0;;
esac
exit 0
S
printf '#!/bin/bash\nexit 0\n' > $T/bin/sleep; chmod +x $T/bin/*
export PATH=$T/bin:$PATH LOG=$T/log SICK=$T/sick
git init -q --bare $T/origin.git
git clone -q $T/origin.git $T/repo 2>/dev/null; cd $T/repo; git config user.email t@t; git config user.name t
mkdir cloud; cp "$HERE/cloud/autoupdate.sh" cloud/
printf '#!/bin/bash\necho build >> "$LOG"\n[ -f "$T_BUILDFAIL" ] && exit 1; exit 0\n' > cloud/build_images.sh
printf 'import sys\n' > cloud/vigilctl.py
git add -A; git commit -qm v1; git push -q origin HEAD:stable; git checkout -q -B stable origin/stable 2>/dev/null || git checkout -q -b stable
V1=$(git rev-parse HEAD)
F=0; chk(){ if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; F=1; fi; }
U="bash $T/repo/cloud/autoupdate.sh"
# an earlier run was cut short: code checked out, never built -> must build now
: > $LOG; $U >/dev/null 2>&1
chk "unfinished update is finished" 'grep -q build $LOG && [ "$(cat $HOME/.vigil-autoupdate/deployed)" = "$V1" ]'
: > $LOG; $U >/dev/null 2>&1
chk "up to date: nothing rebuilt" '! grep -q build $LOG'
cd $T/repo; echo 2 >> cloud/vigilctl.py; git commit -qam v2; git push -q origin HEAD:stable; V2=$(git rev-parse HEAD); git reset -q --hard $V1
: > $LOG; $U >/dev/null 2>&1
chk "new version built and running" '[ "$(cat $HOME/.vigil-autoupdate/deployed)" = "$V2" ] && grep -q "tag vigil-site:latest vigil-site:prev" $LOG'
cd $T/repo; echo 3 >> cloud/vigilctl.py; git commit -qam v3; git push -q origin HEAD:stable; V3=$(git rev-parse HEAD)
touch $SICK; : > $LOG; $U >/dev/null 2>&1; rm $SICK
chk "unhealthy version rolled back" '[ "$(cat $HOME/.vigil-autoupdate/deployed)" = "$V2" ] && [ "$(git -C $T/repo rev-parse HEAD)" = "$V2" ] && grep -q "tag vigil-site:prev vigil-site:latest" $LOG'
: > $LOG; $U >/dev/null 2>&1
chk "failed version not retried" '! grep -q build $LOG'
rm -rf $T; [ $F = 0 ] && echo "ALL PASS"; exit $F
