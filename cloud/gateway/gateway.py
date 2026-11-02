"""
VIGIL cloud gateway.

One public front door for every VIGIL site (one site = one camera's pipeline,
running in its own container). It does four jobs:

  1. Accounts: caregivers sign in with email and password and get a token.
  2. Access control: a token only reaches the sites its user was granted.
     The app sends the token in the same header it used for the Jetson key
     (X-VIGIL-Key), so the app's requests work unchanged.
  3. Proxy: /s/<site_id>/<path> is forwarded to that site's server with the
     site's private key. Sites are never exposed to the internet themselves.
  4. Watchdog: if a site stops hearing from its camera, everyone on that
     site gets a "monitoring offline" push, and another when it recovers.

Config:  $VIGIL_CONFIG/sites.json  (written by vigilctl.py)
Data:    $VIGIL_DATA/gateway.db    (users, tokens, grants, audit log)
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, Response, g, jsonify, request, stream_with_context
from werkzeug.security import check_password_hash, generate_password_hash

CONFIG_DIR = os.environ.get("VIGIL_CONFIG", "/config")
DATA_DIR = os.environ.get("VIGIL_DATA", "/data")
SITES_FILE = os.path.join(CONFIG_DIR, "sites.json")
DB_PATH = os.path.join(DATA_DIR, "gateway.db")
TOKEN_DAYS = int(os.environ.get("VIGIL_TOKEN_DAYS", "90"))
NTFY_SERVER = os.environ.get("VIGIL_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
OFFLINE_AFTER_S = int(os.environ.get("VIGIL_OFFLINE_AFTER_S", "180"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade", "content-length", "content-encoding", "host"}


# ---------------------------------------------------------------- storage
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def _close(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        name TEXT,
        pw_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        disabled INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tokens (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        device TEXT);
    CREATE TABLE IF NOT EXISTS grants (
        user_id INTEGER NOT NULL,
        site_id TEXT NOT NULL,
        role TEXT DEFAULT 'caregiver',
        PRIMARY KEY (user_id, site_id));
    CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT DEFAULT (datetime('now')),
        user_id INTEGER, site_id TEXT, action TEXT, ip TEXT);
    CREATE TABLE IF NOT EXISTS login_fail (
        key TEXT, at REAL);
    """)
    c.commit()
    c.close()


_SITES = {"mtime": 0, "data": {}}


def sites():
    """sites.json is re-read when vigilctl changes it; no restart needed."""
    try:
        m = os.path.getmtime(SITES_FILE)
    except OSError:
        return {}
    if m != _SITES["mtime"]:
        with open(SITES_FILE) as f:
            _SITES["data"] = {s["id"]: s for s in json.load(f).get("sites", [])}
        _SITES["mtime"] = m
    return _SITES["data"]


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def audit(action, site_id=None, user_id=None):
    try:
        db().execute("INSERT INTO audit (user_id, site_id, action, ip) VALUES (?,?,?,?)",
                     (user_id if user_id is not None else getattr(g, "user_id", None), site_id, action,
                      request.headers.get("X-Forwarded-For", request.remote_addr)))
        db().commit()
    except Exception:
        pass


# ---------------------------------------------------------------- auth
def _token_from_request():
    t = request.headers.get("X-VIGIL-Key") or request.headers.get("Authorization", "").replace("Bearer ", "")
    return t or request.args.get("token", "")


def require_user(f):
    @wraps(f)
    def inner(*a, **kw):
        tok = _token_from_request()
        if not tok:
            return jsonify({"status": "error", "reason": "sign in required"}), 401
        row = db().execute(
            "SELECT t.user_id, t.expires_at, u.is_admin, u.disabled FROM tokens t JOIN users u ON u.id=t.user_id "
            "WHERE t.token_hash=?", (_h(tok),)).fetchone()
        if not row or row["expires_at"] < time.time() or row["disabled"]:
            return jsonify({"status": "error", "reason": "session expired, sign in again"}), 401
        g.user_id = row["user_id"]
        g.is_admin = bool(row["is_admin"])
        return f(*a, **kw)
    return inner


def user_sites(user_id, is_admin):
    all_sites = sites()
    if is_admin:
        return list(all_sites.values())
    ids = [r["site_id"] for r in db().execute("SELECT site_id FROM grants WHERE user_id=?", (user_id,))]
    return [all_sites[i] for i in ids if i in all_sites]


def public_site(s):
    st = _STATUS.get(s["id"], {})
    return {"id": s["id"], "name": s.get("name", s["id"]), "home": s.get("home", ""),
            "room": s.get("room", ""), "timezone": s.get("timezone", ""),
            "ntfy_topic": s.get("ntfy_topic", ""), "online": st.get("online"),
            "last_seen": st.get("last_seen"), "url_path": "/s/%s" % s["id"]}


def _too_many_fails(key):
    cutoff = time.time() - 15 * 60
    db().execute("DELETE FROM login_fail WHERE at < ?", (cutoff,))
    n = db().execute("SELECT COUNT(*) FROM login_fail WHERE key=?", (key,)).fetchone()[0]
    return n >= 10


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "vigil-gateway", "sites": len(sites())})


@app.route("/auth/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
    key = "%s|%s" % (email, ip.split(",")[0])
    if _too_many_fails(key):
        return jsonify({"status": "error", "reason": "too many attempts, wait 15 minutes"}), 429
    u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u or u["disabled"] or not check_password_hash(u["pw_hash"], pw):
        db().execute("INSERT INTO login_fail VALUES (?,?)", (key, time.time()))
        db().commit()
        return jsonify({"status": "error", "reason": "wrong email or password"}), 401
    tok = secrets.token_urlsafe(32)
    now = time.time()
    db().execute("INSERT INTO tokens VALUES (?,?,?,?,?)",
                 (_h(tok), u["id"], now, now + TOKEN_DAYS * 86400, (body.get("device") or "")[:80]))
    db().commit()
    audit("login", user_id=u["id"])
    return jsonify({"status": "ok", "token": tok,
                    "user": {"email": u["email"], "name": u["name"], "is_admin": bool(u["is_admin"])},
                    "sites": [public_site(s) for s in user_sites(u["id"], bool(u["is_admin"]))]})


@app.route("/auth/logout", methods=["POST"])
@require_user
def logout():
    db().execute("DELETE FROM tokens WHERE token_hash=?", (_h(_token_from_request()),))
    db().commit()
    return jsonify({"status": "ok"})


@app.route("/auth/me")
@require_user
def me():
    u = db().execute("SELECT email, name, is_admin FROM users WHERE id=?", (g.user_id,)).fetchone()
    return jsonify({"status": "ok", "user": dict(u),
                    "sites": [public_site(s) for s in user_sites(g.user_id, g.is_admin)]})


@app.route("/auth/password", methods=["POST"])
@require_user
def change_password():
    body = request.get_json(silent=True) or {}
    new = body.get("new_password") or ""
    if len(new) < 10:
        return jsonify({"status": "error", "reason": "use at least 10 characters"}), 400
    u = db().execute("SELECT pw_hash FROM users WHERE id=?", (g.user_id,)).fetchone()
    if not check_password_hash(u["pw_hash"], body.get("old_password") or ""):
        return jsonify({"status": "error", "reason": "current password is wrong"}), 401
    db().execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(new), g.user_id))
    db().execute("DELETE FROM tokens WHERE user_id=? AND token_hash<>?", (g.user_id, _h(_token_from_request())))
    db().commit()
    audit("password_changed")
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------- proxy
@app.route("/s/<site_id>/", defaults={"path": ""}, methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
@app.route("/s/<site_id>/<path:path>", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
@require_user
def proxy(site_id, path):
    allowed = {s["id"] for s in user_sites(g.user_id, g.is_admin)}
    if site_id not in allowed:
        return jsonify({"status": "error", "reason": "no access to this camera"}), 403
    s = sites()[site_id]
    if request.method != "GET":
        audit("%s /%s" % (request.method, path[:80]), site_id=site_id)
    target = "%s/%s" % (s["internal_url"].rstrip("/"), path)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP and k.lower() != "x-vigil-key"}
    headers["X-VIGIL-Key"] = s["api_key"]
    params = [(k, v) for k, v in request.args.items(multi=True) if k != "token"]
    streaming = path.startswith("camera/stream")
    try:
        r = requests.request(request.method, target, params=params, headers=headers,
                             data=request.get_data(), stream=True,
                             timeout=(5, None if streaming else 60))
    except requests.RequestException:
        return jsonify({"status": "error", "reason": "this camera's server is not responding"}), 502
    out_headers = [(k, v) for k, v in r.raw.headers.items() if k.lower() not in HOP]
    out_headers.append(("Cache-Control", "no-store"))
    return Response(stream_with_context(r.iter_content(chunk_size=16384)), status=r.status_code,
                    headers=out_headers)


# ---------------------------------------------------------------- watchdog
_STATUS = {}


def _push(topic, title, message, priority="high"):
    if not topic:
        return
    try:
        requests.post("%s/%s" % (NTFY_SERVER, topic), data=message.encode("utf-8"),
                      headers={"Title": title, "Priority": priority, "Tags": "warning"}, timeout=10)
    except requests.RequestException:
        pass


def _check_site(s):
    """A site is online when its camera pipeline sent a heartbeat recently."""
    try:
        r = requests.get(s["internal_url"].rstrip("/") + "/camera/status",
                         headers={"X-VIGIL-Key": s["api_key"]}, timeout=8)
        d = r.json() if r.ok else {}
    except Exception:
        d = {}
    age = None
    if isinstance(d.get("last_seen_minutes_ago"), (int, float)):
        age = float(d["last_seen_minutes_ago"]) * 60.0
    for k in ("seconds_since_heartbeat", "age_s", "heartbeat_age_s"):
        if age is not None:
            break
        if isinstance(d.get(k), (int, float)):
            age = float(d[k])
            break
    if age is None and d.get("last_heartbeat"):
        try:
            t = datetime.fromisoformat(str(d["last_heartbeat"]).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc) if d.get("utc") else t.astimezone()
            age = time.time() - t.timestamp()
        except Exception:
            age = None
    if age is None and isinstance(d.get("online"), bool):
        age = 0 if d["online"] else OFFLINE_AFTER_S + 1
    return d, age


def watchdog_loop():
    while True:
        for s in list(sites().values()):
            d, age = _check_site(s)
            online = age is not None and age <= OFFLINE_AFTER_S
            prev = _STATUS.get(s["id"], {})
            now = time.time()
            st = {"online": online, "checked": now, "age_s": age,
                  "last_seen": (now - age) if (age is not None) else prev.get("last_seen")}
            name = s.get("name", s["id"])
            if prev.get("online") is True and not online:
                st["offline_since"] = now
                st["last_push"] = now
                _push(s.get("ntfy_topic"), "VIGIL: monitoring offline",
                      "The camera '%s' stopped sending data. Falls will not be detected until it is back. "
                      "Check its power and Wi-Fi." % name, priority="urgent")
            elif not online and prev.get("online") is False:
                st["offline_since"] = prev.get("offline_since", now)
                st["last_push"] = prev.get("last_push", now)
                if now - st["last_push"] > 3600:
                    st["last_push"] = now
                    _push(s.get("ntfy_topic"), "VIGIL: still offline",
                          "The camera '%s' has been offline for %d minutes." %
                          (name, (now - st["offline_since"]) / 60))
            elif not online and prev.get("online") is None:
                # Never seen online since the gateway started: warn once after 15 min.
                st["first_check"] = prev.get("first_check", now)
                if now - st["first_check"] > 900:
                    st["online"] = False
                    st["offline_since"] = st["first_check"]
                    st["last_push"] = now
                    _push(s.get("ntfy_topic"), "VIGIL: camera not connected",
                          "The camera '%s' has not sent any data. Falls will not be detected until it does. "
                          "Check its power, Wi-Fi and RTMP settings." % name, priority="urgent")
                else:
                    st["online"] = None
            elif online and prev.get("online") is False:
                _push(s.get("ntfy_topic"), "VIGIL: monitoring restored",
                      "The camera '%s' is back online." % name, priority="default")
            _STATUS[s["id"]] = st
        time.sleep(60)


@app.route("/sites/status")
@require_user
def sites_status():
    return jsonify({"status": "ok", "sites": [public_site(s) for s in user_sites(g.user_id, g.is_admin)]})


init_db()
if os.environ.get("VIGIL_WATCHDOG", "1") == "1":
    threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
