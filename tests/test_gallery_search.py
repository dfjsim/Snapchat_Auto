"""The app's search index over Memories (``gallery_search/…/search.sqlite3``).

A plain SQLite file, so a synthetic one is built here with the tables the reader joins: the FTS
content tables as ordinary tables (that is what they are on disk), the per-snap tag tables, and
one table left out to stand for the app versions that lack it. The join the reader relies on —
``snap_tag_table_content.docid = snap_id_table.rowid`` — is what the fixture encodes.

Every input is synthetic.
"""
import os
import sqlite3

from scripts import gallery_search
from scripts import memories_media_report as mr
from scripts.data import sqlite_open

SNAP_A = "AAAAAAAA-0000-4000-8000-000000000001"
SNAP_B = "BBBBBBBB-0000-4000-8000-000000000002"
SNAP_C = "CCCCCCCC-0000-4000-8000-000000000003"


def _index(path, with_synced=False):
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table snap_id_table (snap_id string, language_id string, tag_version integer);
        create table snap_tag_table_content (docid integer primary key, c0time_tag, c1location_tag,
                                             c2visual_tag, c3meta_tag);
        create table snap_description_table_content (docid integer primary key, c0caption);
        create table snap_time_tag_table (snap_id string, time_tag string);
        create table snap_location_tag_cluster_table (snap_id string, cluster_name string);
        create table snap_visual_tag_cluster_table (snap_id string, cluster_name string);
        create table snap_visual_tag_conf_table (snap_id string, concept string, conf real);
    """)
    if with_synced:
        conn.execute("create table snap_tag_synced (snap_id string)")
    # rowids 1 and 2; the FTS content rows are keyed by those rowids, not by snap id
    conn.execute("insert into snap_id_table (rowid, snap_id, language_id, tag_version) "
                 "values (1, ?, 'English', 2)", (SNAP_A,))
    conn.execute("insert into snap_id_table (rowid, snap_id, language_id, tag_version) "
                 "values (2, ?, 'French', 2)", (SNAP_B.lower(),))
    conn.execute("insert into snap_tag_table_content values (1, '2023,February,Tuesday,afternoon',"
                 " 'Some Town,Country,H0H 0H0,Main St,Some Town,ST', 'glasses,sand', 'Image')")
    conn.execute("insert into snap_tag_table_content values (2, '2023,May', '', '', 'Video')")
    conn.execute("insert into snap_description_table_content values (1, 'a caption')")
    conn.execute("insert into snap_time_tag_table values (?, '2023-02-21')", (SNAP_A,))
    conn.execute("insert into snap_time_tag_table values (?, '2023-05-12')", (SNAP_B,))
    conn.execute("insert into snap_location_tag_cluster_table values (?, 'Some Town')", (SNAP_A,))
    conn.execute("insert into snap_visual_tag_conf_table values (?, 'sand', 0.15)", (SNAP_A,))
    conn.execute("insert into snap_visual_tag_conf_table values (?, 'glasses', 0.5)", (SNAP_A,))
    conn.commit()
    conn.close()
    return path


def test_rows_are_joined_on_docid_and_snap_id(tmp_path):
    recs = gallery_search.load(_index(os.path.join(tmp_path, "search.sqlite3")))
    assert set(recs) == {SNAP_A, SNAP_B}                        # keys are upper-cased
    a = recs[SNAP_A]
    assert a["date"] == "2023-02-21"
    assert a["time_words"] == ["2023", "February", "Tuesday", "afternoon"]
    assert a["places"] == ["Some Town", "Country", "H0H 0H0", "Main St", "Some Town", "ST"]
    assert a["place_cluster"] == "Some Town"
    assert a["visual_tags"] == ["glasses", "sand"]
    assert a["concepts"] == [("glasses", 0.5), ("sand", 0.15)]  # by falling confidence
    assert a["kind"] == "Image" and a["caption"] == "a caption" and a["language"] == "English"
    assert a["wal"] == sqlite_open.BOTH
    b = recs[SNAP_B]
    assert b["snap_id"] == SNAP_B.lower() and b["kind"] == "Video" and b["places"] == []
    assert gallery_search.summary(a) == "2023-02-21 (2023, February, Tuesday, afternoon) · Some Town · Image"


def test_a_missing_or_extra_table_is_not_an_error(tmp_path):
    assert gallery_search.load(_index(os.path.join(tmp_path, "s.sqlite3"), with_synced=True))
    empty = os.path.join(tmp_path, "empty.sqlite3")
    sqlite3.connect(empty).close()
    assert gallery_search.load(empty) == {}
    assert gallery_search.load(os.path.join(tmp_path, "absent.sqlite3")) == {}
    assert gallery_search.load("") == {}


def test_a_snap_indexed_only_after_the_checkpoint_is_marked_wal_only(tmp_path):
    path = _index(os.path.join(tmp_path, "search.sqlite3"))
    # Closing the last connection checkpoints the -wal into the file, so the un-checkpointed state
    # is staged by hand: the main file as of the switch to WAL mode, plus the log written after it.
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=wal")
    conn.close()
    checkpointed = open(path, "rb").read()
    conn = sqlite3.connect(path)
    conn.execute("insert into snap_id_table (rowid, snap_id, language_id, tag_version) "
                 "values (3, ?, 'English', 2)", (SNAP_C,))
    conn.execute("insert into snap_time_tag_table values (?, '2024-01-01')", (SNAP_C,))
    conn.commit()
    wal = open(path + "-wal", "rb").read()
    conn.close()
    with open(path, "wb") as fh:
        fh.write(checkpointed)
    with open(path + "-wal", "wb") as fh:
        fh.write(wal)
    if os.path.exists(path + "-shm"):
        os.remove(path + "-shm")
    assert os.path.getsize(path + "-wal") > 32
    recs = gallery_search.load(path)
    assert recs[SNAP_C]["wal"] == sqlite_open.WAL_ONLY and recs[SNAP_C]["date"] == "2024-01-01"
    assert recs[SNAP_A]["wal"] == sqlite_open.BOTH


def _memory(snap_id, **extra):
    m = mr._bare_memory(snap_id, {"userHash": "aa" * 32})
    m.update(extra)
    return m


def test_the_search_record_gives_a_place_where_there_are_no_coordinates():
    rec = {"date": "2023-02-21", "time_words": ["winter"], "places": ["Main St", "Town"],
           "place_cluster": "Town", "visual_tags": [], "concepts": [], "visual_cluster": "",
           "kind": "Image", "caption": "", "language": "", "tag_version": 2,
           "wal": sqlite_open.BOTH}
    m = _memory("S-1", search=rec, has_location=True)
    assert mr._geo_state(m) == "indexed"
    assert "Town" in mr._geo_compact(m) and "search index" in mr._geo_compact(m)
    assert "Town" in mr._geo_html(m, keychain_available=False)
    assert ("Search index date", "2023-02-21 (winter)",
            "gallery_search › snap_time_tag_table — a local date, no zone") in mr._memory_times(m)
    # coordinates win: the search place is not a second location
    located = _memory("S-2", search=rec, latitude=1.0, longitude=2.0)
    assert mr._geo_state(located) == "yes" and "search index" not in mr._geo_compact(located)
    # no place in the index: the states this Memory had before
    assert mr._geo_state(_memory("S-3", has_location=True)) == "ondevice"
    assert mr._geo_state(_memory("S-4", search=dict(rec, places=[], place_cluster=""))) == "no"
