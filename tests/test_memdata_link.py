"""A cache claim is linked to the Memory that records the claim's UUID about itself.

Newer app versions store MemData identifiers in a Memory's own row (``ZGALLERYSNAP.ZMEMDATAIDS``,
and the entry's ``ZGALLERYENTRY.ZMEMDATAID``). A cache_controller claim whose EXTERNAL_KEY carries
one of them names that Memory by a recorded identifier — the same kind of reference as a ZMEDIAID —
so both reports link it, in both directions. An id several Memories share (an entry's, when the entry
holds several snaps) names none of them. The rule only applies when nothing stronger did.

Every input is synthetic.
"""
import plistlib
import sqlite3

from scripts import cache_controller_report as cc
from scripts import memories_media_report as mr

MD = "5a5a5a5a-1111-4222-8333-444455556666"
SNAP = "77777777-aaaa-4bbb-8ccc-dddddddddddd"
OTHER = "88888888-aaaa-4bbb-8ccc-dddddddddddd"
USER = "11111111-2222-3333-4444-555555555555"
CACHE_KEY = "0123456789abcdef0123456789abcdef"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4 + b"mp42isom" + b"\x55" * 4000


def _record_archive(u, ms=1_700_000_000_000):
    objects = ["$null", {"uuid": plistlib.UID(2), "creationTimeMs": ms, "entryType": 0,
                         "$class": plistlib.UID(3)}, u,
               {"$classname": mr.MEMDATA_RECORD, "$classes": [mr.MEMDATA_RECORD]}]
    return plistlib.dumps({"$version": 100000, "$archiver": "NSKeyedArchiver",
                           "$top": {"root": plistlib.UID(1)}, "$objects": objects},
                          fmt=plistlib.FMT_BINARY)


def _claim(ek, mct=19):
    return {"external_key": ek, "media_context_type": mct}


def test_a_claim_carrying_a_memory_s_own_memdata_id_links_it():
    ids = {MD.upper(): {(SNAP, "h", "ZGALLERYSNAP.ZMEMDATAIDS › snapMemDataId")}}
    memory, basis = cc._memdata_link([_claim(f"{MD}~1")], ids)
    assert memory == {"snap_id": SNAP, "user_hash": "h"}
    assert "ZGALLERYSNAP.ZMEMDATAIDS › snapMemDataId" in basis and "not a match by time" in basis


def test_an_id_several_memories_share_names_none_of_them():
    ids = {MD.upper(): {(SNAP, "h", "ZGALLERYENTRY.ZMEMDATAID"),
                        (OTHER, "h", "ZGALLERYENTRY.ZMEMDATAID")}}
    assert cc._memdata_link([_claim(f"{MD}~1")], ids) == (None, None)
    assert cc._memdata_link([_claim("snap-media-unrelated")], ids) == (None, None)


def _scdb(tmp_path, entry_snaps=1):
    folder = tmp_path / "app" / "Documents" / "gallery_data_object" / "1" / ("ab" * 32)
    folder.mkdir(parents=True)
    conn = sqlite3.connect(str(folder / "scdb-27.sqlite3"))
    conn.execute("create table ZGALLERYENTRY (Z_PK integer primary key, ZMEMDATAID blob)")
    conn.execute("create table ZGALLERYSNAP (Z_PK integer primary key, ZSNAPID varchar, "
                 "ZENTRY integer, ZMEMDATAIDS blob)")
    conn.execute("insert into ZGALLERYENTRY values (1, ?)", (_record_archive(MD),))
    conn.execute("insert into ZGALLERYSNAP values (1, ?, 1, null)", (SNAP,))
    if entry_snaps > 1:
        conn.execute("insert into ZGALLERYSNAP values (2, ?, 1, null)", (OTHER,))
    conn.commit()
    conn.close()
    return str(tmp_path / "app")


def test_the_index_reads_the_entry_column_and_keeps_its_sharing(tmp_path):
    single = cc.load_memory_index(_scdb(tmp_path / "a"))
    assert single["memdata_ids"] == {MD.upper(): {(SNAP, "ab" * 32, "ZGALLERYENTRY.ZMEMDATAID")}}
    shared = cc.load_memory_index(_scdb(tmp_path / "b", entry_snaps=2))
    assert {sid for sid, _h, _f in shared["memdata_ids"][MD.upper()]} == {SNAP, OTHER}
    assert cc._memdata_link([_claim(f"{MD}~1")], shared["memdata_ids"]) == (None, None)


def test_a_shape_rule_still_wins():
    """Rule 4 runs only when rules 1-3 found nothing: a claim naming the snap id keeps its basis."""
    assert mr.classify_snap_claim(f"{MD}~1")[0] is None      # the ~N shape names no Memory itself


def _app_with_claim(tmp_path, ek):
    app = tmp_path / "app"
    sc = app / "Documents" / f"com.snap.file_manager_3_SCContent_{USER}"
    sc.mkdir(parents=True)
    (sc / CACHE_KEY).write_bytes(MP4)
    ccdir = app / "Documents" / "global_scoped" / "cachecontroller"
    ccdir.mkdir(parents=True)
    conn = sqlite3.connect(str(ccdir / "cache_controller.db"))
    conn.execute("create table CACHE_FILE_CLAIM (USER_ID text, CACHE_KEY text, MEDIA_CONTEXT_TYPE "
                 "integer, EXTERNAL_KEY text, CREATION_TIMESTAMP_MILLIS integer, PRIMARY KEY "
                 "(USER_ID, CACHE_KEY, MEDIA_CONTEXT_TYPE, EXTERNAL_KEY))")
    conn.execute("insert into CACHE_FILE_CLAIM values (?, ?, 19, ?, 0)", (USER, CACHE_KEY, ek))
    conn.commit()
    conn.close()
    return str(app)


def _memory(sid, memdata):
    m = mr._bare_memory(sid, {"userHash": "aa" * 32})
    m["memdata"] = memdata
    return m


def test_the_memories_report_recovers_the_file_the_claim_names(tmp_path):
    app = _app_with_claim(tmp_path, f"{MD}~1")
    rec = {"slot": "snapMemDataId", "uuid": MD.upper(), "created_ms": None, "entry_type": 0,
           "field": "ZGALLERYSNAP.ZMEMDATAIDS", "created": ""}
    mems = {SNAP: _memory(SNAP, [rec])}
    mr.collect_media(mems, app, str(tmp_path / "out"))
    files = mems[SNAP]["media_files"]
    assert [(f["ext"], f["role"], f["cache_key"]) for f in files] == [("mp4", "full", CACHE_KEY)]
    assert "records about itself in ZGALLERYSNAP.ZMEMDATAIDS (snapMemDataId)" in files[0]["how"]


def test_the_memories_report_leaves_a_shared_id_alone(tmp_path):
    app = _app_with_claim(tmp_path, f"{MD}~1")
    rec = {"slot": "", "uuid": MD.upper(), "created_ms": None, "entry_type": 0,
           "field": "ZGALLERYENTRY.ZMEMDATAID", "created": ""}
    mems = {SNAP: _memory(SNAP, [rec]), OTHER: _memory(OTHER, [dict(rec)])}
    mr.collect_media(mems, app, str(tmp_path / "out"))
    assert mems[SNAP]["media_files"] == [] and mems[OTHER]["media_files"] == []
