"""The cache_controller report's Memory index reads scdb-27 the way every evidence database is read.

``load_memory_index`` used to open the evidence ``scdb-27.sqlite3`` in place with a bare
``sqlite3.connect`` — which on a WAL database creates a ``-shm`` beside the evidence file — and read
only the -wal-applied state, so a Memory row the log had since changed lost the cache files linked
through its old values. It now goes through ``sqlite_open``: staged copies, both readings.

Every input is synthetic.
"""
import hashlib
import os
import sqlite3

from scripts import cache_controller_report as cc

HASH = "ab" * 32
SNAP_LIVE = "11111111-1111-4111-8111-111111111111"
SNAP_GONE = "22222222-2222-4222-8222-222222222222"
OLD_URL = "https://cf-st.example.net/d/OLDTOKEN0123?bo=x"
NEW_URL = "https://cf-st.example.net/d/NEWTOKEN0123?bo=x"


def _app(tmp_path):
    folder = tmp_path / "app" / "Documents" / "gallery_data_object" / "1" / HASH
    folder.mkdir(parents=True)
    db = str(folder / "scdb-27.sqlite3")
    conn = sqlite3.connect(db)
    conn.execute("pragma journal_mode=wal")
    conn.execute("create table ZGALLERYSNAP (Z_PK integer primary key, ZSNAPID varchar, "
                 "ZMEDIAID varchar, ZMEDIADOWNLOADURL varchar)")
    conn.execute("insert into ZGALLERYSNAP values (1, ?, ?, ?)", (SNAP_LIVE, SNAP_LIVE, OLD_URL))
    conn.execute("insert into ZGALLERYSNAP values (2, ?, ?, null)", (SNAP_GONE, SNAP_GONE))
    conn.commit()
    conn.close()                                     # checkpointed: both rows as inserted
    checkpointed = open(db, "rb").read()
    conn = sqlite3.connect(db)
    conn.execute("update ZGALLERYSNAP set ZMEDIADOWNLOADURL = ? where Z_PK = 1", (NEW_URL,))
    conn.execute("delete from ZGALLERYSNAP where Z_PK = 2")
    conn.commit()
    wal = open(db + "-wal", "rb").read()
    conn.close()
    with open(db, "wb") as fh:
        fh.write(checkpointed)
    with open(db + "-wal", "wb") as fh:
        fh.write(wal)
    if os.path.exists(db + "-shm"):
        os.remove(db + "-shm")
    return str(tmp_path / "app"), db


def _key(url):
    return hashlib.sha256(cc._url_token(url).encode()).hexdigest()[:32]


def test_the_evidence_file_is_never_opened_in_place(tmp_path):
    app, db = _app(tmp_path)
    before = {name: os.path.getmtime(os.path.join(os.path.dirname(db), name))
              for name in os.listdir(os.path.dirname(db))}
    cc.load_memory_index(app)
    after = {name: os.path.getmtime(os.path.join(os.path.dirname(db), name))
             for name in os.listdir(os.path.dirname(db))}
    assert after == before                       # no -shm appeared, nothing was touched


def test_both_readings_link(tmp_path):
    app, _db = _app(tmp_path)
    index = cc.load_memory_index(app)
    # the deleted Memory is still a snap the cache files can name
    assert set(index["snap_ids"]) == {SNAP_LIVE, SNAP_GONE}
    # the current URL and the one the -wal replaced both address this Memory's cache files
    assert index["url_keys"][_key(NEW_URL)][0] == SNAP_LIVE
    assert index["url_keys"][_key(OLD_URL)][0] == SNAP_LIVE
    # the current URL is listed first
    assert index["snap_urls"][SNAP_LIVE] == [NEW_URL, OLD_URL]
