#!/usr/bin/env python3
"""The Sunday numbers: one line per camera for the last 7 days, from VIGIL Cloud.

    python3 tools/weekly_numbers.py                      # asks for your VIGIL email and password
    python3 tools/weekly_numbers.py --days 14

Prints a table and adds the rows to weekly_numbers.csv (open it in Sheets or Excel). Uses only
what VIGIL already records: fall alerts and how caregivers marked them, falls reported as
missed, how fast someone checked, and whether each camera is online right now.
Lab test falls are never counted. Works on any computer with Python 3; nothing to install.
"""
import argparse
import csv
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("VIGIL_API", "https://api.vigilhealth.me")


def call(path, token=None, body=None, timeout=15):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"X-VIGIL-Key": token} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--csv", default="weekly_numbers.csv")
    a = ap.parse_args()
    email = os.environ.get("VIGIL_EMAIL") or input("VIGIL email: ").strip()
    pw = os.environ.get("VIGIL_PASSWORD") or getpass.getpass("Password: ")
    try:
        tok = call("/auth/login", body={"email": email, "password": pw, "device": "weekly numbers"})["token"]
    except urllib.error.HTTPError as e:
        sys.exit("Sign-in failed: %s" % e.read().decode()[:200])
    board = {}
    try:
        ov = call("/overview", tok, timeout=30)
        rows = ov.get("rooms") or ov.get("sites") or []
        board = {r["id"]: r for r in rows}
    except Exception:
        pass
    sites = call("/sites/status", tok)["sites"]
    today = time.strftime("%Y-%m-%d")
    out = []
    for s in sites:
        sid = s["id"]
        name = s.get("name") or sid
        try:
            acc = call("/s/%s/alerts/accuracy?days=%d" % (sid, a.days), tok)
        except Exception as e:
            acc = {"error": str(e)[:60]}
        falls = acc.get("fall_alerts")
        false_ = acc.get("false_alarms")
        out.append({
            "week_ending": today, "camera": sid, "name": name,
            "online_now": {True: "yes", False: "NO"}.get((board.get(sid) or {}).get("online", s.get("online")), "?"),
            "fall_alerts": falls, "real": acc.get("confirmed_real"), "false_alarms": false_,
            "unsure": acc.get("unsure"), "not_marked": acc.get("not_reviewed"),
            "missed_reported": acc.get("missed_reported"),
            "false_per_week": round(false_ / (a.days / 7.0), 2) if isinstance(false_, int) else None,
            "median_check_min": round(acc["median_response_s"] / 60, 1) if acc.get("median_response_s") else None,
            "error": acc.get("error", ""),
        })
    cols = ["camera", "name", "online_now", "fall_alerts", "real", "false_alarms", "not_marked", "missed_reported",
            "false_per_week", "median_check_min"]
    widths = {c: max(len(c), *(len(str(r[c] if r[c] is not None else "-")) for r in out)) for c in cols} if out else {}
    print("\nLast %d days, as of %s\n" % (a.days, today))
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in out:
        print("  ".join(str(r[c] if r[c] is not None else "-").ljust(widths[c]) for c in cols) +
              ("   (%s)" % r["error"] if r["error"] else ""))
    bad = [r for r in out if (r["false_per_week"] or 0) >= 1]
    print("\nGate check: fewer than 1 false alarm per room per week -> %s" %
          ("PASS" if out and not bad else "not yet: " + ", ".join(r["name"] for r in bad) if bad else "no cameras"))
    unmarked = sum(r["not_marked"] or 0 for r in out)
    if unmarked:
        print("Mark the %d unmarked fall alert(s) in the app (Was this a fall?) so these numbers are true." % unmarked)
    new = not os.path.exists(a.csv)
    with open(a.csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()) if out else ["week_ending"])
        if new:
            w.writeheader()
        w.writerows(out)
    print("Added to %s" % os.path.abspath(a.csv))


if __name__ == "__main__":
    main()
