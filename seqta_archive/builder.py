#!/usr/bin/env python3
"""
SEQTA Archive - database builder.

Reads a SEQTA offboarding export (the folder containing data/ and files/)
and produces a single read-only SQLite database for the SEQTA Archive web app.

Stdlib only (Python 3.9+). Nothing is truncated: every CSV row and every cell
is loaded in full. Correspondence bodies are zlib-compressed.

Usage (normally via the CLI):
    seqta-archive build --export /path/to/au_wa_yourschool [--out seqta.db] [--no-corr-fts]
"""

import argparse
import csv
import datetime as dt
import html
import os
import re
import sqlite3
import sys
import time
import zlib

try:
    from . import __version__ as BUILD_VERSION
except ImportError:  # run as a plain script
    BUILD_VERSION = "standalone"

try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)

# Tables merged or handled specially rather than loaded as raw t_<name>
CMSG_RE = re.compile(r"^cmessage_(\d{4})$")
CORR_RE = re.compile(r"^log_correspondence_(\d{4})$")
CORR_PART = "log_correspondence_participant"

# Columns worth indexing on raw tables when present
INDEX_COLS = {
    "id", "studentID", "studentCode", "staffID", "staffCode", "contactID",
    "contactCode", "cmessage", "goalheader", "goal", "bullying", "sickbay",
    "studentplan", "studentplandetail", "studentplantype",
}

BATCH = 5000
T0 = time.time()


def log(msg):
    el = time.time() - T0
    print(f"[{int(el // 60):3d}:{int(el % 60):02d}] {msg}", flush=True)


def qi(name):
    return '"' + name.replace('"', '""') + '"'


def table_name(stem):
    return "t_" + re.sub(r"[^A-Za-z0-9_]", "_", stem)


def open_csv(path):
    f = open(path, encoding="utf-8-sig", errors="replace", newline="")
    return f, csv.reader(f)


def clean_header(h):
    out, seen = [], {}
    for i, c in enumerate(h):
        c = (c or "").replace("﻿", "").strip() or f"col{i + 1}"
        if c in seen:
            seen[c] += 1
            c = f"{c}_{seen[c]}"
        else:
            seen[c] = 0
        out.append(c)
    return out


def fit(row, n):
    if len(row) == n:
        return row
    if len(row) < n:
        return row + [""] * (n - len(row))
    return row[:n]


# ---------------------------------------------------------------- HTML -> text
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
    s = html.unescape(s)
    s = _RE_WS.sub(" ", s)
    s = _RE_NL.sub("\n\n", s)
    return s.strip()


# ---------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE source_tables (
    name TEXT PRIMARY KEY, source TEXT, rows INTEGER, cols INTEGER, kind TEXT);
CREATE TABLE correspondence (
    id INTEGER PRIMARY KEY, type TEXT, subject TEXT, sent TEXT,
    cmessage TEXT, body BLOB, body_len INTEGER, year INTEGER);
CREATE TABLE corr_participant (
    id INTEGER, corr_id INTEGER, address TEXT, person_id TEXT,
    person_type TEXT, role TEXT, success TEXT, updated_date TEXT);
CREATE TABLE student_corr (student_id INTEGER, corr_id INTEGER, sent TEXT,
    PRIMARY KEY (student_id, corr_id)) WITHOUT ROWID;
CREATE TABLE attachment (
    id INTEGER PRIMARY KEY, kind TEXT, ref_id TEXT, student_code TEXT,
    name TEXT, relpath TEXT UNIQUE, size INTEGER, ext TEXT);
CREATE VIRTUAL TABLE records_fts USING fts5(
    body, kind UNINDEXED, ref UNINDEXED, student_id UNINDEXED, sent UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2');
"""


def create_raw_table(con, tname, cols, extra=()):
    allc = list(cols) + list(extra)
    con.execute(f"DROP TABLE IF EXISTS {qi(tname)}")
    con.execute(f"CREATE TABLE {qi(tname)} ({', '.join(qi(c) + ' TEXT' for c in allc)})")
    return allc


def load_raw(con, path, tname, extra_vals=None, extra_cols=(), create=True, cols_override=None):
    f, rd = open_csv(path)
    try:
        header = clean_header(next(rd, []))
        if not header:
            return 0, []
        cols = cols_override or header
        if create:
            create_raw_table(con, tname, cols, extra_cols)
        n = len(header)
        ph = ", ".join("?" * (len(cols) + len(extra_cols)))
        # map header positions onto cols_override if column sets differ
        if cols_override and cols_override != header:
            pos = [header.index(c) if c in header else None for c in cols_override]
        else:
            pos = None
        ins = f"INSERT INTO {qi(tname)} VALUES ({ph})"
        batch, rows = [], 0
        ev = list(extra_vals or [])
        for r in rd:
            if not r:
                continue
            r = fit(r, n)
            if pos is not None:
                r = [r[p] if p is not None else "" for p in pos]
            batch.append(r + ev)
            rows += 1
            if len(batch) >= BATCH:
                con.executemany(ins, batch)
                batch.clear()
        if batch:
            con.executemany(ins, batch)
        return rows, header
    finally:
        f.close()


def index_raw(con, tname, cols, rows):
    if rows < 50:
        return
    for c in cols:
        if c in INDEX_COLS:
            iname = f"ix_{tname}_{c}"
            con.execute(f"CREATE INDEX IF NOT EXISTS {qi(iname)} ON {qi(tname)} ({qi(c)})")


# ---------------------------------------------------------------- correspondence
def load_correspondence(con, path, year, fts):
    f, rd = open_csv(path)
    rows = 0
    try:
        header = clean_header(next(rd, []))
        ix = {c: i for i, c in enumerate(header)}
        need = ["id", "type", "contents", "subject", "updated_date", "cmessage"]
        for c in need:
            if c not in ix:
                raise SystemExit(f"{path}: missing column {c}")
        n = len(header)
        ins = "INSERT OR REPLACE INTO correspondence VALUES (?,?,?,?,?,?,?,?)"
        ins_fts = "INSERT INTO corr_fts(rowid, subject, body) VALUES (?,?,?)"
        batch, fbatch = [], []
        for r in rd:
            if not r:
                continue
            r = fit(r, n)
            try:
                cid = int(r[ix["id"]])
            except ValueError:
                continue
            contents = r[ix["contents"]]
            body = zlib.compress(contents.encode("utf-8"), 6) if contents else None
            subj = r[ix["subject"]]
            batch.append((cid, r[ix["type"]], subj, r[ix["updated_date"]],
                          r[ix["cmessage"]], body, len(contents), year))
            if fts:
                fbatch.append((cid, subj, html_to_text(contents)))
            rows += 1
            if len(batch) >= BATCH:
                con.executemany(ins, batch)
                batch.clear()
                if fbatch:
                    con.executemany(ins_fts, fbatch)
                    fbatch.clear()
                if rows % 100000 == 0:
                    log(f"    {os.path.basename(path)}: {rows:,} rows")
        if batch:
            con.executemany(ins, batch)
        if fbatch:
            con.executemany(ins_fts, fbatch)
    finally:
        f.close()
    return rows


def load_participants(con, path, known_ids):
    f, rd = open_csv(path)
    rows = 0
    try:
        header = clean_header(next(rd, []))
        ix = {c: i for i, c in enumerate(header)}
        n = len(header)
        order = ["id", "log_correspondence", "address", "person_id", "person_type",
                 "role", "success", "updated_date"]
        ins = "INSERT INTO corr_participant VALUES (?,?,?,?,?,?,?,?)"
        batch = []
        for r in rd:
            if not r:
                continue
            r = fit(r, n)
            v = [r[ix[c]] if c in ix else "" for c in order]
            try:
                v[0] = int(v[0])
                v[1] = int(v[1])
            except ValueError:
                continue
            if v[1] not in known_ids:
                continue
            batch.append(v)
            rows += 1
            if len(batch) >= BATCH:
                con.executemany(ins, batch)
                batch.clear()
                if rows % 500000 == 0:
                    log(f"    participants: {rows:,} rows")
        if batch:
            con.executemany(ins, batch)
    finally:
        f.close()
    return rows


# ---------------------------------------------------------------- attachments
RX_PC = re.compile(r"^pc-(\d+)_f-\d+_st-(\d+)_?(.*)$")
RX_SP = re.compile(r"^sp-(\d+)_f-\d+_st-(\d+)_?(.*)$")
RX_M = re.compile(r"^m-(\d+)_f-\d+_?(.*)$")
RX_ST = re.compile(r"(?:^|_)st[_-](\d+)(?:_f[_-]\d+)?_?(.*)$")
META_CSV = re.compile(r"_(FILES?_LIST|FILES?_REF|WITH_ATTACHMENTS)\.csv$", re.I)
# NB: the st-<n> number in SEQTA filenames is the studentCode, not studentID


def index_attachments(con, files_root):
    if not os.path.isdir(files_root):
        log(f"  no files/ folder at {files_root} - attachments skipped")
        return 0
    rows = []
    for dirpath, dirnames, filenames in os.walk(files_root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            # SEQTA's own index CSVs (*_FILE_LIST.csv, *_FILE_REF.csv, *_WITH_ATTACHMENTS.csv)
            if fn.lower().endswith(".csv") and META_CSV.search(fn):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, files_root).replace(os.sep, "/")
            top = rel.split("/", 1)[0]
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            ext = os.path.splitext(fn)[1].lower()
            kind, ref, stu, name = top, "", "", fn
            m = RX_PC.match(fn)
            if top == "pastoralcare" and m:
                kind, ref, stu, name = "pc", m.group(1), m.group(2), m.group(3) or fn
            elif top == "studentplan" and (m := RX_SP.match(fn)):
                kind, ref, stu, name = "sp", m.group(1), m.group(2), m.group(3) or fn
            elif top == "cmessage" and (m := RX_M.match(fn)):
                kind, ref, name = "m", m.group(1), m.group(2) or fn
            elif top == "sip" and (m := RX_ST.search(fn)):
                kind, stu = "sip", m.group(1)
                name = m.group(2) or fn
            elif (m := RX_ST.search(fn)):
                stu = m.group(1)
            rows.append((kind, ref, stu, name, rel, size, ext))
    con.executemany(
        "INSERT OR IGNORE INTO attachment(kind, ref_id, student_code, name, relpath, size, ext) "
        "VALUES (?,?,?,?,?,?,?)", rows)
    con.execute("CREATE INDEX ix_att_ref ON attachment(kind, ref_id)")
    con.execute("CREATE INDEX ix_att_student ON attachment(student_code)")
    return len(rows)


# ---------------------------------------------------------------- record FTS
def has_table(con, t):
    return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone() is not None


def cols_of(con, t):
    return [r[1] for r in con.execute(f"PRAGMA table_info({qi(t)})")]


def build_records_fts(con):
    def run(kind, sql):
        n = con.execute(
            "INSERT INTO records_fts(body, kind, ref, student_id, sent) " + sql).rowcount
        log(f"  records_fts {kind}: {n:,}")

    if has_table(con, "t_pastoralcare"):
        run("pastoral", "SELECT details, 'pastoral', id, studentID, \"from\" "
                        "FROM t_pastoralcare WHERE details <> ''")
    if has_table(con, "t_studentnote") and has_table(con, "t_student"):
        run("note", "SELECT n.note, 'note', n.id, s.studentID, n.updated_date "
                    "FROM t_studentnote n JOIN t_student s ON s.studentCode = n.studentCode "
                    "WHERE n.note <> ''")
    if has_table(con, "t_sickbay"):
        c = cols_of(con, "t_sickbay")
        parts = [x for x in ("notes", "treatment_summary", "ambulance_details",
                             "parentcontact_details", "accidentform_details",
                             "discharged_details") if x in c]
        expr = " || char(10) || ".join(f"coalesce({qi(p)},'')" for p in parts)
        run("sickbay", f"SELECT {expr}, 'sickbay', id, studentID, \"from\" FROM t_sickbay")
    if has_table(con, "t_studentplan"):
        run("plan", "SELECT coalesce(background,'') || char(10) || coalesce(panel,''), "
                    "'plan', id, studentID, updated_date FROM t_studentplan "
                    "WHERE background <> '' OR panel <> ''")
    if has_table(con, "t_cmessage") and has_table(con, "t_cmessageparticipant"):
        run("message", "SELECT DISTINCT m.subject || char(10) || m.contents, 'message', m.id, "
                       "p.studentID, m.updated_date FROM t_cmessage m "
                       "JOIN t_cmessageparticipant p ON p.cmessage = m.id "
                       "WHERE p.studentID <> ''")
    if has_table(con, "t_goal") and has_table(con, "t_goalheader"):
        run("goal", "SELECT coalesce(g.notes,'') || char(10) || coalesce(g.student_notes,''), "
                    "'goal', g.id, h.studentID, g.updated_date FROM t_goal g "
                    "JOIN t_goalheader h ON h.id = g.goalheader")
    if has_table(con, "t_folio"):
        run("folio", "SELECT title || char(10) || contents, 'folio', id, studentID, "
                     "updated_date FROM t_folio")


# ---------------------------------------------------------------- main
def find_export(path):
    """Accept the export folder itself (has data/) or a folder containing exactly one export."""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(os.path.join(path, "data")):
        return path
    if os.path.isdir(path):
        kids = [os.path.join(path, d) for d in sorted(os.listdir(path))
                if os.path.isdir(os.path.join(path, d, "data"))]
        if len(kids) == 1:
            return kids[0]
        if len(kids) > 1:
            raise SystemExit("Several exports found - point --export at one of: " +
                             ", ".join(os.path.basename(k) for k in kids))
    raise SystemExit(f"No SEQTA export found at {path} (expected a folder with data/ inside, "
                     "e.g. au_wa_yourschool)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the SEQTA Archive SQLite database.")
    ap.add_argument("--export", required=True,
                    help="SEQTA export folder (contains data/ and files/)")
    ap.add_argument("--out", required=True, help="output .db path")
    ap.add_argument("--no-corr-fts", action="store_true",
                    help="don't full-text index correspondence bodies (smaller DB)")
    ap.add_argument("--skip-corr", action="store_true",
                    help="skip correspondence entirely (quick test build)")
    ap.add_argument("--corr-years", default="",
                    help="comma list of correspondence years to load, e.g. 2015,2016 (test builds)")
    a = ap.parse_args(argv)
    a.export = find_export(a.export)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)

    data_dir = os.path.join(a.export, "data")
    files_dir = os.path.join(a.export, "files")

    tmp = a.out + ".building"
    for p in (tmp, tmp + "-journal", tmp + "-wal"):
        if os.path.exists(p):
            os.remove(p)
    con = sqlite3.connect(tmp, isolation_level=None)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA cache_size=-400000")
    con.execute("PRAGMA temp_store=FILE")
    con.execute("PRAGMA page_size=8192")
    try:
        con.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x)")
        con.execute("DROP TABLE _fts_probe")
    except sqlite3.OperationalError:
        raise SystemExit("This Python's SQLite has no FTS5 support - use a different python3 "
                         "(e.g. Homebrew python3 on macOS, or build on the Ubuntu VM).")
    con.executescript(SCHEMA)
    corr_fts = not (a.no_corr_fts or a.skip_corr)
    if corr_fts:
        con.execute("CREATE VIRTUAL TABLE corr_fts USING fts5(subject, body, content='', "
                    "tokenize = 'unicode61 remove_diacritics 2')")

    csvs = sorted(f for f in os.listdir(data_dir) if f.lower().endswith(".csv"))
    log(f"SEQTA Archive builder {BUILD_VERSION}: {len(csvs)} CSV files in {data_dir}")

    con.execute("BEGIN")
    # ---- raw tables
    cmsg_files, corr_files, part_file = [], [], None
    for fn in csvs:
        stem = fn[:-4]
        if CMSG_RE.match(stem):
            cmsg_files.append((int(CMSG_RE.match(stem).group(1)), fn))
            continue
        if CORR_RE.match(stem):
            corr_files.append((int(CORR_RE.match(stem).group(1)), fn))
            continue
        if stem == CORR_PART:
            part_file = fn
            continue
        t = table_name(stem)
        rows, cols = load_raw(con, os.path.join(data_dir, fn), t)
        index_raw(con, t, cols, rows)
        con.execute("INSERT INTO source_tables VALUES (?,?,?,?,?)",
                    (t, fn, rows, len(cols), "raw"))
        log(f"  {fn}: {rows:,} rows")

    # ---- cmessage_YYYY merged into t_cmessage (+year)
    if cmsg_files:
        allcols = []
        for _, fn in cmsg_files:
            f, rd = open_csv(os.path.join(data_dir, fn))
            for c in clean_header(next(rd, [])):
                if c not in allcols:
                    allcols.append(c)
            f.close()
        create_raw_table(con, "t_cmessage", allcols, ["year"])
        total = 0
        for year, fn in cmsg_files:
            rows, _ = load_raw(con, os.path.join(data_dir, fn), "t_cmessage",
                               extra_vals=[str(year)], extra_cols=["year"],
                               create=False, cols_override=allcols)
            total += rows
            log(f"  {fn}: {rows:,} rows")
        index_raw(con, "t_cmessage", allcols, total)
        con.execute("INSERT INTO source_tables VALUES (?,?,?,?,?)",
                    ("t_cmessage", "cmessage_*.csv", total, len(allcols) + 1, "merged"))
    con.execute("COMMIT")

    # ---- correspondence
    corr_total = part_total = 0
    if a.corr_years:
        want = {int(y) for y in a.corr_years.split(",") if y.strip()}
        corr_files = [(y, f) for y, f in corr_files if y in want]
    if not a.skip_corr and corr_files:
        for year, fn in sorted(corr_files):
            con.execute("BEGIN")
            n = load_correspondence(con, os.path.join(data_dir, fn), year, corr_fts)
            con.execute("COMMIT")
            corr_total += n
            log(f"  {fn}: {n:,} correspondence rows")
        if part_file:
            con.execute("BEGIN")
            known = {r[0] for r in con.execute("SELECT id FROM correspondence")}
            part_total = load_participants(con, os.path.join(data_dir, part_file), known)
            del known
            con.execute("COMMIT")
            log(f"  {part_file}: {part_total:,} rows")
            log("  indexing participants")
            con.execute("CREATE INDEX ix_cp_corr ON corr_participant(corr_id)")
            log("  linking correspondence to students")
            con.execute("BEGIN")
            con.execute(
                "INSERT OR IGNORE INTO student_corr(student_id, corr_id, sent) "
                "SELECT CAST(p.person_id AS INTEGER), p.corr_id, c.sent "
                "FROM corr_participant p JOIN correspondence c ON c.id = p.corr_id "
                "WHERE p.person_type = 'student' AND p.person_id <> ''")
            con.execute("COMMIT")
            con.execute("CREATE INDEX ix_sc_sent ON student_corr(student_id, sent)")
        con.execute("INSERT INTO source_tables VALUES (?,?,?,?,?)",
                    ("correspondence", "log_correspondence_*.csv", corr_total, 8, "derived"))
        con.execute("INSERT INTO source_tables VALUES (?,?,?,?,?)",
                    ("corr_participant", CORR_PART + ".csv", part_total, 8, "derived"))

    # ---- attachments + record search
    con.execute("BEGIN")
    att = index_attachments(con, files_dir)
    log(f"  attachments indexed: {att:,}")
    build_records_fts(con)
    con.execute("COMMIT")

    # ---- meta
    sc = con.execute("SELECT count(*) FROM student_corr").fetchone()[0]
    meta = {
        "build_version": BUILD_VERSION,
        "built_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_export": os.path.abspath(a.export),
        "correspondence_rows": str(corr_total),
        "correspondence_participants": str(part_total),
        "student_correspondence_links": str(sc),
        "corr_fts": "1" if corr_fts else "0",
        "attachments": str(att),
    }
    con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", meta.items())

    log("optimising FTS indexes")
    con.execute("INSERT INTO records_fts(records_fts) VALUES ('optimize')")
    if corr_fts:
        con.execute("INSERT INTO corr_fts(corr_fts) VALUES ('optimize')")
    log("ANALYZE")
    con.execute("ANALYZE")
    con.close()
    os.replace(tmp, a.out)
    size = os.path.getsize(a.out)
    log(f"done: {a.out} ({size / 1e9:.2f} GB)")


if __name__ == "__main__":
    main()
