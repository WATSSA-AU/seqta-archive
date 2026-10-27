"""Proxy mode and schema tolerance run in fresh interpreters (settings are read at import)."""

import os
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def run(script, env_extra, tmp_path):
    env = dict(os.environ)
    env.update({"SEQTA_CONFIG": str(tmp_path / "none.env"),
                "SEQTA_DATA_DIR": str(tmp_path / "data"),
                "SEQTA_STATE_DIR": str(tmp_path / "state"),
                "PYTHONPATH": os.pathsep.join([ROOT, HERE])})
    env.update(env_extra)
    p = subprocess.run([sys.executable, "-c", textwrap.dedent(script)], env=env,
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


BUILD = """
import make_fake_export, os
from seqta_archive import builder, config
exp = make_fake_export.main([os.environ["SEQTA_DATA_DIR"] + "/x"] + {extra})
builder.main(["--export", exp, "--out", config.DB_PATH])
os.environ["SEQTA_FILES"] = exp + "/files"
"""


def test_proxy_mode(tmp_path):
    out = run(BUILD.format(extra="[]") + """
from seqta_archive import web
app = web.create_app(); c = app.test_client()
H = {"X-Forwarded-User": "e1", "X-Forwarded-Groups": "seqta-archive-users"}
A = {"X-Forwarded-User": "e2", "X-Forwarded-Groups": "role:archive-client:seqta-archive-admin"}
assert c.get("/student/1").status_code == 401                       # no identity
assert c.get("/student/1", headers=H).status_code == 200
assert c.get("/audit", headers=H).status_code == 403
assert c.get("/audit", headers=A).status_code == 200
assert c.get("/audit", headers={"X-Forwarded-User": "x",
             "X-Forwarded-Groups": "role:other:seqta-archive-admin"}).status_code == 403
assert c.get("/login", headers=H).status_code == 404        # no local account pages in proxy mode
assert c.get("/account", headers=H).status_code == 404
assert c.get("/admin/users", headers=A).status_code == 404
assert c.get("/readyz").get_json() == {"status": "ready"}
# requests not from a trusted proxy are refused
assert c.get("/student/1", headers=H, environ_base={"REMOTE_ADDR": "10.9.9.9"}).status_code == 403
print("proxy ok")
""", {"AUTH_MODE": "proxy", "OIDC_CLIENT_ID": "archive-client", "URL_PREFIX": ""}, tmp_path)
    assert "proxy ok" in out


def test_url_prefix(tmp_path):
    out = run(BUILD.format(extra="[]") + """
from seqta_archive import web
app = web.create_app(); c = app.test_client()
r = c.get("/"); assert r.status_code == 302 and r.headers["Location"].endswith("/seqta/")
r = c.get("/seqta/student/1"); assert r.status_code == 302 and "/seqta/login" in r.headers["Location"]
assert c.get("/seqta/healthz").status_code == 200
print("prefix ok")
""", {"AUTH_MODE": "local", "URL_PREFIX": "/seqta", "COOKIE_SECURE": "0"}, tmp_path)
    assert "prefix ok" in out


def test_export_with_missing_columns_and_tables(tmp_path):
    out = run(BUILD.format(extra='["--drop-table", "sickbay", "--drop-column", '
                                 '"contact.phone_mobile", "--drop-column", "pastoralcare.tag"]') + """
from seqta_archive import web
from seqta_archive.auth import hash_password, now_iso
app = web.create_app(); app.config["TESTING"] = True
con = web.open_state_db()
con.execute("INSERT INTO users(username, role, pw_hash, pw_changed, must_change, created) "
            "VALUES ('a','admin',?,?,0,?)", (hash_password("long enough password"), now_iso(), now_iso()))
con.commit()
c = app.test_client()
import re
page = c.get("/login").get_data(as_text=True)
tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
assert c.post("/login", data={"csrf": tok, "username": "a", "password": "long enough password"}).status_code == 302
for url in ["/", "/student/4", "/student/4/report", "/student/4/bundle.zip", "/search?q=invented"]:
    r = c.get(url); assert r.status_code == 200, (url, r.status_code)
print("tolerant ok")
""", {"AUTH_MODE": "local", "URL_PREFIX": "", "COOKIE_SECURE": "0"}, tmp_path)
    assert "tolerant ok" in out
