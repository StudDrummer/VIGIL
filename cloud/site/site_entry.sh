#!/bin/bash
# Starts one VIGIL site: the server, the camera pipeline and the nightly jobs.
# Each runs in its own restart loop, so a crash costs seconds, not the night.
set -u
BASE="$HOME/passive-health-moniter"          # the site's persistent volume
mkdir -p "$BASE/logs"
cd "$BASE"

# Code always comes from the image (updates ship by rebuilding); data stays.
CODE="${VIGIL_CODE_DIR:-/opt/vigil-code}"
cp -f "$CODE"/*.py "$CODE"/vigil_bytetrack.yaml "$CODE"/yolov8n-pose.pt "$BASE"/
[ -f "$BASE/fall_zones.json" ] || echo '[]' > "$BASE/fall_zones.json"
if [ ! -d "$BASE/torch_cache" ] && [ -d /opt/torch_cache ]; then cp -r /opt/torch_cache "$BASE/torch_cache"; fi
export TORCH_HOME="$BASE/torch_cache"

: "${VIGIL_API_KEY:?VIGIL_API_KEY must be set}"
: "${VIGIL_RTSP_URL:?VIGIL_RTSP_URL must be set}"

run_forever () {   # name, command...
  local name="$1"; shift
  while true; do
    echo "[SITE] starting $name"
    "$@" >> "$BASE/logs/$name.log" 2>&1
    echo "[SITE] $name exited with $?; restarting in 5 s" | tee -a "$BASE/logs/$name.log"
    sleep 5
  done
}

run_forever server python3 Server.py &
sleep 5
run_forever camera python3 activity_monitor.py --source "$VIGIL_RTSP_URL" \
    --server http://127.0.0.1:5001 --room-id "${VIGIL_ROOM_ID:-room_1}" &
run_forever jobs python3 site_jobs.py &

# Keep the container's own log short and readable.
tail -F "$BASE/logs/server.log" "$BASE/logs/camera.log" 2>/dev/null | grep --line-buffered -E "FALL|ALERT|ERROR|FATAL|Traceback|SITE|INIT" &
wait -n
