"""
VIGIL Person Pattern Job — runs nightly after person_baseline.py
Builds per-person time-of-day activity heatmap and detects absence from expected windows.
Add to cron: 0 3 * * * python3 /home/rushil-mohan/passive-health-moniter/person_patterns.py --commit
"""
import sqlite3, json, argparse
from datetime import datetime, timedelta
from collections import defaultdict

DB = '/home/rushil-mohan/passive-health-moniter/vigil.db'
LOOKBACK_DAYS = 21
MIN_DAYS_FOR_PATTERN = 5

def run(commit: bool):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    conn.execute("""CREATE TABLE IF NOT EXISTS person_patterns (
        person_id TEXT NOT NULL,
        hour_of_day INTEGER NOT NULL,
        avg_detections_per_day REAL,
        days_observed INTEGER,
        computed_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (person_id, hour_of_day)
    )""")

    residents = conn.execute(
        "SELECT id, name, room_id FROM persons WHERE name IS NOT NULL AND is_resident=1"
    ).fetchall()

    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')

    for p in residents:
        pid, name, room_id = p['id'], p['name'], p['room_id'] or 'room_1'
        rows = conn.execute("""
            SELECT strftime('%H', detected_at) as hour,
                   date(detected_at) as day,
                   COUNT(*) as n
            FROM person_detections
            WHERE person_id=? AND detected_at >= ?
            GROUP BY hour, day
        """, (pid, cutoff)).fetchall()

        if not rows:
            print(f"[{name}] No data in last {LOOKBACK_DAYS} days.")
            continue

        days_with_data = len(set(r['day'] for r in rows))
        if days_with_data < MIN_DAYS_FOR_PATTERN:
            print(f"[{name}] Only {days_with_data} days — need {MIN_DAYS_FOR_PATTERN}.")
            continue

        # Average detections per day per hour
        hour_day = defaultdict(lambda: defaultdict(int))
        for r in rows:
            hour_day[int(r['hour'])][r['day']] = r['n']

        print(f"\n[{name}] Activity pattern ({days_with_data} days):")
        for h in range(24):
            avg = sum(hour_day[h].values()) / days_with_data
            bar = '█' * int(avg)
            print(f"  {h:02d}:00  {bar} ({avg:.1f})")
            if commit:
                conn.execute("""INSERT OR REPLACE INTO person_patterns
                    (person_id, hour_of_day, avg_detections_per_day, days_observed, computed_at)
                    VALUES (?,?,?,?,datetime('now'))""",
                    (pid, h, avg, days_with_data))

        # Check for absence from expected active window today
        active_hours = [h for h in range(24) if
                        sum(hour_day[h].values()) / days_with_data >= 1.0]
        if active_hours:
            peak_start, peak_end = min(active_hours), max(active_hours)
            now_hour = datetime.now().hour
            # If we're past their expected wake time and no detections yet today
            today = datetime.now().strftime('%Y-%m-%d')
            today_count = conn.execute("""
                SELECT COUNT(*) FROM person_detections
                WHERE person_id=? AND date(detected_at)=?
            """, (pid, today)).fetchone()[0]
            if now_hour >= peak_start + 2 and today_count == 0:
                print(f"  *** ALERT: {name} not seen today — normally active by {peak_start}:00 ***")
                if commit:
                    conn.execute("""INSERT INTO person_alerts
                        (alert_type, room_id, person_ids, resolved, created_at)
                        VALUES ('absence_pattern',?,?,0,datetime('now'))""",
                        (room_id, json.dumps([pid])))

    if commit:
        conn.commit()
        print("\n[OK] Patterns and alerts committed.")
    else:
        print("\n[DRY RUN] Nothing written.")
    conn.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    run(args.commit)
