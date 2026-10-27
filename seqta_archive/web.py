"""
SEQTA Archive - read-only web viewer for a SEQTA offboarding export.

Sign-in is either built-in accounts (AUTH_MODE=local) or an authenticating reverse proxy such
as oauth2-proxy (AUTH_MODE=proxy). Every student view, record view, file download and export
is written to the audit log in the state database.
"""

import html as htmlmod
import io
import math
import os
import re
import sqlite3
import tempfile
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from functools import wraps

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from flask import (Blueprint, Flask, Response, abort, g, redirect, render_template,
                   request, send_file, session, url_for)
from markupsafe import Markup, escape

from . import __version__ as APP_VERSION
from . import auth, config, pdf

DB_PATH = config.DB_PATH
FILES_ROOT = config.FILES_ROOT
STATE_DB = config.STATE_DB
URL_PREFIX = config.URL_PREFIX
SCHOOL_NAME = config.SCHOOL_NAME
try:
    LOCAL_TZ = ZoneInfo(config.TIMEZONE) if ZoneInfo else timezone(timedelta(hours=8))
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=8))

PER_PAGE = 50
VIEWABLE = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov", ".mp3",
            ".m4a", ".wav", ".txt", ".webp"}

bp = Blueprint("a", __name__, url_prefix=URL_PREFIX or None,
               template_folder="templates", static_folder="static",
               static_url_path="/static")


# ---------------------------------------------------------------- db
class Rec(dict):
    """Row that returns '' for columns a school's export doesn't have, and supports r[0]."""
    __slots__ = ("_vals",)

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._vals[k]
        return dict.get(self, k, "")

    def __getattr__(self, k):  # Jinja falls back to item access anyway; this helps Python code
        return self[k]


def rec_factory(cur, row):
    r = Rec(zip([d[0] for d in cur.description], row))
    r._vals = row
    return r


def db():
    if "db" not in g:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
        con.row_factory = rec_factory
        con.execute("PRAGMA query_only=1")
        con.execute("PRAGMA cache_size=-65536")
        g.db = con
    return g.db


_warned = set()


def _schema_problem(sql, e):
    """Exports differ a little between schools. A query that references a column or table this
    export doesn't have returns nothing instead of failing the whole page."""
    key = (str(e), sql[:60])
    if key not in _warned:
        _warned.add(key)
        print(f"SCHEMA: {e} in: {' '.join(sql.split())[:200]}", flush=True)
    try:
        g.schema_warnings = getattr(g, "schema_warnings", 0) + 1
    except RuntimeError:
        pass


def q(sql, args=()):
    try:
        return db().execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        if "no such column" in str(e) or "no such table" in str(e):
            _schema_problem(sql, e)
            return []
        raise


def q1(sql, args=()):
    try:
        return db().execute(sql, args).fetchone()
    except sqlite3.OperationalError as e:
        if "no such column" in str(e) or "no such table" in str(e):
            _schema_problem(sql, e)
            return None
        raise


def has_table(name):
    return name in LK.tables


STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, ts TEXT, user TEXT, email TEXT, ip TEXT,
    action TEXT, target TEXT, student_id TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS ix_audit_user ON audit(user);
CREATE INDEX IF NOT EXISTS ix_audit_student ON audit(student_id);
""" + auth.USERS_SCHEMA


def open_state_db(path=None):
    path = path or STATE_DB
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(STATE_SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    if "sess_gen" not in cols:  # databases created by 3.0.0 pre-releases
        con.execute("ALTER TABLE users ADD COLUMN sess_gen INTEGER NOT NULL DEFAULT 0")
        con.commit()
    return con


def audit_db():
    if "adb" not in g:
        g.adb = open_state_db()
    return g.adb


def audit(action, target="", student_id="", detail=""):
    try:
        con = audit_db()
        con.execute(
            "INSERT INTO audit(ts, user, email, ip, action, target, student_id, detail) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             getattr(g, "user", ""), getattr(g, "email", ""),
             client_ip(), action, str(target), str(student_id), str(detail)[:500]))
        con.commit()
    except Exception as e:  # never block a page on audit failure, but say so in the log
        print(f"AUDIT WRITE FAILED: {e}", flush=True)


def client_ip():
    """The connecting address. Only when that's a trusted reverse proxy do we take the
    address it appended to X-Forwarded-For (the right-most entry, which the client can't forge)."""
    remote = request.remote_addr or ""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff and auth.from_trusted_proxy():
        return xff.split(",")[-1].strip()[:64] or remote
    return remote


# ---------------------------------------------------------------- lookups (loaded once)
class Lookups:
    loaded = False

    def load(self):
        if self.loaded:
            return
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        con.row_factory = rec_factory
        self.tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        self.meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
        global FILES_ROOT
        if not os.path.isdir(FILES_ROOT):
            alt = os.path.join(self.meta.get("source_export", ""), "files")
            if self.meta.get("source_export") and os.path.isdir(alt):
                FILES_ROOT = alt
                print(f"Attachments: using {alt} (from the export the database was built from)",
                      flush=True)

        def kv(table, key, val):
            if table not in self.tables:
                return {}
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            if key not in cols or val not in cols:
                return {}
            return {r[0]: r[1] for r in con.execute(f'SELECT "{key}", "{val}" FROM "{table}"')}

        self.staff_by_code, self.staff_by_id = {}, {}
        if "t_staff" in self.tables:
            for r in con.execute("SELECT * FROM t_staff"):
                name = (r["salutation"] or "").strip() or " ".join(
                    x for x in ((r["prefname"] or r["firstname"]), r["surname"]) if x)
                self.staff_by_code[r["staffCode"]] = name
                self.staff_by_id[r["staffID"]] = name
        self.pc_type = kv("t_pastoralcaretype", "id", "label")
        self.house = kv("t_house", "id", "name")
        self.year = kv("t_schoolyear", "id", "name")
        self.roll = kv("t_rollgroup", "id", "code")
        self.campus = kv("t_campus", "id", "name")
        self.country = kv("t_country", "id", "name")
        self.language = kv("t_language", "id", "name")
        self.religion = kv("t_religion", "id", "name")
        self.aboriginality = kv("t_aboriginality", "id", "description")
        self.school = kv("t_school", "id", "name")
        self.plan_field = kv("t_studentplanfield", "id", "label")
        self.years_sorted = sorted(
            ((k, v) for k, v in self.year.items()),
            key=lambda kv_: (int(kv_[1]) if str(kv_[1]).isdigit() else 99, str(kv_[1])))
        con.close()
        self.loaded = True


LK = Lookups()


def staff_name(code=None, sid=None):
    if code:
        return LK.staff_by_code.get(code, f"staff {code}")
    if sid:
        return LK.staff_by_id.get(sid, f"staff {sid}")
    return ""


# ---------------------------------------------------------------- auth
@bp.before_request
def authenticate():
    LK.load()
    g.user, g.email, g.uid, g.is_admin = "", "", None, False
    ep = request.endpoint or ""
    if ep in ("a.healthz", "a.readyz", "a.static"):
        return None
    if config.AUTH_MODE == "proxy":
        user, email, groups = auth.proxy_identity()
        g.user, g.email = user, email
        if not user:
            abort(401)
        g.is_admin = bool(groups & config.ADMIN_GROUPS)
        if not (groups & config.ACCESS_GROUPS):
            audit("denied", request.path)
            abort(403)
        return None
    # local accounts
    if ep in auth.PUBLIC_ENDPOINTS:
        return None
    u = bp.current_user()
    if not u:
        nxt = request.full_path if request.method == "GET" else None
        return redirect(url_for("a.login", next=nxt.rstrip("?") if nxt else None))
    g.user, g.uid, g.is_admin = u["username"], u["id"], u["role"] == "admin"
    g.display = u["display"] or u["username"]
    if ep not in auth.SETUP_ENDPOINTS:
        if u["must_change"]:
            return redirect(url_for("a.account_password"))
        if config.REQUIRE_TOTP and not u["totp_enabled"]:
            return redirect(url_for("a.account_totp"))
    return None


def admin_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not g.is_admin:
            abort(403)
        return f(*a, **kw)
    return w


@bp.after_request
def headers(resp):
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'self'")
    return resp


@bp.teardown_app_request
def close(_exc):
    for k in ("db", "adb"):
        c = g.pop(k, None)
        if c is not None:
            c.close()


# ---------------------------------------------------------------- helpers
def T(v):
    return str(v or "").strip().lower() in ("t", "true", "1", "yes", "y")


def student_display(r):
    first = (r["prefname"] or r["firstname"] or "").strip()
    return " ".join(x for x in (first, (r["surname"] or "").strip()) if x) or f"#{r['studentID']}"


def status_label(s):
    return {"FULL": "Enrolled", "LEFT": "Left"}.get((s or "").upper(), s or "Unknown")


def day_fmt(dt, weekday=False, time_=False, seconds=False):
    """'Tue 14 Oct 2025 11:16' without the non-portable %-d."""
    out = f"{dt.day} {dt.strftime('%b %Y')}"
    if weekday:
        out = f"{dt.strftime('%a')} {out}"
    if time_:
        out += dt.strftime(" %H:%M:%S" if seconds else " %H:%M")
    return out


def now_local():
    return datetime.now(LOCAL_TZ)


def fmt_dt(s):
    """SEQTA timestamps -> 'Tue 14 Oct 2025 11:16' (or date only at midnight)."""
    if not s:
        return ""
    s = str(s)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?", s)
    if not m:
        return s
    y, mo, d, hh, mm = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0))
    except ValueError:
        return s
    if hh is None or (hh == "00" and mm == "00"):
        return day_fmt(dt, weekday=True)
    return day_fmt(dt, weekday=True, time_=True)


def fmt_date(s):
    if not s:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(s))
    if not m:
        return str(s)
    try:
        return day_fmt(datetime(*map(int, m.groups())))
    except ValueError:
        return str(s)


def hsize(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


_RE_DROP = re.compile(r"<(style|script|head|title)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_RE_BREAK = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h[1-6]|p|li)\b[^>]*>", re.I)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"[ \t\r\f\v ]+")
_RE_NL = re.compile(r"\n\s*\n+")


def html_to_text(s):
    if not s:
        return ""
    if "<" not in s:
        return s.strip()
    s = _RE_DROP.sub(" ", s)
    s = _RE_BREAK.sub("\n", s)
    s = _RE_TAG.sub(" ", s)
    s = htmlmod.unescape(s)
    s = _RE_WS.sub(" ", s)
    s = _RE_NL.sub("\n\n", s)
    return s.strip()


def fts_query(text):
    """User text -> safe FTS5 query: every word must match (prefix on the last one)."""
    words = re.findall(r"[\w'@.-]+", text, re.U)
    words = [w.strip("'.-@") for w in words]
    words = [w for w in words if w]
    if not words:
        return None
    parts = ['"' + w.replace('"', '') + '"' for w in words[:12]]
    if len(words[-1]) >= 3:
        parts[-1] = parts[-1] + "*"
    return " ".join(parts)


def mark_snippet(s):
    """snippet() output uses \x02/\x03 as highlight markers; escape everything else."""
    out = str(escape(s or ""))
    return Markup(out.replace("\x02", "<mark>").replace("\x03", "</mark>"))


class Page:
    def __init__(self, total, page, per=PER_PAGE):
        self.total, self.per = total, per
        self.pages = max(1, math.ceil(total / per))
        self.page = max(1, min(page, self.pages))
        self.offset = (self.page - 1) * per

    def window(self):
        lo, hi = max(1, self.page - 2), min(self.pages, self.page + 2)
        return list(range(lo, hi + 1))


def arg_int(name, default=1):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


@bp.app_template_filter("dt")
def _f_dt(s):
    return fmt_dt(s)


@bp.app_template_filter("localdt")
def _f_localdt(s):
    """ISO timestamp with offset (account/audit times, stored UTC) -> local display."""
    if not s:
        return ""
    try:
        return day_fmt(datetime.fromisoformat(str(s)).astimezone(LOCAL_TZ), weekday=True,
                       time_=True)
    except ValueError:
        return str(s)


@bp.app_template_filter("d")
def _f_d(s):
    return fmt_date(s)


@bp.app_template_filter("hsize")
def _f_hsize(n):
    return hsize(n)


@bp.app_template_filter("yes")
def _f_yes(v):
    return T(v)


def page_url(n):
    va = request.view_args or {}
    args = {k: v for k, v in request.args.items() if k not in va}
    args["page"] = n
    return url_for(request.endpoint, **va, **args)


@bp.app_context_processor
def ctx():
    return {"page_url": page_url, "school": SCHOOL_NAME, "school_short": config.SCHOOL_SHORT,
            "archive_note": config.ARCHIVE_NOTE, "version": APP_VERSION,
            "meta": LK.meta if LK.loaded else {},
            "user": getattr(g, "user", ""), "display": getattr(g, "display", "") or
            getattr(g, "user", ""), "is_admin": getattr(g, "is_admin", False),
            "auth_mode": config.AUTH_MODE, "csrf_token": auth.csrf_token,
            "schema_warnings": getattr(g, "schema_warnings", 0),
            "status_label": status_label, "staff_name": staff_name}


# ---------------------------------------------------------------- student data
def get_student(sid):
    r = q1("SELECT * FROM t_student WHERE studentID = ?", (str(sid),))
    if not r:
        abort(404)
    return r


def attachments_for(kind, ref_ids):
    ref_ids = [str(x) for x in ref_ids if x]
    out = {}
    for i in range(0, len(ref_ids), 500):
        chunk = ref_ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for a in q(f"SELECT * FROM attachment WHERE kind=? AND ref_id IN ({ph}) ORDER BY name",
                   [kind] + chunk):
            out.setdefault(a["ref_id"], []).append(a)
    return out


def student_details(s):
    """Ordered (label, value) pairs of the student record with lookups resolved."""
    def look(table, v):
        return table.get(v, v) if v else ""

    fields = [
        ("Student code", s["studentCode"]), ("Username", s["username"]),
        ("Title", s["title"]), ("First name", s["firstname"]),
        ("Preferred name", s["prefname"]), ("Middle name", s["middlename"]),
        ("Surname", s["surname"]), ("Date of birth", fmt_date(s["birth_date"])),
        ("Birthplace", s["birthplace"]),
        ("Gender", {"f": "Female", "m": "Male", "x": "X / not specified"}.get(
            (s["gender"] or "").lower(), s["gender"])),
        ("Entry date", fmt_date(s["entry_date"])), ("Exit date", fmt_date(s["exit_date"])),
        ("Year", look(LK.year, s["schoolyear"])), ("Roll group", look(LK.roll, s["rollgroup"])),
        ("House", look(LK.house, s["house"])), ("Campus", look(LK.campus, s["campus"])),
        ("Email", s["email"]), ("Mobile", s["phone_mobile"]),
        ("Government ID", s["government_id"]), ("USI", s["usi"]),
        ("Aboriginality", look(LK.aboriginality, s["aboriginality"])),
        ("Country of birth", look(LK.country, s["country_birth"])),
        ("Country of residence", look(LK.country, s["country_residence"])),
        ("Home language", look(LK.language, s["language_home"])),
        ("Religion", look(LK.religion, s["religion"])),
        ("Previous school", look(LK.school, s["school_previous"])),
        ("Next school", look(LK.school, s["school_next"])),
        ("Visa subclass", s["visasubclass"]),
        ("Boarder", "Yes" if T(s["boarder"]) else ""),
        ("Repeating", "Yes" if T(s["repeating"]) else ""),
        ("Record updated", fmt_dt(s["updated_date"])),
    ]
    return [(k, v) for k, v in fields if v]


NOTE_FIELDS = [("notes_medical", "Medical"), ("history_medical", "Medical history"),
               ("notes_doctor", "Doctor"), ("notes_emergency", "Emergency"),
               ("notes_custody", "Custody"), ("notes_special", "Special"),
               ("notes_religion", "Religion"), ("notes_general", "General")]


def student_flags(s):
    out = []
    for col, label in (("alert_medical", "Medical alert"), ("alert_custody", "Custody alert"),
                       ("alert_attendance", "Attendance alert"), ("alert_special", "Special alert")):
        if T(s[col]):
            out.append(label)
    return out


def load_profile(s, for_report=False):
    sid, code = s["studentID"], s["studentCode"]
    P = {"s": s, "name": student_display(s), "details": student_details(s),
         "flags": student_flags(s),
         "notes_on_file": [(lbl, s[c]) for c, lbl in NOTE_FIELDS if (s[c] or "").strip()]}

    P["alerts"] = q("SELECT alertType, updated_date FROM t_studentalert WHERE studentID=? "
                    "ORDER BY updated_date DESC", (sid,)) if has_table("t_studentalert") else []

    # contacts
    P["contacts"] = []
    if has_table("t_studentContact"):
        rows = q("SELECT sc.*, c.salutation, c.title, c.firstname, c.prefname, c.surname, "
                 "c.phone_home, c.phone_work, c.phone_mobile AS c_mobile, c.email AS c_email, "
                 "c.address_street, c.address_suburb, c.address_state, c.address_postcode, "
                 "c.employer, c.occupation, c.archived "
                 "FROM t_studentContact sc LEFT JOIN t_contact c ON c.contactID = sc.contactID "
                 "WHERE sc.studentID=? ORDER BY CAST(sc.priority AS INTEGER) DESC, "
                 "sc.contacttypelabel", (sid,))
        for r in rows:
            name = (r["salutation"] or "").strip() or " ".join(
                x for x in ((r["prefname"] or r["firstname"]), r["surname"]) if x) \
                or f"contact {r['contactcode']}"
            addr = ", ".join(x for x in (r["address_street"], r["address_suburb"],
                                         " ".join(y for y in (r["address_state"],
                                                              r["address_postcode"]) if y)) if x)
            P["contacts"].append({
                "name": name, "rel": r["contacttypelabel"], "priority": r["priority"],
                "phones": [(lbl, v) for lbl, v in (("Mobile", r["c_mobile"]),
                                                   ("Home", r["phone_home"]),
                                                   ("Work", r["phone_work"])) if v],
                "email": r["c_email"], "addr": addr, "custody": r["notes_custody"],
                "receives": [lbl for col, lbl in (("email", "email"), ("sms", "SMS"),
                                                  ("mail", "mail"),
                                                  ("coneqt_access", "portal")) if T(r[col])],
                "occupation": " · ".join(x for x in (r["occupation"], r["employer"]) if x),
                "archived": T(r["archived"])})

    # pastoral care
    P["pastoral"] = []
    if has_table("t_pastoralcare"):
        rows = q('SELECT * FROM t_pastoralcare WHERE studentID=? ORDER BY "from" DESC, id DESC',
                 (sid,))
        att = attachments_for("pc", [r["id"] for r in rows])
        for r in rows:
            sent = [lbl for col, lbl in (
                ("emailed_parents", "parents"), ("smsed_parents", "SMS parents"),
                ("emailed_student", "student"), ("smsed_student", "SMS student"),
                ("emailed_teachers", "teachers"), ("emailed_coords", "coordinators"),
                ("emailed_all", "all staff"), ("emailed_restricted", "restricted group"))
                if T(r[col])]
            if (r["emailed_individuals"] or "[]") not in ("", "[]"):
                sent.append("individual staff")
            P["pastoral"].append({
                "id": r["id"], "type": LK.pc_type.get(r["pastoralcaretype"],
                                                     r["pastoralcaretype"] or "Pastoral care"),
                "tag": r["tag"], "from": r["from"], "until": r["until"],
                "staff": staff_name(code=r["staffCode"]),
                "via": staff_name(code=r["staff_viaCode"])
                if r["staff_viaCode"] and r["staff_viaCode"] != r["staffCode"] else "",
                "details": r["details"], "followup": T(r["requires_followup"]),
                "followed_up": T(r["followed_up"]), "present": T(r["student_present"]),
                "points": r["points"] if r["points"] not in ("", "0", None) else "",
                "restricted": T(r["restricted"]), "sent": sent,
                "files": att.get(r["id"], [])})
    P["pastoral_types"] = sorted({p["type"] for p in P["pastoral"]})

    # sickbay
    P["sickbay"] = []
    if has_table("t_sickbay"):
        rows = q('SELECT * FROM t_sickbay WHERE studentID=? ORDER BY "from" DESC', (sid,))
        ids = [r["id"] for r in rows]
        extra = {}
        if ids:
            ph = ",".join("?" * len(ids))
            for tbl, prefix, label in (("t_sickbay_treatment", "treatment_", "Treatment"),
                                       ("t_sickbay_observation", "observation_", "Observation")):
                if has_table(tbl):
                    for t in q(f"SELECT * FROM {tbl} WHERE sickbay IN ({ph}) ORDER BY timestamp",
                               ids):
                        vals = [t[k] for k in t.keys() if k.startswith(prefix) and t[k]]
                        if vals:
                            extra.setdefault(t["sickbay"], []).append(
                                (label, t["timestamp"], " · ".join(vals)))
            if has_table("t_sickbay_bodyitem"):
                for t in q(f"SELECT * FROM t_sickbay_bodyitem WHERE sickbay IN ({ph})", ids):
                    if t["note"]:
                        extra.setdefault(t["sickbay"], []).append(
                            ("Body chart", t["timestamp"], t["note"]))
        for r in rows:
            flags = []
            for col, lbl in (("ambulance", "Ambulance"), ("parentcontact", "Parent contacted"),
                             ("accidentform", "Accident form"), ("discharged", "Discharged")):
                if T(r[col]) or (r[col + "_details"] or "").strip():
                    flags.append((lbl, r[col + "_details"], col == "ambulance" and T(r[col])))
            P["sickbay"].append({
                "id": r["id"], "from": r["from"], "until": r["until"],
                "location": r["location"], "staff": staff_name(code=r["staffCode"]),
                "notes": r["notes"], "treatment": r["treatment_summary"], "flags": flags,
                "attendance": r["attendancesolution"], "extra": extra.get(r["id"], [])})

    # notes
    P["notes"] = q("SELECT * FROM t_studentnote WHERE studentCode=? ORDER BY updated_date DESC",
                   (code,)) if has_table("t_studentnote") else []

    # support plans
    P["plans"] = []
    if has_table("t_studentplan"):
        rows = q("SELECT * FROM t_studentplan WHERE studentID=? ORDER BY updated_date DESC", (sid,))
        att = attachments_for("sp", [r["id"] for r in rows])
        fields = {}
        if rows and has_table("t_studentplandetail") and \
                has_table("t_studentplandetailStudentplanfield"):
            ph = ",".join("?" * len(rows))
            for d in q("SELECT d.studentplan, f.studentplanfield, f.value "
                       "FROM t_studentplandetail d JOIN t_studentplandetailStudentplanfield f "
                       f"ON f.studentplandetail = d.id WHERE d.studentplan IN ({ph})",
                       [r["id"] for r in rows]):
                if d["value"]:
                    fields.setdefault(d["studentplan"], []).append(
                        (LK.plan_field.get(d["studentplanfield"], "Field"), d["value"]))
        for r in rows:
            P["plans"].append({"r": r, "files": att.get(r["id"], []),
                               "fields": fields.get(r["id"], [])})

    # goals
    P["goals"] = []
    if has_table("t_goalheader"):
        for h in q("SELECT * FROM t_goalheader WHERE studentID=? ORDER BY updated_date DESC",
                   (sid,)):
            goals = q("SELECT * FROM t_goal WHERE goalheader=? ORDER BY year DESC, id",
                      (h["id"],)) if has_table("t_goal") else []
            gl = []
            for gr in goals:
                det = q("SELECT * FROM t_goaldetail WHERE goal=?", (gr["id"],)) \
                    if has_table("t_goaldetail") else []
                gl.append({"g": gr, "details": det})
            P["goals"].append({"h": h, "goals": gl})

    # bullying
    P["bullying"] = []
    if has_table("t_bullyingStudent") and has_table("t_bullying"):
        P["bullying"] = q("SELECT b.*, bs.role FROM t_bullyingStudent bs "
                          "JOIN t_bullying b ON b.id = bs.bullying WHERE bs.studentID=? "
                          "ORDER BY b.datetime DESC", (sid,))

    # direct messages (SEQTA cmessage)
    P["messages"] = []
    if has_table("t_cmessage") and has_table("t_cmessageparticipant"):
        rows = q("SELECT DISTINCT m.id, m.subject, m.sender_name, m.updated_date, m.year, "
                 "length(m.contents) AS clen, "
                 "(SELECT count(*) FROM t_cmessageparticipant x WHERE x.cmessage = m.id) AS nparts "
                 + (", m.contents " if for_report else "") +
                 "FROM t_cmessageparticipant p JOIN t_cmessage m ON m.id = p.cmessage "
                 "WHERE p.studentID=? ORDER BY m.updated_date DESC", (sid,))
        att = attachments_for("m", [r["id"] for r in rows])
        for r in rows:
            P["messages"].append({"r": r, "files": att.get(r["id"], []),
                                  "text": html_to_text(r["contents"]) if for_report else ""})

    # folio
    P["folio"] = q("SELECT * FROM t_folio WHERE studentID=? ORDER BY updated_date DESC",
                   (sid,)) if has_table("t_folio") else []

    # documents filed against the student (SIP folder + anything else keyed by code)
    P["documents"] = q("SELECT * FROM attachment WHERE student_code=? AND kind NOT IN "
                       "('pc','sp','m') ORDER BY name", (code,))

    # correspondence
    P["corr_count"] = q1("SELECT count(*) FROM student_corr WHERE student_id=?",
                         (int(sid),))[0] if has_table("student_corr") else 0
    P["corr_recent"] = corr_page(int(sid), 0, 300, max_groups=15) if P["corr_count"] else []
    P["corr_shown"] = sum(len(x["ids"]) for x in P["corr_recent"])

    P["att_total"] = (sum(len(p["files"]) for p in P["pastoral"]) +
                      sum(len(p["files"]) for p in P["plans"]) +
                      sum(len(m["files"]) for m in P["messages"]) + len(P["documents"]))
    return P


def corr_page(sid, offset, limit, text=None, ctype=None, group=True, max_groups=None):
    """Correspondence rows for a student, newest first.

    SEQTA logs one row per recipient, so a single email to 13 teachers is 13 rows.
    With group=True, consecutive rows with the same type, subject, sender and minute
    are folded into one entry listing all recipients.
    """
    where, args = ["sc.student_id = ?"], [sid]
    if ctype:
        where.append("c.type = ?")
        args.append(ctype)
    if text:
        where.append("c.subject LIKE ?")
        args.append(f"%{text}%")
    rows = q("SELECT c.id, c.type, c.subject, c.sent, c.body_len FROM student_corr sc "
             "JOIN correspondence c ON c.id = sc.corr_id WHERE " + " AND ".join(where) +
             " ORDER BY sc.sent DESC, c.id DESC LIMIT ? OFFSET ?", args + [limit, offset])
    parts = participants_for([r["id"] for r in rows])
    out = []
    for r in rows:
        pp = parts.get(r["id"], [])
        sender = next((person_label(p) for p in pp if p["role"] == "sender"), "")
        rec = [p for p in pp if p["role"] == "recipient"]
        key = (r["type"], r["subject"], (r["sent"] or "")[:16], sender)
        if group and out and out[-1]["key"] == key:
            out[-1]["rec"] += rec
            out[-1]["ids"].append(r["id"])
            continue
        if max_groups and len(out) >= max_groups:
            break
        out.append({"key": key, "r": r, "sender": sender, "rec": rec, "ids": [r["id"]]})
    for x in out:
        x["who"] = summarise_recipients(x["sender"], x["rec"])
    return out


def summarise_recipients(sender, rec):
    names, seen = [], set()
    for p in rec:
        lbl = person_label(p) or p["address"]
        if lbl and lbl not in seen:
            seen.add(lbl)
            names.append(lbl)
    shown = names[:4]
    more = len(names) - len(shown)
    to = ", ".join(shown) + (f" +{more} more" if more > 0 else "")
    failed = sum(1 for p in rec if p["success"] and not T(p["success"]))
    return {"from": sender, "to": to, "failed": failed, "n": len(names)}


def corr_count(sid, text=None, ctype=None):
    where, args = ["sc.student_id = ?"], [sid]
    if ctype:
        where.append("c.type = ?")
        args.append(ctype)
    if text:
        where.append("c.subject LIKE ?")
        args.append(f"%{text}%")
    return q1("SELECT count(*) FROM student_corr sc JOIN correspondence c ON c.id = sc.corr_id "
              "WHERE " + " AND ".join(where), args)[0]


_contact_cache = {}


def contact_name(cid):
    if cid in _contact_cache:
        return _contact_cache[cid]
    r = q1("SELECT salutation, firstname, prefname, surname FROM t_contact WHERE contactID=?",
           (cid,)) if has_table("t_contact") else None
    name = ""
    if r:
        name = (r["salutation"] or "").strip() or " ".join(
            x for x in ((r["prefname"] or r["firstname"]), r["surname"]) if x)
    if len(_contact_cache) > 20000:
        _contact_cache.clear()
    _contact_cache[cid] = name or f"contact {cid}"
    return _contact_cache[cid]


_student_cache = {}


def student_name_by_id(sid):
    if sid in _student_cache:
        return _student_cache[sid]
    r = q1("SELECT studentID, firstname, prefname, surname FROM t_student WHERE studentID=?",
           (str(sid),))
    name = student_display(r) if r else f"student {sid}"
    if len(_student_cache) > 20000:
        _student_cache.clear()
    _student_cache[sid] = name
    return name


def participants_for(ids):
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        if not chunk:
            continue
        ph = ",".join("?" * len(chunk))
        for p in q(f"SELECT * FROM corr_participant WHERE corr_id IN ({ph}) ORDER BY id", chunk):
            out.setdefault(p["corr_id"], []).append(p)
    return out


def person_label(p):
    t, pid = p["person_type"], p["person_id"]
    if t == "staff":
        return staff_name(sid=pid)
    if t == "contact":
        return contact_name(pid)
    if t == "student":
        return student_name_by_id(pid)
    return ""


_RE_HREF = re.compile(r"\s(?:href|xlink:href)\s*=", re.I)
_RE_ACTION = re.compile(r"\s(?:action|formaction)\s*=", re.I)


def body_html_response(content, plain=False):
    if plain or "<" not in (content or ""):
        content = ("<!doctype html><meta charset='utf-8'><pre style='white-space:pre-wrap;"
                   "font:14px/1.5 -apple-system,Segoe UI,sans-serif;margin:16px'>"
                   + htmlmod.escape(content or "") + "</pre>")
    content = _RE_HREF.sub(" data-href=", content)
    content = _RE_ACTION.sub(" data-action=", content)
    content += ("<style>img{max-width:100%}img[src^='http'],img[src^='//']{display:none}"
                "a{color:inherit;text-decoration:underline dotted;cursor:default}</style>")
    resp = Response(content, mimetype="text/html")
    # sandbox: no scripts, no forms, no remote loads (blocks tracking pixels / old http logos)
    resp.headers["Content-Security-Policy"] = (
        "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:")
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


# ---------------------------------------------------------------- routes
@bp.route("/healthz")
def healthz():
    return {"status": "ok", "version": APP_VERSION}


@bp.route("/readyz")
def readyz():
    try:
        q1("SELECT 1 FROM t_student LIMIT 1")
        return {"status": "ready"}
    except Exception as e:
        print(f"readyz failed: {e}", flush=True)
        return {"status": "error"}, 503


@bp.route("/")
def home():
    stats = {
        "students": q1("SELECT count(*) FROM t_student")[0],
        "enrolled": q1("SELECT count(*) FROM t_student WHERE status='FULL'")[0],
        "pastoral": q1("SELECT count(*) FROM t_pastoralcare")[0]
        if has_table("t_pastoralcare") else 0,
        "sickbay": q1("SELECT count(*) FROM t_sickbay")[0] if has_table("t_sickbay") else 0,
        "plans": q1("SELECT count(*) FROM t_studentplan")[0] if has_table("t_studentplan") else 0,
        "corr": int(LK.meta.get("correspondence_rows", 0) or 0),
        "files": int(LK.meta.get("attachments", 0) or 0),
    }
    rng = q1('SELECT min("from"), max("from") FROM t_pastoralcare') \
        if has_table("t_pastoralcare") else None
    return render_template("home.html", stats=stats, rng=rng)


@bp.route("/students")
def students():
    text = (request.args.get("q") or "").strip()
    status = request.args.get("status", "")
    year = request.args.get("year", "")
    where, args = [], []
    if text:
        for w in text.split()[:5]:
            like = f"%{w}%"
            where.append("(firstname LIKE ? OR prefname LIKE ? OR surname LIKE ? OR "
                         "middlename LIKE ? OR studentCode = ? OR username LIKE ? OR "
                         "government_id = ?)")
            args += [like, like, like, like, w, like, w]
    if status in ("FULL", "LEFT"):
        where.append("status = ?")
        args.append(status)
    if year:
        where.append("schoolyear = ?")
        args.append(year)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    total = q1("SELECT count(*) FROM t_student" + wsql, args)[0]
    pg = Page(total, arg_int("page"))
    rows = q("SELECT * FROM t_student" + wsql +
             " ORDER BY surname COLLATE NOCASE, firstname COLLATE NOCASE LIMIT ? OFFSET ?",
             args + [pg.per, pg.offset])
    audit("list_students", text, "", f"status={status} year={year} page={pg.page}")
    if total == 1 and text and pg.page == 1:
        return redirect(url_for("a.student", sid=rows[0]["studentID"]))
    out = [{"r": r, "name": student_display(r), "year": LK.year.get(r["schoolyear"], ""),
            "roll": LK.roll.get(r["rollgroup"], ""), "house": LK.house.get(r["house"], "")}
           for r in rows]
    return render_template("students.html", rows=out, pg=pg, text=text, status=status,
                           year=year, years=LK.years_sorted)


@bp.route("/student/<int:sid>")
def student(sid):
    s = get_student(sid)
    P = load_profile(s)
    audit("view_student", f"/student/{sid}", sid, P["name"])
    return render_template("student.html", P=P, LK=LK)


def build_report(sid, with_corr):
    s = get_student(sid)
    P = load_profile(s, for_report=True)
    corr = []
    if with_corr in ("list", "full") and P["corr_count"]:
        corr = corr_page(sid, 0, 100000)
        if with_corr == "full":
            for c in corr:
                row = q1("SELECT body FROM correspondence WHERE id=?", (c["r"]["id"],))
                c["text"] = html_to_text(zlib.decompress(row["body"]).decode("utf-8", "replace")) \
                    if row and row["body"] else ""
    return P, corr


def corr_arg():
    v = request.args.get("corr", "list")
    return v if v in ("none", "list", "full") else "list"


_REPORT_CSS = None


def render_report(P, corr, with_corr, pdf=False):
    global _REPORT_CSS
    if pdf and _REPORT_CSS is None:
        with open(os.path.join(os.path.dirname(__file__), "static", "report.css")) as f:
            _REPORT_CSS = f.read()
    return render_template("report.html", P=P, corr=corr, with_corr=with_corr, pdf=pdf,
                           inline_css=_REPORT_CSS if pdf else "",
                           generated=day_fmt(now_local(), time_=True))


def report_pdf(html):
    """HTML -> PDF bytes, or None if no PDF engine is available."""
    try:
        return pdf.render(html)
    except Exception as e:
        print(f"PDF rendering failed: {e}", flush=True)
        return None


def safe_name(P, sid):
    return re.sub(r"[^A-Za-z0-9 _-]", "_", P["name"]).strip() or str(sid)


def collect_attachments(P):
    files = []
    for p in P["pastoral"]:
        files += [(f"pastoral/{(p['from'] or '')[:10]}_{a['name']}", a) for a in p["files"]]
    for p in P["plans"]:
        files += [(f"plans/{a['name']}", a) for a in p["files"]]
    for m in P["messages"]:
        files += [(f"messages/{(m['r']['updated_date'] or '')[:10]}_{a['name']}", a)
                  for a in m["files"]]
    files += [(f"documents/{a['name']}", a) for a in P["documents"]]
    return files


def write_attachments(z, files, prefix=""):
    seen, missing = set(), []
    for arc, a in files:
        arc = prefix + arc
        path = safe_file_path(a["relpath"])
        if not path:
            missing.append(a["relpath"])
            continue
        base, n = arc, 2
        while arc in seen:
            root, ext = os.path.splitext(base)
            arc, n = f"{root} ({n}){ext}", n + 1
        seen.add(arc)
        comp = zipfile.ZIP_STORED if a["ext"] in (".jpg", ".jpeg", ".png", ".mp4", ".mov",
                                                  ".zip", ".docx", ".xlsx", ".pptx",
                                                  ".m4a", ".mp3") else zipfile.ZIP_DEFLATED
        z.write(path, arc, compress_type=comp)
    if missing:
        z.writestr(prefix + "MISSING_FILES.txt",
                   "Listed in the SEQTA export but not present on the server:\n" + "\n".join(missing))
    return len(files) - len(missing)


@bp.route("/student/<int:sid>/report")
def report(sid):
    with_corr = corr_arg()
    P, corr = build_report(sid, with_corr)
    audit("report", f"/student/{sid}/report", sid, f"{P['name']} corr={with_corr}")
    return render_report(P, corr, with_corr)


@bp.route("/student/<int:sid>/report.pdf")
def report_pdf_download(sid):
    with_corr = corr_arg()
    P, corr = build_report(sid, with_corr)
    pdf = report_pdf(render_report(P, corr, with_corr, pdf=True))
    if pdf is None:
        abort(503, "PDF generation isn't available on this server. Use Report → Print → Save as PDF.")
    audit("report_pdf", f"/student/{sid}/report.pdf", sid, f"{P['name']} corr={with_corr}")
    return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True,
                     download_name=f"{safe_name(P, sid)} - SEQTA record.pdf")


@bp.route("/student/<int:sid>/bundle.zip")
def bundle_zip(sid):
    """Everything for one student: the record as PDF plus every attachment, in one ZIP."""
    with_corr = corr_arg()
    P, corr = build_report(sid, with_corr)
    name = safe_name(P, sid)
    html = render_report(P, corr, with_corr, pdf=True)
    pdf = report_pdf(html)
    files = collect_attachments(P)
    tmp = tempfile.TemporaryFile()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        if pdf is not None:
            z.writestr(f"{name} - SEQTA record.pdf", pdf, compress_type=zipfile.ZIP_STORED)
        else:  # fallback: still useful, but Windows may block HTML inside a downloaded ZIP
            z.writestr(f"{name} - SEQTA record.html", html)
        n = write_attachments(z, files, prefix="attachments/") if files else 0
        z.writestr("README.txt",
                   f"SEQTA record for {P['name']} ({P['s']['studentCode']})\n"
                   f"Exported {day_fmt(now_local(), time_=True)} by {g.user} "
                   f"from the {SCHOOL_NAME} SEQTA Archive.\n"
                   f"Correspondence in report: {with_corr}. Attachments: {n}.\n"
                   "CONFIDENTIAL - contains student wellbeing, medical and family information.\n")
    tmp.seek(0)
    audit("download_bundle", f"/student/{sid}/bundle.zip", sid,
          f"{P['name']} corr={with_corr} pdf={'yes' if pdf else 'no'} files={n}")
    return send_file(tmp, mimetype="application/zip", as_attachment=True,
                     download_name=f"{name} - SEQTA record.zip")


@bp.route("/student/<int:sid>/attachments.zip")
def attachments_zip(sid):
    s = get_student(sid)
    P = load_profile(s)
    files = collect_attachments(P)
    if not files:
        abort(404)
    tmp = tempfile.TemporaryFile()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        n = write_attachments(z, files)
    tmp.seek(0)
    audit("download_zip", f"/student/{sid}/attachments.zip", sid, f"{n} files")
    return send_file(tmp, mimetype="application/zip", as_attachment=True,
                     download_name=f"{safe_name(P, sid)} - SEQTA attachments.zip")


@bp.route("/student/<int:sid>/correspondence")
def student_corr(sid):
    s = get_student(sid)
    text = (request.args.get("q") or "").strip()
    ctype = request.args.get("type") or None
    total = corr_count(sid, text or None, ctype)
    pg = Page(total, arg_int("page"), 400)
    rows = corr_page(sid, pg.offset, pg.per, text or None, ctype)
    types = [r[0] for r in q("SELECT DISTINCT c.type FROM student_corr sc JOIN correspondence c "
                             "ON c.id = sc.corr_id WHERE sc.student_id=?", (sid,))]
    audit("view_student_correspondence", f"/student/{sid}/correspondence", sid,
          f"page={pg.page} q={text}")
    return render_template("correspondence_list.html", s=s, name=student_display(s), rows=rows,
                           pg=pg, text=text, ctype=ctype or "", types=sorted(types))


@bp.route("/correspondence/<int:cid>")
def correspondence(cid):
    c = q1("SELECT id, type, subject, sent, cmessage, body_len, year FROM correspondence "
           "WHERE id=?", (cid,))
    if not c:
        abort(404)
    parts = participants_for([cid]).get(cid, [])
    people = [{"role": p["role"], "type": p["person_type"], "name": person_label(p),
               "address": p["address"] if p["address"] != p["person_id"] else "",
               "ok": T(p["success"]) if p["success"] else None} for p in parts]
    studs = q("SELECT sc.student_id FROM student_corr sc WHERE sc.corr_id=?", (cid,))
    students_ = [(r[0], student_name_by_id(r[0])) for r in studs]
    siblings = []
    if c["sent"] and len(students_) == 1:
        for r in q("SELECT c.id FROM student_corr sc JOIN correspondence c ON c.id = sc.corr_id "
                   "WHERE sc.student_id=? AND sc.sent >= ? AND sc.sent < ? AND c.type=? "
                   "AND c.subject IS ? AND c.id <> ? ORDER BY c.id LIMIT 200",
                   (students_[0][0], c["sent"][:16], c["sent"][:16] + "\uffff", c["type"],
                    c["subject"], cid)):
            pp = participants_for([r[0]]).get(r[0], [])
            to = [person_label(p) or p["address"] for p in pp if p["role"] == "recipient"]
            siblings.append((r[0], ", ".join(t for t in to if t)))
    audit("view_correspondence", f"/correspondence/{cid}",
          students_[0][0] if len(students_) == 1 else "", c["subject"])
    return render_template("correspondence.html", c=c, people=people, students=students_,
                           siblings=siblings)


@bp.route("/correspondence/<int:cid>/body")
def correspondence_body(cid):
    r = q1("SELECT type, body FROM correspondence WHERE id=?", (cid,))
    if not r:
        abort(404)
    content = zlib.decompress(r["body"]).decode("utf-8", "replace") if r["body"] else ""
    st = q("SELECT student_id FROM student_corr WHERE corr_id=? LIMIT 2", (cid,))
    audit("view_correspondence_body", f"/correspondence/{cid}/body",
          st[0][0] if len(st) == 1 else "")
    return body_html_response(content, plain=(r["type"] == "sms"))


@bp.route("/message/<mid>/body")
def message_body(mid):
    if not has_table("t_cmessage"):
        abort(404)
    r = q1("SELECT contents FROM t_cmessage WHERE id=?", (mid,))
    if not r:
        abort(404)
    st = q("SELECT DISTINCT studentID FROM t_cmessageparticipant WHERE cmessage=? "
           "AND studentID <> '' LIMIT 2", (mid,)) if has_table("t_cmessageparticipant") else []
    audit("view_message", f"/message/{mid}", st[0][0] if len(st) == 1 else "")
    return body_html_response(r["contents"])


def qi(name):
    return '"' + str(name).replace('"', '""') + '"'


def safe_file_path(relpath):
    root = os.path.realpath(FILES_ROOT)
    p = os.path.realpath(os.path.join(root, relpath))
    if not p.startswith(root + os.sep) or not os.path.isfile(p):
        return None
    return p


@bp.route("/file/<int:fid>")
def file(fid):
    a = q1("SELECT * FROM attachment WHERE id=?", (fid,))
    if not a:
        abort(404)
    path = safe_file_path(a["relpath"])
    if not path:
        abort(404, "File is listed in the export index but is not on the server.")
    inline = a["ext"] in VIEWABLE and request.args.get("dl") != "1"
    stu = ""
    if a["student_code"]:
        r = q1("SELECT studentID FROM t_student WHERE studentCode=?", (a["student_code"],))
        stu = r[0] if r else ""
    audit("download_file" if not inline else "view_file", f"/file/{fid}", stu, a["name"])
    resp = send_file(path, as_attachment=not inline, download_name=a["name"],
                     conditional=True)
    if a["ext"] == ".pdf" and inline:
        # Chromium's PDF viewer won't run in a sandboxed document
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    else:
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; " \
                                                  "media-src 'self'; style-src 'unsafe-inline'"
    return resp


@bp.route("/search")
def search():
    text = (request.args.get("q") or "").strip()
    scope = request.args.get("scope", "records")
    kind = request.args.get("kind", "")
    results, corr, error = [], [], None
    fq = fts_query(text) if text else None
    if text and not fq:
        error = "Enter at least one word to search for."
    if fq and scope == "records":
        where, args = ["records_fts MATCH ?"], [fq]
        if kind:
            where.append("kind = ?")
            args.append(kind)
        try:
            rows = q("SELECT kind, ref, student_id, sent, "
                     "snippet(records_fts, 0, char(2), char(3), ' … ', 28) AS snip "
                     "FROM records_fts WHERE " + " AND ".join(where) +
                     " ORDER BY rank LIMIT 300", args)
        except sqlite3.OperationalError as e:
            rows, error = [], f"Search error: {e}"
        for r in rows:
            results.append({"kind": r["kind"], "ref": r["ref"], "sid": r["student_id"],
                            "name": student_name_by_id(r["student_id"]) if r["student_id"] else "",
                            "sent": r["sent"], "snip": mark_snippet(r["snip"])})
    elif fq and scope == "corr":
        if not has_table("corr_fts"):
            error = "Correspondence body search wasn't built into this database " \
                    "(built with --no-corr-fts). Subjects can still be filtered per student."
        else:
            try:
                ids = [r[0] for r in q("SELECT rowid FROM corr_fts WHERE corr_fts MATCH ? "
                                       "ORDER BY rank LIMIT 300", (fq,))]
            except sqlite3.OperationalError as e:
                ids, error = [], f"Search error: {e}"
            if ids:
                ph = ",".join("?" * len(ids))
                crow = {r["id"]: r for r in q(
                    f"SELECT id, type, subject, sent FROM correspondence WHERE id IN ({ph})", ids)}
                links = {}
                for r in q(f"SELECT corr_id, student_id FROM student_corr WHERE corr_id IN ({ph})",
                           ids):
                    links.setdefault(r["corr_id"], []).append(r["student_id"])
                for i in ids:
                    if i in crow:
                        st = links.get(i, [])
                        corr.append({"c": crow[i], "students": [
                            (x, student_name_by_id(x)) for x in st[:3]], "more": max(0, len(st) - 3)})
    if text:
        audit("search", text, "", f"scope={scope} kind={kind}")
    return render_template("search.html", text=text, scope=scope, kind=kind, results=results,
                           corr=corr, error=error, has_corr_fts=has_table("corr_fts"))


@bp.route("/tables")
def tables():
    rows = q("SELECT * FROM source_tables ORDER BY name")
    return render_template("tables.html", rows=rows)


@bp.route("/table/<name>")
def table(name):
    src = q1("SELECT * FROM source_tables WHERE name=?", (name,))
    if not src:
        abort(404)
    cols = [r[1] for r in db().execute(f"PRAGMA table_info({qi(name)})")]
    show = [c for c in cols if c != "body"]
    text = (request.args.get("q") or "").strip()
    col = request.args.get("col", "")
    where, args = "", []
    if text:
        if col in show:
            where, args = f" WHERE {qi(col)} LIKE ?", [f"%{text}%"]
        else:
            where = " WHERE " + " OR ".join(f"{qi(c)} LIKE ?" for c in show)
            args = [f"%{text}%"] * len(show)
    total = q1(f"SELECT count(*) FROM {qi(name)}" + where, args)[0] if (text or src["rows"] is None) \
        else src["rows"]
    pg = Page(total, arg_int("page"), 100)
    sel = ", ".join(qi(c) for c in show)
    rows = q(f"SELECT {sel} FROM {qi(name)}" + where + " LIMIT ? OFFSET ?",
             args + [pg.per, pg.offset])
    audit("view_table", name, "", f"page={pg.page} q={text}")
    return render_template("table.html", name=name, src=src, cols=show, rows=rows, pg=pg,
                           text=text, col=col)


@bp.route("/record/<kind>/<ref>")
def record(kind, ref):
    """Jump from a search hit to the right place on the student's profile."""
    tbl = {"pastoral": "t_pastoralcare", "sickbay": "t_sickbay", "plan": "t_studentplan",
           "folio": "t_folio"}.get(kind)
    sid = request.args.get("sid", "")
    if not sid and tbl and has_table(tbl):
        r = q1(f"SELECT studentID FROM {tbl} WHERE id=?", (ref,))
        sid = r[0] if r else ""
    if not str(sid).isdigit():
        abort(404)
    anchor = {"pastoral": f"pc-{ref}", "sickbay": f"sb-{ref}", "plan": f"sp-{ref}",
              "note": f"note-{ref}", "message": f"msg-{ref}", "goal": "goals",
              "folio": "folio"}.get(kind, "")
    return redirect(url_for("a.student", sid=int(sid)) + (f"#{anchor}" if anchor else ""))


@bp.route("/audit")
@admin_required
def audit_view():
    con = audit_db()
    user = (request.args.get("user") or "").strip()
    action = request.args.get("action", "")
    stu = (request.args.get("student") or "").strip()
    where, args = [], []
    if user:
        where.append("user LIKE ?")
        args.append(f"%{user}%")
    if action:
        where.append("action = ?")
        args.append(action)
    if stu:
        where.append("student_id = ?")
        args.append(stu)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = con.execute("SELECT count(*) FROM audit" + w, args).fetchone()[0]
    pg = Page(total, arg_int("page"), 100)
    rows = con.execute("SELECT * FROM audit" + w + " ORDER BY id DESC LIMIT ? OFFSET ?",
                       args + [pg.per, pg.offset]).fetchall()
    actions = [r[0] for r in con.execute("SELECT DISTINCT action FROM audit ORDER BY 1")]
    audit("view_audit", "/audit", stu, f"user={user} action={action} page={pg.page}")
    out = []
    for r in rows:
        try:
            ts = day_fmt(datetime.fromisoformat(r["ts"]).astimezone(LOCAL_TZ), weekday=True,
                         time_=True, seconds=True)
        except ValueError:
            ts = r["ts"]
        out.append({"r": r, "ts": ts})
    return render_template("audit.html", rows=out, pg=pg, user_f=user, action=action,
                           actions=actions, stu=stu)


@bp.app_errorhandler(401)
@bp.app_errorhandler(403)
@bp.app_errorhandler(404)
@bp.app_errorhandler(500)
@bp.app_errorhandler(503)
def err(e):
    code = getattr(e, "code", 500)
    msg = {401: "You're not signed in.",
           403: "Your account doesn't have access to the SEQTA Archive. Ask ICT to add you.",
           404: getattr(e, "description", None) or "Not found.",
           500: "Something went wrong.",
           503: getattr(e, "description", None) or "Temporarily unavailable."}.get(code, "Error")
    if code == 404 and "The requested URL was not found" in msg:
        msg = "Not found."
    return render_template("error.html", code=code, msg=msg), code


# Account pages only exist with built-in accounts; in proxy mode the IdP owns all of that.
current_user = auth.register(bp, audit_db, audit) if config.AUTH_MODE == "local" else None


def create_app():
    app = Flask(__name__, static_folder=None)
    app.secret_key = config.secret_key()
    app.config.update(
        MAX_CONTENT_LENGTH=1024 * 1024,
        SESSION_COOKIE_NAME="seqta_archive",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PATH=(URL_PREFIX or "/"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=config.SESSION_HOURS),
    )
    app.register_blueprint(bp)
    if URL_PREFIX:
        @app.route("/")
        def _root():
            return redirect(URL_PREFIX + "/")
    open_state_db().close()
    return app
