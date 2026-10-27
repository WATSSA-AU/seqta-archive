"""
seqta-archive command line.

  seqta-archive build --export PATH [--out seqta.db] [--no-corr-fts]
  seqta-archive serve [--host 127.0.0.1] [--port 8080]
  seqta-archive user add USERNAME [--admin] [--display "Name"]
  seqta-archive user passwd USERNAME
  seqta-archive user list
  seqta-archive user disable|enable|unlock|reset-totp USERNAME
  seqta-archive check
"""

import argparse
import getpass
import os
import sqlite3
import sys

from . import __version__, config


def _state():
    from .web import open_state_db
    con = open_state_db()
    _fix_owner()
    return con


def _fix_owner():
    """Run as root on Linux, the state DB would end up root-owned and unwritable by the
    service. Give it back to whoever owns the state directory."""
    if os.name == "nt" or os.geteuid() != 0:
        return
    st = os.stat(config.STATE_DIR)
    for suffix in ("", "-wal", "-shm"):
        p = config.STATE_DB + suffix
        if os.path.exists(p):
            os.chown(p, st.st_uid, st.st_gid)


def cmd_build(a):
    from . import builder
    out = a.out or config.DB_PATH
    argv = ["--export", a.export, "--out", out]
    if a.no_corr_fts:
        argv.append("--no-corr-fts")
    if a.skip_corr:
        argv.append("--skip-corr")
    builder.main(argv)


def cmd_serve(a):
    from waitress import serve

    from .web import create_app
    app = create_app()
    host = a.host or config.HOST
    port = a.port or config.PORT
    print(f"SEQTA Archive {__version__} on http://{host}:{port}{config.URL_PREFIX or ''}/ "
          f"(auth={config.AUTH_MODE}, data={config.DB_PATH})", flush=True)
    if config.AUTH_MODE == "local" and not config.COOKIE_SECURE:
        print("WARNING: COOKIE_SECURE=0 - sign-in cookies will be sent over plain HTTP. "
              "Only do this on a trusted network or behind HTTPS.", flush=True)
    if config.AUTH_MODE == "proxy" and host not in ("127.0.0.1", "::1", "localhost"):
        print("NOTE: proxy mode trusts X-Forwarded-* headers only from TRUSTED_PROXIES "
              f"({', '.join(config.TRUSTED_PROXIES)}).", flush=True)
    serve(app, host=host, port=port, threads=config.THREADS, channel_timeout=300,
          max_request_header_size=65536, url_scheme="https" if config.COOKIE_SECURE else "http",
          ident="seqta-archive")


def _ask_password(username):
    from .auth import password_problem
    while True:
        pw = getpass.getpass(f"New password for {username}: ")
        problem = password_problem(pw, username)
        if problem:
            print(problem)
            continue
        if getpass.getpass("Again: ") != pw:
            print("They don't match.")
            continue
        return pw


def cmd_user(a):
    from .auth import hash_password, now_iso, random_password
    con = _state()
    if a.action == "list":
        rows = con.execute("SELECT username, display, role, disabled, totp_enabled, last_login "
                           "FROM users ORDER BY username COLLATE NOCASE").fetchall()
        if not rows:
            print("No users yet. Create one with: seqta-archive user add USERNAME --admin")
        for r in rows:
            flags = ",".join(x for x, on in (("disabled", r["disabled"]),
                                             ("2fa", r["totp_enabled"])) if on)
            print(f"{r['username']:24} {r['role']:6} {flags:14} last login {r['last_login'] or '-'}"
                  f"  {r['display']}")
        return
    if not a.username:
        sys.exit("username required")
    row = con.execute("SELECT * FROM users WHERE username=?", (a.username,)).fetchone()
    if a.action == "add":
        if row:
            sys.exit(f"{a.username} already exists")
        if a.random:
            pw, must = random_password(), 1
        elif a.password_stdin:
            from .auth import password_problem
            pw, must = sys.stdin.readline().rstrip("\r\n"), 0
            problem = password_problem(pw, a.username)
            if problem:
                sys.exit(problem)
        else:
            pw, must = _ask_password(a.username), 0
        con.execute("INSERT INTO users(username, display, role, pw_hash, pw_changed, must_change, "
                    "created) VALUES (?,?,?,?,?,?,?)",
                    (a.username, a.display or "", "admin" if a.admin else "user",
                     hash_password(pw), now_iso(), must, now_iso()))
        con.commit()
        _fix_owner()
        print(f"Created {a.username} ({'admin' if a.admin else 'user'}).")
        if a.random:
            print(f"Temporary password: {pw}   (they'll be asked to change it)")
        return
    if not row:
        sys.exit(f"No user {a.username}")
    if a.action == "passwd":
        pw = _ask_password(a.username)
        con.execute("UPDATE users SET pw_hash=?, pw_changed=?, must_change=0, failed=0, "
                    "locked_until=NULL WHERE id=?", (hash_password(pw), now_iso(), row["id"]))
    elif a.action in ("disable", "enable"):
        con.execute("UPDATE users SET disabled=? WHERE id=?", (int(a.action == "disable"),
                                                               row["id"]))
    elif a.action == "unlock":
        con.execute("UPDATE users SET failed=0, locked_until=NULL WHERE id=?", (row["id"],))
    elif a.action == "reset-totp":
        con.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL WHERE id=?", (row["id"],))
    con.commit()
    _fix_owner()
    print("Done.")


def cmd_check(_a):
    ok = True
    print(f"SEQTA Archive {__version__}")
    print(f"config file : {config.CONFIG_PATH} ({'loaded' if config.CONFIG_LOADED else 'not found'})")
    print(f"auth mode   : {config.AUTH_MODE}")
    print(f"database    : {config.DB_PATH}")
    try:
        c = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        meta = dict(c.execute("SELECT key, value FROM meta"))
        n = c.execute("SELECT count(*) FROM t_student").fetchone()[0]
        print(f"              built {meta.get('built_at')}, {n} students, "
              f"{meta.get('correspondence_rows')} correspondence rows")
    except Exception as e:
        ok = False
        print(f"              NOT READABLE: {e}")
    files = config.FILES_ROOT
    if not os.path.isdir(files):
        try:
            alt = os.path.join(meta.get("source_export", ""), "files")
            if meta.get("source_export") and os.path.isdir(alt):
                files = alt
        except NameError:
            pass
    print(f"attachments : {files} ({'ok' if os.path.isdir(files) else 'MISSING'})")
    print(f"state db    : {config.STATE_DB}")
    try:
        n = _state().execute("SELECT count(*) FROM users").fetchone()[0]
        print(f"              {n} local user(s)")
        if config.AUTH_MODE == "local" and n == 0:
            print("              create the first admin: seqta-archive user add USERNAME --admin")
    except Exception as e:
        ok = False
        print(f"              NOT WRITABLE: {e}")
    from . import pdf
    eng = pdf.engine()
    print(f"pdf engine  : {eng or 'NONE - bundles will contain an HTML report'}")
    sys.exit(0 if ok else 1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="seqta-archive",
                                description="Read-only viewer for a SEQTA offboarding export.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build the database from a SEQTA export folder")
    b.add_argument("--export", required=True,
                   help="the export folder (contains data/ and files/), or its parent")
    b.add_argument("--out", help=f"output database (default {config.DB_PATH})")
    b.add_argument("--no-corr-fts", action="store_true",
                   help="don't index email/SMS bodies for search (smaller database)")
    b.add_argument("--skip-corr", action="store_true", help=argparse.SUPPRESS)
    b.set_defaults(fn=cmd_build)

    s = sub.add_parser("serve", help="run the web app (waitress)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    u = sub.add_parser("user", help="manage local accounts")
    u.add_argument("action", choices=["add", "passwd", "list", "disable", "enable", "unlock",
                                      "reset-totp"])
    u.add_argument("username", nargs="?")
    u.add_argument("--admin", action="store_true")
    u.add_argument("--display")
    u.add_argument("--random", action="store_true",
                   help="generate a temporary password (must be changed at first sign-in)")
    u.add_argument("--password-stdin", action="store_true",
                   help="read the password from standard input (for scripts)")
    u.set_defaults(fn=cmd_user)

    c = sub.add_parser("check", help="check configuration, data and PDF support")
    c.set_defaults(fn=cmd_check)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
