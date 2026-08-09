"""
VIGIL Per-Person Baseline & Anomaly Job
Run nightly via cron (e.g. 3:00am, after Night_anamaly.py and
nightly_ml_classifier.py). Computes each NAMED person's personal
gait/posture baseline from their own history and flags today's
reading as a health_signal alert if it deviates significantly.

Usage:
  python3 person_baseline.py --dry-run     # print only, no writes
  python3 person_baseline.py --commit      # write baselines + alerts
"""
import sqlite3
import json
import argparse
import statistics
from datetime import datetime, timedelta

DB = '/home/rushil-mohan/passive-health-moniter/vigil.db'

MIN_DAYS_FOR_BASELINE = 7     # need this many days of history before trusting a baseline
LOOKBACK_DAYS         = 30    # how far back to pull history from
ANOMALY_Z             = 1.5   # std-devs from personal mean to flag as anomalous

METRICS = [
    ("avg_speed",     "Walking Speed",   "m/s", False),  # (col, label, unit, higher_is_worse)
    ("avg_cadence",   "Cadence",         "spm", False),
    ("avg_posture",   "Posture Score",   "/100", False),
    ("avg_asymmetry", "Gait Asymmetry",  "%",   True),
]


def get_named_persons(conn):
    return conn.execute("""
        SELECT id, name, room_id FROM persons
        WHERE name IS NOT NULL AND name != ''
    """).fetchall()


def get_person_history(conn, person_id, days=LOOKBACK_DAYS):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return conn.execute("""
        SELECT date(detected_at) as date,
               AVG(NULLIF(gait_speed_ms, 0))   as avg_speed,
               AVG(NULLIF(cadence_spm, 0))     as avg_cadence,
               AVG(posture_score)              as avg_posture,
               AVG(asymmetry_pct)              as avg_asymmetry,
               COUNT(*) as samples
        FROM person_detections
        WHERE person_id = ? AND detected_at >= ?
        GROUP BY date(detected_at)
        ORDER BY date ASC
    """, (person_id, cutoff)).fetchall()


def ensure_baseline_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS person_baselines (
            person_id TEXT NOT NULL,
            metric TEXT NOT NULL,
            mean_val REAL,
            std_val REAL,
            days_used INTEGER,
            computed_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (person_id, metric)
        )
    """)
    conn.commit()


def compute_and_store(conn, commit: bool, explain: bool):
    ensure_baseline_table(conn)
    persons = get_named_persons(conn)
    if not persons:
        print("[INFO] No named persons yet — nothing to baseline.")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")

    for p in persons:
        pid, name, room_id = p["id"], p["name"], p["room_id"]
        rows = get_person_history(conn, pid)
        days_with_data = len(rows)

        if days_with_data < MIN_DAYS_FOR_BASELINE:
            print(f"[{name:20s}] {days_with_data}/{MIN_DAYS_FOR_BASELINE} days — "
                  f"building baseline, not enough history yet.")
            continue

        today_row = next((r for r in rows if r["date"] == today_str), None)

        for col, label, unit, higher_is_worse in METRICS:
            vals = [r[col] for r in rows if r[col] is not None]
            if len(vals) < MIN_DAYS_FOR_BASELINE:
                continue
            mean_v = statistics.mean(vals)
            std_v  = statistics.pstdev(vals) if len(vals) > 1 else 0.0

            if commit:
                conn.execute("""
                    INSERT INTO person_baselines (person_id, metric, mean_val, std_val, days_used, computed_at)
                    VALUES (?,?,?,?,?,datetime('now'))
                    ON CONFLICT(person_id, metric) DO UPDATE SET
                        mean_val=excluded.mean_val, std_val=excluded.std_val,
                        days_used=excluded.days_used, computed_at=excluded.computed_at
                """, (pid, col, mean_v, std_v, len(vals)))

            if explain:
                print(f"[{name:20s}] {label:16s} baseline mean={mean_v:.2f} std={std_v:.2f} "
                      f"({len(vals)} days)")

            # Check today's reading against this metric's baseline
            if today_row is not None and today_row[col] is not None and std_v > 0.001:
                z = (today_row[col] - mean_v) / std_v
                is_anomaly = (z >= ANOMALY_Z) if higher_is_worse else (abs(z) >= ANOMALY_Z)
                if is_anomaly:
                    direction = "above" if today_row[col] > mean_v else "below"
                    msg = (f"{label} today ({today_row[col]:.2f} {unit}) is "
                           f"{abs(z):.1f} std devs {direction} {name}'s personal "
                           f"{len(vals)}-day baseline ({mean_v:.2f} {unit}).")
                    print(f"  [ANOMALY] {name}: {msg}")
                    if commit:
                        existing = conn.execute("""
                            SELECT id FROM person_alerts
                            WHERE alert_type='health_signal' AND room_id=?
                            AND person_ids=? AND date(created_at)=date('now')
                            AND resolved=0
                        """, (room_id, json.dumps([pid]))).fetchone()
                        if not existing:
                            conn.execute("""
                                INSERT INTO person_alerts (alert_type, room_id, person_ids, resolved, created_at)
                                VALUES ('health_signal', ?, ?, 0, datetime('now'))
                            """, (room_id, json.dumps([pid])))

    if commit:
        conn.commit()
        print("\n[OK] Baselines and alerts committed.")
    else:
        print("\n[DRY RUN] No changes written. Run with --commit to save.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--explain", action="store_true", help="print per-metric baseline detail")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    compute_and_store(conn, commit=args.commit, explain=args.explain)
    conn.close()
