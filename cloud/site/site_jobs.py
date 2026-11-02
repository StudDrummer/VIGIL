"""The Jetson ran these from cron. In the cloud they run from this loop.

  02:00  nightly anomaly check (Night_anamaly.py)
  03:00  per-person baselines and patterns
  every 6 h  log trimming, so a site never fills the disk
Times are the site's local time (TZ environment variable).
"""
import os
import subprocess
import time
from datetime import datetime

BASE = os.path.expanduser("~/passive-health-moniter")
JOBS = [
    (2, 0, ["python3", "Night_anamaly.py"]),
    (3, 0, ["python3", "person_baseline.py", "--commit"]),
    (3, 15, ["python3", "person_patterns.py", "--commit"]),
]


def trim_logs(limit=50 * 1024 * 1024):
    d = os.path.join(BASE, "logs")
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if os.path.getsize(p) > limit:
                with open(p, "rb") as f:
                    f.seek(-limit // 5, os.SEEK_END)
                    tail = f.read()
                with open(p, "wb") as f:
                    f.write(tail)
        except OSError:
            pass


def main():
    done = set()
    last_trim = 0
    while True:
        now = datetime.now()
        for h, m, cmd in JOBS:
            key = (now.date(), h, m)
            if now.hour == h and now.minute >= m and key not in done:
                done.add(key)
                if os.path.exists(os.path.join(BASE, cmd[1])):
                    print("[JOBS] %s %s" % (now.isoformat(timespec="minutes"), " ".join(cmd)), flush=True)
                    subprocess.run(cmd, cwd=BASE, timeout=3600)
        if time.time() - last_trim > 6 * 3600:
            trim_logs()
            last_trim = time.time()
        time.sleep(30)


if __name__ == "__main__":
    main()
