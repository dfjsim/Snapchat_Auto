"""The snap editor's session record, and the context-34 claims it explains.

``userPreferences/pref.docobjects`` keeps the snap the editor is working on under the docprefitem key
``SnapEditor-SnapSessionContext``: a TSAF container whose ``GPBData`` value is a protobuf naming the
snap's cached files by CACHE_KEY, the claim key ``<UUID>~<position>`` and context they are claimed
under, and when it was saved and edited. Only the latest version is a live row; earlier ones are
carved from superseded -wal frames and kept only when a claim corroborates them.

Every input is synthetic.
"""
import os
import sqlite3

from scripts import cache_controller_report as cc
from scripts.data import snap_session, sqlite_open

CLAIM = "3c3c3c3c-1111-4222-8333-444455556666"
KEY_A = "a" * 32
KEY_B = "b" * 32
SAVED, EDITED = 1_749_143_516, 1_749_143_515_880


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _f(number, value):
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _session(keys=(KEY_A,), claim=CLAIM, context=34, saved=SAVED):
    media = b"".join(_f(4, _f(6, i) + _f(8, 2) + _f(10, k.encode())) for i, k in enumerate(keys, 1))
    inner = media + _f(8, 1) + _f(17, _f(7, EDITED))
    body = _f(1, saved) + _f(2, _f(2, inner) + _f(5, _f(1, claim.encode()) + _f(2, context)))
    return (b"\x0c\x00\x00\x00TSAF\x03\x00\x04\x00" + b",\x08SE" + snap_session.MARKER
            + (29).to_bytes(4, "little") + len(body).to_bytes(4, "little") + body
            + b"\x00\x00\x0a\x00\x00\x00SnapEditor\x00")


def test_the_record_is_read_field_by_field():
    rec = snap_session.records_in(_session(keys=(KEY_A, KEY_B)))[0]
    assert rec == {"saved_unix": SAVED, "edited_ms": EDITED, "claim_uuid": CLAIM.upper(),
                   "context": 34, "media": [{"cache_key": KEY_A, "position": 1},
                                            {"cache_key": KEY_B, "position": 2}]}


def test_a_record_missing_a_field_or_cut_short_is_nothing():
    assert snap_session.records_in(_session(claim="not-a-uuid")) == []
    whole = _session()
    assert snap_session.records_in(whole[:-30]) == []                 # body runs off the end
    assert snap_session.decode(_f(1, SAVED)) is None                  # no media, no claim


def _store(tmp_path):
    """A pref store whose live row is the current session and whose -wal still holds an older one."""
    folder = tmp_path / "app" / "Documents" / "user_scoped" / ("ab" * 32) / "userPreferences"
    folder.mkdir(parents=True)
    db = str(folder / "pref.docobjects")
    conn = sqlite3.connect(db)
    conn.execute("pragma journal_mode=wal")
    conn.execute("create table docprefitem (rowid integer primary key autoincrement, p blob, "
                 "key string, unique (key))")
    conn.commit()
    conn.close()
    checkpointed = open(db, "rb").read()
    conn = sqlite3.connect(db)
    conn.execute("insert into docprefitem (p, key) values (?, ?)",
                 (_session(keys=(KEY_B,), saved=SAVED - 600), snap_session.SESSION_KEY))
    conn.commit()
    conn.execute("insert into docprefitem (p, key) values (?, ?)",
                 (_session(keys=(KEY_A,), claim="9d9d9d9d-1111-4222-8333-444455556666",
                           saved=SAVED - 300), "Other"))
    conn.commit()
    conn.execute("update docprefitem set p = ? where key = ?", (_session(), snap_session.SESSION_KEY))
    conn.commit()
    wal = open(db + "-wal", "rb").read()
    conn.close()
    with open(db, "wb") as fh:
        fh.write(checkpointed)
    with open(db + "-wal", "wb") as fh:
        fh.write(wal)
    if os.path.exists(db + "-shm"):
        os.remove(db + "-shm")
    return str(tmp_path / "app")


def test_live_and_carved_versions_are_attached_to_the_files_they_name(tmp_path):
    app = _store(tmp_path)
    claims = {KEY_A: [(f"{CLAIM}~1", 34)], KEY_B: [(f"{CLAIM}~1", 34)]}
    found = snap_session.read(app, claims)
    assert [(r["saved_unix"], r["wal"]) for r in found[KEY_A]] == [(SAVED, sqlite_open.WAL_ONLY)]
    # the older session was overwritten in place: only a superseded frame still holds it, and a
    # claim on that file says the same, so it is kept — carved
    assert [(r["saved_unix"], r["wal"]) for r in found[KEY_B]] == \
        [(SAVED - 600, sqlite_open.CARVED)]


def test_an_uncorroborated_carved_record_is_left_out(tmp_path):
    app = _store(tmp_path)
    found = snap_session.read(app, {KEY_A: [(f"{CLAIM}~1", 34)]})       # nothing claims KEY_B
    assert KEY_B not in found


def test_context_34_editor_claims_get_their_own_category():
    assert cc.classify_external_key(f"{CLAIM}~1", 34) == ("Snap editor", None)
    assert cc.classify_external_key(f"{CLAIM}~1", 19) == ("Other", None)
    assert cc.classify_external_key(CLAIM, 34) == ("Other", None)
    assert cc._category_of([{"category": "Other"}, {"category": "Snap editor"}]) == "Snap editor"
    assert "SnapEditor-SnapSessionContext" in cc.MCT_BASIS[34]


def test_the_detail_shows_the_record(tmp_path):
    entry = {"session": [{"saved": "2025-06-05 17:11:56", "edited": "2025-06-05 17:11:55",
                          "claim_uuid": CLAIM.upper(), "position": 1, "context": 34,
                          "store": "/x/pref.docobjects", "wal": sqlite_open.CARVED}]}
    html = cc._session_html(entry, None, None)
    assert "SnapEditor-SnapSessionContext" in html and f"{CLAIM.upper()}~1" in html
    assert "carved" in html and "34 (Snap editor working copy)" in html
