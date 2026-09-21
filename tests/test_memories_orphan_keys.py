"""Key rows in ``gallery.encrypteddb`` that no Memory row accounts for.

Such a row is a Memory the app no longer lists, whose key — and, where the gallery has them,
coordinates and address — survive. It is reported as a RECOVERED row, unless a listed Memory holds
the same key pair: that is the same media object under another id (a duplicate, or a key a Memory
adopted), and one media object is one row. A row a Memory merely references under a *different*
key is listed, naming that Memory.

Every input is synthetic; the keys are arbitrary bytes and nothing is decrypted.
"""
from scripts import memories_media_report as mr
from scripts.data import sqlite_open

PROFILE = {"userHash": "ab" * 32}
KEY_1, IV_1 = b"\x01" * 32, b"\x11" * 16
KEY_2, IV_2 = b"\x02" * 32, b"\x22" * 16
WRAPPED_KEY, WRAPPED_IV = b"\x03" * 48, b"\x33" * 32


def _listed(snap_id, key=None, iv=None, refs=()):
    m = mr._bare_memory(snap_id, PROFILE)
    m.update({"key": key, "iv": iv, "media_refs": list(refs)})
    return m


def test_a_key_row_nothing_accounts_for_becomes_a_recovered_row():
    memories = {"LIVE-1": _listed("LIVE-1", KEY_1, IV_1)}
    orphans = {"GONE-1": (KEY_2, IV_2, 0, sqlite_open.MAIN_ONLY)}
    out = mr.orphan_key_memories(PROFILE, memories, orphans,
                                 {"GONE-1": ((45.0, -73.0), sqlite_open.BOTH)},
                                 {"GONE-1": "Some Town"}, persisted="")
    assert list(out) == ["GONE-1"]
    m = out["GONE-1"]
    assert m["key"] == KEY_2 and m["iv"] == IV_2 and not m["is_meo"] and not m["key_wrapped"]
    assert (m["latitude"], m["longitude"], m["address"]) == (45.0, -73.0, "Some Town")
    assert m["has_location"] is True
    assert m["wal"] == sqlite_open.MAIN_ONLY                    # the key row's own marker
    assert m["recovery"]["method"] == mr.METHOD_KEY_ROW and m["recovery"]["referenced_by"] == []
    assert m["user_hash"] == PROFILE["userHash"] and m["media_files"] == []


def test_the_same_key_as_a_listed_memory_is_the_same_media_object():
    memories = {"LIVE-1": _listed("LIVE-1", KEY_1, IV_1, refs=["DUP-1"])}
    orphans = {"DUP-1": (KEY_1, IV_1, 0, sqlite_open.BOTH),   # a duplicate's original, same key
               "LIVE-1": (KEY_1, IV_1, 0, sqlite_open.BOTH)}  # a listed id is never an orphan
    assert mr.orphan_key_memories(PROFILE, memories, orphans, {}, {}, "") == {}


def test_a_referenced_row_under_a_different_key_is_listed_and_names_the_referrer():
    memories = {"LIVE-1": _listed("LIVE-1", KEY_1, IV_1, refs=["ORIG-1"])}
    orphans = {"ORIG-1": (WRAPPED_KEY, WRAPPED_IV, 1, sqlite_open.BOTH)}
    out = mr.orphan_key_memories(PROFILE, memories, orphans, {}, {}, persisted="")
    m = out["ORIG-1"]
    assert m["is_meo"] and m["key_wrapped"] and m["key"] is None   # locked without a persistedkey
    assert m["recovery"]["referenced_by"] == ["LIVE-1"]
    assert m["latitude"] is None and m["has_location"] is False


def test_a_wrapped_key_is_unwrapped_when_the_persistedkey_is_at_hand(monkeypatch):
    monkeypatch.setattr(mr, "unwrap_meo_key", lambda persisted, key, iv: (KEY_2, IV_2))
    out = mr.orphan_key_memories(PROFILE, {}, {"MEO-1": (WRAPPED_KEY, WRAPPED_IV, 1,
                                                          sqlite_open.BOTH)}, {}, {}, "master")
    assert out["MEO-1"]["key"] == KEY_2 and out["MEO-1"]["is_meo"] and not out["MEO-1"]["key_wrapped"]
    # …and an unwrapped key equal to a listed Memory's is, again, the same media object
    listed = {"LIVE-2": _listed("LIVE-2", KEY_2, IV_2)}
    assert mr.orphan_key_memories(PROFILE, listed, {"MEO-1": (WRAPPED_KEY, WRAPPED_IV, 1,
                                                              sqlite_open.BOTH)}, {}, {}, "m") == {}


def test_the_index_row_badges_and_filters_a_recovered_row():
    m = mr._bare_memory("GONE-1", PROFILE)
    m["recovery"] = {"method": mr.METHOD_KEY_ROW, "source": "gallery.encrypteddb snap_key_iv",
                     "referenced_by": ["LIVE-1"]}
    summary = mr._wal_summary_html({"GONE-1": m})
    assert "recovered from a key row" in summary
    m2 = mr._bare_memory("IDX-1", PROFILE)
    m2["recovery"] = {"method": mr.METHOD_SEARCH_INDEX, "source": "gallery_search/search.sqlite3"}
    assert "search index alone" in mr._wal_summary_html({"IDX-1": m2})
    assert mr._wal_summary_html({"LIVE-1": mr._bare_memory("LIVE-1", PROFILE)}) == ""
