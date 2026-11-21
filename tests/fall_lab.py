#!/usr/bin/env python3
"""FALL_LAB_V1: guided fall test that RECORDS every trial.

Run on the Jetson, with VIGIL running and the camera seeing the whole room:

    tmux new -s lab        (keeps running if the connection drops; reattach: tmux attach -t lab)
    python3 ~/VIGIL/tests/fall_lab.py

For each trial it tells you what to do, counts down, then (a) checks whether
VIGIL raised a fall alert and (b) saves the stick figures of the last 40 s
(fall_probes/NN_label.json). At the end it prints the score and packs every
recording into ~/vigil_fall_lab.tar.gz, so the detector can be replayed and
tuned on YOUR falls, not made-up ones.

Use a thick mattress or crash pad for every fall. Stop at any time with Ctrl-C:
everything recorded so far is kept and packed.

    python3 ~/VIGIL/tests/fall_lab.py --video

also saves a short video clip of each trial in fall_probes/videos/ (MODEL_CHECK_V1), so a
different pose model can be compared on exactly these trials with tools/pose_compare.py. The
clips stay on the Jetson: they are never packed into the file you send. Delete them with
rm -r ~/passive-health-moniter/fall_probes/videos when you are done.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import time
import threading
import urllib.request

BASE = os.environ.get("VIGIL_JETSON_DIR", os.path.expanduser("~/passive-health-moniter"))
LOGS = [os.path.join(BASE, p) for p in ("camera.log", "logs/camera.log")]
PROBES = os.path.join(BASE, "fall_probes")
FLAG = "/dev/shm/vigil_probe"
LAB_FLAG = "/dev/shm/vigil_lab"     # LAB_MODE_V1: the server records lab falls quietly while this is fresh
DB = os.path.join(BASE, "vigil.db")
UNIT = os.environ.get("VIGIL_CAMERA_UNIT", "vigil-camera")
STARTED = time.time()
GAP_S = 32           # the detector waits 30 s between alerts

# (label, is_fall, instructions, seconds to do it and stay)
TRIALS = [
    ("walk", False, "Walk normally back and forth across the room.", 20),
    ("sit_chair_fast", False, "Walk to a chair and drop into it briskly, as you would when tired.", 20),
    ("bend_pick_up", False, "Bend down, pick something up off the floor, stand up.", 15),
    ("crouch_10s", False, "Crouch or squat, hold 10 seconds, stand up.", 20),
    ("lie_bed", False, "Lie down on the bed or couch the normal way. Stay 10 seconds.", 25),
    ("couch_slouch_fidget", False, "Sit on the couch or a low chair, slouch, shift around, lean forward and back, "
     "reach for things, the way people really sit for a while (the Nov 16 false alarms). Keep it up 45 s.", 50),
    ("sit_floor_on_purpose", False, "Sit down on the floor on purpose, cross-legged, like you would to talk or eat. Stay 20 s.", 35),
    ("kneel_look_under", False, "Kneel and look under the bed or couch for 10 seconds, then stand.", 20),
    ("fall_forward", True, "From KNEELING by the mattress, fall forward onto it. Stay still 20 s.", 30),
    ("fall_sideways", True, "From KNEELING, fall sideways onto the mattress. Stay still 20 s.", 30),
    ("fall_backward_sit", True, "From SQUATTING, fall back onto your bottom on the mattress, then lie back. Stay 20 s.", 30),
    ("fall_toward_camera", True, "Kneel facing the camera and fall forward TOWARD the camera onto the mattress. Stay 20 s.", 30),
    ("fall_slump_chair_to_floor", True, "Sit on a chair at the mattress edge, slide off onto the mattress quickly, end SITTING on it. Stay 25 s.", 40),
    ("fall_slow_collapse", True, "From standing, sink slowly (about 3 s) down to the mattress as if fainting, end lying. Stay 20 s.", 30),
    ("fall_behind_furniture", True, "Fall onto the mattress where a chair or table partly hides you. Stay 20 s.", 30),
]


def log_size():
    for p in LOGS:
        if os.path.exists(p):
            return p, os.path.getsize(p)
    return None, 0


def db_max():
    """Highest alert id so far. LAB_DB_V1: the lab scores from the alerts VIGIL actually stored."""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        n = c.execute("SELECT COALESCE(MAX(id),0) FROM alerts").fetchone()[0]
        c.close()
        return n
    except sqlite3.Error:
        return None


def db_falls_since(last_id):
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        rows = c.execute("SELECT id, detected_at, payload FROM alerts WHERE alert_type='fall' AND id > ? ORDER BY id",
                         (last_id,)).fetchall()
        c.close()
        return ["alert #%d at %s: %s" % (r[0], r[1], r[2]) for r in rows]
    except sqlite3.Error:
        return []


def journal(since):
    """The camera's log when VIGIL runs as a systemd service (no camera.log file)."""
    try:
        out = subprocess.run(["journalctl", "-u", UNIT, "-o", "cat", "--no-pager", "--since", "@%d" % int(since)],
                             capture_output=True, text=True, timeout=20)
        return out.stdout.splitlines() if out.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        return []


def camera_url():
    """The camera address VIGIL itself uses: VIGIL_RTSP_URL, or the running camera process's --source."""
    u = os.environ.get("VIGIL_RTSP_URL")
    if u:
        return u
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            args = open("/proc/%s/cmdline" % pid, "rb").read().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"activity_monitor.py") for a in args) and b"--source" in args:
            i = args.index(b"--source")
            if i + 1 < len(args):
                return args[i + 1].decode()
    return None


class Clip:
    """MODEL_CHECK_V1: records one trial from the camera, with each frame's time, for tools/pose_compare.py."""

    def __init__(self, url):
        import cv2
        self.cv2 = cv2
        self.cap = cv2.VideoCapture(url)
        self.ok = self.cap.isOpened()
        self.rec = None
        self.lock = threading.Lock()
        if self.ok:
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            with self.lock:
                if self.rec is not None:
                    w, times = self.rec
                    h0, w0 = frame.shape[:2]
                    if w0 > 960:
                        frame = self.cv2.resize(frame, (960, int(h0 * 960 / w0)))
                    if w is None:
                        path = self.path
                        w = self.cv2.VideoWriter(path, self.cv2.VideoWriter_fourcc(*"mp4v"), 15.0,
                                                 (frame.shape[1], frame.shape[0]))
                        self.rec = (w, times)
                    w.write(frame)
                    times.append(time.time())

    def start(self, path):
        with self.lock:
            self.path = path
            self.rec = (None, [])

    def stop(self):
        with self.lock:
            if self.rec is None:
                return
            w, times = self.rec
            self.rec = None
        if w is not None:
            w.release()
            with open(self.path + ".json", "w") as f:
                json.dump({"times": times}, f)


def lab_flag(on=True):
    try:
        if on:
            with open(LAB_FLAG, "w") as f:
                f.write(str(int(time.time())))
        elif os.path.exists(LAB_FLAG):
            os.remove(LAB_FLAG)
    except OSError:
        pass


def falls_since(path, offset, t0=None, last_id=None):
    """Fall decisions since the mark: from the alerts table (primary), else the camera log."""
    if last_id is not None:
        return db_falls_since(last_id)
    if path:
        with open(path, errors="replace") as f:
            f.seek(offset)
            # only real alert decisions: "[FALL] track=..." (LAB_FIX_V1: not "[FALL] 2 furniture zone(s) loaded")
            return [l.strip() for l in f if l.startswith("[FALL] track=")]
    return [l.strip() for l in journal(t0 or time.time()) if l.startswith("[FALL] track=")]


def countdown(s, msg):
    for r in range(s, 0, -1):
        sys.stdout.write("\r    %s %2ds " % (msg, r)); sys.stdout.flush(); time.sleep(1)
    print("\r    %s done.   " % msg)


def pack(results):
    with open(os.path.join(PROBES, "lab_results.json"), "w") as f:
        json.dump(results, f, indent=1)
    out = os.path.expanduser("~/vigil_fall_lab.tar.gz")
    with tarfile.open(out, "w:gz") as tz:
        tz.add(PROBES, arcname="fall_probes",
               filter=lambda ti: None if "/videos" in ti.name else ti)   # clips never leave the Jetson
        for p in LOGS:
            if os.path.exists(p):
                tz.add(p, arcname="camera.log")
                break
        else:
            jl = os.path.join(PROBES, "camera_journal.log")
            with open(jl, "w") as f:
                f.write("\n".join(journal(STARTED - 60)))
            tz.add(jl, arcname="camera.log")
    print("\nPacked: %s  (send this file)" % out)


def preflight():
    """PREFLIGHT_V1: prove everything works before anyone falls on a mattress."""
    ok = True
    def line(good, msg):
        nonlocal ok
        ok = ok and good
        print("  [%s] %s" % (" OK " if good else "FIX ", msg))
    path, size = log_size()
    if path:
        line(time.time() - os.path.getmtime(path) < 120, "camera log is being written (%s)" % path)
    else:
        j = journal(time.time() - 24 * 3600)
        line(bool(j), "camera log readable from the %s service (journalctl)" % UNIT)
    line(db_max() is not None, "VIGIL's alert database is readable (%s)" % DB)
    try:
        urllib.request.urlopen("http://localhost:5001/health", timeout=3).read()
        line(True, "VIGIL server is answering")
    except Exception:
        line(False, "VIGIL server is answering (http://localhost:5001/health)")
    lab_flag(True)
    line(os.path.exists(LAB_FLAG), "lab mode can be switched on (phones stay quiet during trials)")
    lab_flag(False)
    try:
        d = json.load(open("/dev/shm/vigil_in_view.json"))
        line(time.time() - float(d.get("t", 0)) < 5, "camera is analysing frames right now")
    except Exception:
        line(False, "camera is analysing frames right now (no /dev/shm/vigil_in_view.json)")
    os.makedirs(PROBES, exist_ok=True)
    name = "preflight_%d" % int(time.time())
    with open(FLAG, "w") as f:
        f.write(name)
    target = os.path.join(PROBES, name + ".json")
    for _ in range(8):
        if os.path.exists(target):
            break
        time.sleep(0.5)
    saved = os.path.exists(target)
    line(saved, "recording a trial works (stand in view for this check)")
    if saved:
        j = json.load(open(target))
        n = max((len(v) for v in (j.get("tracks") or {}).values()), default=0)
        line(n > 0, "the recording has stick figures in it (%d frames)" % n)
        line(bool(j.get("shape")), "the recording knows the picture size")
        line(True, "bed and couch zones in the recording: %d" % len(j.get("zones") or []))
        os.remove(target)
    print("\nReady for the fall lab." if ok else "\nFix the items marked FIX first.")
    return ok


def main():
    if "--check" in sys.argv:
        sys.exit(0 if preflight() else 1)
    os.makedirs(PROBES, exist_ok=True)
    if db_max() is None:
        print("Cannot read VIGIL's database at %s. Set VIGIL_JETSON_DIR." % DB)
        sys.exit(1)
    print(__doc__)
    print("Lab mode is ON while this runs: falls are recorded as lab tests, nobody's phone rings.\n")
    clip = None
    if "--video" in sys.argv:
        url = camera_url()
        clip = Clip(url) if url else None
        if clip is None or not clip.ok:
            print("Could not open the camera for video clips; continuing without them.\n")
            clip = None
        else:
            os.makedirs(os.path.join(PROBES, "videos"), exist_ok=True)
            print("Saving a video clip of each trial in fall_probes/videos/ (kept on the Jetson).\n")
    # LAB_RESUME_V1: results are saved after every trial; a dropped connection loses nothing
    saved = os.path.join(PROBES, "lab_results.json")
    results, start = [], 1
    try:
        prev = json.load(open(saved))
        if prev and len(prev) < len(TRIALS) and input("Continue the last run from trial %d? (Y/n) " % (len(prev) + 1)).lower() != "n":
            results, start = prev, len(prev) + 1
    except Exception:
        pass
    import atexit
    atexit.register(lab_flag, False)          # never leave phones silenced after the lab
    input("Press Enter when the room is clear, the mattress is down and you are ready...")
    stamp = time.strftime("%m%d%H%M")
    try:
        for i, (label, is_fall, how, secs) in enumerate(TRIALS, 1):
            if i < start:
                continue
            print("\n[%d/%d] %s%s" % (i, len(TRIALS), "FALL: " if is_fall else "", how))
            input("    Press Enter, then start within 3 seconds...")
            lab_flag(True)
            path, off = log_size()
            t0, last_id = time.time(), db_max()
            name = "%s_%02d_%s" % (stamp, i, label)
            if clip:
                clip.start(os.path.join(PROBES, "videos", name + ".mp4"))
            countdown(3, "get ready")
            countdown(secs, "go")
            if clip:
                clip.stop()
            with open(FLAG, "w") as f:
                f.write(name)
            time.sleep(2.5)                        # the camera saves the recording within 1 s
            hits = falls_since(path, off, t0, last_id)
            ok = bool(hits) == is_fall
            results.append({"label": label, "is_fall": is_fall, "alerted": bool(hits), "log": hits, "probe": name})
            print("    %s  VIGIL %s" % ("RIGHT" if ok else "WRONG", "raised a fall alert" if hits else "stayed quiet"))
            json.dump(results, open(saved, "w"), indent=1)
            if i < len(TRIALS):
                countdown(GAP_S, "stand up and rest")
    except KeyboardInterrupt:
        print("\nStopped early.")
    finally:
        lab_flag(False)
    f_ = [r for r in results if r["is_fall"]]; a_ = [r for r in results if not r["is_fall"]]
    print("\nFalls caught %d/%d   ordinary activity left alone %d/%d" % (
        sum(r["alerted"] for r in f_), len(f_), sum(not r["alerted"] for r in a_), len(a_)))
    pack(results)


if __name__ == "__main__":
    main()
