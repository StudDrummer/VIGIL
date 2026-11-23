#!/bin/bash
# JETSON_DOCTOR_V1: checks everything the home Jetson needs to stay online, and fixes what it can.
#
#   bash ~/VIGIL/jetson-backend/jetson_doctor.sh           check only (changes nothing)
#   bash ~/VIGIL/jetson-backend/jetson_doctor.sh --fix     check, then fix what is broken
#   bash ~/VIGIL/jetson-backend/jetson_doctor.sh --install sets the Jetson up to look after itself:
#                                                          services start at boot, a check-and-fix
#                                                          every 15 minutes, logs capped in size
#
# "Online" in the app means: VIGIL Cloud reaches this Jetson over Tailscale, and the camera
# pipeline (vigil-camera) sent a heartbeat in the last 3 minutes. So the order of checks is:
# clock, Tailscale, the three services, the server, the camera picture, the heartbeat, then
# the things that wear a Jetson down over months (disk, database, memory, heat, logs).
set -uo pipefail
DEST="${VIGIL_JETSON_DIR:-$HOME/passive-health-moniter}"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
SERVICES="vigil vigil-camera vigil-face"
MODE="${1:-check}"
BAD=0; WARNN=0; FIXED=0
ok()   { printf "  \033[32mOK\033[0m    %s\n" "$*"; }
warn() { printf "  \033[33mWARN\033[0m  %s\n" "$*"; WARNN=$((WARNN+1)); }
bad()  { printf "  \033[31mFAIL\033[0m  %s\n" "$*"; BAD=$((BAD+1)); }
fix()  { printf "  \033[36mFIX\033[0m   %s\n" "$*"; FIXED=$((FIXED+1)); }
hdr()  { printf "\n== %s\n" "$*"; }
FIXING=0; [ "$MODE" = "--fix" ] && FIXING=1

if [ "$MODE" = "--install" ]; then
  echo "== Setting the Jetson up to look after itself (asks for your password once)"
  sudo -v || exit 1
  sudo systemctl enable tailscaled $SERVICES 2>&1 | grep -v "^Created symlink" || true
  sudo tailscale set --auto-update 2>/dev/null || true
  # the check-and-fix every 15 minutes, as root so it can restart services
  sudo tee /etc/systemd/system/vigil-doctor.service >/dev/null <<EOF
[Unit]
Description=VIGIL check and repair
After=network-online.target
[Service]
Type=oneshot
User=root
Environment=VIGIL_JETSON_DIR=$DEST
Environment=HOME=$HOME
ExecStart=/bin/bash $SELF --fix
EOF
  sudo tee /etc/systemd/system/vigil-doctor.timer >/dev/null <<'EOF'
[Unit]
Description=VIGIL check and repair every 15 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Persistent=true
[Install]
WantedBy=timers.target
EOF
  # restart the VIGIL services by themselves if they ever crash
  for s in $SERVICES; do
    sudo mkdir -p /etc/systemd/system/$s.service.d
    printf '[Unit]\nStartLimitIntervalSec=0\n[Service]\nRestart=always\nRestartSec=10\n' | sudo tee /etc/systemd/system/$s.service.d/10-vigil-restart.conf >/dev/null
  done
  # vigil-camera needs the network (camera and server) before it starts after a power cut
  sudo mkdir -p /etc/systemd/system/vigil-camera.service.d
  printf '[Unit]\nWants=network-online.target\nAfter=network-online.target vigil.service\n' | sudo tee /etc/systemd/system/vigil-camera.service.d/20-vigil-order.conf >/dev/null
  # system logs never grow past 300 MB
  sudo mkdir -p /etc/systemd/journald.conf.d
  printf '[Journal]\nSystemMaxUse=300M\n' | sudo tee /etc/systemd/journald.conf.d/vigil.conf >/dev/null
  sudo systemctl restart systemd-journald
  # the clock: needed for heartbeats, Tailscale and HTTPS
  sudo timedatectl set-ntp true 2>/dev/null || true
  sudo systemctl daemon-reload
  sudo systemctl enable --now vigil-doctor.timer
  echo "Done. Every 15 minutes the Jetson now checks itself and fixes what it can."
  echo "See what it did:  journalctl -u vigil-doctor -n 60 --no-pager"
  exit 0
fi

SUDO=""
if [ $FIXING = 1 ] && [ "$(id -u)" != 0 ]; then sudo -v && SUDO="sudo"; fi

echo "VIGIL Jetson check  $(date '+%Y-%m-%d %H:%M')  $([ $FIXING = 1 ] && echo '(fixing)')"

hdr "Clock"
up=$(awk '{printf "%.0f", $1/3600}' /proc/uptime)
ok "running for ${up} h since the last restart"
if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then ok "clock is synced ($(date '+%H:%M %Z'))"
else
  bad "clock not synced: heartbeats, Tailscale and HTTPS can fail"
  [ $FIXING = 1 ] && { $SUDO timedatectl set-ntp true; $SUDO systemctl restart systemd-timesyncd 2>/dev/null; fix "turned time sync on"; }
fi

hdr "Tailscale (how VIGIL Cloud reaches this Jetson)"
if ! command -v tailscale >/dev/null; then bad "Tailscale is not installed"
else
  if ! systemctl is-active -q tailscaled; then
    bad "tailscaled is not running"
    [ $FIXING = 1 ] && { $SUDO systemctl enable --now tailscaled; fix "started tailscaled"; sleep 5; }
  fi
  if ! systemctl is-enabled -q tailscaled 2>/dev/null; then
    warn "tailscaled does not start at boot"
    [ $FIXING = 1 ] && { $SUDO systemctl enable tailscaled; fix "tailscaled now starts at boot"; }
  fi
  ts=$(tailscale status --json 2>/dev/null)
  python3 - "$ts" <<'PY'
import json, sys, datetime
try:
    d = json.loads(sys.argv[1] or "{}")
except ValueError:
    d = {}
st = d.get("BackendState")
me = d.get("Self") or {}
ips = [i for i in (me.get("TailscaleIPs") or []) if "." in i]
if st != "Running":
    print("  \033[31mFAIL\033[0m  Tailscale is %s: VIGIL Cloud cannot reach this Jetson." % (st or "not running"))
    if st == "NeedsLogin":
        print("        Run: sudo tailscale up   and open the link it prints, signed in to the same Tailscale account")
    sys.exit(3)
print("  \033[32mOK\033[0m    Tailscale running, this Jetson is %s (%s)" % (", ".join(ips), me.get("HostName", "")))
want = __import__("os").environ.get("VIGIL_TAILSCALE_IP", "")
if want and ips and ips[0] != want:
    print("  \033[33mWARN\033[0m  Its Tailscale address changed (was %s). On the cloud VM run:" % want)
    print("        python3 ~/VIGIL/cloud/vigilctl.py add-remote --id home00 --name 'Home (test)' --url http://%s:5001 --key <the Jetson API key>" % ips[0])
exp = me.get("KeyExpiry")
if exp:
    t = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
    days = (t - datetime.datetime.now(datetime.timezone.utc)).days
    if days < 30:
        print("  \033[33mWARN\033[0m  Tailscale login expires in %d days. In the Tailscale admin page, open this machine's ... menu > Disable key expiry" % days)
    else:
        print("  \033[32mOK\033[0m    Tailscale login good for %d more days" % days)
else:
    print("  \033[32mOK\033[0m    Tailscale login never expires")
peers = [p for p in (d.get("Peer") or {}).values() if p.get("Online")]
print("  \033[32mOK\033[0m    %d other Tailscale machine(s) online (the cloud VM should be one)" % len(peers))
PY
  rc=$?
  if [ $rc = 3 ]; then
    BAD=$((BAD+1))
    if [ $FIXING = 1 ]; then $SUDO tailscale up >/dev/null 2>&1 & sleep 10; fix "asked Tailscale to connect (if it needs a login, run: sudo tailscale up)"; fi
  fi
fi

hdr "VIGIL services"
for s in $SERVICES; do
  a=$(systemctl is-active $s 2>/dev/null); e=$(systemctl is-enabled $s 2>/dev/null)
  if [ "$a" = active ]; then
    since=$(systemctl show $s -p ActiveEnterTimestamp --value)
    n=$(systemctl show $s -p NRestarts --value 2>/dev/null)
    ok "$s running since $since${n:+, restarted $n times}"
    [ "${n:-0}" -gt 20 ] 2>/dev/null && warn "$s keeps crashing and restarting: journalctl -u $s -n 80 --no-pager"
  else
    bad "$s is $a"
    journalctl -u $s -b -n 6 --no-pager -o cat 2>/dev/null | sed 's/^/        /'
    if [ $FIXING = 1 ]; then $SUDO systemctl reset-failed $s 2>/dev/null; $SUDO systemctl restart $s; fix "restarted $s"; fi
  fi
  if [ "$e" != enabled ]; then
    warn "$s does not start by itself after a power cut ($e)"
    [ $FIXING = 1 ] && { $SUDO systemctl enable $s 2>/dev/null; fix "$s now starts at boot"; }
  fi
done
[ $FIXING = 1 ] && [ $FIXED -gt 0 ] && { echo "  waiting 40 s for VIGIL to start..."; sleep 40; }

hdr "Server, camera picture and heartbeat"
KEY=$(python3 - <<'PY'
import os
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        cmd = open("/proc/%s/cmdline" % pid, "rb").read()
        if b"Server.py" not in cmd:
            continue
        env = dict(l.split("=", 1) for l in open("/proc/%s/environ" % pid, "rb").read().decode(errors="replace").split("\0") if "=" in l)
        if env.get("VIGIL_API_KEY"):
            print(env["VIGIL_API_KEY"]); break
    except Exception:
        pass
PY
)
[ -z "$KEY" ] && KEY=$($SUDO systemctl show vigil -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^VIGIL_API_KEY=//p')
if ss -ltn 2>/dev/null | grep -q ':5001 '; then ok "server listening on port 5001"; else bad "nothing listening on port 5001 (the vigil service)"; fi
if [ -z "$KEY" ]; then warn "could not read the API key (run with sudo to check the heartbeat)"
else
  st=$(curl -s -m 8 -H "X-VIGIL-Key: $KEY" http://localhost:5001/camera/status)
  python3 - "$st" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
except ValueError:
    print("  \033[31mFAIL\033[0m  the server did not answer /camera/status"); sys.exit(3)
m = d.get("last_seen_minutes_ago")
if d.get("online"):
    print("  \033[32mOK\033[0m    heartbeat %.0f s ago, %s fps: the app shows this camera online" % ((m or 0) * 60, d.get("fps")))
else:
    print("  \033[31mFAIL\033[0m  no heartbeat for %s: the app shows OFFLINE" % ("%.0f min" % m if m is not None else "ever"))
    sys.exit(3)
PY
  hb=$?
fi
for f in /dev/shm/vigil_latest.jpg; do
  if [ -f $f ]; then
    age=$(( $(date +%s) - $(stat -c %Y $f) ))
    if [ $age -lt 30 ]; then ok "camera picture is live (${age} s old)"; else bad "camera picture is ${age} s old: the camera or vigil-camera is stuck"; fi
  else bad "no camera picture at all: vigil-camera is not reading the camera"; fi
done
if [ "${hb:-0}" = 3 ]; then
  BAD=$((BAD+1))
  journalctl -u vigil-camera -n 200 --no-pager -o cat 2>/dev/null | grep -aiE "heartbeat|401|403|refused|error|Traceback" | tail -6 | sed 's/^/        /'
  if [ $FIXING = 1 ]; then $SUDO systemctl restart vigil vigil-camera; fix "restarted vigil and vigil-camera"; sleep 45
    s2=$(curl -s -m 8 -H "X-VIGIL-Key: $KEY" http://localhost:5001/camera/status)
    echo "$s2" | grep -q '"online": *true' && ok "heartbeat back after the restart" || bad "still no heartbeat after a restart: send this output to Claude"
  fi
fi
CAMLOG="$DEST/camera.log"
if [ -f "$CAMLOG" ]; then
  tb=$(tail -400 "$CAMLOG" | grep -ac Traceback)
  [ "$tb" = 0 ] && ok "no crashes in the camera log" || warn "$tb crash(es) in the last 400 lines of camera.log: tail -80 $CAMLOG"
fi

hdr "What wears a Jetson down over months"
pct=$(df --output=pcent / | tail -1 | tr -dc 0-9)
if [ "$pct" -ge 90 ]; then bad "disk ${pct}% full"
elif [ "$pct" -ge 80 ]; then warn "disk ${pct}% full"
else ok "disk ${pct}% full"; fi
if [ "$pct" -ge 80 ] && [ $FIXING = 1 ]; then
  $SUDO journalctl --vacuum-size=200M >/dev/null 2>&1
  find "$DEST" -maxdepth 1 -name "*.log.[0-9]*" -mtime +14 -delete 2>/dev/null
  fix "trimmed system logs and old rotated logs (biggest folders: du -sh $DEST/* | sort -h | tail)"
fi
for f in "$DEST"/*.log; do
  [ -f "$f" ] || continue
  sz=$(du -m "$f" | cut -f1)
  if [ "$sz" -gt 500 ]; then
    warn "$(basename "$f") is ${sz} MB"
    [ $FIXING = 1 ] && { tail -c 50M "$f" > "$f.tmp" && cat "$f.tmp" > "$f" && rm -f "$f.tmp"; fix "cut $(basename "$f") to its last 50 MB"; }
  fi
done
python3 - "$DEST/vigil.db" <<'PY'
import os, sqlite3, sys
db = sys.argv[1]
if not os.path.exists(db):
    print("  \033[31mFAIL\033[0m  no database at %s" % db); sys.exit(3)
mb = os.path.getsize(db) / 1e6
wal = os.path.getsize(db + "-wal") / 1e6 if os.path.exists(db + "-wal") else 0
try:
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=15)
    r = c.execute("PRAGMA quick_check").fetchone()[0]
    c.close()
except Exception as e:
    r = str(e)
if r == "ok":
    print("  \033[32mOK\033[0m    database healthy (%.0f MB%s)" % (mb, ", journal %.0f MB" % wal if wal > 1 else ""))
else:
    print("  \033[31mFAIL\033[0m  database check: %s. Stop and send this to Claude; do not delete anything." % r)
    sys.exit(3)
if wal > 200:
    print("  \033[33mWARN\033[0m  database journal is %.0f MB (restarting vigil folds it back in)" % wal)
PY
[ $? = 3 ] && BAD=$((BAD+1))
python3 - <<'PY'
m = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
avail = m["MemAvailable"] / 1024; total = m["MemTotal"] / 1024
swap = (m.get("SwapTotal", 0) - m.get("SwapFree", 0)) / 1024
tag = "\033[32mOK\033[0m  " if avail > 600 else "\033[33mWARN\033[0m"
print("  %s  memory: %.0f of %.0f MB free, %.0f MB in swap" % (tag, avail, total, swap))
import glob
hot = []
for z in glob.glob("/sys/class/thermal/thermal_zone*"):
    try:
        t = int(open(z + "/temp").read()) / 1000.0
        n = open(z + "/type").read().strip()
    except Exception:
        continue
    if 0 < t < 150:
        hot.append((t, n))
if hot:
    t, n = max(hot)
    tag = "\033[32mOK\033[0m  " if t < 75 else ("\033[33mWARN\033[0m" if t < 90 else "\033[31mFAIL\033[0m")
    print("  %s  hottest part %.0f C (%s)%s" % (tag, t, n, "" if t < 75 else ": check the fan and that nothing blocks the air"))
PY
pm=$(nvpmodel -q 2>/dev/null | grep -i "power mode" | head -1)
[ -n "$pm" ] && ok "${pm}"
if [ "$(id -u)" != 0 ] && [ -d "$HOME/VIGIL/.git" ] && [ -f "$DEST/VERSION" ]; then   # not as root: keeps the repo yours
  git -C "$HOME/VIGIL" fetch -q origin main 2>/dev/null
  have=$(cat "$DEST/VERSION"); want=$(git -C "$HOME/VIGIL" rev-parse --short origin/main 2>/dev/null)
  if [ -n "$want" ] && [ "${want#$have}" = "$want" ] && [ "${have#$want}" = "$have" ]; then
    warn "VIGIL code here is $have, newest is $want: bash ~/VIGIL/jetson-backend/jetson_update.sh"
  else ok "VIGIL code is up to date ($have)"; fi
fi
if systemctl list-timers 2>/dev/null | grep -q vigil-doctor; then ok "self-check runs every 15 minutes"
else warn "self-check is not set up: bash $SELF --install"; fi

echo
if [ $BAD = 0 ]; then printf "\033[32mAll good.\033[0m %d warning(s).\n" $WARNN
elif [ $FIXING = 1 ]; then printf "%d problem(s), %d fix(es) tried. Run again without --fix to see where it stands.\n" $BAD $FIXED
else printf "\033[31m%d problem(s).\033[0m Run:  bash %s --fix\n" $BAD "$SELF"; fi
exit $([ $BAD = 0 ] && echo 0 || echo 1)
