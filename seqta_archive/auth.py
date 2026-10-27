"""
Sign-in.

AUTH_MODE=local  - accounts stored in the state DB: scrypt/pbkdf2 password hashes (werkzeug),
                   optional TOTP (RFC 6238, stdlib), lockout after repeated failures,
                   sessions revoked when a password changes or an account is disabled.
AUTH_MODE=proxy  - identity and groups come from X-Forwarded-* headers set by oauth2-proxy
                   (or similar). Only accepted from TRUSTED_PROXIES.
"""

import base64
import hashlib
import hmac
import ipaddress
import secrets
import socket
import struct
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from flask import abort, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import config

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    pw_hash TEXT NOT NULL,
    pw_changed TEXT NOT NULL,
    must_change INTEGER NOT NULL DEFAULT 1,
    totp_secret TEXT,
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    totp_last_step INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created TEXT NOT NULL,
    last_login TEXT,
    sess_gen INTEGER NOT NULL DEFAULT 0
);
"""


def now_iso():
    # microseconds: password-change timestamps double as session revocation tokens
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------- passwords
def hash_password(pw):
    return generate_password_hash(pw)


def password_problem(pw, username=""):
    if len(pw) < config.MIN_PASSWORD:
        return f"Use at least {config.MIN_PASSWORD} characters."
    if username and username.lower() in pw.lower():
        return "Don't include your username in the password."
    if len(set(pw)) < 5:
        return "That password is too repetitive."
    return None


_DUMMY_HASH = generate_password_hash("not-a-real-password")


def random_password():
    # 4 groups of 5 from an unambiguous alphabet, e.g. kq7m2-x9vtd-...
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))


# ---------------------------------------------------------------- TOTP (RFC 6238)
def totp_new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp_at(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_verify(secret, code, last_step=0, window=1):
    """Return the matched time step (to block replay) or None."""
    code = "".join(c for c in (code or "") if c.isdigit())
    if len(code) != 6 or not secret:
        return None
    now = int(time.time() // 30)
    for step in range(now - window, now + window + 1):
        if step > last_step and hmac.compare_digest(_totp_at(secret, step), code):
            return step
    return None


def totp_uri(secret, username):
    issuer = f"SEQTA Archive ({config.SCHOOL_SHORT or config.SCHOOL_NAME})"
    return (f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30")


def totp_qr_svg(uri):
    try:
        import segno
    except ImportError:
        return ""
    return segno.make(uri, error="m").svg_inline(scale=5, border=2)


# ---------------------------------------------------------------- CSRF
def csrf_token():
    t = session.get("csrf")
    if not t:
        t = secrets.token_urlsafe(32)
        session["csrf"] = t
    return t


def check_csrf():
    sent = request.form.get("csrf", "")
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        abort(400, "Your session expired or the form was stale. Go back, reload and try again.")


# ---------------------------------------------------------------- proxy mode
_trusted_nets = None
_trusted_at = 0.0


def _trusted():
    global _trusted_nets, _trusted_at
    if _trusted_nets is None or time.time() - _trusted_at > 60:
        _trusted_at = time.time()
        nets = []
        for item in config.TRUSTED_PROXIES:
            try:
                nets.append(ipaddress.ip_network(item, strict=False))
                continue
            except ValueError:
                pass
            try:  # a hostname, e.g. the oauth2-proxy container name in Docker
                for fam, _, _, _, addr in socket.getaddrinfo(item, None):
                    nets.append(ipaddress.ip_network(addr[0]))
            except OSError:
                print(f"TRUSTED_PROXIES: can't resolve {item!r}", flush=True)
        _trusted_nets = nets
    return _trusted_nets


def from_trusted_proxy():
    try:
        ip = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    return any(ip in n for n in _trusted())


def _norm_group(x):
    """Plain claim values, or role:<client>:<name> for this client only."""
    x = x.strip().lstrip("/")
    if x.startswith("role:"):
        parts = x.split(":")
        if len(parts) == 3 and config.OIDC_CLIENT_ID and parts[1] == config.OIDC_CLIENT_ID:
            return parts[2]
        if len(parts) == 2:
            return parts[1]
        return ""
    return x


def proxy_identity():
    """(user, email, groups) from oauth2-proxy headers, or abort."""
    if not from_trusted_proxy():
        abort(403, "Requests must come through the sign-in proxy.")
    user = (request.headers.get("X-Forwarded-Preferred-Username")
            or request.headers.get("X-Forwarded-User") or "")
    email = request.headers.get("X-Forwarded-Email", "")
    groups = {_norm_group(x) for x in request.headers.get("X-Forwarded-Groups", "").split(",")
              if x.strip()}
    groups.discard("")
    return user, email, groups


# ---------------------------------------------------------------- local mode: routes
PUBLIC_ENDPOINTS = {"a.login", "a.static", "a.healthz", "a.readyz"}
SETUP_ENDPOINTS = {"a.logout", "a.account", "a.account_password", "a.account_totp",
                   "a.static"}


def safe_next(target):
    if not target:
        return None
    p = urlparse(target)
    if p.scheme or p.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def register(bp, state_db, audit):
    """Add the local-account routes to the blueprint."""

    def get_user(uid=None, username=None):
        con = state_db()
        if uid is not None:
            return con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

    def current_user():
        """Resolve the session to a user row, enforcing expiry/revocation. None if signed out."""
        uid = session.get("uid")
        if not uid:
            return None
        u = get_user(uid=uid)
        if (not u or u["disabled"] or session.get("pwv") != u["pw_changed"]
                or session.get("gen") != u["sess_gen"]):
            session.clear()
            return None
        started = session.get("t0", 0)
        if time.time() - started > config.SESSION_HOURS * 3600:
            session.clear()
            return None
        return u

    bp.current_user = current_user

    FAIL_MSG = ("Sign-in failed. Check your username, password and code. "
                "After several failures the account is locked for a while.")

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        nxt = safe_next(request.values.get("next")) or url_for("a.home")
        if request.method == "POST":
            check_csrf()
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            code = request.form.get("code") or ""
            u = get_user(username=username) if username else None
            con = state_db()
            usable = bool(u) and not u["disabled"] and not (
                u["locked_until"] and u["locked_until"] > now_iso())
            # Always do one hash check so timing doesn't reveal which accounts exist
            pw_ok = check_password_hash(u["pw_hash"] if usable else _DUMMY_HASH, password) \
                and usable
            step = None
            if pw_ok and u["totp_enabled"]:
                step = totp_verify(u["totp_secret"], code, u["totp_last_step"])
            ok = pw_ok and (not u["totp_enabled"] or step is not None)
            if ok:
                con.execute("UPDATE users SET failed=0, locked_until=NULL, last_login=?, "
                            "totp_last_step=CASE WHEN ? IS NULL THEN totp_last_step ELSE ? END "
                            "WHERE id=?", (now_iso(), step, step, u["id"]))
                con.commit()
                session.clear()
                session.permanent = True
                session["uid"] = u["id"]
                session["pwv"] = u["pw_changed"]
                session["gen"] = u["sess_gen"]
                session["t0"] = time.time()
                g.user, g.email = u["username"], ""
                audit("login", "/login")
                return redirect(nxt)
            if u and usable:
                # wrong password OR right password + wrong/missing code: both count
                failed = u["failed"] + 1
                lock = None
                if failed >= config.LOCKOUT_ATTEMPTS:
                    lock = (datetime.now(timezone.utc) +
                            timedelta(minutes=config.LOCKOUT_MINUTES)).isoformat(
                                timespec="microseconds")
                    failed = 0
                con.execute("UPDATE users SET failed=?, locked_until=? WHERE id=?",
                            (failed, lock, u["id"]))
                con.commit()
            g.user, g.email = username[:80], ""
            reason = ("no such user" if not u else "disabled" if u["disabled"] else
                      "locked" if not usable else "bad code" if pw_ok else "bad password")
            audit("login_failed", "/login", "", reason)
            error = FAIL_MSG
            time.sleep(0.5)
        return render_template("login.html", error=error, nxt=nxt), (401 if error else 200)

    @bp.route("/logout", methods=["POST"])
    def logout():
        check_csrf()
        uid = session.get("uid")
        audit("logout", "/logout")
        if uid:
            # end this user's sessions server-side, not just in this browser
            con = state_db()
            con.execute("UPDATE users SET sess_gen = sess_gen + 1 WHERE id=?", (uid,))
            con.commit()
        session.clear()
        return redirect(url_for("a.login"))

    @bp.route("/account")
    def account():
        u = current_user()
        return render_template("account.html", u=u)

    @bp.route("/account/password", methods=["GET", "POST"])
    def account_password():
        u = current_user()
        error = None
        if request.method == "POST":
            check_csrf()
            cur = request.form.get("current") or ""
            new = request.form.get("new") or ""
            again = request.form.get("again") or ""
            if not check_password_hash(u["pw_hash"], cur):
                error = "Your current password is wrong."
            elif new != again:
                error = "The new passwords don't match."
            elif check_password_hash(u["pw_hash"], new):
                error = "Choose a password you haven't used here."
            else:
                error = password_problem(new, u["username"])
            if not error:
                changed = now_iso()
                con = state_db()
                con.execute("UPDATE users SET pw_hash=?, pw_changed=?, must_change=0 WHERE id=?",
                            (hash_password(new), changed, u["id"]))
                con.commit()
                session["pwv"] = changed
                audit("password_changed", "/account/password")
                return redirect(url_for("a.account"))
        return render_template("account_password.html", u=u, error=error,
                               min_len=config.MIN_PASSWORD)

    @bp.route("/account/totp", methods=["GET", "POST"])
    def account_totp():
        u = current_user()
        error = None
        secret = session.get("totp_pending") or totp_new_secret()
        session["totp_pending"] = secret
        if request.method == "POST":
            check_csrf()
            if not check_password_hash(u["pw_hash"], request.form.get("password") or ""):
                error = "Enter your current password to change two-step sign-in."
                uri = totp_uri(secret, u["username"])
                return render_template("account_totp.html", u=u, secret=secret, uri=uri,
                                       qr=totp_qr_svg(uri), error=error,
                                       config_required=config.REQUIRE_TOTP), 400
            if request.form.get("action") == "disable":
                if config.REQUIRE_TOTP:
                    abort(400, "Two-step sign-in is required here.")
                con = state_db()
                con.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL WHERE id=?",
                            (u["id"],))
                con.commit()
                audit("totp_disabled", "/account/totp")
                return redirect(url_for("a.account"))
            step = totp_verify(secret, request.form.get("code"))
            if step is None:
                error = "That code didn't match. Check the time on your phone and try again."
            else:
                con = state_db()
                con.execute("UPDATE users SET totp_secret=?, totp_enabled=1, totp_last_step=? "
                            "WHERE id=?", (secret, step, u["id"]))
                con.commit()
                session.pop("totp_pending", None)
                audit("totp_enabled", "/account/totp")
                return redirect(url_for("a.account"))
        uri = totp_uri(secret, u["username"])
        return render_template("account_totp.html", u=u, secret=secret, uri=uri,
                               qr=totp_qr_svg(uri), error=error,
                               config_required=config.REQUIRE_TOTP)

    # ---------------------------------------------------------------- admin: users
    @bp.route("/admin/users", methods=["GET", "POST"])
    def admin_users():
        if not g.is_admin:
            abort(403)
        con = state_db()
        shown_password = None
        error = None
        if request.method == "POST":
            check_csrf()
            act = request.form.get("action")
            uid = request.form.get("uid", type=int)
            target = get_user(uid=uid) if uid else None
            if act == "create":
                username = (request.form.get("username") or "").strip()
                display = (request.form.get("display") or "").strip()
                role = "admin" if request.form.get("role") == "admin" else "user"
                if not username or len(username) > 80 or any(c.isspace() for c in username):
                    error = "Usernames can't be blank or contain spaces."
                elif get_user(username=username):
                    error = f"{username} already exists."
                else:
                    pw = random_password()
                    con.execute("INSERT INTO users(username, display, role, pw_hash, pw_changed, "
                                "must_change, created) VALUES (?,?,?,?,?,1,?)",
                                (username, display, role, hash_password(pw), now_iso(),
                                 now_iso()))
                    con.commit()
                    shown_password = (username, pw)
                    audit("user_created", username, "", f"role={role}")
            elif target is None:
                error = "No such user."
            elif act == "reset_password":
                pw = random_password()
                con.execute("UPDATE users SET pw_hash=?, pw_changed=?, must_change=1, failed=0, "
                            "locked_until=NULL, sess_gen=sess_gen+1 WHERE id=?",
                            (hash_password(pw), now_iso(), uid))
                con.commit()
                shown_password = (target["username"], pw)
                audit("user_password_reset", target["username"])
            elif act == "reset_totp":
                con.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL WHERE id=?", (uid,))
                con.commit()
                audit("user_totp_reset", target["username"])
            elif act in ("disable", "enable"):
                if uid == g.uid:
                    error = "You can't disable your own account."
                else:
                    con.execute("UPDATE users SET disabled=?, sess_gen=sess_gen+1 WHERE id=?",
                                (1 if act == "disable" else 0, uid))
                    con.commit()
                    audit(f"user_{act}d", target["username"])
            elif act in ("make_admin", "make_user"):
                if uid == g.uid:
                    error = "You can't change your own role."
                else:
                    con.execute("UPDATE users SET role=? WHERE id=?",
                                ("admin" if act == "make_admin" else "user", uid))
                    con.commit()
                    audit("user_role_changed", target["username"], "", act)
            elif act == "unlock":
                con.execute("UPDATE users SET failed=0, locked_until=NULL WHERE id=?", (uid,))
                con.commit()
                audit("user_unlocked", target["username"])
        users = con.execute("SELECT * FROM users ORDER BY disabled, role DESC, username "
                            "COLLATE NOCASE").fetchall()
        return render_template("admin_users.html", users=users, shown_password=shown_password,
                               error=error, now=now_iso())

    return current_user
