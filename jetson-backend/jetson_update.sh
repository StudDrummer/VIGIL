#!/bin/bash
# JETSON_UPDATE_V1: put the newest code (main) on the home Jetson and restart VIGIL.
#
#   bash ~/VIGIL/jetson-backend/jetson_update.sh          update and restart
#   bash ~/VIGIL/jetson-backend/jetson_update.sh --undo   put the previous files back
#
# First time on the Jetson:
#   git clone https://github.com/StudDrummer/VIGIL.git ~/VIGIL
#
# Copies the code into ~/passive-health-moniter (set VIGIL_JETSON_DIR to change),
# keeps a backup of every file it replaces, never touches the database, tuned
# settings (*.json) or models, then restarts whatever runs VIGIL there: systemd
# services are restarted, programs started by hand are started again the same way.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${VIGIL_JETSON_DIR:-$HOME/passive-health-moniter}"
[ -f "$DEST/Server.py" ] || { echo "No VIGIL found in $DEST. Set VIGIL_JETSON_DIR=/path/to/it"; exit 1; }
BK="$DEST/.vigil-backups"

if [ "${1:-}" = "--undo" ]; then
  last=$(ls -1d "$BK"/* 2>/dev/null | tail -1)
  [ -n "$last" ] || { echo "No backup to restore."; exit 1; }
  cp -a "$last/." "$DEST/" && echo "Restored files from $(basename "$last")."
  mv "$last" "$last.undone"
else
  git -C "$REPO" checkout -q main 2>/dev/null
  git -C "$REPO" pull -q --ff-only || echo "(could not reach GitHub; using the copy here)"
  stamp=$(date +%Y%m%d-%H%M%S); mkdir -p "$BK/$stamp"
  n=0
  for f in "$REPO"/jetson-backend/*.py "$REPO"/jetson-backend/vigil_bytetrack.yaml; do
    b=$(basename "$f")
    if ! cmp -s "$f" "$DEST/$b"; then
      [ -f "$DEST/$b" ] && cp -a "$DEST/$b" "$BK/$stamp/"
      cp "$f" "$DEST/$b"; n=$((n+1)); echo "  updated $b"
    fi
  done
  for f in "$REPO"/jetson-backend/*.json; do        # settings only if missing: yours are kept
    [ -f "$DEST/$(basename "$f")" ] || cp "$f" "$DEST/"
  done
  git -C "$REPO" rev-parse --short HEAD > "$DEST/VERSION"
  ls -1d "$BK"/* 2>/dev/null | head -n -10 | xargs -r rm -rf     # keep the last 10 backups
  if [ $n = 0 ]; then echo "Already up to date ($(cat "$DEST/VERSION"))."; exit 0; fi
  echo "$n file(s) updated to $(cat "$DEST/VERSION"). Backup: $BK/$stamp"
fi

echo "== Restarting VIGIL"
python3 - "$DEST" <<'PY'
import os, re, signal, subprocess, sys, time
dest = os.path.realpath(sys.argv[1])
found = []
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        cmd = [c.decode() for c in open("/proc/%s/cmdline" % pid, "rb").read().split(b"\0") if c]
        cwd = os.readlink("/proc/%s/cwd" % pid)
    except Exception:
        continue
    if not cmd or "python" not in os.path.basename(cmd[0]) or int(pid) == os.getpid():
        continue
    script = next((c for c in cmd[1:] if c.endswith(".py")), None)
    if not script:
        continue
    sp = os.path.realpath(script if os.path.isabs(script) else os.path.join(cwd, script))
    if os.path.dirname(sp) != dest:
        continue
    try:
        cg = open("/proc/%s/cgroup" % pid).read()
    except Exception:
        cg = ""
    unit = None
    m = re.findall(r"/([^/\s]+\.service)", cg)
    if m and not m[-1].startswith("user@"):
        unit = (m[-1], "user@" in cg)
    ppid = open("/proc/%s/stat" % pid).read().rsplit(")", 1)[1].split()[1]
    try:
        parent = open("/proc/%s/cmdline" % ppid, "rb").read().replace(b"\0", b" ").decode()
    except Exception:
        parent = ""
    try:
        env = dict(l.split("=", 1) for l in open("/proc/%s/environ" % pid, "rb").read().decode(errors="replace").split("\0") if "=" in l)
    except PermissionError:
        env = None
    found.append(dict(pid=int(pid), cmd=cmd, cwd=cwd, unit=unit, env=env, looped=".sh" in parent, name=os.path.basename(sp)))

if not found:
    print("  Nothing running from %s. Start VIGIL the way you normally do." % dest)
    sys.exit(0)
units = {f["unit"] for f in found if f["unit"]}
for u, user in sorted(units):
    r = subprocess.run(["systemctl", "--user", "restart", u] if user else ["sudo", "systemctl", "restart", u])
    print("  restarted service %s%s" % (u, "" if r.returncode == 0 else " (FAILED)"))
hand = [f for f in found if not f["unit"]]
for f in hand:
    try:
        os.kill(f["pid"], signal.SIGTERM)
    except PermissionError:
        print("  %s runs as another user: run this script with sudo" % f["name"]); f["skip"] = True
    except ProcessLookupError:
        pass
for f in hand:
    for _ in range(50):
        if not os.path.exists("/proc/%d" % f["pid"]):
            break
        time.sleep(0.2)
    else:
        try: os.kill(f["pid"], signal.SIGKILL)
        except Exception: pass
order = lambda f: (0 if f["name"].lower() == "server.py" else 1)
for f in sorted(hand, key=order):
    if f.get("skip"):
        continue
    if f["looped"]:
        print("  %s restarts by itself (its start script keeps it running)" % f["name"]); continue
    os.makedirs(os.path.join(dest, "logs"), exist_ok=True)
    log = open(os.path.join(dest, "logs", f["name"].replace(".py", ".log")), "a")
    subprocess.Popen(f["cmd"], cwd=f["cwd"], env=f["env"] or os.environ, stdout=log, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    print("  started %s again (log: logs/%s)" % (f["name"], f["name"].replace(".py", ".log")))
    if order(f) == 0:
        time.sleep(4)
PY
sleep 6
curl -fsS -m 5 http://localhost:5001/health >/dev/null 2>&1 && echo "Server answering. Done." || echo "Server not answering yet: give it a minute, then check  curl localhost:5001/health"
