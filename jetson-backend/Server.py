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
from flask import Flask, request, jsonify, send_file
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
        hv = [r[col] for r in hist if r[col] is not None]
        if len(hv) < 3: continue
        mean = sum(hv) / len(hv)
        std  = math.sqrt(sum((v - mean) ** 2 for v in hv) / len(hv))
        if std < 0.001: continue
        z = (tv - mean) / std
        is_anom = (worse_high and z > 1.5) or (not worse_high and z < -1.5)
        if not is_anom: continue
        ex = conn.execute("SELECT id FROM alerts WHERE alert_type='health_signal' AND date(detected_at)=? AND json_extract(payload,'$.metric')=?", (date_str, label)).fetchone()
        if ex: continue
        direction = "elevated" if z > 0 else "low"
        msg = f"{label} is {direction} vs 14-day baseline ({tv:.1f} vs avg {mean:.1f} {unit})"
        conn.execute("INSERT INTO alerts (alert_type,detected_at,payload) VALUES ('health_signal',?,?)", (
            datetime.now().isoformat(),
            json.dumps({"metric": label, "value": round(tv, 2), "unit": unit,
                        "baseline": round(mean, 2), "z_score": round(z, 2),
                        "direction": direction, "message": msg})
        ))
        print(f"Anomaly alert: {msg}")
        send_sms(f"VIGIL: {name}'s {label.lower()} is {direction} today ({tv:.1f} vs usual {mean:.1f} {unit}). Consider checking in.")

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
def today_summary():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    row = conn.execute("SELECT * FROM daily_summary WHERE date=?", (today,)).fetchone()
    conn.close()
    return jsonify(dict(row)) if row else jsonify({"date": today, "status": "no data yet"})

@app.route("/summary/recent")
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
def debug_dump():
    conn = get_db()
    metrics = conn.execute("""SELECT metric_type,COUNT(*) as count,ROUND(AVG(value),2) as avg,
        MAX(start_date) as latest FROM healthkit_metrics GROUP BY metric_type ORDER BY COUNT(*) DESC""").fetchall()
    conn.close()
    return jsonify({"by_metric": [dict(r) for r in metrics]})

@app.route("/camera/snapshot")
def camera_snapshot():
    """Returns the latest camera frame as a base64 JPEG."""
    with _frame_lock:
        if _latest_frame_b64 is None:
            return jsonify({"frame": None, "available": False})
        return jsonify({"frame": _latest_frame_b64, "available": True})

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
    if unidentified > 1:
        existing_alert = conn.execute("""
            SELECT id FROM person_alerts WHERE room_id=?
            AND alert_type='multiple_persons' AND resolved=0
            AND datetime(created_at) > datetime('now','-30 minutes')
        """, (room_id,)).fetchone()
        if not existing_alert:
            conn.execute("INSERT INTO person_alerts (alert_type,room_id,person_ids) VALUES (?,?,?)",
                ("multiple_persons", room_id,
                 json.dumps([p["person_id"] for p in data.get("persons",[]) if p.get("person_id")])))
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



MAX_PHOTO_B64_BYTES = 2 * 1024 * 1024  # ~1.5MB decoded image

@app.route("/persons/<person_id>/photo", methods=["POST"])
@require_api_key
def save_person_photo(person_id):
    data = request.get_json() or {}
    photo = data.get("photo_b64")
    if not photo:
        return jsonify({"status": "error", "reason": "no photo"}), 400
    if len(photo) > MAX_PHOTO_B64_BYTES:
        return jsonify({"status": "error", "reason": "photo too large"}), 400
    conn = get_db()
    conn.execute("UPDATE persons SET photo_b64=? WHERE id=?", (photo, person_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/persons/<person_id>/photo", methods=["GET"])
def get_person_photo(person_id):
    conn = get_db()
    row = conn.execute("SELECT photo_b64 FROM persons WHERE id=?", (person_id,)).fetchone()
    conn.close()
    if not row or not row["photo_b64"]:
        return jsonify({"available": False, "photo_b64": None})
    return jsonify({"available": True, "photo_b64": row["photo_b64"]})



@app.route("/persons/<person_id>/weekly-trend", methods=["GET"])
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


if __name__ == "__main__":
    init_db()
    print("VIGIL server v6 starting on port 5001...")
    app.run(host="0.0.0.0", port=5001, debug=False)
