"""
VIGIL — Nightly Anomaly Detection
nightly_anomaly.py

Runs once per night via cron (e.g. 2am). Fits an Isolation Forest on the
last 30 days of daily_summary data, scores every day, and writes anomaly
scores + flags back into the DB. Also computes 14-day z-scores per metric
for the server-side health_signal alerts.

Install:
    pip install scikit-learn numpy --break-system-packages

Cron setup (run on Jetson as rushil-mohan):
    crontab -e
    Add: 0 2 * * * /usr/bin/python3 /home/rushil-mohan/passive-health-moniter/nightly_anomaly.py >> /home/rushil-mohan/passive-health-moniter/logs/anomaly.log 2>&1

Usage:
    python3 nightly_anomaly.py              # normal run
    python3 nightly_anomaly.py --days 60   # use 60-day window
    python3 nightly_anomaly.py --dry-run   # print results, don't write DB
    python3 nightly_anomaly.py --verbose   # print per-metric z-scores
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta

DB_PATH  = os.path.expanduser("~/passive-health-moniter/vigil.db")
LOG_PATH = os.path.expanduser("~/passive-health-moniter/logs/anomaly.log")

# Metrics fed to Isolation Forest — all numeric columns in daily_summary
IFOREST_FEATURES = [
    "step_count",
    "hrv_sdnn",
    "resting_hr",
    "sleep_hours",
    "active_calories",
    "spo2_avg",
    "respiratory_rate",
    "walking_speed_ms",
    "walking_asymmetry_pct",
    "double_support_pct",
]

# Metrics used for z-score health_signal alerts — must have clinical meaning
ZSCORE_METRICS = {
    "hrv_sdnn":              ("HRV SDNN",            "ms",  False),  # lower = worse
    "resting_hr":            ("Resting HR",           "bpm", True),   # higher = worse
    "spo2_avg":              ("SpO2",                 "%",   False),  # lower = worse
    "walking_speed_ms":      ("Walking Speed",        "m/s", False),  # lower = worse
    "walking_asymmetry_pct": ("Gait Asymmetry",       "%",   True),   # higher = worse
    "sleep_hours":           ("Sleep Duration",       "hrs", False),  # lower = worse
    "step_count":            ("Daily Steps",          "steps",False), # lower = worse
}

ZSCORE_THRESHOLD = 1.8   # more conservative than the app-side 1.5


# DATABASE

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_anomaly_columns(conn):
    """Add anomaly columns if they don't exist (migration-safe)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_summary)")}
    for col, typ in [
        ("anomaly_score", "REAL"),
        ("anomaly_flag",  "INTEGER"),
        ("anomaly_detail","TEXT"),   # JSON breakdown per metric
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE daily_summary ADD COLUMN {col} {typ}")
            print(f"[MIGRATE] Added daily_summary.{col}")
    conn.commit()


def ensure_alerts_table(conn):
    """Alerts table should exist from server.py init, but just in case."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type  TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            payload     TEXT,
            seen        INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


# 
# DATA LOADING
# 

def load_history(conn, days=30):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT * FROM daily_summary WHERE date >= ? ORDER BY date ASC",
        (cutoff,)
    ).fetchall()
    return [dict(r) for r in rows]



# ISOLATION FOREST


def run_isolation_forest(rows, contamination=0.1):
    """
    Fit Isolation Forest on available features.
    Returns dict[date] -> (score: float 0-100, is_anomaly: bool).

    Score interpretation:
        0-30   = very normal
        30-60  = somewhat unusual
        60-80  = anomalous
        80-100 = strongly anomalous
    """
    try:
        import numpy as np
        from sklearn.ensemble import IsolationForest
        from sklearn.preprocessing import StandardScaler
        from sklearn.impute import SimpleImputer
    except ImportError:
        print("[ERROR] scikit-learn not installed. Run:")
        print("  pip install scikit-learn numpy --break-system-packages")
        return {}

    if len(rows) < 7:
        print(f"[IFOREST] Only {len(rows)} days — need 7+ for meaningful results. Skipping.")
        return {}

    # Build feature matrix — use only columns with sufficient data
    dates = [r["date"] for r in rows]
    X_raw = []
    usable_features = []

    for feat in IFOREST_FEATURES:
        col_vals = [r.get(feat) for r in rows]
        n_valid  = sum(1 for v in col_vals if v is not None)
        if n_valid < max(3, len(rows) * 0.3):   # skip if too sparse
            continue
        usable_features.append(feat)
        X_raw.append(col_vals)

    if not usable_features:
        print("[IFOREST] No features with sufficient data. Skipping.")
        return {}

    print(f"[IFOREST] Using {len(usable_features)} features: {', '.join(usable_features)}")

    X = np.array(X_raw, dtype=float).T   # shape: (n_days, n_features)

    # Impute missing values with column median
    imputer = SimpleImputer(strategy="median")
    X = imputer.fit_transform(X)

    # Standardise
    scaler = StandardScaler()
    X = scaler.fit_transform(X)

    # Fit model — contamination = expected fraction of anomaly days
    n_est = min(200, max(50, len(rows) * 5))
    clf = IsolationForest(
        n_estimators=n_est,
        contamination=contamination,
        random_state=42,
        n_jobs=-1,
    )
    clf.fit(X)

    # score_samples returns negative values; more negative = more anomalous
    raw_scores = clf.score_samples(X)   # shape: (n_days,)
    labels     = clf.predict(X)          # -1 = anomaly, 1 = normal

    # Normalise to 0–100 (higher = more anomalous)
    mn, mx = raw_scores.min(), raw_scores.max()
    span   = mx - mn if mx != mn else 1.0
    norm   = 100.0 * (mx - raw_scores) / span

    results = {}
    for i, date in enumerate(dates):
        results[date] = {
            "score":      round(float(norm[i]), 1),
            "is_anomaly": bool(labels[i] == -1),
        }
    return results


# 
# Z-SCORE ANALYSIS
# 

def compute_zscores(rows, baseline_days=14):
    """
    For each metric and each day, compute z-score vs the prior N days.
    Returns dict[date] -> dict[metric] -> (value, z_score, is_bad).
    """
    if len(rows) < 4:
        return {}

    results = {}
    dates   = [r["date"] for r in rows]

    for i, row in enumerate(rows):
        date    = row["date"]
        results[date] = {}

        # Baseline: rows strictly before this date (up to baseline_days)
        baseline_rows = rows[max(0, i - baseline_days) : i]
        if len(baseline_rows) < 3:
            continue

        for col, (label, unit, higher_is_worse) in ZSCORE_METRICS.items():
            val = row.get(col)
            if val is None:
                continue

            hist = [r[col] for r in baseline_rows if r.get(col) is not None]
            if len(hist) < 3:
                continue

            mean = sum(hist) / len(hist)
            std  = math.sqrt(sum((v - mean)**2 for v in hist) / len(hist))
            if std < 0.001:
                continue

            z = (val - mean) / std
            threshold = ZSCORE_THRESHOLD
            is_bad = (higher_is_worse and z > threshold) or \
                     (not higher_is_worse and z < -threshold)

            results[date][col] = {
                "label":          label,
                "unit":           unit,
                "value":          round(val, 3),
                "mean":           round(mean, 3),
                "std":            round(std, 3),
                "z_score":        round(z, 3),
                "higher_is_worse":higher_is_worse,
                "is_bad":         is_bad,
            }

    return results


# 
# HEALTH SIGNAL ALERTS
# 

def write_health_signal_alerts(conn, zscore_results, today_str, dry_run=False):
    """
    Write health_signal alerts for today's anomalous metrics.
    Deduplicates: won't re-fire for the same metric on the same day.
    """
    today_z = zscore_results.get(today_str, {})
    fired   = 0

    for col, info in today_z.items():
        if not info["is_bad"]:
            continue

        # Check for existing alert today for this metric
        existing = conn.execute("""
            SELECT id FROM alerts
            WHERE alert_type = 'health_signal'
              AND date(detected_at) = ?
              AND json_extract(payload, '$.metric') = ?
        """, (today_str, col)).fetchone()

        if existing:
            print(f"[ALERT] Already fired for {info['label']} today — skipping")
            continue

        direction = "elevated" if info["z_score"] > 0 else "low"
        message   = (
            f"{info['label']} is {direction} vs your 14-day baseline "
            f"({info['value']:.1f} vs avg {info['mean']:.1f} {info['unit']}, "
            f"z={info['z_score']:+.2f})"
        )
        payload = json.dumps({
            "metric":    col,
            "label":     info["label"],
            "value":     info["value"],
            "mean":      info["mean"],
            "std":       info["std"],
            "z_score":   info["z_score"],
            "direction": direction,
            "message":   message,
        })

        print(f"[ALERT] {message}")
        if not dry_run:
            conn.execute(
                "INSERT INTO alerts (alert_type, detected_at, payload) VALUES (?,?,?)",
                ("health_signal", datetime.now().isoformat(), payload)
            )
            fired += 1

    if not dry_run and fired > 0:
        conn.commit()
    return fired


# 
# WRITE SCORES TO DB
# 

def write_scores(conn, iforest_results, zscore_results, dry_run=False):
    """Write anomaly_score and anomaly_flag back to daily_summary."""
    updated = 0

    for date, ifo in iforest_results.items():
        score    = ifo["score"]
        is_anom  = ifo["is_anomaly"]

        # Supplement with z-score flags
        zday     = zscore_results.get(date, {})
        bad_cols = [col for col, info in zday.items() if info["is_bad"]]

        # Flag if IF says anomaly OR any metric has bad z-score
        final_flag  = 1 if (is_anom or len(bad_cols) > 0) else 0

        # Build detail JSON for the app to explain why
        detail = {
            "isolation_forest_score": score,
            "isolation_forest_anomaly": is_anom,
            "zscore_flags": bad_cols,
            "zscore_detail": {
                col: {
                    "z": zday[col]["z_score"],
                    "val": zday[col]["value"],
                    "avg": zday[col]["mean"],
                }
                for col in bad_cols
            },
        }

        if dry_run:
            print(
                f"[DRY] {date}  score={score:.1f}  flag={final_flag}"
                + (f"  flags={bad_cols}" if bad_cols else "")
            )
        else:
            conn.execute("""
                UPDATE daily_summary
                SET anomaly_score  = ?,
                    anomaly_flag   = ?,
                    anomaly_detail = ?,
                    updated_at     = datetime('now')
                WHERE date = ?
            """, (score, final_flag, json.dumps(detail), date))
            updated += 1

    if not dry_run:
        conn.commit()
    return updated


# 
# MAIN
# 

def main():
    parser = argparse.ArgumentParser(description="VIGIL Nightly Anomaly Detection")
    parser.add_argument("--days",      type=int, default=30,
                        help="How many days of history to use (default: 30)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Print results but don't write to DB")
    parser.add_argument("--verbose",   action="store_true",
                        help="Print per-metric z-scores")
    parser.add_argument("--alerts",    action="store_true", default=True,
                        help="Write health_signal alerts for today (default: True)")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    start_time = datetime.now()
    print(f"\n{'='*50}")
    print(f"  VIGIL Nightly Anomaly Detection")
    print(f"  {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at {DB_PATH}")
        sys.exit(1)

    conn = get_db()
    ensure_anomaly_columns(conn)
    ensure_alerts_table(conn)

    # Load history
    rows = load_history(conn, days=args.days)
    print(f"[DATA] Loaded {len(rows)} days of history (last {args.days} days)")

    if len(rows) < 3:
        print("[INFO] Not enough data yet — need at least 3 days. Exiting.")
        conn.close()
        return

    today_str = datetime.now().strftime("%Y-%m-%d")

    # Step 1: Isolation Forest
    print("\n[STEP 1] Running Isolation Forest...")
    iforest_results = run_isolation_forest(rows)

    if iforest_results:
        n_flagged = sum(1 for v in iforest_results.values() if v["is_anomaly"])
        print(f"[IFOREST] {n_flagged}/{len(iforest_results)} days flagged as anomalous")

        # Print today's score
        today_ifo = iforest_results.get(today_str)
        if today_ifo:
            print(f"[IFOREST] Today: score={today_ifo['score']:.1f} anomaly={today_ifo['is_anomaly']}")
    else:
        iforest_results = {}

    # Step 2: Z-score analysis
    print("\n[STEP 2] Computing z-scores...")
    zscore_results = compute_zscores(rows, baseline_days=14)

    if args.verbose:
        today_z = zscore_results.get(today_str, {})
        if today_z:
            print(f"\n  Z-scores for {today_str}:")
            for col, info in sorted(today_z.items()):
                flag = " ← BAD" if info["is_bad"] else ""
                print(f"    {info['label']:25s}  z={info['z_score']:+.2f}  "
                      f"val={info['value']:.2f}  avg={info['mean']:.2f}{flag}")

    # Step 3: Write scores
    print("\n[STEP 3] Writing anomaly scores to database...")
    n_updated = write_scores(conn, iforest_results, zscore_results, dry_run=args.dry_run)
    print(f"[DB] Updated {n_updated} rows")

    # Step 4: Health signal alerts for today
    if args.alerts:
        print("\n[STEP 4] Checking today's metrics for health signals...")
        n_alerts = write_health_signal_alerts(
            conn, zscore_results, today_str, dry_run=args.dry_run
        )
        print(f"[ALERTS] Fired {n_alerts} new health_signal alerts")

    # Summary
    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n[DONE] Completed in {elapsed:.1f}s")
    print(f"{'='*50}\n")
    conn.close()


if __name__ == "__main__":
    main()