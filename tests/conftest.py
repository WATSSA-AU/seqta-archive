"""
Test fixtures. Settings are read at import time, so the environment is set here before the
package is imported, and each test module gets a fresh fake export + database.
"""

import os
import re
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

_TMP = tempfile.mkdtemp(prefix="seqta-archive-test-")
os.environ.update({
    "SEQTA_CONFIG": os.path.join(_TMP, "none.env"),
    "SEQTA_DATA_DIR": os.path.join(_TMP, "data"),
    "SEQTA_STATE_DIR": os.path.join(_TMP, "state"),
    "AUTH_MODE": "local",
    "COOKIE_SECURE": "0",
    "SCHOOL_NAME": "Test School",
    "SCHOOL_SHORT": "TS",
    "MIN_PASSWORD_LENGTH": "12",
})

import make_fake_export  # noqa: E402

ADMIN_PW = "correct horse battery staple"
USER_PW = "another long passphrase"


@pytest.fixture(scope="session")
def built():
    """A fake export and a database built from it."""
    from seqta_archive import builder, config
    export = make_fake_export.main([os.path.join(_TMP, "export")])
    builder.main(["--export", export, "--out", config.DB_PATH])
    return {"export": export, "db": config.DB_PATH, "files": os.path.join(export, "files")}


@pytest.fixture(scope="session")
def app(built):
    os.environ["SEQTA_FILES"] = built["files"]
    from seqta_archive import config
    config.FILES_ROOT = built["files"]
    from seqta_archive import web
    web.FILES_ROOT = built["files"]
    a = web.create_app()
    a.config["TESTING"] = True
    from seqta_archive.auth import hash_password, now_iso
    con = web.open_state_db()
    for name, role, pw in (("admin", "admin", ADMIN_PW), ("deputy", "user", USER_PW)):
        con.execute("INSERT OR IGNORE INTO users(username, display, role, pw_hash, pw_changed, "
                    "must_change, created) VALUES (?,?,?,?,?,0,?)",
                    (name, name.title(), role, hash_password(pw), now_iso(), now_iso()))
    con.commit()
    return a


def csrf_from(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf token on page"
    return m.group(1)


def login(client, username, password, code=""):
    page = client.get("/login").get_data(as_text=True)
    return client.post("/login", data={"csrf": csrf_from(page), "username": username,
                                       "password": password, "code": code, "next": "/"})


@pytest.fixture
def admin_client(app):
    c = app.test_client()
    r = login(c, "admin", ADMIN_PW)
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    return c


@pytest.fixture
def user_client(app):
    c = app.test_client()
    assert login(c, "deputy", USER_PW).status_code == 302
    return c
