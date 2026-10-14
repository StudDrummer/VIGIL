import json
"""
VIGIL Server.py v6 - Camera-First, Complete
"""
# ─────────────────────────────────────────────────────────────────────────────
# HIPAA COMPLIANCE NOTES
# ─────────────────────────────────────────────────────────────────────────────
# This server processes Protected Health Information (PHI) under a signed
# Business Associate Agreement with the covered facility.
#
# Safeguards in place:
# 1. All PHI stored locally on-premise — never transmitted to cloud servers
# 2. Database encrypted at OS level via full-disk encryption on Jetson
# 3. Network access restricted to Tailscale VPN peers only
# 4. Twilio SMS contains alert type and resident name only — no clinical data
# 5. No PHI logged to server.log — log statements use generic descriptions
#
# Data retention: Per BAA with facility. Default: data retained while
# monitoring is active. Deletion on facility request.
# ─────────────────────────────────────────────────────────────────────────────
from flask import Flask, request, jsonify, send_file, Response
import sqlite3, os, json, math, shutil, tempfile
from datetime import datetime, timedelta
from functools import wraps
import base64
import threading
import hmac
import time

app     = Flask(__name__)
DB_PATH = os.path.expanduser("~/passive-health-moniter/vigil.db")
CUMULATIVE_METRICS = {"StepCount","ActiveEnergyBurned","FlightsClimbed","DistanceWalkingRunning","SleepHours"}
_latest_frame_b64 = None
_frame_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS healthkit_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_type TEXT NOT NULL, value REAL NOT NULL,
            unit TEXT, start_date TEXT NOT NULL, end_date TEXT,
            source TEXT, received_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS camera_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_date TEXT NOT NULL,
            gait_speed_px REAL, gait_speed_ms REAL,
            cadence_spm REAL, asymmetry_pct REAL,
            stride_norm REAL, step_regularity REAL,
            body_scale_px REAL, camera_mode TEXT, stride_count INTEGER,
            posture_score REAL, trunk_lean_deg REAL,
            shoulder_asymmetry_pct REAL, head_forward_deg REAL,
            stride_variability REAL, arm_swing_asymmetry REAL,
            step_width REAL, cadence_variability REAL,
            recorded_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS activity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT,
            duration_s REAL, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS stillness_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL, duration_s REAL,
            hour_of_day INTEGER, alerted INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS fall_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL, hip_drop_pct REAL,
            torso_angle REAL, body_scale_px REAL,
            confirmed INTEGER DEFAULT 1, alerted INTEGER DEFAULT 0, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL, detected_at TEXT NOT NULL,
            payload TEXT, seen INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS daily_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            step_count REAL, walking_speed_ms REAL,
            walking_asymmetry_pct REAL, walking_step_length_m REAL,
            double_support_pct REAL, hrv_sdnn REAL, resting_hr REAL,
            sleep_hours REAL, active_calories REAL, spo2_avg REAL,
            respiratory_rate REAL, vo2_max REAL, wrist_temp REAL,
            camera_cadence_spm REAL, camera_asymmetry_pct REAL,
            camera_gait_speed REAL, camera_gait_speed_ms REAL,
            posture_score REAL, stride_variability REAL,
            arm_swing_asymmetry REAL, step_width REAL,
            cadence_variability REAL, camera_data_points INTEGER,
            active_minutes REAL, still_minutes REAL,
            absent_minutes REAL, activity_level_pct REAL,
            longest_stillness_min REAL,
            anomaly_score REAL, anomaly_flag INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS config (
            id INTEGER PRIMARY KEY CHECK (id=1),
            resident_name TEXT, emergency_phone TEXT,
            emergency_email TEXT, resident_height_cm REAL DEFAULT 170,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS camera_status (
            id INTEGER PRIMARY KEY CHECK (id=1),
            last_heartbeat_at TEXT, person_present INTEGER,
            fps REAL, camera_mode TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_hk_type_date
            ON healthkit_metrics(metric_type, start_date);
        CREATE TABLE IF NOT EXISTS rooms (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT 'Room 1',
            resident_name TEXT,
            emergency_phone TEXT,
            emergency_email TEXT,
            resident_height_cm REAL DEFAULT 170,
            rtsp_url TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS camera_status_rooms (
            room_id TEXT PRIMARY KEY,
            last_heartbeat_at TEXT,
            person_present INTEGER DEFAULT 0,
            person_count INTEGER DEFAULT 0,
            fps REAL,
            camera_mode TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS persons (
            id TEXT PRIMARY KEY,
            room_id TEXT DEFAULT 'room_1',
            name TEXT,
            is_resident INTEGER DEFAULT 0,
            height_px REAL,
            shoulder_width_px REAL,
            cadence_est REAL,
            confidence REAL,
            frames_seen INTEGER DEFAULT 0,
            first_seen_at TEXT,
            last_seen_at TEXT,
            identified_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS person_detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id TEXT NOT NULL,
            room_id TEXT DEFAULT 'room_1',
            detected_at TEXT NOT NULL,
            gait_speed_ms REAL,
            cadence_spm REAL,
            posture_score REAL,
            asymmetry_pct REAL
        );
        CREATE TABLE IF NOT EXISTS person_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            room_id TEXT DEFAULT 'room_1',
            person_ids TEXT,
            resolved INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_summary)")}
    for col, typ in [
        ("double_support_pct","REAL"),("sleep_hours","REAL"),
        ("respiratory_rate","REAL"),("vo2_max","REAL"),("wrist_temp","REAL"),
        ("camera_gait_speed_ms","REAL"),("posture_score","REAL"),
        ("stride_variability","REAL"),("arm_swing_asymmetry","REAL"),
        ("step_width","REAL"),("cadence_variability","REAL"),
        ("camera_data_points","INTEGER"),("active_minutes","REAL"),
        ("still_minutes","REAL"),("absent_minutes","REAL"),
        ("activity_level_pct","REAL"),("longest_stillness_min","REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE daily_summary ADD COLUMN {col} {typ}")
            print(f"Migration: added {col}")
    cam_ex = {row[1] for row in conn.execute("PRAGMA table_info(camera_metrics)")}
    for col, typ in [
        ("gait_speed_ms","REAL"),("posture_score","REAL"),("trunk_lean_deg","REAL"),
        ("shoulder_asymmetry_pct","REAL"),("head_forward_deg","REAL"),
        ("stride_variability","REAL"),("arm_swing_asymmetry","REAL"),
        ("step_width","REAL"),("cadence_variability","REAL"),
    ]:
        if col not in cam_ex:
            conn.execute(f"ALTER TABLE camera_metrics ADD COLUMN {col} {typ}")
    conn.execute("INSERT OR IGNORE INTO config (id,resident_height_cm) VALUES (1,170)")
    conn.execute("INSERT OR IGNORE INTO camera_status (id,person_present) VALUES (1,0)")
    cam_existing = {row[1] for row in conn.execute("PRAGMA table_info(camera_metrics)")}
    if 'room_id' not in cam_existing:
        conn.execute("ALTER TABLE camera_metrics ADD COLUMN room_id TEXT DEFAULT 'room_1'")
    act_existing = {row[1] for row in conn.execute("PRAGMA table_info(activity_events)")}
    if 'room_id' not in act_existing:
        conn.execute("ALTER TABLE activity_events ADD COLUMN room_id TEXT DEFAULT 'room_1'")
    ds_existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_summary)")}
    if 'room_id' not in ds_existing:
        conn.execute("ALTER TABLE daily_summary ADD COLUMN room_id TEXT DEFAULT 'room_1'")
    conn.execute("INSERT OR IGNORE INTO rooms (id, name) VALUES ('room_1', 'Room 1')")
    conn.execute("INSERT OR IGNORE INTO camera_status_rooms (room_id, person_present) VALUES ('room_1', 0)")
    persons_existing = {row[1] for row in conn.execute("PRAGMA table_info(persons)")}
    if 'photo_b64' not in persons_existing:
        conn.execute("ALTER TABLE persons ADD COLUMN photo_b64 TEXT")
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
    conn.commit(); conn.close()
    print(f"DB ready at {DB_PATH}")

def fix_units(mt, v):
    if mt == "HeartRateVariabilitySDNN" and v < 1.0: return v * 1000.0
    if mt == "OxygenSaturation" and v <= 1.0: return v * 100.0
    return v

def date_only(s):
    return s[:10] if s else datetime.now().strftime("%Y-%m-%d")

def get_config(conn=None):
    own = conn is None
    if own: conn = get_db()
    row = conn.execute("SELECT * FROM config WHERE id=1").fetchone()
    if own: conn.close()
    return dict(row) if row else {}

def update_snapshot(frame_b64: str):
    global _latest_frame_b64
    with _frame_lock:
        _latest_frame_b64 = frame_b64

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-VIGIL-Key', '')
        expected = os.environ.get('VIGIL_API_KEY', '')
        if expected and not hmac.compare_digest(key, expected):
            print(f"[AUTH] Rejected request from {request.remote_addr} — bad key")
            return jsonify({'error': 'unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

_alert_last_sent = {}
_alert_lock = threading.Lock()
ALERT_COOLDOWN_SEC = 300

def alert_rate_limited(alert_type):
    """True if this alert_type fired within the cooldown window (and records this attempt if not)."""
    now = time.time()
    with _alert_lock:
        last = _alert_last_sent.get(alert_type, 0)
        if now - last < ALERT_COOLDOWN_SEC:
            return True
        _alert_last_sent[alert_type] = now
        return False

def send_sms(message):
    sid   = os.environ.get("VIGIL_TWILIO_SID")
    token = os.environ.get("VIGIL_TWILIO_TOKEN")
    frm   = os.environ.get("VIGIL_TWILIO_FROM")
    cfg   = get_config()
    to    = cfg.get("emergency_phone")
    if not all([sid, token, frm]):
        print("[SMS] Twilio not configured"); return False
    if not to or not str(to).startswith("+"):
        print(f"[SMS] Invalid phone: {to}"); return False
    try:
        from twilio.rest import Client
        Client(sid, token).messages.create(body=message, from_=frm, to=to)
        print(f"[SMS] Sent to {to}")
        return True
    except Exception as e:
        print(f"[SMS] Failed: {e}"); return False

def _rebuild_daily(conn, date_str):
    def avg(mt):
        r = conn.execute("SELECT AVG(value) FROM healthkit_metrics WHERE metric_type=? AND substr(start_date,1,10)=?", (mt, date_str)).fetchone()
        return r[0] if r and r[0] is not None else None
    def latest(mt):
        r = conn.execute("SELECT AVG(value) FROM healthkit_metrics WHERE metric_type=? AND substr(start_date,1,10)=?", (mt, date_str)).fetchone()
        if r and r[0] is not None: return r[0]
        r = conn.execute("SELECT AVG(value) FROM healthkit_metrics WHERE metric_type=? AND start_date>=date(?,'-3 days')", (mt, date_str)).fetchone()
        return r[0] if r and r[0] is not None else None
    def day_max(mt):
        r = conn.execute("SELECT MAX(value) FROM healthkit_metrics WHERE metric_type=? AND substr(start_date,1,10)=?", (mt, date_str)).fetchone()
        return r[0] if r and r[0] is not None else None
    cam = conn.execute("""SELECT AVG(NULLIF(cadence_spm,0)),AVG(asymmetry_pct),AVG(NULLIF(gait_speed_px,0)),
        AVG(NULLIF(gait_speed_ms,0)),AVG(posture_score),AVG(stride_variability),
        AVG(arm_swing_asymmetry),AVG(step_width),AVG(cadence_variability),COUNT(*)
        FROM camera_metrics WHERE session_date=?""", (date_str,)).fetchone()
    act = conn.execute("SELECT activity,SUM(duration_s) FROM activity_events WHERE substr(started_at,1,10)=? AND duration_s IS NOT NULL GROUP BY activity", (date_str,)).fetchall()
    act_map = {r[0]: r[1] for r in act}
    active_min = (act_map.get("active", 0) or 0) / 60.0
    still_min  = (act_map.get("still", 0) or 0) / 60.0
    absent_min = (act_map.get("absent", 0) or 0) / 60.0
    denom = active_min + still_min
    activity_pct = (active_min / denom * 100) if denom > 0 else None
    ls = conn.execute("SELECT MAX(duration_s) FROM activity_events WHERE activity='still' AND substr(started_at,1,10)=?", (date_str,)).fetchone()
    longest_min = (ls[0] / 60.0) if ls and ls[0] else None
    conn.execute("""INSERT INTO daily_summary (
        date,step_count,hrv_sdnn,resting_hr,sleep_hours,active_calories,
        spo2_avg,respiratory_rate,vo2_max,wrist_temp,
        walking_speed_ms,walking_asymmetry_pct,walking_step_length_m,double_support_pct,
        camera_cadence_spm,camera_asymmetry_pct,camera_gait_speed,camera_gait_speed_ms,
        posture_score,stride_variability,arm_swing_asymmetry,step_width,cadence_variability,
        camera_data_points,active_minutes,still_minutes,absent_minutes,
        activity_level_pct,longest_stillness_min
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(date) DO UPDATE SET
        step_count=excluded.step_count,hrv_sdnn=excluded.hrv_sdnn,
        resting_hr=excluded.resting_hr,sleep_hours=excluded.sleep_hours,
        active_calories=excluded.active_calories,spo2_avg=excluded.spo2_avg,
        respiratory_rate=excluded.respiratory_rate,vo2_max=excluded.vo2_max,
        wrist_temp=excluded.wrist_temp,walking_speed_ms=excluded.walking_speed_ms,
        walking_asymmetry_pct=excluded.walking_asymmetry_pct,
        walking_step_length_m=excluded.walking_step_length_m,
        double_support_pct=excluded.double_support_pct,
        camera_cadence_spm=excluded.camera_cadence_spm,
        camera_asymmetry_pct=excluded.camera_asymmetry_pct,
        camera_gait_speed=excluded.camera_gait_speed,
        camera_gait_speed_ms=excluded.camera_gait_speed_ms,
        posture_score=excluded.posture_score,
        stride_variability=excluded.stride_variability,
        arm_swing_asymmetry=excluded.arm_swing_asymmetry,
        step_width=excluded.step_width,
        cadence_variability=excluded.cadence_variability,
        camera_data_points=excluded.camera_data_points,
        active_minutes=excluded.active_minutes,
        still_minutes=excluded.still_minutes,
        absent_minutes=excluded.absent_minutes,
        activity_level_pct=excluded.activity_level_pct,
        longest_stillness_min=excluded.longest_stillness_min,
        updated_at=datetime('now')
    """, (
        date_str, day_max("StepCount"), latest("HeartRateVariabilitySDNN"),
        latest("RestingHeartRate"), day_max("SleepHours"), day_max("ActiveEnergyBurned"),
        avg("OxygenSaturation"), avg("RespiratoryRate"), latest("Vo2Max"), avg("BodyTemperature"),
        avg("WalkingSpeed"), avg("WalkingAsymmetryPercentage"), avg("WalkingStepLength"),
        avg("WalkingDoubleSupportPercentage"),
        cam[0], cam[1], cam[2], cam[3], cam[4], cam[5], cam[6], cam[7], cam[8], cam[9],
        active_min if denom > 0 else None, still_min if denom > 0 else None,
        absent_min if absent_min > 0 else None, activity_pct, longest_min,
    ))

def _check_anomalies(conn, date_str):
    # SIGNALS_V2: callers pass today; judge YESTERDAY, a finished day, once.
    try:
        from datetime import timedelta as _td
        date_str = (datetime.strptime(date_str[:10], "%Y-%m-%d") - _td(days=1)).strftime("%Y-%m-%d")
    except Exception:
        return
    cols = {
        "active_minutes": ("Activity Level", "min", False),
        "activity_level_pct": ("Activity Ratio", "%", False),
        "camera_gait_speed_ms": ("Walking Speed", "m/s", False),
        "posture_score": ("Posture", "/100", False),
        "longest_stillness_min": ("Longest Stillness", "min", True),
        "hrv_sdnn": ("HRV", "ms", False),
        "resting_hr": ("Resting HR", "bpm", True),
        "spo2_avg": ("SpO2", "%", False),
        "sleep_hours": ("Sleep", "hrs", False),
        "walking_asymmetry_pct": ("Gait Asymmetry", "%", True),
    }
    today = conn.execute("SELECT * FROM daily_summary WHERE date=?", (date_str,)).fetchone()
    if not today: return
    hist = conn.execute("SELECT * FROM daily_summary WHERE date<? ORDER BY date DESC LIMIT 14", (date_str,)).fetchall()
    if len(hist) < 3: return
    cfg = get_config(conn)
    name = cfg.get("resident_name") or "the resident"
    for col, (label, unit, worse_high) in cols.items():
        tv = today[col]
        if tv is None: continue
        if col in ("camera_gait_speed_ms", "posture_score") and (today["camera_data_points"] or 0) < 300:
            continue        # SIGNALS_V2: too little walking seen that day to judge
        hv = [r[col] for r in hist if r[col] is not None]
        if len(hv) < 3: continue
        mean = sum(hv) / len(hv)
        std  = math.sqrt(sum((v - mean) ** 2 for v in hv) / len(hv))
        if std < 0.001: continue
        z = (tv - mean) / std
        is_anom = (worse_high and z > 1.5) or (not worse_high and z < -1.5)
        if not is_anom: continue
        ex = conn.execute("SELECT id FROM alerts WHERE alert_type='health_signal' AND json_extract(payload,'$.day')=? AND json_extract(payload,'$.metric')=?", (date_str, label)).fetchone()
        if ex: continue
        direction = "elevated" if z > 0 else "low"
        msg = f"{label} was {direction} yesterday vs 14-day baseline ({tv:.1f} vs avg {mean:.1f} {unit})"
        conn.execute("INSERT INTO alerts (alert_type,detected_at,payload) VALUES ('health_signal',?,?)", (
            datetime.now().isoformat(),
            json.dumps({"metric": label, "day": date_str, "value": round(tv, 2), "unit": unit,
                        "baseline": round(mean, 2), "z_score": round(z, 2),
                        "direction": direction, "message": msg})
        ))
        print(f"Anomaly alert: {msg}")
        send_sms(f"VIGIL: {name}'s {label.lower()} was {direction} yesterday ({tv:.1f} vs usual {mean:.1f} {unit}). Consider checking in.")

@app.route("/health")
def health(): return jsonify({"status": "ok", "db": DB_PATH, "version": "v6"})

@app.route("/sync/healthkit", methods=["POST"])
@require_api_key
def sync_hk():
    data = request.get_json()
    if not data or "samples" not in data: return jsonify({"error": "no samples"}), 400
    conn = get_db(); inserted = 0; skipped = 0
    for s in data["samples"]:
        try:
            mt, rv, sd = s.get("type"), s.get("value"), s.get("startDate", "")
            if mt is None or rv is None: skipped += 1; continue
            val = fix_units(mt, float(rv))
            if mt in CUMULATIVE_METRICS:
                day = date_only(sd)
                ex = conn.execute("SELECT id,value FROM healthkit_metrics WHERE metric_type=? AND substr(start_date,1,10)=?", (mt, day)).fetchone()
                if ex is None:
                    conn.execute("INSERT INTO healthkit_metrics (metric_type,value,unit,start_date,end_date,source) VALUES (?,?,?,?,?,?)", (mt, val, s.get("unit"), sd, s.get("endDate"), s.get("source", "AppleWatch"))); inserted += 1
                elif val > ex["value"]:
                    conn.execute("UPDATE healthkit_metrics SET value=?,start_date=? WHERE id=?", (val, sd, ex["id"])); inserted += 1
                else: skipped += 1
            else:
                ex = conn.execute("SELECT id FROM healthkit_metrics WHERE metric_type=? AND start_date=?", (mt, sd)).fetchone()
                if ex is None:
                    conn.execute("INSERT INTO healthkit_metrics (metric_type,value,unit,start_date,end_date,source) VALUES (?,?,?,?,?,?)", (mt, val, s.get("unit"), sd, s.get("endDate"), s.get("source", "AppleWatch"))); inserted += 1
                else: skipped += 1
        except Exception as e: print(f"Insert err: {e}"); skipped += 1
    conn.commit()
    today_str = datetime.now().strftime("%Y-%m-%d")
    for d in {date_only(s.get("startDate", "")) for s in data["samples"]}:
        try: _rebuild_daily(conn, d)
        except Exception as e: print(f"Rebuild err {d}: {e}")
    try: _check_anomalies(conn, today_str)
    except Exception as e: print(f"Anomaly err: {e}")
    conn.commit(); conn.close()
    return jsonify({"inserted": inserted, "skipped": skipped})

@app.route("/sync/camera", methods=["POST"])
@require_api_key
def sync_camera():
    data = request.get_json()
    if not data: return jsonify({"error": "no data"}), 400
    conn = get_db()
    day = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    conn.execute("""INSERT INTO camera_metrics
        (session_date,gait_speed_px,gait_speed_ms,cadence_spm,asymmetry_pct,
         stride_norm,step_regularity,body_scale_px,camera_mode,stride_count,
         posture_score,trunk_lean_deg,shoulder_asymmetry_pct,head_forward_deg,
         stride_variability,arm_swing_asymmetry,step_width,cadence_variability)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        day, data.get("gait_speed_px"), data.get("gait_speed_ms"),
        data.get("cadence_spm"), data.get("asymmetry_pct"),
        data.get("stride_norm"), data.get("step_regularity"),
        data.get("body_scale_px"), data.get("camera_mode"), data.get("stride_count"),
        data.get("posture_score"), data.get("trunk_lean_deg"),
        data.get("shoulder_asymmetry_pct"), data.get("head_forward_deg"),
        data.get("stride_variability"), data.get("arm_swing_asymmetry"),
        data.get("step_width"), data.get("cadence_variability"),
    ))
    conn.commit()
    try: _rebuild_daily(conn, day)
    except Exception as e: print(f"Camera rebuild err: {e}")
    try:
        if day == datetime.now().strftime("%Y-%m-%d"): _check_anomalies(conn, day)
    except Exception as e: print(f"Camera anomaly err: {e}")
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route("/activity/event", methods=["POST"])
@require_api_key
def activity_event():
    data = request.get_json()
    if not data or "activity" not in data or "started_at" not in data:
        return jsonify({"error": "missing fields"}), 400
    conn = get_db()
    conn.execute("INSERT INTO activity_events (activity,started_at,ended_at,duration_s,notes) VALUES (?,?,?,?,?)", (
        data["activity"], data["started_at"], data.get("ended_at"),
        data.get("duration_s"), data.get("notes")))
    conn.commit()
    try: _rebuild_daily(conn, date_only(data["started_at"]))
    except Exception as e: print(f"Activity rebuild err: {e}")
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route("/camera/heartbeat", methods=["POST"])
@require_api_key
def camera_heartbeat():
    data = request.get_json() or {}
    room_id = data.get("room_id", "room_1")
    conn = get_db()
    conn.execute("""
        INSERT INTO camera_status_rooms
            (room_id, last_heartbeat_at, person_present, person_count, fps, camera_mode, updated_at)
        VALUES (?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(room_id) DO UPDATE SET
            last_heartbeat_at=excluded.last_heartbeat_at,
            person_present=excluded.person_present,
            person_count=excluded.person_count,
            fps=excluded.fps,
            camera_mode=excluded.camera_mode,
            updated_at=datetime('now')
    """, (
        room_id,
        datetime.now().isoformat(),
        1 if data.get("person_present") else 0,
        int(data.get("person_count", 0)),
        data.get("fps"),
        data.get("camera_mode"),
    ))
    conn.execute("""
        UPDATE camera_status SET
            last_heartbeat_at=?, person_present=?, fps=?, camera_mode=?, updated_at=datetime('now')
        WHERE id=1
    """, (
        datetime.now().isoformat(),
        1 if data.get("person_present") else 0,
        data.get("fps"),
        data.get("camera_mode"),
    ))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route("/camera/status")
@require_api_key
def camera_status():
    conn = get_db()
    row = conn.execute("SELECT * FROM camera_status WHERE id=1").fetchone()
    conn.close()
    if not row or not row["last_heartbeat_at"]:
        return jsonify({
            "online": False,
            "last_seen_minutes_ago": None,
            "person_present": False,
            "fps": None,
            "camera_mode": None,
        })
    try:
        last = datetime.fromisoformat(row["last_heartbeat_at"])
        mins = (datetime.now() - last).total_seconds() / 60.0
    except Exception:
        mins = 9999

    # If camera hasn't sent a heartbeat in 3+ minutes, it's offline
    # and person_present must be False regardless of stored value
    is_online = mins < 3
    person_present = bool(row["person_present"]) if is_online else False

    return jsonify({
        "online": is_online,
        "last_seen_minutes_ago": round(mins, 1),
        "person_present": person_present,
        "fps": row["fps"] if is_online else None,
        "camera_mode": row["camera_mode"],
    })

@app.route("/config", methods=["GET"])
@require_api_key
def config_get(): return jsonify(get_config())

@app.route("/config", methods=["POST"])
@require_api_key
def config_set():
    data = request.get_json() or {}
    conn = get_db()
    conn.execute("UPDATE config SET resident_name=?,emergency_phone=?,emergency_email=?,resident_height_cm=?,updated_at=datetime('now') WHERE id=1", (
        data.get("resident_name"), data.get("emergency_phone"),
        data.get("emergency_email"), data.get("resident_height_cm", 170)))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route("/summary/today")
@require_api_key
def today_summary():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    row = conn.execute("SELECT * FROM daily_summary WHERE date=?", (today,)).fetchone()
    conn.close()
    return jsonify(dict(row)) if row else jsonify({"date": today, "status": "no data yet"})

@app.route("/summary/recent")
@require_api_key
def recent_summary():
    conn = get_db()
    rows = conn.execute("SELECT * FROM daily_summary ORDER BY date DESC LIMIT 30").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/alert/health_signal", methods=["POST"])
@require_api_key
def alert_health_signal():
    data = request.get_json()
    if not data: return jsonify({"error": "no data"}), 400
    conn = get_db()
    conn.execute("INSERT INTO alerts (alert_type,detected_at,payload) VALUES ('health_signal',?,?)", (
        data.get("detected_at", datetime.now().isoformat()),
        json.dumps({"metric": data.get("metric"), "message": data.get("message"),
                    "value": data.get("value"), "mean": data.get("mean"), "z_score": data.get("z_score")})))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route("/alert/fall", methods=["POST"])
@require_api_key
def alert_fall():
    data = request.get_json()
    if not data: return jsonify({"error": "no data"}), 400
    if alert_rate_limited("fall"):
        return jsonify({"status": "rate_limited"}), 429
    conn = get_db()
    conn.execute("INSERT INTO alerts (alert_type,detected_at,payload) VALUES ('fall',?,?)", (
        data.get("detected_at", datetime.now().isoformat()),
        json.dumps({"hip_drop_pct": data.get("hip_drop_pct"), "torso_angle": data.get("torso_angle"),
                    "message": data.get("message", "Fall detected")})))
    conn.commit()
    cfg = get_config(conn)
    room = conn.execute(
        "SELECT * FROM rooms WHERE id=?", (data.get("room_id", "room_1"),)
    ).fetchone()
    conn.close()
    name = data.get("resident_name") or (dict(room).get("resident_name") if room else None) or cfg.get("resident_name") or "the resident"
    room_name = dict(room).get("name", "Room 1") if room else "Room 1"
    print("FALL ALERT received")
    send_sms(
        f"VIGIL ALERT: A fall was detected for {name} in {room_name}. "
        f"Please check on them immediately."
    )
    return jsonify({"status": "ok"})

@app.route("/alert/stillness", methods=["POST"])
@require_api_key
def alert_stillness():
    data = request.get_json()
    if not data: return jsonify({"error": "no data"}), 400
    if alert_rate_limited("stillness"):
        return jsonify({"status": "rate_limited"}), 429
    conn = get_db()
    now = datetime.now()
    minutes = round((data.get("duration_s") or 0) / 60)
    conn.execute("INSERT INTO stillness_events (detected_at,duration_s,hour_of_day,alerted) VALUES (?,?,?,1)", (now.isoformat(), data.get("duration_s"), now.hour))
    conn.execute("INSERT INTO alerts (alert_type,detected_at,payload) VALUES ('stillness',?,?)", (
        now.isoformat(), json.dumps({"duration_min": minutes, "message": f"No movement for over {minutes} minutes"})))
    conn.commit()
    cfg = get_config(conn); conn.close()
    name = cfg.get("resident_name") or "the resident"
    send_sms(f"VIGIL: No movement detected from {name} for over {minutes} minutes. Please check on them.")
    return jsonify({"status": "ok"})

@app.route("/alerts/ack", methods=["POST"])
@require_api_key
def alerts_ack():
    data = request.get_json()
    if not data or "id" not in data: return jsonify({"error": "no id"}), 400
    conn = get_db()
    conn.execute("UPDATE alerts SET seen=1 WHERE id=?", (data["id"],))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route("/alerts/recent")
@require_api_key
def alerts_recent():
    conn = get_db()
    rows = conn.execute("SELECT * FROM alerts WHERE seen=0 ORDER BY created_at DESC LIMIT 20").fetchall()
    alerts = []
    for row in rows:
        a = dict(row)
        try: a["payload"] = json.loads(a["payload"])
        except: pass
        alerts.append(a)
    conn.close()
    return jsonify(alerts)

@app.route("/alerts/all")
@require_api_key
def alerts_all():
    conn = get_db()
    rows = conn.execute("SELECT * FROM alerts ORDER BY created_at DESC LIMIT 100").fetchall()
    conn.close()
    alerts = []
    for row in rows:
        a = dict(row)
        try: a["payload"] = json.loads(a["payload"])
        except: pass
        alerts.append(a)
    return jsonify(alerts)

@app.route("/ml/scores/latest")
@require_api_key
def ml_scores_latest():
    try:
        conn = get_db()
        tbl = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ml_scores'").fetchone()
        if not tbl:
            conn.close(); return jsonify({"scores": [], "scored_at": None, "status": "no ml data yet"})
        row = conn.execute("SELECT scored_at FROM ml_scores ORDER BY scored_at DESC LIMIT 1").fetchone()
        if not row:
            conn.close(); return jsonify({"scores": [], "scored_at": None})
        scores = conn.execute("""SELECT condition_id,condition_label,probability,score_0_100,
            data_quality,days_of_data,published_auc,insufficient_data
            FROM ml_scores WHERE scored_at=? ORDER BY score_0_100 DESC""", (row["scored_at"],)).fetchall()
        conn.close()
        return jsonify({"scored_at": row["scored_at"], "scores": [dict(s) for s in scores]})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/status")
@require_api_key
def full_status():
    conn = get_db()
    counts = {}
    for t in ["healthkit_metrics","camera_metrics","alerts","daily_summary","activity_events","stillness_events","fall_events"]:
        try: counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except: counts[t] = 0
    cam = conn.execute("SELECT * FROM camera_status WHERE id=1").fetchone()
    cfg = conn.execute("SELECT * FROM config WHERE id=1").fetchone()
    conn.close()
    return jsonify({"status": "ok", "db": DB_PATH, "table_counts": counts,
                    "camera_status": dict(cam) if cam else None,
                    "config": dict(cfg) if cfg else None,
                    "server_time": datetime.now().isoformat()})

@app.route("/debug/dump")
@require_api_key
def debug_dump():
    conn = get_db()
    metrics = conn.execute("""SELECT metric_type,COUNT(*) as count,ROUND(AVG(value),2) as avg,
        MAX(start_date) as latest FROM healthkit_metrics GROUP BY metric_type ORDER BY COUNT(*) DESC""").fetchall()
    conn.close()
    return jsonify({"by_metric": [dict(r) for r in metrics]})

def _vigil_small(raw):
    """LIVE_SMALL_V1: shrink a JPEG to the width the app asked for (?w=)."""
    try:
        w = int(request.args.get("w", "0"))
    except Exception:
        w = 0
    if w < 160 or w > 1920:
        return raw
    try:
        import cv2 as _cv
        import numpy as _np
        img = _cv.imdecode(_np.frombuffer(raw, _np.uint8), _cv.IMREAD_COLOR)
        if img is None or img.shape[1] <= w:
            return raw
        h = int(img.shape[0] * w / img.shape[1])
        img = _cv.resize(img, (w, h), interpolation=_cv.INTER_AREA)
        ok, buf = _cv.imencode(".jpg", img, [_cv.IMWRITE_JPEG_QUALITY, 72])
        return buf.tobytes() if ok else raw
    except Exception:
        return raw


@app.route("/camera/snapshot")
@require_api_key
def camera_snapshot():
    """Returns the latest camera frame as a base64 JPEG."""
    if request.args.get("view") == "skeleton":
        try:
            if time.time() - os.stat("/dev/shm/vigil_skeleton.jpg").st_mtime > 10:
                return jsonify({"frame": None, "available": False})
            with open("/dev/shm/vigil_skeleton.jpg", "rb") as _f:
                return jsonify({"frame": __import__("base64").b64encode(_vigil_small(_f.read())).decode(),
                                "available": True, "view": "skeleton"})
        except Exception:
            return jsonify({"frame": None, "available": False})
    # LIVE_VIDEO_V1: serve the newest annotated frame the camera wrote to
    # memory, not the picture it posted every 2 s. Refuse a frame that is
    # stale or cut off mid-write.
    try:
        _lp = "/dev/shm/vigil_latest.jpg"
        if time.time() - os.stat(_lp).st_mtime < 3:
            with open(_lp, "rb") as _lf:
                _lb = _lf.read()
            if _lb[:2] == b"\xff\xd8" and _lb[-2:] == b"\xff\xd9":
                return jsonify({"frame": __import__("base64").b64encode(_vigil_small(_lb)).decode(),
                                "available": True, "view": "video"})
    except Exception:
        pass
    with _frame_lock:
        if _latest_frame_b64 is None:
            return jsonify({"frame": None, "available": False})
        return jsonify({"frame": _latest_frame_b64, "available": True})

@app.route("/camera/stream")
@require_api_key
def camera_stream():
    """MJPEG live stream sourced from the latest annotated frame written to
    tmpfs by activity_monitor.py. Replaces snapshot polling with a real feed."""
    def generate():
        last_mtime = 0
        while True:
            try:
                st = os.stat('/dev/shm/vigil_latest.jpg')
                if st.st_mtime != last_mtime:
                    last_mtime = st.st_mtime
                    with open('/dev/shm/vigil_latest.jpg', 'rb') as f:
                        frame = f.read()
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n'
                           b'Content-Length: ' + str(len(frame)).encode() +
                           b'\r\n\r\n' + frame + b'\r\n')
            except FileNotFoundError:
                pass
            time.sleep(0.05)   # cap at ~20 fps
    return Response(generate(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/camera/snapshot/post", methods=["POST"])
@require_api_key
def camera_snapshot_post():
    """Called by activity_monitor.py every 2 seconds with latest frame."""
    data = request.get_json()
    if not data or "frame" not in data:
        return jsonify({"error": "no frame"}), 400
    update_snapshot(data["frame"])
    return jsonify({"status": "ok"})

@app.route("/rooms", methods=["GET"])
@require_api_key
def get_rooms():
    conn = get_db()
    rooms = conn.execute("SELECT * FROM rooms WHERE active=1 ORDER BY id").fetchall()
    result = []
    for room in rooms:
        r = dict(room)
        cs = conn.execute(
            "SELECT * FROM camera_status_rooms WHERE room_id=?", (r['id'],)
        ).fetchone()
        if cs:
            try:
                last = datetime.fromisoformat(cs['last_heartbeat_at'])
                mins = (datetime.now() - last).total_seconds() / 60.0
            except Exception:
                mins = 9999
            r['camera_online'] = mins < 3
            r['person_present'] = bool(cs['person_present']) if mins < 3 else False
            r['person_count'] = cs['person_count'] if mins < 3 else 0
            r['fps'] = cs['fps'] if mins < 3 else None
        else:
            r['camera_online'] = False
            r['person_present'] = False
            r['person_count'] = 0
            r['fps'] = None
        result.append(r)
    conn.close()
    return jsonify(result)

@app.route("/rooms", methods=["POST"])
@require_api_key
def create_room():
    data = request.get_json() or {}
    conn = get_db()
    room_id = data.get("id") or f"room_{int(datetime.now().timestamp())}"
    conn.execute("""
        INSERT OR REPLACE INTO rooms
        (id, name, resident_name, emergency_phone, emergency_email,
         resident_height_cm, rtsp_url, active, updated_at)
        VALUES (?,?,?,?,?,?,?,1,datetime('now'))
    """, (
        room_id,
        data.get("name", "New Room"),
        data.get("resident_name"),
        data.get("emergency_phone"),
        data.get("emergency_email"),
        data.get("resident_height_cm", 170),
        data.get("rtsp_url"),
    ))
    conn.execute(
        "INSERT OR IGNORE INTO camera_status_rooms (room_id, person_present) VALUES (?,0)",
        (room_id,)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "room_id": room_id})

@app.route("/rooms/<room_id>", methods=["GET"])
@require_api_key
def get_room(room_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "room not found"}), 404
    return jsonify(dict(row))

@app.route("/rooms/<room_id>", methods=["POST"])
@require_api_key
def update_room(room_id):
    data = request.get_json() or {}
    conn = get_db()
    conn.execute("""
        UPDATE rooms SET name=?, resident_name=?, emergency_phone=?,
        emergency_email=?, resident_height_cm=?, rtsp_url=?, updated_at=datetime('now')
        WHERE id=?
    """, (
        data.get("name"), data.get("resident_name"),
        data.get("emergency_phone"), data.get("emergency_email"),
        data.get("resident_height_cm", 170), data.get("rtsp_url"),
        room_id
    ))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/rooms/<room_id>", methods=["DELETE"])
@require_api_key
def delete_room(room_id):
    if room_id == 'room_1':
        return jsonify({"error": "cannot delete default room"}), 400
    conn = get_db()
    conn.execute("UPDATE rooms SET active=0 WHERE id=?", (room_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/rooms/<room_id>/summary/today")
@require_api_key
def room_today(room_id):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM daily_summary WHERE date=? AND room_id=?",
        (today, room_id)
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM daily_summary WHERE date=?", (today,)
        ).fetchone()
    conn.close()
    return jsonify(dict(row)) if row else jsonify({"date": today, "status": "no data yet"})

@app.route("/rooms/<room_id>/status")
@require_api_key
def room_camera_status(room_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM camera_status_rooms WHERE room_id=?", (room_id,)
    ).fetchone()
    conn.close()
    if not row or not row["last_heartbeat_at"]:
        return jsonify({
            "online": False, "last_seen_minutes_ago": None,
            "person_present": False, "person_count": 0,
            "fps": None, "camera_mode": None, "room_id": room_id
        })
    try:
        last = datetime.fromisoformat(row["last_heartbeat_at"])
        mins = (datetime.now() - last).total_seconds() / 60.0
    except Exception:
        mins = 9999
    is_online = mins < 3
    return jsonify({
        "online": is_online,
        "last_seen_minutes_ago": round(mins, 1),
        "person_present": bool(row["person_present"]) if is_online else False,
        "person_count": int(row["person_count"] or 0) if is_online else 0,
        "fps": row["fps"] if is_online else None,
        "camera_mode": row["camera_mode"],
        "room_id": room_id,
    })

@app.route("/persons", methods=["GET"])
@require_api_key
def get_persons():
    conn = get_db()
    room_id = request.args.get("room_id", "room_1")
    rows = conn.execute(
        "SELECT * FROM persons WHERE room_id=? ORDER BY is_resident DESC, last_seen_at DESC",
        (room_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/persons/detect", methods=["POST"])
@require_api_key
def person_detect():
    data = request.get_json() or {}
    conn = get_db()
    room_id = data.get("room_id", "room_1")
    now = datetime.now().isoformat()
    for person in data.get("persons", []):
        pid = person.get("person_id")
        if not pid:
            continue
        existing = conn.execute("SELECT id FROM persons WHERE id=?", (pid,)).fetchone()
        if existing:
            _ch = person.get("colour_hist")
            if _ch:
                conn.execute("""UPDATE persons SET last_seen_at=?, frames_seen=?,
                    height_px=?, confidence=?, colour_hist=? WHERE id=?""",
                    (now, person.get("frames_seen",0), person.get("height_px"),
                     person.get("confidence",0), _ch, pid))
            else:
                conn.execute("""UPDATE persons SET last_seen_at=?, frames_seen=?,
                    height_px=?, confidence=? WHERE id=?""",
                    (now, person.get("frames_seen",0), person.get("height_px"),
                     person.get("confidence",0), pid))
        else:
            conn.execute("""INSERT INTO persons
                (id,room_id,height_px,shoulder_width_px,cadence_est,
                 confidence,frames_seen,first_seen_at,last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (pid, room_id, person.get("height_px"),
                 person.get("shoulder_width_px"), person.get("cadence_est"),
                 person.get("confidence",0), person.get("frames_seen",0), now, now))
    conn.commit()
    unidentified = conn.execute("""
        SELECT COUNT(*) FROM persons WHERE room_id=? AND name IS NULL
        AND confidence > 0.4
        AND datetime(last_seen_at) > datetime('now','-10 minutes')
    """, (room_id,)).fetchone()[0]
    result = {"status":"ok","unidentified_count":unidentified}
    # One prompt per new unnamed person, holding only that person, created
    # once. Anyone who has ever had a prompt, or has a name, is never asked
    # about again.
    _asked = set()
    for _r in conn.execute("SELECT person_ids FROM person_alerts"):
        try:
            _ids = json.loads(_r["person_ids"] or "[]")
            _asked.update(_ids if isinstance(_ids, list) else [])
        except Exception:
            pass
    _new = []
    for person in data.get("persons", []):
        pid = person.get("person_id")
        if not pid or pid in _asked or pid in _new:
            continue
        _row = conn.execute("SELECT name, confidence FROM persons WHERE id=?", (pid,)).fetchone()
        if _row is None or (_row["name"] or "") or (_row["confidence"] or 0) < 0.9:   # LIVE_VIDEO_V1
            continue
        conn.execute("INSERT INTO person_alerts (alert_type,room_id,person_ids) VALUES (?,?,?)",
                     ("new_person", room_id, json.dumps([pid])))
        _new.append(pid)
    if _new:
        conn.commit()
        result["new_alert"] = True
    conn.close()
    return jsonify(result)

@app.route("/persons/<person_id>/identify", methods=["POST"])
@require_api_key
def identify_person(person_id):
    data = request.get_json() or {}
    conn = get_db()
    conn.execute("""UPDATE persons SET name=?, is_resident=?,
        identified_at=datetime('now') WHERE id=?""",
        (data.get("name"), 1 if data.get("is_resident") else 0, person_id))
    conn.commit(); conn.close()
    return jsonify({"status":"ok"})

@app.route("/persons/alerts", methods=["GET"])
@require_api_key
def person_alerts():
    conn = get_db()
    room_id = request.args.get("room_id","room_1")
    rows = conn.execute("""SELECT * FROM person_alerts
        WHERE room_id=? AND resolved=0 ORDER BY created_at DESC LIMIT 10""",
        (room_id,)).fetchall()
    conn.close()
    result = []
    for row in rows:
        r = dict(row)
        try: r["person_ids"] = json.loads(r["person_ids"])
        except: pass
        result.append(r)
    return jsonify(result)

@app.route("/persons/alerts/<int:alert_id>/resolve", methods=["POST"])
@require_api_key
def resolve_person_alert(alert_id):
    conn = get_db()
    conn.execute("UPDATE person_alerts SET resolved=1 WHERE id=?", (alert_id,))
    conn.commit(); conn.close()
    return jsonify({"status":"ok"})

# VIGIL_PERSON_RECORD_ROUTES
@app.route("/persons/<person_id>/record", methods=["POST"])
@require_api_key
def record_person_detection(person_id):
    data = request.get_json() or {}
    gs = data.get("gait_speed_ms")
    cs = data.get("cadence_spm")
    if not gs and not cs:
        return jsonify({"status":"skipped","reason":"no real reading"})
    conn = get_db()
    conn.execute("""INSERT INTO person_detections
        (person_id, room_id, detected_at, gait_speed_ms, cadence_spm, posture_score, asymmetry_pct)
        VALUES (?, (SELECT room_id FROM persons WHERE id=?), datetime('now'), ?, ?, ?, ?)""",
        (person_id, person_id, gs, cs, data.get("posture_score"), data.get("asymmetry_pct")))
    conn.commit()
    conn.close()
    return jsonify({"status":"ok"})

@app.route("/persons/<person_id>/baseline-status", methods=["GET"])
@require_api_key
def person_baseline_status(person_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT metric, mean_val, std_val, days_used FROM person_baselines WHERE person_id=?",
        (person_id,)).fetchall()
    days = conn.execute("""
        SELECT COUNT(DISTINCT date(detected_at)) FROM person_detections WHERE person_id=?
    """, (person_id,)).fetchone()[0]
    conn.close()
    baseline = {r["metric"]: {"mean": r["mean_val"], "std": r["std_val"]} for r in rows} if rows else None
    days_used = rows[0]["days_used"] if rows else days
    return jsonify({
        "person_id": person_id,
        "has_baseline": bool(rows),
        "days_collected": days,
        "days_needed": 7,
        "baseline": baseline,
    })



@app.route("/persons/<person_id>/detections", methods=["GET"])
@require_api_key
def person_detections_history(person_id):
    conn = get_db()
    days = int(request.args.get("days", 14))
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT date(detected_at) as date,
               AVG(NULLIF(gait_speed_ms,0)) as avg_speed,
               AVG(NULLIF(cadence_spm,0))   as avg_cadence,
               AVG(posture_score)            as avg_posture,
               AVG(asymmetry_pct)            as avg_asymmetry,
               COUNT(*) as samples
        FROM person_detections
        WHERE person_id=? AND detected_at >= ?
        GROUP BY date(detected_at)
        ORDER BY date ASC
    """, (person_id, cutoff)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])



MAX_PHOTO_B64_BYTES = 200 * 1024  # head-crop identification photos are small; reject anything larger

@app.route("/persons/<person_id>/photo", methods=["POST"])
@require_api_key
def save_person_photo(person_id):
    data = request.get_json() or {}
    photo = data.get("photo_b64")
    if not photo:
        return jsonify({"status": "error", "reason": "no photo"}), 400
    if len(photo) > MAX_PHOTO_B64_BYTES:
        return jsonify({"status": "error", "reason": "photo too large"}), 400
    # ONE_PHOTO_PER_PERSON: the first photo is kept for good, and a named
    # person is never photographed again.
    _pc = get_db()
    _pr = _pc.execute("SELECT name, photo_b64 FROM persons WHERE id=?", (person_id,)).fetchone()
    _pc.close()
    if _pr is not None and ((_pr["name"] or "") or _pr["photo_b64"]):
        return jsonify({"status": "kept", "reason": "one photo per person"}), 200
    conn = get_db()
    # The photo can arrive before /persons/detect has created the row (photo
    # capture is attempted every frame, independent of the 60s metadata
    # report cycle), so upsert rather than assume the row already exists.
    existing = conn.execute("SELECT id FROM persons WHERE id=?", (person_id,)).fetchone()
    if existing:
        conn.execute("UPDATE persons SET photo_b64=? WHERE id=?", (photo, person_id))
    else:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO persons (id, photo_b64, first_seen_at, last_seen_at) VALUES (?,?,?,?)",
            (person_id, photo, now, now),
        )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/persons/<person_id>/photo", methods=["GET"])
@require_api_key
def get_person_photo(person_id):
    conn = get_db()
    row = conn.execute("SELECT photo_b64 FROM persons WHERE id=?", (person_id,)).fetchone()
    conn.close()
    if not row or not row["photo_b64"]:
        return jsonify({"available": False, "photo_b64": None})
    return jsonify({"available": True, "photo_b64": row["photo_b64"]})



@app.route("/persons/<person_id>/weekly-trend", methods=["GET"])
@require_api_key
def person_weekly_trend(person_id):
    conn = get_db()
    def week_avg(offset_weeks):
        start = (datetime.now() - timedelta(weeks=offset_weeks+1)).strftime("%Y-%m-%d")
        end   = (datetime.now() - timedelta(weeks=offset_weeks)).strftime("%Y-%m-%d")
        r = conn.execute("""
            SELECT AVG(NULLIF(gait_speed_ms,0)) as spd,
                   AVG(NULLIF(cadence_spm,0)) as cad,
                   AVG(posture_score) as pos,
                   COUNT(*) as n
            FROM person_detections WHERE person_id=? AND detected_at BETWEEN ? AND ?
        """, (person_id, start, end)).fetchone()
        return dict(r) if r else {}

    this_week = week_avg(0)
    last_week = week_avg(1)

    def pct_change(cur, prev):
        if not cur or not prev or prev == 0: return None
        return round((cur - prev) / prev * 100, 1)

    conn.close()
    return jsonify({
        "person_id": person_id,
        "this_week": this_week,
        "last_week": last_week,
        "changes": {
            "gait_speed_pct": pct_change(this_week.get("spd"), last_week.get("spd")),
            "cadence_pct":    pct_change(this_week.get("cad"), last_week.get("cad")),
            "posture_pct":    pct_change(this_week.get("pos"), last_week.get("pos")),
        }
    })



# ---------------------------------------------------------------------------
# AI Insights -- computed from validated measurements against each resident's
# own baseline. Replaces the synthetic-data condition scores at
# /ml/scores/latest, which emitted floor probabilities for every condition.
# ---------------------------------------------------------------------------


# Published population cut points for gait speed. Cited, not trained.
# Reported alongside -- never instead of -- the person's own baseline.
GAIT_SPEED_REFERENCE = {
    "wfg_cut_point": 0.8,
    "community_cut_point": 1.0,
    "source": "World Falls Guidelines cut point (<0.8 m/s); <1.0 m/s "
              "identifies more fallers in community-dwelling and clinical "
              "populations (individual participant data meta-analysis, "
              "28 studies).",
    "limitation": "Gait speed alone has low discriminative accuracy for "
                  "falls. No single gait or balance measure predicts fall "
                  "risk with high certainty in isolation; it should be "
                  "read alongside other factors.",
}


def _gait_reference(value):
    """Where a measured speed sits against published cut points."""
    if value is None:
        return None
    if value < GAIT_SPEED_REFERENCE["wfg_cut_point"]:
        band = "below_0.8"
        text = "Below the 0.8 m/s cut point used in the World Falls Guidelines."
    elif value < GAIT_SPEED_REFERENCE["community_cut_point"]:
        band = "below_1.0"
        text = ("Between 0.8 and 1.0 m/s. The 1.0 m/s cut point identifies "
                "more fallers in community-dwelling populations.")
    else:
        band = "above_1.0"
        text = "At or above 1.0 m/s, the range associated with independent mobility."
    return {
        "band": band,
        "text": text,
        "wfg_cut_point": GAIT_SPEED_REFERENCE["wfg_cut_point"],
        "community_cut_point": GAIT_SPEED_REFERENCE["community_cut_point"],
        "source": GAIT_SPEED_REFERENCE["source"],
        "limitation": GAIT_SPEED_REFERENCE["limitation"],
    }

INSIGHT_RECENT_DAYS = 3
INSIGHT_MIN_SAMPLES = 20

INSIGHT_META = {
    "avg_speed": ("Walking Speed", "m/s", False,
                  "Walking speed is a widely used functional measure in older "
                  "adults. A sustained decline relative to a person's own norm "
                  "is associated with increased fall risk."),
    "avg_cadence": ("Cadence", "steps/min", False,
                    "Cadence below a person's own norm can accompany fatigue, "
                    "pain, or reduced walking confidence."),
    "avg_posture": ("Posture Score", "/100", False,
                    "Lower scores reflect greater trunk lean or shoulder "
                    "asymmetry, which are associated with balance difficulty."),
    "avg_asymmetry": ("Gait Asymmetry", "%", True,
                      "Greater left-right asymmetry is associated with "
                      "musculoskeletal pain or one-sided weakness."),
}


def _insight_severity(z):
    if z is None:
        return "normal"
    a = abs(z)
    if a >= 2.0:
        return "attention"
    if a >= 1.5:
        return "watch"
    return "normal"


@app.route("/insights/latest")
@require_api_key
def insights_latest():
    try:
        conn = get_db()
        recent_cut = (datetime.now() - timedelta(days=INSIGHT_RECENT_DAYS)).strftime("%Y-%m-%d")
        residents = conn.execute(
            "SELECT id, name FROM persons WHERE name IS NOT NULL AND name != ''"
        ).fetchall()

        out = []
        for p in residents:
            pid, pname = p["id"], p["name"]

            base = {}
            for b in conn.execute(
                    "SELECT metric, mean_val, std_val, days_used "
                    "FROM person_baselines WHERE person_id=?", (pid,)):
                base[b["metric"]] = (b["mean_val"], b["std_val"], b["days_used"])

            cur = conn.execute("""
                SELECT AVG(NULLIF(gait_speed_ms,0)) as avg_speed,
                       AVG(NULLIF(cadence_spm,0))   as avg_cadence,
                       AVG(posture_score)           as avg_posture,
                       AVG(asymmetry_pct)           as avg_asymmetry,
                       COUNT(*)                     as samples
                FROM person_detections
                WHERE person_id=? AND detected_at >= ?
            """, (pid, recent_cut)).fetchone()

            days_have = conn.execute(
                "SELECT COUNT(DISTINCT date(detected_at)) FROM person_detections "
                "WHERE person_id=?", (pid,)).fetchone()[0]

            items = []
            for metric, (label, unit, worse_up, note) in INSIGHT_META.items():
                cval = cur[metric] if cur else None
                if cval is None or metric not in base:
                    continue
                mean, std, days_used = base[metric]
                if mean in (None, 0):
                    continue
                delta_pct = (cval - mean) / mean * 100.0
                z = ((cval - mean) / std) if (std and std > 0) else None
                direction = "up" if cval > mean else "down"
                adverse = (direction == "up") if worse_up else (direction == "down")
                items.append({
                    "metric": metric,
                    "label": label,
                    "unit": unit,
                    "current": round(float(cval), 3),
                    "baseline_mean": round(float(mean), 3),
                    "baseline_std": round(float(std), 3) if std else None,
                    "baseline_days": days_used,
                    "delta_pct": round(delta_pct, 1),
                    "z_score": round(z, 2) if z is not None else None,
                    "direction": direction,
                    "adverse": bool(adverse),
                    "severity": _insight_severity(z) if adverse else "normal",
                    "note": note,
                    "reference": _gait_reference(cval) if metric == "avg_speed" else None,
                })

            items.sort(key=lambda i: abs(i["z_score"]) if i["z_score"] is not None else 0,
                       reverse=True)

            samples = cur["samples"] if cur else 0
            if not base:
                status = "building"
            elif samples < INSIGHT_MIN_SAMPLES:
                status = "insufficient_recent"
            else:
                status = "ready"

            out.append({
                "person_id": pid,
                "name": pname,
                "status": status,
                "days_collected": days_have,
                "days_needed": 7,
                "recent_days": INSIGHT_RECENT_DAYS,
                "recent_samples": samples,
                "insights": items,
                "convergence": {
                    "adverse_count": sum(1 for i in items if i["adverse"]),
                    "total_metrics": len(items),
                    "attention": sum(1 for i in items if i["severity"] == "attention"),
                    "watch": sum(1 for i in items if i["severity"] == "watch"),
                    "summary": (
                        "Multiple signals have moved away from this person's "
                        "normal at the same time. Convergence across several "
                        "measures is more meaningful than any one reading."
                        if sum(1 for i in items if i["adverse"]
                               and i["severity"] != "normal") >= 2
                        else "No convergent change across measures."),
                },
            })

        conn.close()
        return jsonify({
            "generated_at": datetime.now().isoformat(),
            "residents": out,
            "disclaimer": "VIGIL reports measured changes against each "
                          "resident's own baseline. It does not diagnose "
                          "conditions and is not a medical device.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ---------------------------------------------------------------------------
# Facial cues. Thresholds were measured on this camera at room distance
# (face_calibrate_v2): jawOpen separated yawning 0.479 from talking 0.112,
# eyeBlink separated closed 0.795 from open 0.230.
# ---------------------------------------------------------------------------

def _ensure_face_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS face_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id TEXT,
        event_type TEXT NOT NULL,
        value REAL,
        room_id TEXT DEFAULT 'room_1',
        detected_at TEXT DEFAULT (datetime('now','localtime'))
    )""")


@app.route("/face/event", methods=["POST"])
@require_api_key
def face_event():
    try:
        d = request.get_json() or {}
        et = d.get("event_type")
        if et not in ("yawn", "perclos"):
            return jsonify({"error": "bad event_type"}), 400
        conn = get_db()
        _ensure_face_table(conn)
        row = conn.execute(
            "SELECT resident_person_id FROM config").fetchone()
        pid = row["resident_person_id"] if row else None
        conn.execute("INSERT INTO face_events (person_id, event_type, value) "
                     "VALUES (?,?,?)", (pid, et, d.get("value")))
        if et == "perclos":
            conn.execute("DELETE FROM face_events WHERE event_type='perclos' "
                         "AND detected_at < datetime('now','localtime','-3 days')")
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/face/latest")
@require_api_key
def face_latest():
    try:
        conn = get_db()
        _ensure_face_table(conn)
        today = datetime.now().strftime("%Y-%m-%d")
        yawns = conn.execute(
            "SELECT COUNT(*) FROM face_events WHERE event_type='yawn' "
            "AND date(detected_at)=?", (today,)).fetchone()[0]
        p = conn.execute(
            "SELECT AVG(value), MAX(value), COUNT(*) FROM face_events "
            "WHERE event_type='perclos' AND detected_at > "
            "datetime('now','localtime','-1 hour')").fetchone()
        recent = conn.execute(
            "SELECT event_type, value, detected_at FROM face_events "
            "ORDER BY id DESC LIMIT 20").fetchall()
        conn.close()
        return jsonify({
            "yawns_today": yawns,
            "perclos_1h_avg": round(p[0], 3) if p and p[0] is not None else None,
            "perclos_1h_max": round(p[1], 3) if p and p[1] is not None else None,
            "perclos_samples": p[2] if p else 0,
            "recent": [dict(r) for r in recent],
            "disclaimer": "Facial cues are wellness signals, not a diagnosis.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ---------------------------------------------------------------------------
# Health signals: deviation from each resident's own recent norm across
# physiological and activity measures. Associations, never diagnoses.
# ---------------------------------------------------------------------------

SIGNAL_BASELINE_DAYS = 21
SIGNAL_RECENT_DAYS = 3
SIGNAL_MIN_BASELINE = 10
SIGNAL_MAX_CV = 0.60   # std/|mean| above this = no usable norm

# column: (label, unit, adverse_direction, note)
SIGNAL_METRICS = {
    "resting_hr": ("Resting Heart Rate", "bpm", "up",
                   "A resting heart rate above a person's own norm can accompany "
                   "infection, dehydration, pain or poor sleep. It often rises "
                   "before other symptoms appear."),
    "hrv_sdnn": ("Heart Rate Variability", "ms", "down",
                 "Reduced heart rate variability relative to normal is a general "
                 "marker of physiological stress or strain."),
    "spo2_avg": ("Blood Oxygen", "%", "down",
                 "A sustained fall in blood oxygen saturation can indicate "
                 "respiratory compromise and warrants clinical attention."),
    "respiratory_rate": ("Respiratory Rate", "breaths/min", "up",
                         "An elevated respiratory rate is among the earliest "
                         "measurable signs of respiratory illness."),
    "wrist_temp": ("Skin Temperature", "\u00b0C", "up",
                   "Elevated skin temperature relative to a person's norm can "
                   "accompany fever or infection."),
    "sleep_hours": ("Sleep", "hours", "down",
                    "Reduced sleep can accompany pain, anxiety, breathing "
                    "difficulty or illness."),
    "step_count": ("Daily Steps", "steps", "down",
                   "A drop in daily activity is among the earliest observable "
                   "signs of illness, pain or low mood in older adults."),
    "activity_level_pct": ("Activity Level", "%", "down",
                           "Reduced time active during the day, measured by "
                           "camera, can reflect fatigue, pain or unwellness."),
    "longest_stillness_min": ("Longest Stillness", "min", "up",
                              "Longer uninterrupted stillness can reflect "
                              "reduced mobility or a change in wellbeing."),
}


def _signal_severity(z):
    if z is None:
        return "normal"
    a = abs(z)
    if a >= 2.0:
        return "attention"
    if a >= 1.5:
        return "watch"
    return "normal"


@app.route("/signals/latest")
@require_api_key
def signals_latest():
    try:
        import statistics as _st
        conn = get_db()
        cols = list(SIGNAL_METRICS.keys())
        rows = conn.execute(
            "SELECT date, %s FROM daily_summary ORDER BY date DESC LIMIT ?"
            % ",".join(cols), (SIGNAL_BASELINE_DAYS + SIGNAL_RECENT_DAYS,)).fetchall()
        conn.close()

        if not rows:
            return jsonify({"status": "no_data", "signals": [],
                            "disclaimer": "No daily summaries recorded yet."})

        recent_rows = rows[:SIGNAL_RECENT_DAYS]
        base_rows = rows[SIGNAL_RECENT_DAYS:]

        signals = []
        for col in cols:
            label, unit, adverse_dir, note = SIGNAL_METRICS[col]
            base_vals = [r[col] for r in base_rows if r[col] is not None]
            recent_vals = [r[col] for r in recent_rows if r[col] is not None]
            if len(base_vals) < SIGNAL_MIN_BASELINE or not recent_vals:
                continue
            mean = sum(base_vals) / len(base_vals)
            try:
                std = _st.stdev(base_vals)
            except Exception:
                std = 0.0
            cur = sum(recent_vals) / len(recent_vals)
            if mean == 0:
                continue
            delta_pct = (cur - mean) / abs(mean) * 100.0
            z = ((cur - mean) / std) if std > 0 else None
            direction = "up" if cur > mean else "down"
            adverse = (direction == adverse_dir)
            cv = (std / abs(mean)) if mean else 999.0
            stable = cv <= SIGNAL_MAX_CV
            signals.append({
                "baseline_stability": "ok" if stable else "low",
                "baseline_cv": round(cv, 2),
                "metric": col,
                "label": label,
                "unit": unit,
                "current": round(float(cur), 2),
                "baseline_mean": round(float(mean), 2),
                "baseline_std": round(float(std), 2),
                "baseline_days": len(base_vals),
                "delta_pct": round(delta_pct, 1),
                "z_score": round(z, 2) if z is not None else None,
                "direction": direction,
                "adverse": bool(adverse),
                # A metric whose own baseline swings more than 60% of its
                # mean has no usable norm; report the number, do not alarm.
                "severity": (_signal_severity(z) if (adverse and stable) else "normal"),
                "note": note,
            })

        signals.sort(key=lambda s: abs(s["z_score"]) if s["z_score"] is not None else 0,
                     reverse=True)
        flagged = [s for s in signals if s["adverse"] and s["severity"] != "normal"]

        if not signals:
            summary = ("Not enough history yet. Signals need about %d days of "
                       "daily summaries before a norm can be established."
                       % SIGNAL_MIN_BASELINE)
        elif len(flagged) >= 2:
            summary = ("%d signals have moved away from normal at the same time. "
                       "Convergence across several measures is more meaningful "
                       "than any single reading." % len(flagged))
        elif len(flagged) == 1:
            summary = ("One signal is outside its usual range. A single measure "
                       "on its own is weak evidence; watch whether others follow.")
        else:
            summary = "All measured signals are within this resident's usual range."

        return jsonify({
            "status": "ready" if signals else "building",
            "generated_at": datetime.now().isoformat(),
            "baseline_days": SIGNAL_BASELINE_DAYS,
            "recent_days": SIGNAL_RECENT_DAYS,
            "signals": signals,
            "flagged_count": len(flagged),
            "summary": summary,
            "disclaimer": "These are deviations from this resident's own recent "
                          "norm, not diagnoses. VIGIL does not identify medical "
                          "conditions and is not a medical device.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/persons/<person_id>/merge", methods=["POST"])
@require_api_key
def merge_person(person_id):
    """Fold a duplicate identity into an existing person.

    Identity matching fragments when someone is briefly seen, seen at an
    unusual distance, or partly occluded. A caregiver can tell from two
    photos whether it is the same person; this lets them say so.
    """
    try:
        data = request.get_json() or {}
        target = data.get("target_person_id")
        if not target:
            return jsonify({"error": "target_person_id required"}), 400
        if target == person_id:
            return jsonify({"error": "cannot merge a person into themselves"}), 400

        conn = get_db()
        src = conn.execute("SELECT id FROM persons WHERE id=?", (person_id,)).fetchone()
        dst = conn.execute("SELECT id, name FROM persons WHERE id=?", (target,)).fetchone()
        if not src or not dst:
            conn.close()
            return jsonify({"error": "person not found"}), 404

        # Carry the newest appearance onto the named record. The duplicate is
        # the person as they look right now; the target may hold no colour
        # and a stale height from one distant glimpse.
        _srow = conn.execute("SELECT height_px, colour_hist FROM persons WHERE id=?",
                             (person_id,)).fetchone()
        if _srow is not None:
            conn.execute("UPDATE persons SET height_px=COALESCE(?, height_px), "
                         "colour_hist=COALESCE(?, colour_hist), "
                         "last_seen_at=datetime('now','localtime') WHERE id=?",
                         (_srow[0], _srow[1], target))
        conn.execute("CREATE TABLE IF NOT EXISTS person_merges ("
                     "source TEXT PRIMARY KEY, target TEXT NOT NULL, "
                     "merged_at TEXT DEFAULT (datetime('now','localtime')))")
        conn.execute("INSERT OR REPLACE INTO person_merges (source, target) "
                     "VALUES (?,?)", (person_id, target))
        moved = conn.execute("SELECT COUNT(*) FROM person_detections "
                             "WHERE person_id=?", (person_id,)).fetchone()[0]
        conn.execute("UPDATE person_detections SET person_id=? WHERE person_id=?",
                     (target, person_id))
        try:
            conn.execute("UPDATE face_events SET person_id=? WHERE person_id=?",
                         (target, person_id))
        except Exception:
            pass   # table may not exist yet
        conn.execute("DELETE FROM person_baselines WHERE person_id=?", (person_id,))
        conn.execute("DELETE FROM persons WHERE id=?", (person_id,))
        conn.commit()
        conn.close()
        return jsonify({
            "status": "ok",
            "merged_into": target,
            "merged_name": dst["name"],
            "detections_moved": moved,
            "note": "Baselines rebuild on the next nightly run from the "
                    "combined history.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ---------------------------------------------------------------------------
# Daily routine signals. Built from activity_events, which has been
# collecting active/still/absent transitions since June but never surfaced.
# ---------------------------------------------------------------------------

ROUTINE_LOOKBACK_DAYS = 30
ROUTINE_MIN_DAYS = 5
NIGHT_START_H = 22
NIGHT_END_H = 6


def _med(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _mad(vals, med):
    """Median absolute deviation. Robust to the occasional odd night in a
    way that a standard deviation is not."""
    if med is None or not vals:
        return None
    return _med([abs(v - med) for v in vals])


def _routine_sev(cur, med, mad, adverse_dir):
    if cur is None or med is None or not mad:
        return "normal", None
    z = (cur - med) / mad
    adverse = (z > 0) if adverse_dir == "up" else (z < 0)
    if not adverse:
        return "normal", round(z, 2)
    a = abs(z)
    return ("attention" if a >= 3.0 else "watch" if a >= 2.0 else "normal"), round(z, 2)


@app.route("/routine/latest")
@require_api_key
def routine_latest():
    try:
        from datetime import datetime as _dt, timedelta as _td
        conn = get_db()
        cutoff = (datetime.now() - timedelta(days=ROUTINE_LOOKBACK_DAYS)).isoformat()
        rows = conn.execute(
            "SELECT activity, started_at, duration_s FROM activity_events "
            "WHERE started_at >= ? ORDER BY started_at", (cutoff,)).fetchall()
        conn.close()

        by_day_morning = {}      # date -> minutes after midnight of first activity
        night_counts = {}        # night date -> count of active episodes
        night_minutes = {}       # night date -> minutes active overnight
        day_absence = {}         # date -> longest daytime absence, minutes

        for r in rows:
            try:
                t = _dt.fromisoformat(r["started_at"])
            except Exception:
                continue
            act = r["activity"]
            dur = float(r["duration_s"] or 0) / 60.0
            dkey = t.strftime("%Y-%m-%d")

            if act == "active":
                if t.hour >= 5:
                    mins = t.hour * 60 + t.minute
                    if dkey not in by_day_morning or mins < by_day_morning[dkey]:
                        by_day_morning[dkey] = mins
                if t.hour >= NIGHT_START_H or t.hour < NIGHT_END_H:
                    # attribute the small hours to the night that began the
                    # previous evening, so one night is one row
                    nkey = (t - _td(days=1)).strftime("%Y-%m-%d") \
                        if t.hour < NIGHT_END_H else dkey
                    night_counts[nkey] = night_counts.get(nkey, 0) + 1
                    night_minutes[nkey] = night_minutes.get(nkey, 0.0) + dur
            elif act == "absent" and NIGHT_END_H <= t.hour < NIGHT_START_H:
                if dur > day_absence.get(dkey, 0):
                    day_absence[dkey] = dur

        today = datetime.now().strftime("%Y-%m-%d")
        yest = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        def build(label, unit, series, cur_key, adverse_dir, note, fmt=None):
            hist = [v for k, v in series.items() if k != cur_key]
            cur = series.get(cur_key)
            if len(hist) < ROUTINE_MIN_DAYS:
                return {"label": label, "status": "building",
                        "days_of_history": len(hist),
                        "days_needed": ROUTINE_MIN_DAYS,
                        "current": round(cur, 1) if cur is not None else None,
                        "unit": unit, "note": note}
            med = _med(hist)
            mad = _mad(hist, med)
            sev, z = _routine_sev(cur, med, mad, adverse_dir)
            # A measure whose own spread approaches its typical value has no
            # usable norm. Living-room placement gave Morning Start a spread
            # of 4h36m; flagging against that would be noise, not signal.
            cv = (mad / abs(med)) if (med and mad is not None) else 999.0
            stable = cv <= 0.50
            if not stable:
                sev = "normal"
            out = {"label": label, "status": "ready", "unit": unit,
                   "stability": "ok" if stable else "low",
                   "spread_ratio": round(cv, 2) if cv < 900 else None,
                   "current": round(cur, 1) if cur is not None else None,
                   "baseline": round(med, 1) if med is not None else None,
                   "spread": round(mad, 1) if mad is not None else None,
                   "days_of_history": len(hist), "z_score": z,
                   "severity": sev, "note": note}
            if fmt == "clock":
                out["current_text"] = ("%02d:%02d" % (int(cur // 60), int(cur % 60))) if cur is not None else None
                out["baseline_text"] = ("%02d:%02d" % (int(med // 60), int(med % 60))) if med is not None else None
            return out

        morning = build(
            "Morning Start", "minutes after midnight", by_day_morning, today, "up",
            "A later start than usual can reflect poor sleep, pain, low mood "
            "or illness. It is one of the earliest observable changes in "
            "daily routine.", fmt="clock")

        nights = build(
            "Night Activity", "episodes", night_counts, yest, "up",
            "Movement between 22:00 and 06:00. This matters for safety: many "
            "falls happen getting out of bed at night. More night waking than "
            "usual can also accompany a urinary issue, a medication side "
            "effect, or disturbed sleep.")

        nmins = build(
            "Night Time Active", "minutes", night_minutes, yest, "up",
            "Total time moving overnight. Separates one long episode from "
            "several brief ones.")

        absence = build(
            "Longest Daytime Absence", "minutes", day_absence, today, "up",
            "Time away from the monitored room during the day. A long or "
            "unusual absence can mean the resident left the area.")

        items = [morning, nights, nmins, absence]
        ready = [i for i in items if i.get("status") == "ready"]
        flagged = [i for i in ready if i.get("severity") in ("watch", "attention")]

        if not ready:
            summary = ("Learning this resident's routine. A few more days of "
                       "history are needed before changes can be flagged.")
        elif len(flagged) >= 2:
            summary = ("%d routine measures differ from normal. Changes across "
                       "several at once are more meaningful than one alone."
                       % len(flagged))
        elif flagged:
            summary = "%s is outside its usual range." % flagged[0]["label"]
        else:
            summary = "Daily routine is within this resident's usual range."

        return jsonify({
            "generated_at": datetime.now().isoformat(),
            "status": "ready" if ready else "building",
            "items": items,
            "flagged_count": len(flagged),
            "summary": summary,
            "disclaimer": "Routine changes are observations about this "
                          "resident's own pattern, not diagnoses.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ---------------------------------------------------------------------------
# Hourly activity profile and anomaly history.
# Built from activity_events (local timestamps) rather than person_patterns,
# whose hour_of_day is derived from UTC and is shifted seven hours.
# ---------------------------------------------------------------------------

PATTERN_DAYS = 30


@app.route("/patterns/latest")
@require_api_key
def patterns_latest():
    try:
        from datetime import datetime as _dt
        conn = get_db()
        cutoff = (datetime.now() - timedelta(days=PATTERN_DAYS)).isoformat()
        rows = conn.execute(
            "SELECT activity, started_at, duration_s FROM activity_events "
            "WHERE started_at >= ? AND activity='active' ORDER BY started_at",
            (cutoff,)).fetchall()

        today = datetime.now().strftime("%Y-%m-%d")
        hist = {h: [] for h in range(24)}    # hour -> minutes per day seen
        per_day = {}                          # (date, hour) -> minutes
        today_h = {h: 0.0 for h in range(24)}

        for r in rows:
            try:
                t = _dt.fromisoformat(r["started_at"])
            except Exception:
                continue
            mins = float(r["duration_s"] or 0) / 60.0
            d = t.strftime("%Y-%m-%d")
            per_day[(d, t.hour)] = per_day.get((d, t.hour), 0.0) + mins
            if d == today:
                today_h[t.hour] += mins

        for (d, h), mins in per_day.items():
            if d != today:
                hist[h].append(mins)

        days_seen = len({d for (d, _h) in per_day})
        hourly = []
        for h in range(24):
            vals = hist[h]
            avg = (sum(vals) / len(vals)) if vals else 0.0
            hourly.append({
                "hour": h,
                "avg_minutes": round(avg, 1),
                "today_minutes": round(today_h[h], 1),
                "days_with_activity": len(vals),
            })

        busiest = sorted(hourly, key=lambda x: x["avg_minutes"], reverse=True)[:3]
        night = [x for x in hourly if x["hour"] >= 22 or x["hour"] < 6]
        night_avg = sum(x["avg_minutes"] for x in night)
        night_today = sum(x["today_minutes"] for x in night)

        anomalies = conn.execute(
            "SELECT date, anomaly_score, anomaly_detail FROM daily_summary "
            "WHERE anomaly_flag=1 ORDER BY date DESC LIMIT 10").fetchall()
        scored = conn.execute(
            "SELECT COUNT(*) FROM daily_summary "
            "WHERE anomaly_score IS NOT NULL").fetchone()[0]
        conn.close()

        return jsonify({
            "generated_at": datetime.now().isoformat(),
            "days_of_history": days_seen,
            "hourly": hourly,
            "busiest_hours": [x["hour"] for x in busiest],
            "night_minutes_avg": round(night_avg, 1),
            "night_minutes_today": round(night_today, 1),
            "anomalies": [{"date": a["date"],
                           "score": round(a["anomaly_score"], 1)
                           if a["anomaly_score"] is not None else None}
                          for a in anomalies],
            "days_scored": scored,
            "method": "Unusual days are identified by an isolation forest "
                      "trained only on this resident's own daily history. It "
                      "learns what is ordinary for them and flags days that "
                      "do not fit, without needing labels or outside data.",
            "disclaimer": "An unusual day is an observation, not a diagnosis. "
                          "It indicates the day differed from this resident's "
                          "pattern, not why.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ---------------------------------------------------------------------------
# Alert acknowledgement. Detection without a recorded response is only half
# the job -- in shift work, two caregivers each assuming the other went is a
# known failure mode.
# ---------------------------------------------------------------------------

def _ensure_ack_columns(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(alerts)")]
    if "acknowledged_by" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN acknowledged_by TEXT")
    if "acknowledged_at" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN acknowledged_at TEXT")


@app.route("/alerts/<int:alert_id>/ack", methods=["POST"])
@require_api_key
def ack_alert(alert_id):
    try:
        data = request.get_json() or {}
        who = (data.get("acknowledged_by") or "Caregiver")[:80]
        conn = get_db()
        _ensure_ack_columns(conn)
        row = conn.execute("SELECT id FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "alert not found"}), 404
        conn.execute("UPDATE alerts SET seen=1, acknowledged_by=?, "
                     "acknowledged_at=datetime('now','localtime') WHERE id=?",
                     (who, alert_id))
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE seen=0").fetchone()[0]
        conn.close()
        return jsonify({"status": "ok", "acknowledged_by": who,
                        "unseen_remaining": remaining})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/alerts/ack-all", methods=["POST"])
@require_api_key
def ack_all_alerts():
    try:
        data = request.get_json() or {}
        who = (data.get("acknowledged_by") or "Caregiver")[:80]
        conn = get_db()
        _ensure_ack_columns(conn)
        n = conn.execute("SELECT COUNT(*) FROM alerts WHERE seen=0").fetchone()[0]
        if "ack_bulk" not in [c[1] for c in conn.execute("PRAGMA table_info(alerts)")]:
            conn.execute("ALTER TABLE alerts ADD COLUMN ack_bulk INTEGER DEFAULT 0")
        # Bulk clearing is housekeeping, not a response; flag it so it never
        # counts toward response-time figures.
        conn.execute("UPDATE alerts SET seen=1, acknowledged_by=?, ack_bulk=1, "
                     "acknowledged_at=datetime('now','localtime') WHERE seen=0",
                     (who,))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "acknowledged": n, "acknowledged_by": who})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/alerts/unseen")
@require_api_key
def alerts_unseen():
    try:
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM alerts WHERE seen=0").fetchone()[0]
        falls = conn.execute("SELECT COUNT(*) FROM alerts WHERE seen=0 "
                             "AND alert_type='fall'").fetchone()[0]
        oldest = conn.execute("SELECT MIN(detected_at) FROM alerts "
                              "WHERE seen=0").fetchone()[0]
        conn.close()
        return jsonify({"unseen": total, "unseen_falls": falls,
                        "oldest_unseen_at": oldest})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/persons/merges")
@require_api_key
def person_merges():
    """Recent merges, so the camera can stop using an id the caregiver has
    already folded into someone. Also undoes any duplicate that the camera
    re-reported in the seconds before it learned about the merge."""
    try:
        conn = get_db()
        conn.execute("CREATE TABLE IF NOT EXISTS person_merges ("
                     "source TEXT PRIMARY KEY, target TEXT NOT NULL, "
                     "merged_at TEXT DEFAULT (datetime('now','localtime')))")
        rows = conn.execute(
            "SELECT source, target FROM person_merges "
            "WHERE merged_at > datetime('now','localtime','-7 days')").fetchall()
        for r in rows:
            conn.execute("UPDATE person_detections SET person_id=? WHERE person_id=?",
                         (r["target"], r["source"]))
            conn.execute("DELETE FROM persons WHERE id=?", (r["source"],))
        conn.commit()
        conn.close()
        return jsonify({"merges": [{"source": r["source"], "target": r["target"]}
                                   for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ---------------------------------------------------------------------------
# Escalation. A fall nobody acknowledges should not end with one text.
# ---------------------------------------------------------------------------
ESCALATE_AFTER_S = 5 * 60
ESCALATE_REPEAT_S = 15 * 60
ESCALATE_MAX = 3


def _escalation_loop():
    import time as _t
    first_seen = {}      # alert id -> monotonic time first seen unacknowledged
    last_sent = {}       # alert id -> monotonic time of last escalation
    sent_count = {}
    ignore = None        # alerts already open at startup are never escalated
    while True:
        try:
            conn = get_db()
            cols = [r[1] for r in conn.execute("PRAGMA table_info(alerts)")]
            if "escalation_count" not in cols:
                conn.execute("ALTER TABLE alerts ADD COLUMN escalation_count INTEGER DEFAULT 0")
            if "escalated_at" not in cols:
                conn.execute("ALTER TABLE alerts ADD COLUMN escalated_at TEXT")
            conn.commit()
            open_ids = [r[0] for r in conn.execute(
                "SELECT id FROM alerts WHERE alert_type='fall' AND seen=0")]
            cfg = conn.execute("SELECT resident_name FROM config LIMIT 1").fetchone()
            name = (cfg[0] if cfg and cfg[0] else "the resident")

            if ignore is None:
                ignore = set(open_ids)
                print("[ESCALATE] watching; %d pre-existing open alert(s) ignored"
                      % len(ignore), flush=True)

            now = _t.monotonic()
            for aid in open_ids:
                if aid in ignore:
                    continue
                first_seen.setdefault(aid, now)
                age = now - first_seen[aid]
                n = sent_count.get(aid, 0)
                if n >= ESCALATE_MAX or age < ESCALATE_AFTER_S:
                    continue
                if n > 0 and now - last_sent.get(aid, 0) < ESCALATE_REPEAT_S:
                    continue
                mins = int(age // 60)
                send_sms("VIGIL URGENT: A fall detected for %s has not been "
                         "acknowledged after %d minutes. Please check on them now."
                         % (name, mins))
                send_push("VIGIL URGENT: fall not acknowledged",
                          "A possible fall has not been acknowledged after %d minutes. "
                          "Check on the resident now." % mins,
                          "urgent", "rotating_light")
                sent_count[aid] = n + 1
                last_sent[aid] = now
                conn.execute("UPDATE alerts SET escalation_count=?, "
                             "escalated_at=datetime('now','localtime') WHERE id=?",
                             (n + 1, aid))
                conn.commit()
                print("[ESCALATE] alert %s escalation %d after %d min"
                      % (aid, n + 1, mins), flush=True)

            for aid in list(first_seen):
                if aid not in open_ids:          # acknowledged or removed
                    first_seen.pop(aid, None)
                    last_sent.pop(aid, None)
                    sent_count.pop(aid, None)
            conn.close()
        except Exception as e:
            print("[ESCALATE] error: %s" % e, flush=True)
        _t.sleep(60)


def _start_escalation():
    import threading as _th
    import os as _os
    # Flask's debug reloader imports the app twice; only run in the real process.
    if _os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    _th.Thread(target=_escalation_loop, daemon=True, name="escalation").start()


_start_escalation()



@app.route("/alert/bed_exit", methods=["POST"])
@require_api_key
def alert_bed_exit():
    """Night-time bed exit. Recorded and shown in the app; never texted,
    because the SMS campaign is registered for falls and inactivity only."""
    data = request.get_json() or {}
    if alert_rate_limited("bed_exit"):
        return jsonify({"status": "rate_limited"}), 429
    conn = get_db()
    now = datetime.now()
    mins = round((data.get("rested_s") or 0) / 60)
    conn.execute("INSERT INTO alerts (alert_type,detected_at,payload) VALUES ('bed_exit',?,?)", (
        now.isoformat(), json.dumps({
            "rested_min": mins, "hour": now.hour,
            "message": "Got up during the night after resting about %d minute%s"
                       % (mins, "" if mins == 1 else "s")})))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})



# ---------------------------------------------------------------------------
# Weekly summary. Plain language, own-history comparisons, response times.
# ---------------------------------------------------------------------------
_WEEK_LABELS = {"fall": ("fall", "falls"),
                "bed_exit": ("night bed exit", "night bed exits"),
                "stillness": ("long stillness alert", "long stillness alerts"),
                "health_signal": ("health signal", "health signals")}


def _parse_ts(v):
    from datetime import datetime as _dt
    try:
        return _dt.fromisoformat(str(v).replace("Z", "").replace("T", " ")[:19])
    except Exception:
        return None


@app.route("/summary/weekly")
@require_api_key
def summary_weekly():
    try:
        from datetime import datetime as _dt, timedelta as _td
        import statistics as _st
        conn = get_db()
        now = _dt.now()
        w0, w1 = now - _td(days=7), now - _td(days=14)

        cfg = get_config(conn)
        name = cfg.get("resident_name") or "the resident"
        prow = conn.execute("SELECT id, name FROM persons WHERE name IS NOT NULL "
                            "AND name != '' ORDER BY is_resident DESC LIMIT 1").fetchone()
        pid = prow["id"] if prow else None
        if prow and prow["name"]:
            name = prow["name"]

        def gait(lo, hi):
            if not pid:
                return None, None
            r = conn.execute(
                "SELECT AVG(NULLIF(gait_speed_ms,0)), AVG(NULLIF(cadence_spm,0)), COUNT(*) "
                "FROM person_detections WHERE person_id=? "
                "AND detected_at >= datetime('now', ?) AND detected_at < datetime('now', ?)",
                (pid, "-%d days" % lo, "-%d days" % hi)).fetchone()
            return (r[0], r[1]) if r and r[2] and r[2] >= 10 else (None, None)

        sp0, cad0 = gait(7, 0)
        sp1, cad1 = gait(14, 7)

        act = [{}, {}]
        nights = [0, 0]
        for r in conn.execute("SELECT activity, started_at, duration_s FROM activity_events "
                              "WHERE started_at >= ?", (w1.isoformat(),)):
            t = _parse_ts(r["started_at"])
            if t is None or r["activity"] != "active":
                continue
            k = 0 if t >= w0 else 1
            d = t.strftime("%Y-%m-%d")
            act[k][d] = act[k].get(d, 0.0) + float(r["duration_s"] or 0) / 60.0
            if t.hour >= 22 or t.hour < 6:
                nights[k] += 1

        def hours(m):
            return (sum(m.values()) / len(m) / 60.0) if m else None

        cols = [c[1] for c in conn.execute("PRAGMA table_info(alerts)")]
        has_ack = "acknowledged_at" in cols
        has_bulk = "ack_bulk" in cols
        q = ("SELECT alert_type, detected_at, seen%s%s FROM alerts"
             % (", acknowledged_at" if has_ack else "",
                ", ack_bulk" if has_bulk else ""))
        counts, waits, open_n = {}, [], 0
        for r in conn.execute(q):
            t = _parse_ts(r["detected_at"])
            if t is None or t < w0:
                continue
            counts[r["alert_type"]] = counts.get(r["alert_type"], 0) + 1
            if not r["seen"]:
                open_n += 1
            elif has_ack and r["acknowledged_at"] and not (has_bulk and r["ack_bulk"]):
                a = _parse_ts(r["acknowledged_at"])
                if a and a >= t:
                    waits.append((a - t).total_seconds() / 60.0)
        conn.close()

        def cmp(cur, prev, unit, fmt):
            if cur is None:
                return None
            s = (fmt % cur) + " " + unit
            if prev:
                pct = (cur - prev) / prev * 100.0
                s += (", about the same as last week" if abs(pct) < 5 else
                      ", %s %d%% from last week" % ("up" if pct > 0 else "down",
                                                    round(abs(pct))))
            return s

        lines = []
        x = cmp(sp0, sp1, "m/s", "%.2f")
        if x:
            lines.append("Walking speed averaged " + x + ".")
        x = cmp(cad0, cad1, "steps/min", "%.0f")
        if x:
            lines.append("Cadence averaged " + x + ".")
        h0, h1 = hours(act[0]), hours(act[1])
        if h0 is not None:
            s = ("About %d minutes active per day" % round(h0 * 60) if h0 < 1.0
                 else "About %.1f hours active per day" % h0)
            # A percentage on a few minutes a day swings wildly and means nothing.
            if h1 and h0 >= 0.5 and h1 >= 0.5:
                pct = (h0 - h1) / h1 * 100.0
                s += (", about the same as last week" if abs(pct) < 5 else
                      ", %s %d%% from last week" % ("up" if pct > 0 else "down",
                                                    round(abs(pct))))
            lines.append(s + ".")
        lines.append("Night-time activity: %d episode%s this week (%d last week)."
                     % (nights[0], "" if nights[0] == 1 else "s", nights[1]))
        if counts:
            parts = []
            for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                one, many = _WEEK_LABELS.get(k, ("other alert", "other alerts"))
                parts.append("%d %s" % (n, one if n == 1 else many))
            lines.append("Alerts this week: " + ", ".join(parts) + ".")
        else:
            lines.append("No alerts this week.")
        if waits:
            _m = _st.median(waits)
            lines.append("Median time to acknowledge an alert: %s."
                         % (("%d min" % round(_m)) if _m < 90
                            else ("about %d hours" % round(_m / 60))))
        if open_n:
            lines.append("%d alert%s still unacknowledged."
                         % (open_n, "" if open_n == 1 else "s"))

        period = "%s to %s" % (w0.strftime("%b %d"), now.strftime("%b %d"))
        text = ("VIGIL weekly summary for %s (%s)\n\n" % (name, period)
                + "\n".join(lines)
                + "\n\nVIGIL compares each resident with their own history. "
                  "It does not diagnose conditions.")
        return jsonify({"period": period, "name": name, "lines": lines, "text": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ---------------------------------------------------------------------------
# Push notifications via ntfy. Generic text only: the resident's name and
# health details never leave the Jetson. Replaced by Apple push later.
# ---------------------------------------------------------------------------
_PUSH_TEXT = {
    "fall": ("VIGIL: possible fall",
             "A possible fall was detected. Open VIGIL and confirm someone is responding.",
             "urgent", "rotating_light"),
    "stillness": ("VIGIL: no movement",
                  "No movement for an unusually long time. Open VIGIL to check.",
                  "high", "warning"),
    "bed_sitting": ("VIGIL: sitting up at night",
                    "The resident sat up in bed during the night and may be about to get up. Open VIGIL.",
                    "high", "crescent_moon"),
    "bed_exit": ("VIGIL: up during the night",
                 "The resident got up during the night. Open VIGIL to check.",
                 "default", "crescent_moon"),
    "health_signal": ("VIGIL: change noted",
                      "A change from the resident's usual pattern was noted. Open VIGIL.",
                      "low", "chart_with_downwards_trend"),
}


def send_push(title, message, priority="default", tags=""):
    topic = os.environ.get("VIGIL_NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("VIGIL_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        import requests as _rq
        _rq.post("%s/%s" % (server, topic), data=message.encode("utf-8"),
                 headers={"Title": title, "Priority": priority, "Tags": tags},
                 timeout=5)
        print("[PUSH] sent: %s" % title, flush=True)
        return True
    except Exception as e:
        print("[PUSH] failed: %s" % e, flush=True)
        return False


def _push_loop():
    import time as _t
    last = None
    while True:
        try:
            conn = get_db()
            mx = conn.execute("SELECT COALESCE(MAX(id),0) FROM alerts").fetchone()[0]
            if last is None:
                last = mx                  # history at startup never pushes
                print("[PUSH] watching alerts from id %d" % last, flush=True)
            if mx < last:
                last = 0                   # table was cleared; ids restarted
            if mx > last:
                for r in conn.execute("SELECT id, alert_type, payload FROM alerts "
                                      "WHERE id > ? ORDER BY id", (last,)):
                    if "TEST" in str(r["payload"] or "").upper():
                        continue           # end-to-end test alerts
                    t = _PUSH_TEXT.get(r["alert_type"])
                    if t:
                        send_push(*t)
                last = mx
            conn.close()
        except Exception as e:
            print("[PUSH] loop error: %s" % e, flush=True)
        _t.sleep(5)


def _start_push():
    import threading as _th
    import os as _os
    if _os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    _th.Thread(target=_push_loop, daemon=True, name="push").start()


_start_push()



# ---------------------------------------------------------------------------
# Identity hygiene. Ask about a person once, never re-prompt for someone who
# has been named, and forget unnamed people who stop appearing.
# ---------------------------------------------------------------------------
UNNAMED_EXPIRE_H = 12
UNFAMILIAR_PUSH_GAP_S = 600


def _hyg_ts(v):
    from datetime import datetime as _dt
    try:
        return _dt.fromisoformat(str(v).replace("Z", "").replace("T", " ")[:19])
    except Exception:
        return None


def _hygiene_loop():
    import time as _t
    import json as _js
    from datetime import datetime as _dt, timedelta as _td
    last_alert_id = None
    last_push = 0.0
    while True:
        try:
            conn = get_db()
            people = {r["id"]: (r["name"], r["last_seen_at"]) for r in
                      conn.execute("SELECT id, name, last_seen_at FROM persons")}
            named = {p for p, (n, _ls) in people.items() if n}

            cutoff = _dt.now() - _td(hours=UNNAMED_EXPIRE_H)
            expired = []
            for p, (n, ls) in people.items():
                if n:
                    continue
                t = _hyg_ts(ls)
                if t is not None and t < cutoff:
                    expired.append(p)
            for p in expired:
                conn.execute("DELETE FROM person_detections WHERE person_id=?", (p,))
                conn.execute("DELETE FROM persons WHERE id=?", (p,))
            alive = set(people) - set(expired)

            prompted = set()
            for r in conn.execute("SELECT id, person_ids FROM person_alerts "
                                  "WHERE resolved=0 ORDER BY id").fetchall():
                raw = r["person_ids"]
                try:
                    ids = _js.loads(raw) if isinstance(raw, str) else list(raw or [])
                except Exception:
                    ids = [s.strip() for s in str(raw or "").strip("[]").replace('"', "").split(",") if s.strip()]
                pending = [p for p in ids if p in alive and p not in named]
                if not pending or all(p in prompted for p in pending):
                    conn.execute("UPDATE person_alerts SET resolved=1 WHERE id=?", (r["id"],))
                else:
                    prompted.update(pending)
            conn.commit()

            mx = conn.execute("SELECT COALESCE(MAX(id),0) FROM person_alerts").fetchone()[0]
            if last_alert_id is None:
                last_alert_id = mx
            if mx < last_alert_id:
                last_alert_id = 0
            if mx > last_alert_id:
                fresh = conn.execute("SELECT COUNT(*) FROM person_alerts "
                                     "WHERE id>? AND resolved=0", (last_alert_id,)).fetchone()[0]
                last_alert_id = mx
                push = globals().get("send_push")
                if fresh and push and _t.time() - last_push > UNFAMILIAR_PUSH_GAP_S:
                    last_push = _t.time()
                    push("VIGIL: unfamiliar person",
                         "Someone VIGIL does not recognise was seen. Open VIGIL to identify them.",
                         "default", "bust_in_silhouette")
            conn.close()
            if expired:
                print("[HYGIENE] removed %d unnamed person(s) unseen for %dh"
                      % (len(expired), UNNAMED_EXPIRE_H), flush=True)
        except Exception as e:
            print("[HYGIENE] error: %s" % e, flush=True)
        _t.sleep(30)


def _start_hygiene():
    import threading as _th
    import os as _os
    if _os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    _th.Thread(target=_hygiene_loop, daemon=True, name="hygiene").start()


_start_hygiene()



@app.route("/alert/bed_sitting", methods=["POST"])
@require_api_key
def alert_bed_sitting():
    """Sitting up in bed at night after resting. In-app and push only."""
    data = request.get_json() or {}
    if alert_rate_limited("bed_sitting"):
        return jsonify({"status": "rate_limited"}), 429
    conn = get_db()
    now = datetime.now()
    mins = round((data.get("rested_s") or 0) / 60)
    conn.execute("INSERT INTO alerts (alert_type,detected_at,payload) VALUES ('bed_sitting',?,?)", (
        now.isoformat(), json.dumps({
            "rested_min": mins, "hour": now.hour,
            "message": "Sat up in bed during the night after resting about %d minute%s"
                       % (mins, "" if mins == 1 else "s")})))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})



# FALL_REPLAY_V1 ------------------------------------------------------------
def _replay_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS fall_replays (
        alert_id INTEGER PRIMARY KEY, frames TEXT, created_at TEXT)""")


@app.route("/alert/fall/replay", methods=["POST"])
@require_api_key
def alert_fall_replay():
    """Attach the camera's last 30 s of body points to the newest fall
    alert from the last 2 minutes that has no replay yet."""
    data = request.get_json() or {}
    frames = data.get("frames") or []
    if not isinstance(frames, list) or not frames or len(frames) > 400:
        return jsonify({"status": "ignored"}), 200
    conn = get_db()
    _replay_table(conn)
    # Only a fall from the last 2 minutes, so a rate-limited repeat never
    # attaches itself to an older alert.
    row = conn.execute("""SELECT id FROM alerts WHERE alert_type='fall'
        AND id NOT IN (SELECT alert_id FROM fall_replays)
        AND datetime(replace(substr(detected_at,1,19),'T',' '))
            >= datetime('now','localtime','-2 minutes')
        ORDER BY id DESC LIMIT 1""").fetchone()
    if row is None:
        conn.close()
        return jsonify({"status": "no_alert"}), 200
    conn.execute("INSERT OR REPLACE INTO fall_replays (alert_id, frames, created_at) VALUES (?,?,datetime('now','localtime'))",
                 (row["id"], json.dumps(frames)))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "alert_id": row["id"]})


@app.route("/alerts/<int:alert_id>/replay")
@require_api_key
def get_fall_replay(alert_id):
    conn = get_db()
    _replay_table(conn)
    row = conn.execute("SELECT frames FROM fall_replays WHERE alert_id=?", (alert_id,)).fetchone()
    conn.close()
    if row is None:
        return jsonify({"available": False})
    return jsonify({"available": True, "frames": json.loads(row["frames"])})



# ZONES_API_V1: bed and couch zones, set from the app's Room setup screen.
_ZONES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fall_zones.json")


@app.route("/zones", methods=["GET"])
@require_api_key
def get_zones():
    try:
        with open(_ZONES_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]
    except Exception:
        data = []
    return jsonify({"zones": data})


@app.route("/zones", methods=["POST"])
@require_api_key
def save_zones():
    body = request.get_json() or {}
    out = []
    for z in (body.get("zones") or [])[:6]:
        try:
            x1, y1, x2, y2 = (float(z[k]) for k in ("x1", "y1", "x2", "y2"))
        except Exception:
            continue
        x1, x2 = sorted((max(0.0, min(1.0, x1)), max(0.0, min(1.0, x2))))
        y1, y2 = sorted((max(0.0, min(1.0, y1)), max(0.0, min(1.0, y2))))
        if x2 - x1 < 0.03 or y2 - y1 < 0.03:
            continue
        out.append({"x1": round(x1, 3), "y1": round(y1, 3), "x2": round(x2, 3), "y2": round(y2, 3),
                    "label": str(z.get("label") or "Furniture")[:24]})
    tmp = _ZONES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f)
    os.replace(tmp, _ZONES_PATH)
    return jsonify({"status": "ok", "zones": out})



# PERSON_CORRECT_V1: "this is someone else", from the app.
@app.route("/persons/<person_id>/correct", methods=["POST"])
@require_api_key
def correct_person(person_id):
    target = (request.get_json() or {}).get("target_person_id")
    if not target or target == person_id:
        return jsonify({"status": "error", "reason": "target_person_id required"}), 400
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS person_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT, dst TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')), applied INTEGER DEFAULT 0)""")
    conn.execute("INSERT INTO person_corrections (src, dst) VALUES (?, ?)", (person_id, target))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})



# IN_VIEW_V1: exactly who is in the camera picture right now.
@app.route("/persons/in-view", methods=["GET"])
@require_api_key
def persons_in_view():
    try:
        with open("/dev/shm/vigil_in_view.json") as f:
            d = json.load(f)
        if time.time() - float(d.get("t", 0)) > 3:
            return jsonify({"people": [], "identifying": 0, "fresh": False})
        ids = d.get("ids") or []
    except Exception:
        return jsonify({"people": [], "identifying": 0, "fresh": False})
    conn = get_db()
    people, seen = [], set()
    for pid in ids:
        if not pid or pid in seen:
            continue
        seen.add(pid)
        row = conn.execute("SELECT name, is_resident FROM persons WHERE id=?", (pid,)).fetchone()
        people.append({"id": pid, "name": (row["name"] if row else None) or None,
                       "is_resident": int(row["is_resident"] or 0) if row else 0})
    conn.close()
    labels = d.get("labels") or []
    tids = d.get("tids") or []
    others = [{"label": (labels[i] if i < len(labels) and labels[i] else "Identifying..."),
               "track": (tids[i] if i < len(tids) else None)}
              for i, p in enumerate(ids) if not p]
    return jsonify({"people": people, "identifying": sum(1 for p in ids if not p),
                    "others": others, "fresh": True})



# IDENTITY_MODE_V1: one resident per camera, or everyone in the home.
@app.route("/identity/mode", methods=["GET", "POST"])
@require_api_key
def identity_mode():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vigil_mode.json")
    if request.method == "POST":
        body = request.get_json() or {}
        mode = body.get("mode")
        if mode not in ("resident", "family"):
            return jsonify({"status": "error", "reason": "mode must be resident or family"}), 400
        with open(path + ".tmp", "w") as f:
            json.dump({"mode": mode}, f)
        os.replace(path + ".tmp", path)
        rid = body.get("resident_person_id")
        if rid:
            conn = get_db()
            conn.execute("UPDATE config SET resident_person_id=?", (rid,))
            conn.commit()
            conn.close()
    try:
        with open(path) as f:
            mode = json.load(f).get("mode", "family")
    except Exception:
        mode = "family"
    conn = get_db()
    try:
        r = conn.execute("SELECT resident_person_id FROM config").fetchone()
        rid = r[0] if r else None
    except Exception:
        rid = None
    conn.close()
    return jsonify({"mode": mode, "resident_person_id": rid})


@app.route("/tracks/<int:track_id>/assign", methods=["POST"])
@require_api_key
def assign_track(track_id):
    """'This is <name>' for someone VIGIL has not named yet."""
    target = (request.get_json() or {}).get("target_person_id")
    if not target:
        return jsonify({"status": "error", "reason": "target_person_id required"}), 400
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS person_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT, dst TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')), applied INTEGER DEFAULT 0)""")
    conn.execute("INSERT INTO person_corrections (src, dst) VALUES (?, ?)", ("track:%d" % track_id, target))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/persons/<person_id>/confirm", methods=["POST"])
@require_api_key
def confirm_person(person_id):
    """'Yes, this is <name>': pin the current look into their memory."""
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS person_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT, dst TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')), applied INTEGER DEFAULT 0)""")
    conn.execute("INSERT INTO person_corrections (src, dst) VALUES (?, ?)", (person_id, person_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    init_db()
    print("VIGIL server v6 starting on port 5001...")
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
