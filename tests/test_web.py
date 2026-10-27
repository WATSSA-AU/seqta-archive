import io
import sqlite3
import zipfile

from conftest import ADMIN_PW, USER_PW, csrf_from, login


def test_signed_out_redirects(app):
    c = app.test_client()
    r = c.get("/student/1")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert c.get("/healthz").status_code == 200


def test_pages(admin_client, built):
    c = admin_client
    for url in ["/", "/students", "/students?q=Ava", "/student/1", "/student/4",
                "/student/1/correspondence", "/search?q=invented", "/search?q=notification&scope=corr",
                "/tables", "/table/t_pastoralcare", "/audit", "/admin/users", "/account"]:
        r = c.get(url)
        assert r.status_code == 200, (url, r.status_code)
    page = c.get("/student/1").get_data(as_text=True)
    assert "deliberately long invented pastoral note" in page
    assert "Ms Teacher" in page                       # staff names resolved
    assert "Enrolled" in page or "Left" in page
    assert "Follow-up required" in page               # t/f flags shown


def test_correspondence_body_is_sandboxed(admin_client, built):
    cid = sqlite3.connect(built["db"]).execute(
        "SELECT id FROM correspondence WHERE type='email' LIMIT 1").fetchone()[0]
    r = admin_client.get(f"/correspondence/{cid}/body")
    assert r.status_code == 200
    assert "sandbox" in r.headers["Content-Security-Policy"]
    body = r.get_data(as_text=True)
    assert " href=" not in body.lower()


def test_bundle(admin_client):
    r = admin_client.get("/student/1/bundle.zip")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = z.namelist()
    assert any(n.endswith(" - SEQTA record.pdf") or n.endswith(" - SEQTA record.html")
               for n in names)
    assert any(n.startswith("attachments/pastoral/") for n in names)
    assert "README.txt" in names


def test_file_download_and_audit(admin_client, built):
    fid = sqlite3.connect(built["db"]).execute(
        "SELECT id FROM attachment WHERE kind='pc' LIMIT 1").fetchone()[0]
    assert admin_client.get(f"/file/{fid}").status_code == 200
    audit = admin_client.get("/audit?action=view_file").get_data(as_text=True)
    assert "view_file" in audit


def test_user_cannot_see_admin_pages(user_client):
    assert user_client.get("/student/2").status_code == 200
    assert user_client.get("/audit").status_code == 403
    assert user_client.get("/admin/users").status_code == 403


def test_bad_password_and_lockout(app):
    c = app.test_client()
    for _ in range(5):
        assert login(c, "deputy", "wrong password here").status_code == 401
    # now locked, even with the right password - and the message doesn't say so
    r = login(c, "deputy", USER_PW)
    assert r.status_code == 401
    assert login(c, "nobody-here", "whatever pw").get_data(as_text=True).count("Sign-in failed") == 1
    # admin unlocks via CLI-equivalent
    from seqta_archive import web
    con = web.open_state_db()
    con.execute("UPDATE users SET failed=0, locked_until=NULL WHERE username='deputy'")
    con.commit()
    assert login(c, "deputy", USER_PW).status_code == 302


def test_logout_needs_csrf(admin_client):
    assert admin_client.post("/logout").status_code == 400


def test_admin_creates_user_who_must_change_password(admin_client, app):
    page = admin_client.get("/admin/users").get_data(as_text=True)
    r = admin_client.post("/admin/users", data={"csrf": csrf_from(page), "action": "create",
                                                 "username": "yearco", "display": "Year Co",
                                                 "role": "user"})
    html = r.get_data(as_text=True)
    import re
    temp = re.search(r'<code class="key">([^<]+)</code>', html).group(1)
    c = app.test_client()
    assert login(c, "yearco", temp).status_code == 302
    r = c.get("/student/1")
    assert r.status_code == 302 and "/account/password" in r.headers["Location"]
    page = c.get("/account/password").get_data(as_text=True)
    r = c.post("/account/password", data={"csrf": csrf_from(page), "current": temp,
                                          "new": "my own long passphrase", "again":
                                          "my own long passphrase"})
    assert r.status_code == 302
    assert c.get("/student/1").status_code == 200


def test_totp_enrol_and_login(app):
    from seqta_archive import auth
    c = app.test_client()
    assert login(c, "admin", ADMIN_PW).status_code == 302
    page = c.get("/account/totp").get_data(as_text=True)
    import re
    import time
    secret = re.search(r'<code class="key">([A-Z2-7]+)</code>', page).group(1)
    code = auth._totp_at(secret, int(time.time() // 30))
    # changing the second factor needs the password
    assert c.post("/account/totp", data={"csrf": csrf_from(page), "code": code}).status_code == 400
    page = c.get("/account/totp").get_data(as_text=True)
    secret = re.search(r'<code class="key">([A-Z2-7]+)</code>', page).group(1)
    code = auth._totp_at(secret, int(time.time() // 30))
    r = c.post("/account/totp", data={"csrf": csrf_from(page), "code": code, "password": ADMIN_PW})
    assert r.status_code == 302
    c2 = app.test_client()
    assert login(c2, "admin", ADMIN_PW).status_code == 401            # code now required
    code2 = auth._totp_at(secret, int(time.time() // 30) + 1)          # next step (no replay)
    assert login(c2, "admin", ADMIN_PW, code2).status_code == 302
    # right password + wrong codes count toward lockout
    c3 = app.test_client()
    for _ in range(5):
        assert login(c3, "admin", ADMIN_PW, "000000").status_code == 401
    from seqta_archive import web as _w
    row = _w.open_state_db().execute("SELECT locked_until FROM users WHERE username='admin'").fetchone()
    assert row["locked_until"], "wrong TOTP codes must lock the account"
    # reset so other tests aren't affected
    from seqta_archive import web
    con = web.open_state_db()
    con.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL, failed=0, locked_until=NULL "
                "WHERE username='admin'")
    con.commit()


def test_logout_revokes_copied_cookie(app):
    a = app.test_client()
    assert login(a, "deputy", USER_PW).status_code == 302
    cookie = a.get_cookie("seqta_archive").value
    page = a.get("/account").get_data(as_text=True)
    a.post("/logout", data={"csrf": csrf_from(page)})
    b = app.test_client()
    b.set_cookie("seqta_archive", cookie)
    r = b.get("/students")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_audit_ip_not_forgeable(admin_client):
    admin_client.get("/student/3", headers={"X-Forwarded-For": "6.6.6.6"},
                     environ_base={"REMOTE_ADDR": "10.1.2.3"})
    from seqta_archive import web
    row = web.open_state_db().execute(
        "SELECT ip FROM audit WHERE action='view_student' AND student_id='3' ORDER BY id DESC"
    ).fetchone()
    assert row["ip"] == "10.1.2.3"


def test_password_change_revokes_other_sessions(app):
    a, b = app.test_client(), app.test_client()
    assert login(a, "deputy", USER_PW).status_code == 302
    assert login(b, "deputy", USER_PW).status_code == 302
    page = a.get("/account/password").get_data(as_text=True)
    new = "a completely new passphrase"
    a.post("/account/password", data={"csrf": csrf_from(page), "current": USER_PW, "new": new,
                                      "again": new})
    r = b.get("/student/1")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    page = a.get("/account/password").get_data(as_text=True)
    a.post("/account/password", data={"csrf": csrf_from(page), "current": new, "new": USER_PW + "!",
                                      "again": USER_PW + "!"})
    from seqta_archive import auth, web
    con = web.open_state_db()
    con.execute("UPDATE users SET pw_hash=? WHERE username='deputy'", (auth.hash_password(USER_PW),))
    con.commit()


def test_open_redirect_blocked(app):
    c = app.test_client()
    page = c.get("/login").get_data(as_text=True)
    r = c.post("/login", data={"csrf": csrf_from(page), "username": "admin",
                               "password": ADMIN_PW, "next": "https://evil.example/"})
    assert r.status_code == 302 and "evil" not in r.headers["Location"]
