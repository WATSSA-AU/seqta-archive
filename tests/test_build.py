import sqlite3


def test_build_counts(built):
    c = sqlite3.connect(built["db"])
    assert c.execute("SELECT count(*) FROM t_student").fetchone()[0] == 24
    meta = dict(c.execute("SELECT key, value FROM meta"))
    assert int(meta["correspondence_rows"]) == 24 * 4
    # every correspondence row is linked to its student
    assert c.execute("SELECT count(*) FROM student_corr").fetchone()[0] == 24 * 4


def test_no_truncation(built):
    c = sqlite3.connect(built["db"])
    longest = c.execute("SELECT max(length(details)) FROM t_pastoralcare").fetchone()[0]
    assert longest > 1000


def test_attachments_linked(built):
    c = sqlite3.connect(built["db"])
    kinds = dict(c.execute("SELECT kind, count(*) FROM attachment GROUP BY kind"))
    assert kinds["pc"] == 24 and kinds["sp"] == 8 and kinds["m"] == 6 and kinds["sip"] == 1
    # every pc/sp/m attachment points at a real record
    assert c.execute("SELECT count(*) FROM attachment a JOIN t_pastoralcare p ON p.id = a.ref_id "
                     "WHERE a.kind='pc'").fetchone()[0] == 24


def test_fts(built):
    c = sqlite3.connect(built["db"])
    assert c.execute("SELECT count(*) FROM records_fts WHERE records_fts MATCH 'invented'"
                     ).fetchone()[0] > 0
    assert c.execute("SELECT count(*) FROM corr_fts WHERE corr_fts MATCH 'notification'"
                     ).fetchone()[0] == 72


def test_find_export_parent(built, tmp_path):
    import os

    from seqta_archive.builder import find_export
    parent = os.path.dirname(built["export"])
    assert find_export(parent) == built["export"]
