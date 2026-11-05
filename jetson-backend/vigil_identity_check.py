#!/usr/bin/env python3
"""
Who-is-who report: every correction made in the app, matched to the decision
that caused it, plus what each identity cue has learned.

    python3 vigil_identity_check.py            # last 2 hours
    python3 vigil_identity_check.py 6          # last 6 hours

Read-only. Paste the output to whoever is tuning identity.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
since = datetime.now() - timedelta(hours=HOURS)


def log_lines():
    pat = re.compile(r"\[(REID|REID7|V8|CORRECTION|LEARN|TRACK_VERIFY)\]")
    try:
        out = subprocess.run(["journalctl", "-u", "vigil-camera", "--since", since.strftime("%Y-%m-%d %H:%M:%S"),
                              "-o", "short-iso", "--no-pager"], capture_output=True, text=True, timeout=60).stdout
        lines = [l for l in out.splitlines() if pat.search(l)]
        if lines:
            return lines, "journal"
    except Exception:
        pass
    lines = []
    for d in (os.path.join(HERE, "logs"), HERE):
        try:
            for f in os.listdir(d):
                if f.endswith(".log") and "camera" in f:
                    with open(os.path.join(d, f), errors="replace") as fh:
                        lines += [l.rstrip() for l in fh if pat.search(l)]
        except OSError:
            pass
    return lines, "log files"


def path_of(why):
    w = why.lower()
    for key, name in (("v8 overrides", "fused evidence overruled"), ("v8 ", "fused evidence"),
                      ("fingerprint strong", "appearance, strong"), ("fingerprint+height", "appearance + height"),
                      ("fingerprint", "appearance"), ("clothes", "clothing colour + height")):
        if key in w:
            return name
    return "other"


lines, src = log_lines()
assign = {}          # track -> list of (line index, person, how)
counts = Counter()
for i, l in enumerate(lines):
    m = re.search(r"\[REID\] track (\d+) -> (.+?) \| (.*)", l)
    if m:
        t, who, why = int(m.group(1)), m.group(2), m.group(3)
        if who.startswith("NEW"):
            counts["new person created"] += 1
        elif who.startswith("NOT SURE"):
            counts["Not sure"] += 1
        else:
            counts["named by " + path_of(why)] += 1
            assign.setdefault(t, []).append((i, who, path_of(why)))
        continue
    m = re.search(r"\[V8\] RELINK track (\d+) -> (\S+)", l)
    if m:
        counts["kept name after a short loss"] += 1
        assign.setdefault(int(m.group(1)), []).append((i, m.group(2), "kept after a short loss"))
        continue
    if "[V8] SWAP" in l or "[REID7] SWAP" in l:
        counts["names swapped back after a check"] += 1
        for t, old, new in re.findall(r"track (\d+): (\S+) -> (\S+)", l):
            assign.setdefault(int(t), []).append((i, new, "swap after a check"))
    elif "released from" in l:
        counts["name taken off (evidence against)"] += 1
    elif "was on two people" in l:
        counts["same name on two people, fixed"] += 1
    elif "[TRACK_VERIFY]" in l:
        counts["re-identified (stopped matching)"] += 1

print("VIGIL who-is-who report, last %.0f h (log: %s, %d lines)" % (HOURS, src, len(lines)))
print("\nDecisions:")
for k, v in counts.most_common():
    print("  %-40s %d" % (k, v))

names = {}
try:
    db = sqlite3.connect(os.path.join(HERE, "vigil.db"))
    names = {r[0]: r[1] for r in db.execute("SELECT id, name FROM persons")}
    rows = db.execute("SELECT src, dst, created_at FROM person_corrections WHERE datetime(created_at,'localtime') >= ? "
                      "ORDER BY id", (since.strftime("%Y-%m-%d %H:%M:%S"),)).fetchall()
except Exception as e:
    rows = []
    print("\n(could not read corrections: %s)" % e)

kinds = Counter()
caused = Counter()
print("\nCorrections in the app: %d" % len(rows))
for s_, d_, at in rows:
    if d_ == "__deleted__":
        kinds["deleted a person"] += 1
        continue
    if s_ == d_:
        kinds["confirmed (Yes, this is ...)"] += 1
        continue
    if str(s_).startswith("track:"):
        t = int(s_.split(":")[1])
        hist = assign.get(t, [])
        if hist:
            _i, who, how = hist[-1]
            kinds["fixed a wrong name"] += 1
            caused[how] += 1
            print("  %s  track %s: shown as %s (%s), really %s" % (at, t, names.get(who, who), how, names.get(d_, d_)))
        else:
            kinds["named an unknown / Not sure person"] += 1
    else:
        kinds["merged or corrected a person entry"] += 1
for k, v in kinds.most_common():
    print("  %-40s %d" % (k, v))
if caused:
    print("\nWrong names came from:")
    for k, v in caused.most_common():
        print("  %-40s %d" % (k, v))

try:
    with open(os.path.join(HERE, "identity_v8.json")) as f:
        rep = json.load(f).get("report", {})
    print("\nWhat each cue has learned (examples: same person / different people, weight 0-1):")
    for c in ("appearance", "face", "body", "height", "walk"):
        r = rep.get(c)
        if r:
            print("  %-11s %4d / %-4d weight %.2f" % (c, r["same_examples"], r["different_examples"], r["weight"]))
    print("  face views kept:", {names.get(p, p): n for p, n in (rep.get("faces_known") or {}).items()})
    print("  walks kept:     ", {names.get(p, p): n for p, n in (rep.get("walks_known") or {}).items()})
except Exception:
    print("\n(no identity_v8.json yet: it appears after the first naming or correction)")
face = [l for l in lines if "face matching" in l]
if face:
    print("\n" + face[-1].split("]", 1)[-1].strip())
