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
import hmac
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
    CREATE TABLE IF NOT EXISTS reset_codes (
        email TEXT PRIMARY KEY, code_hash TEXT, expires REAL, tries INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS site_state (
        site_id TEXT PRIMARY KEY, last_alert_id INTEGER);
    CREATE TABLE IF NOT EXISTS verify_codes (
        user_id INTEGER PRIMARY KEY, code_hash TEXT, expires REAL, tries INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS push_tokens (
        token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, env TEXT DEFAULT 'production',
        created REAL, last_ok REAL);
    CREATE TABLE IF NOT EXISTS site_names (
        site_id TEXT PRIMARY KEY, name TEXT, home TEXT);
    """)
    cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    for col, typ in (("phone", "TEXT"), ("email_alerts", "INTEGER DEFAULT 1")):   # ACCOUNTS_V1
        if col not in cols:
            c.execute("ALTER TABLE users ADD COLUMN %s %s" % (col, typ))
    if "email_verified" not in cols:                                   # VERIFY_EMAIL_V1
        c.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0")
        c.execute("UPDATE users SET email_verified=1")                 # accounts from before this check
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


def _site_names():
    """SITE_NAMES_V1: names and homes the family chose in the app (override vigilctl's)."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=10)
        rows = {r[0]: (r[1], r[2]) for r in c.execute("SELECT site_id, name, home FROM site_names")}
        c.close()
        return rows
    except Exception:
        return {}


def public_site(s, _names=None):
    st = _STATUS.get(s["id"], {})
    nm, hm = (_names if _names is not None else _site_names()).get(s["id"], (None, None))
    return {"id": s["id"], "name": nm or s.get("name", s["id"]), "home": hm if hm is not None else s.get("home", ""),
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
                    "user": _user_json(u),
                    "sites": [public_site(s) for s in user_sites(u["id"], bool(u["is_admin"]))]})


# ---------------------------------------------------------------- ACCOUNTS_V1
# Families create their own account in the app, then add a camera with the
# setup code printed on its card. Passwords are stored only as salted hashes.
import re as _re
import smtplib
from email.message import EmailMessage

SMTP = {k: os.environ.get("VIGIL_SMTP_" + k.upper(), "") for k in ("host", "port", "user", "password", "from")}
EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def email_enabled():
    return bool(SMTP["host"] and SMTP["from"])


def send_email(to, subject, body, wait=False):
    """Plain-text email through the configured SMTP account. Never raises."""
    if not email_enabled() or not to:
        return False

    def _go():
        try:
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = SMTP["from"], to, subject
            m.set_content(body)
            port = int(SMTP["port"] or 587)
            if port == 465:
                srv = smtplib.SMTP_SSL(SMTP["host"], port, timeout=20)
            else:
                srv = smtplib.SMTP(SMTP["host"], port, timeout=20)
                srv.ehlo()
                try:
                    srv.starttls()
                    srv.ehlo()
                except smtplib.SMTPNotSupportedError:
                    pass
            if SMTP["user"]:
                srv.login(SMTP["user"], SMTP["password"])
            srv.send_message(m)
            srv.quit()
            return True
        except Exception as e:
            print("[EMAIL] failed to %s: %s" % (to, e), flush=True)
            return False
    if wait:
        return _go()
    threading.Thread(target=_go, daemon=True).start()
    return True


def _new_token(user_id, device=""):
    tok = secrets.token_urlsafe(32)
    now = time.time()
    db().execute("INSERT INTO tokens VALUES (?,?,?,?,?)", (_h(tok), user_id, now, now + TOKEN_DAYS * 86400,
                                                          (device or "")[:80]))
    db().commit()
    return tok


def _user_json(u):
    return {"email": u["email"], "name": u["name"], "phone": u["phone"] if "phone" in u.keys() else None,
            "email_alerts": bool(u["email_alerts"]) if "email_alerts" in u.keys() else True,
            "email_verified": bool(u["email_verified"]) if "email_verified" in u.keys() else True,
            "is_admin": bool(u["is_admin"])}


@app.route("/auth/signup", methods=["POST"])
def signup():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    name = (body.get("name") or "").strip()[:60]
    phone = (body.get("phone") or "").strip()[:30]
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0]
    if _too_many_fails("signup|" + ip):
        return jsonify({"status": "error", "reason": "too many attempts, wait 15 minutes"}), 429
    if not EMAIL_RE.match(email):
        return jsonify({"status": "error", "reason": "please enter a valid email address"}), 400
    if len(pw) < 8:
        return jsonify({"status": "error", "reason": "use a password of at least 8 characters"}), 400
    if not name:
        return jsonify({"status": "error", "reason": "please enter your name"}), 400
    if db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        db().execute("INSERT INTO login_fail VALUES (?,?)", ("signup|" + ip, time.time()))
        db().commit()
        return jsonify({"status": "error", "reason": "an account with this email already exists; sign in instead"}), 409
    # Without email set up on the server nobody could receive a code, so the
    # address is taken as given; otherwise it must be confirmed with a code.
    db().execute("INSERT INTO users (email, name, pw_hash, phone, email_verified) VALUES (?,?,?,?,?)",
                 (email, name, generate_password_hash(pw), phone, 0 if email_enabled() else 1))
    db().commit()
    u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    audit("signup", user_id=u["id"])
    if email_enabled():
        _send_verify_code(u, welcome=True)
    return jsonify({"status": "ok", "token": _new_token(u["id"], body.get("device")),
                    "user": _user_json(u), "sites": []})


# ---------------------------------------------------------------- email check (VERIFY_EMAIL_V1)
def _send_verify_code(u, welcome=False):
    code = "%06d" % secrets.randbelow(1000000)
    db().execute("INSERT OR REPLACE INTO verify_codes VALUES (?,?,?,0)", (u["id"], _h(code), time.time() + 86400))
    db().commit()
    hello = ("Hi %s,\n\nWelcome to VIGIL. " % (u["name"] or "there")) if welcome else ""
    return send_email(u["email"], "Your VIGIL code: %s" % code,
                      hello + "Your code to confirm this email address is %s\n\n"
                      "Type it in the VIGIL app. It works for 24 hours. VIGIL sends fall alerts only to "
                      "confirmed addresses.\n\nIf you did not make a VIGIL account, ignore this email.\n\n- VIGIL" % code)


@app.route("/auth/verify", methods=["POST"])
@require_user
def verify_email():
    code = _re.sub(r"[^0-9]", "", (request.get_json(silent=True) or {}).get("code") or "")
    r = db().execute("SELECT * FROM verify_codes WHERE user_id=?", (g.user_id,)).fetchone()
    if not r or r["expires"] < time.time() or r["tries"] >= 5:
        return jsonify({"status": "error", "reason": "that code has expired; tap Send a new code"}), 400
    if not secrets.compare_digest(r["code_hash"], _h(code)):
        db().execute("UPDATE verify_codes SET tries=tries+1 WHERE user_id=?", (g.user_id,))
        db().commit()
        return jsonify({"status": "error", "reason": "that code is not right"}), 400
    db().execute("UPDATE users SET email_verified=1 WHERE id=?", (g.user_id,))
    db().execute("DELETE FROM verify_codes WHERE user_id=?", (g.user_id,))
    db().commit()
    audit("email_verified")
    u = db().execute("SELECT * FROM users WHERE id=?", (g.user_id,)).fetchone()
    return jsonify({"status": "ok", "user": _user_json(u)})


@app.route("/auth/verify/resend", methods=["POST"])
@require_user
def verify_resend():
    key = "verify|%s" % g.user_id
    if _too_many_fails(key):
        return jsonify({"status": "error", "reason": "too many codes asked for, wait 15 minutes"}), 429
    if not email_enabled():
        return jsonify({"status": "error", "reason": "email is not set up on this VIGIL server yet"}), 503
    db().execute("INSERT INTO login_fail VALUES (?,?)", (key, time.time()))
    db().commit()
    u = db().execute("SELECT * FROM users WHERE id=?", (g.user_id,)).fetchone()
    if u["email_verified"]:
        return jsonify({"status": "ok", "already": True})
    _send_verify_code(u)
    return jsonify({"status": "ok"})


def _normalise_code(code):
    return _re.sub(r"[^A-Z0-9]", "", (code or "").upper())


@app.route("/auth/claim", methods=["POST"])
@require_user
def claim_camera():
    """Add a camera to this account with the setup code from its card."""
    body = request.get_json(silent=True) or {}
    code = _normalise_code(body.get("code"))
    key = "claim|%s" % g.user_id
    if _too_many_fails(key):
        return jsonify({"status": "error", "reason": "too many wrong codes, wait 15 minutes"}), 429
    site = next((s for s in sites().values() if code and _normalise_code(s.get("setup_code")) == code), None)
    if not site:
        db().execute("INSERT INTO login_fail VALUES (?,?)", (key, time.time()))
        db().commit()
        return jsonify({"status": "error", "reason": "that code was not recognised; check the card and try again"}), 404
    has_owner = db().execute("SELECT 1 FROM grants WHERE site_id=? AND role='owner'", (site["id"],)).fetchone()
    already = db().execute("SELECT 1 FROM grants WHERE user_id=? AND site_id=?", (g.user_id, site["id"])).fetchone()
    db().execute("INSERT OR IGNORE INTO grants VALUES (?,?,?)",
                 (g.user_id, site["id"], "caregiver" if has_owner else "owner"))
    db().commit()
    audit("claim", site_id=site["id"])
    if has_owner and not already:            # MEMBERS_V1: owners always know who joined
        me_ = db().execute("SELECT name, email FROM users WHERE id=?", (g.user_id,)).fetchone()
        for o in db().execute("SELECT u.email FROM grants gr JOIN users u ON u.id=gr.user_id "
                              "WHERE gr.site_id=? AND gr.role='owner' AND u.disabled=0", (site["id"],)):
            send_email(o[0], "VIGIL: %s joined %s" % (me_["name"] or me_["email"], site.get("name", site["id"])),
                       "%s (%s) used the setup code to join '%s' and can now see its camera and alerts.\n\n"
                       "If you did not expect this, open VIGIL > Settings > People with access and remove them."
                       % (me_["name"] or "Someone", me_["email"], site.get("name", site["id"])))
    role = db().execute("SELECT role FROM grants WHERE user_id=? AND site_id=?", (g.user_id, site["id"])).fetchone()
    return jsonify({"status": "ok", "site": public_site(site), "role": role[0] if role else "caregiver",
                    "sites": [public_site(s) for s in user_sites(g.user_id, g.is_admin)]})


@app.route("/auth/profile", methods=["GET", "POST"])
@require_user
def profile():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if "name" in body:
            db().execute("UPDATE users SET name=? WHERE id=?", ((body.get("name") or "").strip()[:60], g.user_id))
        if "phone" in body:
            db().execute("UPDATE users SET phone=? WHERE id=?", ((body.get("phone") or "").strip()[:30], g.user_id))
        if "email_alerts" in body:
            db().execute("UPDATE users SET email_alerts=? WHERE id=?", (1 if body.get("email_alerts") else 0, g.user_id))
        db().commit()
    u = db().execute("SELECT * FROM users WHERE id=?", (g.user_id,)).fetchone()
    return jsonify({"status": "ok", "user": _user_json(u)})


@app.route("/auth/test-email", methods=["POST"])
@require_user
def test_email():
    u = db().execute("SELECT * FROM users WHERE id=?", (g.user_id,)).fetchone()
    if not email_enabled():
        return jsonify({"status": "error", "reason": "email is not set up on this VIGIL server yet"}), 503
    ok = send_email(u["email"], "VIGIL test email",
                    "This is a test. Real VIGIL alerts will arrive at this address.\n\n- VIGIL", wait=True)
    return jsonify({"status": "ok" if ok else "error",
                    "reason": None if ok else "the email could not be sent; check the server's email settings"})


@app.route("/auth/forgot", methods=["POST"])
def forgot_password():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0]
    if _too_many_fails("forgot|" + ip):
        return jsonify({"status": "error", "reason": "too many attempts, wait 15 minutes"}), 429
    db().execute("INSERT INTO login_fail VALUES (?,?)", ("forgot|" + ip, time.time()))
    db().commit()
    u = db().execute("SELECT * FROM users WHERE email=? AND disabled=0", (email,)).fetchone()
    if u:
        code = "%06d" % secrets.randbelow(1000000)
        db().execute("INSERT OR REPLACE INTO reset_codes VALUES (?,?,?,0)", (email, _h(code), time.time() + 1800))
        db().commit()
        send_email(email, "Your VIGIL code: %s" % code,
                   "Your VIGIL password reset code is %s\n\nIt works for 30 minutes. If you did not ask for it, "
                   "you can ignore this email.\n\n- VIGIL" % code)
    # Same answer whether or not the account exists, so emails cannot be probed.
    return jsonify({"status": "ok", "email_enabled": email_enabled()})


@app.route("/auth/reset", methods=["POST"])
def reset_password():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    code = (body.get("code") or "").strip()
    new = body.get("new_password") or ""
    if len(new) < 8:
        return jsonify({"status": "error", "reason": "use a password of at least 8 characters"}), 400
    r = db().execute("SELECT * FROM reset_codes WHERE email=?", (email,)).fetchone()
    if not r or r["expires"] < time.time() or r["tries"] >= 5:
        return jsonify({"status": "error", "reason": "that code has expired; ask for a new one"}), 400
    if not secrets.compare_digest(r["code_hash"], _h(code)):
        db().execute("UPDATE reset_codes SET tries=tries+1 WHERE email=?", (email,))
        db().commit()
        return jsonify({"status": "error", "reason": "that code is not right"}), 400
    db().execute("UPDATE users SET pw_hash=?, email_verified=1 WHERE email=?", (generate_password_hash(new), email))
    db().execute("DELETE FROM reset_codes WHERE email=?", (email,))
    u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    db().execute("DELETE FROM tokens WHERE user_id=?", (u["id"],))
    db().commit()
    audit("password_reset", user_id=u["id"])
    return jsonify({"status": "ok", "token": _new_token(u["id"], body.get("device")), "user": _user_json(u),
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
    u = db().execute("SELECT * FROM users WHERE id=?", (g.user_id,)).fetchone()
    return jsonify({"status": "ok", "user": _user_json(u),
                    "sites": [public_site(s) for s in user_sites(g.user_id, g.is_admin)]})


@app.route("/auth/password", methods=["POST"])
@require_user
def change_password():
    body = request.get_json(silent=True) or {}
    new = body.get("new_password") or ""
    if len(new) < 8:
        return jsonify({"status": "error", "reason": "use at least 8 characters"}), 400
    u = db().execute("SELECT pw_hash FROM users WHERE id=?", (g.user_id,)).fetchone()
    if not check_password_hash(u["pw_hash"], body.get("old_password") or ""):
        return jsonify({"status": "error", "reason": "current password is wrong"}), 401
    db().execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(new), g.user_id))
    db().execute("DELETE FROM tokens WHERE user_id=? AND token_hash<>?", (g.user_id, _h(_token_from_request())))
    db().commit()
    audit("password_changed")
    return jsonify({"status": "ok"})


# MEMBERS_V1: who can see a camera, and removing them.
def _role(site_id, user_id=None):
    r = db().execute("SELECT role FROM grants WHERE user_id=? AND site_id=?",
                     (g.user_id if user_id is None else user_id, site_id)).fetchone()
    return r[0] if r else None


@app.route("/auth/site/<site_id>/members")
@require_user
def site_members(site_id):
    if not (_role(site_id) or g.is_admin):
        return jsonify({"status": "error", "reason": "not your camera"}), 403
    rows = db().execute("SELECT u.id, u.name, u.email, gr.role FROM grants gr JOIN users u ON u.id=gr.user_id "
                        "WHERE gr.site_id=? AND u.disabled=0 ORDER BY gr.role='owner' DESC, u.name", (site_id,)).fetchall()
    s_ = sites().get(site_id, {})
    return jsonify({"status": "ok", "my_role": _role(site_id), "setup_code": s_.get("setup_code", "") if _role(site_id) == "owner" else "",
                    "members": [{"id": r["id"], "name": r["name"] or "", "email": r["email"], "role": r["role"],
                                 "me": r["id"] == g.user_id} for r in rows]})


@app.route("/auth/site/<site_id>/members/<int:uid>/remove", methods=["POST"])
@require_user
def site_member_remove(site_id, uid):
    mine = _role(site_id)
    if uid != g.user_id and mine != "owner" and not g.is_admin:
        return jsonify({"status": "error", "reason": "only the owner can remove people"}), 403
    target = _role(site_id, uid)
    if not target:
        return jsonify({"status": "error", "reason": "not a member"}), 404
    if target == "owner":
        others = db().execute("SELECT COUNT(*) FROM grants WHERE site_id=? AND role='owner' AND user_id<>?",
                              (site_id, uid)).fetchone()[0]
        if not others:
            return jsonify({"status": "error", "reason": "the owner cannot leave; make someone else owner first or delete the account"}), 400
    db().execute("DELETE FROM grants WHERE site_id=? AND user_id=?", (site_id, uid))
    db().commit()
    audit("member_removed:%d" % uid, site_id=site_id)
    return jsonify({"status": "ok"})


@app.route("/auth/site/<site_id>/members/<int:uid>/owner", methods=["POST"])
@require_user
def site_member_make_owner(site_id, uid):
    if _role(site_id) != "owner" and not g.is_admin:
        return jsonify({"status": "error", "reason": "only the owner can do this"}), 403
    if not _role(site_id, uid):
        return jsonify({"status": "error", "reason": "not a member"}), 404
    db().execute("UPDATE grants SET role='owner' WHERE site_id=? AND user_id=?", (site_id, uid))
    db().commit()
    audit("owner_added:%d" % uid, site_id=site_id)
    return jsonify({"status": "ok"})


# SETUP_LIVE_V1: the relay box says how far it got (Wi-Fi, camera found,
# sending), so the app can show real progress while the family sets it up.
_RELAY = {}
try:
    with open(os.path.join(DATA_DIR, "relay_status.json")) as _f:
        _RELAY.update(json.load(_f))
except Exception:
    pass


@app.route("/relay/<site_id>/status", methods=["POST"])
def relay_status_post(site_id):
    s_ = sites().get(site_id)
    key = request.headers.get("X-Relay-Key", "")
    want = (s_ or {}).get("relay_key_sha256")
    if not s_ or not want or not hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), want):
        return jsonify({"status": "error"}), 403
    body = request.get_json(silent=True) or {}
    _RELAY[site_id] = {k: body.get(k) for k in ("state", "camera", "mode", "wifi", "signal_dbm", "version", "code")}
    _RELAY[site_id]["at"] = time.time()
    try:                                   # AUTOUPDATE_V1: vigilctl.py boxes reads this
        tmp = os.path.join(DATA_DIR, ".relay_status.tmp")
        with open(tmp, "w") as f:
            json.dump(_RELAY, f)
        os.replace(tmp, os.path.join(DATA_DIR, "relay_status.json"))
    except Exception:
        pass
    return jsonify({"status": "ok"})


@app.route("/auth/site/<site_id>/settings", methods=["POST"])
@require_user
def site_settings(site_id):
    """SITE_NAMES_V1: an owner renames a camera, or puts it in a home with others."""
    if not (_role(site_id) == "owner" or g.is_admin):
        return jsonify({"status": "error", "reason": "only the owner can change this"}), 403
    body = request.get_json(silent=True) or {}
    cur = _site_names().get(site_id, (None, None))
    name = body["name"].strip()[:40] if isinstance(body.get("name"), str) else cur[0]
    home = body["home"].strip()[:40] if isinstance(body.get("home"), str) else cur[1]
    if name is not None and not name:
        return jsonify({"status": "error", "reason": "type a name"}), 400
    db().execute("INSERT OR REPLACE INTO site_names (site_id, name, home) VALUES (?,?,?)", (site_id, name, home))
    db().commit()
    audit("rename", site_id=site_id)
    return jsonify({"status": "ok", "site": public_site(sites()[site_id]),
                    "sites": [public_site(x) for x in user_sites(g.user_id, g.is_admin)]})


@app.route("/auth/site/<site_id>/copy-people", methods=["POST"])
@require_user
def copy_people(site_id):
    """COPY_PEOPLE_V1: the same family on two cameras. Copies each person (name,
    who lives there, height, details, photo) from another camera this owner has;
    the photo teaches the new camera their face. People already there are kept."""
    src = (request.get_json(silent=True) or {}).get("from")
    if not (_role(site_id) == "owner" or g.is_admin) or not (_role(src) == "owner" or g.is_admin):
        return jsonify({"status": "error", "reason": "you need to own both cameras"}), 403
    a, b = sites().get(src), sites().get(site_id)
    if not a or not b or src == site_id:
        return jsonify({"status": "error", "reason": "choose another camera"}), 400
    try:
        people = requests.get("%s/persons" % a["internal_url"].rstrip("/"), headers={"X-VIGIL-Key": a["api_key"]},
                              timeout=20).json()
    except Exception:
        return jsonify({"status": "error", "reason": "the other camera's server is not responding"}), 502
    added, skipped = [], []
    keys = ("is_resident", "height_cm", "birth_year", "sex", "weight_kg", "emergency_name", "emergency_phone", "relationship")
    for p in people if isinstance(people, list) else []:
        name = (p.get("name") or "").strip()
        if not name:
            continue                                         # unnamed visitors stay where they were seen
        body = {k: p.get(k) for k in keys if p.get(k) is not None}
        body["name"] = name
        ph = p.get("photo_b64")
        if ph and len(ph) <= 200 * 1024:
            body["photo_b64"] = ph
        try:
            r = requests.post("%s/persons/new" % b["internal_url"].rstrip("/"), json=body,
                              headers={"X-VIGIL-Key": b["api_key"]}, timeout=20)
            (added if r.status_code == 200 else skipped).append(name)
            if r.status_code == 200 and p.get("consent_at"):
                requests.post("%s/persons/%s/consent" % (b["internal_url"].rstrip("/"), r.json()["person_id"]),
                              json={"given": True, "by": p.get("consent_by") or "copied"},
                              headers={"X-VIGIL-Key": b["api_key"]}, timeout=20)
        except Exception:
            skipped.append(name)
    audit("copy people from %s" % src, site_id=site_id)
    return jsonify({"status": "ok", "added": added, "already_there": skipped})


@app.route("/auth/site/<site_id>/relay")
@require_user
def relay_status_get(site_id):
    if not (_role(site_id) or g.is_admin):
        return jsonify({"status": "error", "reason": "not your camera"}), 403
    r = dict(_RELAY.get(site_id) or {})
    age = time.time() - r["at"] if r.get("at") else None
    st = _STATUS.get(site_id, {})
    return jsonify({"status": "ok", "box_online": age is not None and age < 45, "box_age_s": age,
                    "state": r.get("state"), "camera": r.get("camera"), "wifi": r.get("wifi"),
                    "signal_dbm": r.get("signal_dbm"), "code": r.get("code"),
                    "site_online": bool(st.get("online"))})


# ACCOUNT_DELETE_V1 (App Store 5.1.1(v)): delete the account from inside the app.
@app.route("/auth/delete", methods=["POST"])
@require_user
def delete_account():
    body = request.get_json(silent=True) or {}
    u = db().execute("SELECT * FROM users WHERE id=?", (g.user_id,)).fetchone()
    if not check_password_hash(u["pw_hash"], body.get("password") or ""):
        return jsonify({"status": "error", "reason": "password is wrong"}), 401
    uid = g.user_id
    # a home this person owns alone passes to its longest-standing caregiver
    for (sid,) in db().execute("SELECT site_id FROM grants WHERE user_id=? AND role='owner'", (uid,)).fetchall():
        others = db().execute("SELECT COUNT(*) FROM grants WHERE site_id=? AND role='owner' AND user_id<>?", (sid, uid)).fetchone()[0]
        if not others:
            nxt = db().execute("SELECT user_id FROM grants WHERE site_id=? AND user_id<>? ORDER BY rowid LIMIT 1", (sid, uid)).fetchone()
            if nxt:
                db().execute("UPDATE grants SET role='owner' WHERE site_id=? AND user_id=?", (sid, nxt[0]))
    for sql in ("DELETE FROM tokens WHERE user_id=?", "DELETE FROM grants WHERE user_id=?",
                "DELETE FROM push_tokens WHERE user_id=?",
                "DELETE FROM verify_codes WHERE user_id=?", "DELETE FROM users WHERE id=?"):
        db().execute(sql, (uid,))
    db().execute("DELETE FROM reset_codes WHERE email=?", (u["email"],))
    db().execute("UPDATE audit SET user_id=NULL WHERE user_id=?", (uid,))
    db().commit()
    audit("account_deleted", user_id=None)
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


# APPLE_PUSH_V1 ------------------------------------------------------------
# Alerts go straight to the VIGIL app through Apple's push service. Needs
# /config/apns.json {"key_id", "team_id", "bundle_id"} and /config/apns_key.p8
# (written by vigilctl.py apns). Until then this is a no-op and ntfy carries on.
APNS_CFG = os.path.join(CONFIG_DIR, "apns.json")
APNS_KEY = os.path.join(CONFIG_DIR, "apns_key.p8")
_APNS = {"jwt": None, "at": 0.0, "client": None}


def apns_config():
    try:
        with open(APNS_CFG) as f:
            c = json.load(f)
        with open(APNS_KEY) as f:
            c["key"] = f.read()
        return c if c.get("key_id") and c.get("team_id") else None
    except Exception:
        return None


def _apns_jwt(c):
    if _APNS["jwt"] and time.time() - _APNS["at"] < 2400:      # Apple wants a fresh one at least hourly
        return _APNS["jwt"]
    import jwt                                                  # PyJWT with cryptography (ES256)
    _APNS["jwt"] = jwt.encode({"iss": c["team_id"], "iat": int(time.time())}, c["key"],
                              algorithm="ES256", headers={"kid": c["key_id"]})
    _APNS["at"] = time.time()
    return _APNS["jwt"]


def _apns_client():
    if _APNS["client"] is None:
        import httpx
        _APNS["client"] = httpx.Client(http2=True, timeout=10)
    return _APNS["client"]


def send_apns(user_ids, title, body, site_id=None, kind="alert", urgent=False):
    """Push to every phone of these users. Returns how many phones accepted it."""
    c = apns_config()
    if not c or not user_ids:
        return 0
    con = sqlite3.connect(DB_PATH, timeout=10)
    q = ",".join("?" * len(user_ids))
    rows = con.execute("SELECT token, env FROM push_tokens WHERE user_id IN (%s)" % q, list(user_ids)).fetchall()
    payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default",
                       "interruption-level": "time-sensitive" if urgent else "active"},
               "site": site_id, "kind": kind}
    ok = 0
    hdr = {"apns-topic": c.get("bundle_id", "com.rushilmohan.vigilapp"), "apns-push-type": "alert",
           "apns-priority": "10"}
    for token, env in rows:
        # A phone running a build from Xcode uses Apple's sandbox; TestFlight and the
        # App Store use production. Try the remembered one, then the other.
        order = ["sandbox", "production"] if env == "sandbox" else ["production", "sandbox"]
        try:
            for i, e in enumerate(order):
                host = "api.sandbox.push.apple.com" if e == "sandbox" else "api.push.apple.com"
                r = _apns_client().post("https://%s/3/device/%s" % (host, token), json=payload,
                                        headers=dict(hdr, authorization="bearer " + _apns_jwt(c)))
                if r.status_code == 200:
                    ok += 1
                    con.execute("UPDATE push_tokens SET last_ok=?, env=? WHERE token=?", (time.time(), e, token))
                    break
                if r.status_code == 400 and "BadDeviceToken" in r.text and i == 0:
                    continue                                    # wrong environment for this build
                if r.status_code == 410 or (r.status_code == 400 and "BadDeviceToken" in r.text):
                    con.execute("DELETE FROM push_tokens WHERE token=?", (token,))   # app deleted
                else:
                    print("[APNS] %s %s" % (r.status_code, r.text[:200]), flush=True)
                break
        except Exception as e:
            print("[APNS] %s" % e, flush=True)
    con.commit()
    con.close()
    return ok


def _site_user_ids(site_id):
    con = sqlite3.connect(DB_PATH, timeout=10)
    ids = [r[0] for r in con.execute("""SELECT DISTINCT u.id FROM users u LEFT JOIN grants g
                                        ON g.user_id=u.id AND g.site_id=?
                                        WHERE u.disabled=0 AND (g.site_id IS NOT NULL OR u.is_admin=1)""", (site_id,))]
    con.close()
    return ids


def _alert_site(s, title, message, priority="high"):
    _push(s.get("ntfy_topic"), title, message, priority)
    try:
        send_apns(_site_user_ids(s["id"]), title, message, site_id=s["id"], kind="status",
                  urgent=priority == "urgent")
    except Exception as e:
        print("[APNS] %s" % e, flush=True)


@app.route("/auth/push-token", methods=["POST", "DELETE"])
@require_user
def push_token():
    body = request.get_json(silent=True) or {}
    tok = "".join(ch for ch in str(body.get("token") or "") if ch in "0123456789abcdefABCDEF")
    if len(tok) < 32:
        return jsonify({"status": "error", "reason": "no token"}), 400
    if request.method == "DELETE":
        db().execute("DELETE FROM push_tokens WHERE token=? AND user_id=?", (tok, g.user_id))
    else:
        env = "sandbox" if body.get("env") == "sandbox" else "production"
        db().execute("INSERT OR REPLACE INTO push_tokens (token, user_id, env, created) VALUES (?,?,?,?)",
                     (tok, g.user_id, env, time.time()))
    db().commit()
    return jsonify({"status": "ok", "push_ready": bool(apns_config())})


@app.route("/internal/push/<site_id>", methods=["POST"])
def internal_push(site_id):
    """A camera's own alerts (falls, night, stillness) reach its family's phones."""
    s_ = sites().get(site_id)
    if not s_ or not hmac.compare_digest(request.headers.get("X-VIGIL-Key", ""), s_.get("api_key", "")):
        return jsonify({"status": "error"}), 403
    body = request.get_json(silent=True) or {}
    title = str(body.get("title") or "VIGIL")[:120]
    msg = str(body.get("message") or "")[:400]
    n = send_apns(_site_user_ids(site_id), title, msg, site_id=site_id, kind=str(body.get("kind") or "alert")[:20],
                  urgent=body.get("priority") in ("urgent", "high"))
    return jsonify({"status": "ok", "phones": n})


def _site_emails(site_id):
    """Everyone granted this camera who wants email alerts (admins included)."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=10)
        rows = c.execute("""SELECT DISTINCT u.email FROM users u
                            LEFT JOIN grants g ON g.user_id=u.id AND g.site_id=?
                            WHERE u.disabled=0 AND COALESCE(u.email_alerts,1)=1 AND COALESCE(u.email_verified,1)=1
                              AND g.site_id IS NOT NULL""",
                         (site_id,)).fetchall()
        c.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _email_site(s, subject, body):
    for to in _site_emails(s["id"]):
        send_email(to, subject, body)


# ALERT_EMAIL_V1: every new alert on a camera is emailed to its caregivers.
_ALERT_TEXT = {
    "fall": ("Possible fall", "VIGIL saw a possible fall. Please check on them now, and tap \"I'm checking\" in the app."),
    "stillness": ("No movement", "VIGIL has seen no movement for an unusually long time. Please check on them."),
    "bed_exit": ("Up during the night", "VIGIL saw them get up from bed during the night."),
    "bed_sitting": ("Sitting up in bed", "VIGIL saw them sit up in bed during the night; they may be about to get up."),
    "health_signal": ("Change noted", "VIGIL noticed a change from their usual pattern. Open the app to see it."),
}


def _alert_email_once():
    c = sqlite3.connect(DB_PATH, timeout=10)
    for s in list(sites().values()):
        try:
            r = requests.get(s["internal_url"].rstrip("/") + "/alerts/all",
                             headers={"X-VIGIL-Key": s["api_key"]}, timeout=10)
            alerts = r.json() if r.ok else []
        except Exception:
            continue
        if not isinstance(alerts, list):
            continue
        ids = [int(a.get("id") or 0) for a in alerts]
        top = max(ids) if ids else 0
        row = c.execute("SELECT last_alert_id FROM site_state WHERE site_id=?", (s["id"],)).fetchone()
        if row is None or top < (row[0] or 0):
            c.execute("INSERT OR REPLACE INTO site_state VALUES (?,?)", (s["id"], top))   # start fresh, no backlog
            c.commit()
            continue
        last = row[0] or 0
        for a in sorted((a for a in alerts if int(a.get("id") or 0) > last), key=lambda a: int(a["id"])):
            kind = a.get("alert_type")
            payload = a.get("payload") or {}
            if "TEST" in str(payload).upper() or kind not in _ALERT_TEXT:
                continue
            title, text = _ALERT_TEXT[kind]
            if kind == "fall" and "floor" in str(payload).lower():
                title, text = "Person on the floor", "Someone has been on the floor for over a minute. Please check on them now."
            when = str(a.get("detected_at") or "")[:16].replace("T", " ")
            _email_site(s, "VIGIL: %s (%s)" % (title, s.get("name", s["id"])),
                        "%s\n\nCamera: %s\nTime: %s (camera's local time)\n\nOpen the VIGIL app to see details "
                        "and the stick-figure replay.\n\n- VIGIL" % (text, s.get("name", s["id"]), when))
        c.execute("INSERT OR REPLACE INTO site_state VALUES (?,?)", (s["id"], top))
        c.commit()
    c.close()


def alert_email_loop():
    while True:
        if email_enabled():
            try:
                _alert_email_once()
            except Exception as e:
                print("[EMAIL] alert loop error: %s" % e, flush=True)
        time.sleep(15)


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
            name = (_site_names().get(s["id"], (None,))[0]) or s.get("name", s["id"])   # SITE_NAMES_V1
            if prev.get("online") is True and not online:
                st["offline_since"] = now
                st["last_push"] = now
                _alert_site(s, "VIGIL: monitoring offline",
                      "The camera '%s' stopped sending data. Falls will not be detected until it is back. "
                      "Check its power and Wi-Fi." % name, priority="urgent")
                _email_site(s, "VIGIL: camera offline (%s)" % name,
                            "The camera '%s' stopped sending data, so falls cannot be detected right now.\n\n"
                            "Please check that the camera and the small VIGIL box next to it are plugged in, and that "
                            "the Wi-Fi is working. Unplugging both for 10 seconds and plugging them back in fixes most "
                            "problems.\n\n- VIGIL" % name)
            elif not online and prev.get("online") is False:
                st["offline_since"] = prev.get("offline_since", now)
                st["last_push"] = prev.get("last_push", now)
                if now - st["last_push"] > 3600:
                    st["last_push"] = now
                    _alert_site(s, "VIGIL: still offline",
                          "The camera '%s' has been offline for %d minutes." %
                          (name, (now - st["offline_since"]) / 60))
            elif not online and prev.get("online") is None:
                # Never seen online since the gateway started: warn once after 15 min.
                st["first_check"] = prev.get("first_check", now)
                if now - st["first_check"] > 900:
                    st["online"] = False
                    st["offline_since"] = st["first_check"]
                    st["last_push"] = now
                    _alert_site(s, "VIGIL: camera not connected",
                          "The camera '%s' has not sent any data. Falls will not be detected until it does. "
                          "Check its power, Wi-Fi and RTMP settings." % name, priority="urgent")
                else:
                    st["online"] = None
            elif online and prev.get("online") is False:
                _alert_site(s, "VIGIL: monitoring restored",
                      "The camera '%s' is back online." % name, priority="default")
                _email_site(s, "VIGIL: camera back online (%s)" % name,
                            "The camera '%s' is working again.\n\n- VIGIL" % name)
            _STATUS[s["id"]] = st
        time.sleep(60)


@app.route("/sites/status")
@require_user
def sites_status():
    return jsonify({"status": "ok", "sites": [public_site(s) for s in user_sites(g.user_id, g.is_admin)]})


init_db()
if os.environ.get("VIGIL_WATCHDOG", "1") == "1":
    threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()
    threading.Thread(target=alert_email_loop, daemon=True, name="alert-email").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
