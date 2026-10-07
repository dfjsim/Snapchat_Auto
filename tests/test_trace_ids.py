"""``--trace-ids``: where an identifier occurs in an extraction — and only where.

The search exists to answer "did the device record this connection anywhere?" for data that cannot
leave the machine it is on, so two things matter as much as finding the hits: every encoding a value
is plausibly stored in is tried, and the output carries locations only. A sentinel stored next to
each identifier must never appear in the log or the JSON.

Every input is synthetic.
"""
import base64
import json
import logging
import os
import sqlite3
import uuid

import Snapchat_Auto as app
from scripts import trace_ids
from scripts.data import sqlite_open

ID_MAIN = "1111aaaa-2222-4333-8444-55555555cccc"      # only in the checkpointed file (later changed)
ID_WAL = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"       # only once the -wal is applied, as BE bytes
ID_GONE = "9999dddd-8888-4777-8666-5555555eeeee"      # only in a superseded -wal frame
ID_LE = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"        # little-endian UUID bytes in a plain file
CACHE_KEY = "00a1b2c3d4e5f60718293a4b5c6d7e8f"        # 32 hex digits, stored as raw bytes
STICKER_BYTES = b"\x0f\xbf\xffsynthetic1"              # an id kept as bytes in one place ...
STICKER_ID = base64.b64encode(STICKER_BYTES).decode()  # ... and as padded base64 in another
SECRET = "SENTINEL-CONTENT-MUST-NOT-LEAK"


def _run_folder(tmp_path):
    """A run folder whose ExtractedData holds one WAL database and one binary file."""
    docs = tmp_path / "run" / "ExtractedData" / "Application" / "APP" / "Documents"
    docs.mkdir(parents=True)
    db = str(docs / "store.sqlite")
    conn = sqlite3.connect(db)
    conn.execute("pragma journal_mode=wal")
    conn.execute("create table t (id integer primary key, name text, data blob, note text)")
    conn.execute("insert into t values (1, ?, null, ?)", (ID_MAIN, SECRET))
    conn.commit()
    conn.close()                                       # checkpoints: the file holds row 1 as above
    checkpointed = open(db, "rb").read()
    conn = sqlite3.connect(db)
    conn.execute("pragma wal_autocheckpoint=0")
    conn.execute("update t set name = 'renamed' where id = 1")
    conn.execute("insert into t values (2, 'x', ?, ?)", (uuid.UUID(ID_WAL).bytes, SECRET))
    conn.commit()
    conn.execute("insert into t values (3, ?, null, ?)", (ID_GONE.upper(), SECRET))
    conn.commit()
    conn.execute("update t set name = 'gone' where id = 3")     # a later frame for the same page
    conn.commit()
    wal = open(db + "-wal", "rb").read()
    conn.close()
    with open(db, "wb") as fh:
        fh.write(checkpointed)
    with open(db + "-wal", "wb") as fh:
        fh.write(wal)
    if os.path.exists(db + "-shm"):
        os.remove(db + "-shm")
    blob = bytearray(os.urandom(64)) + uuid.UUID(ID_LE).bytes_le + SECRET.encode() \
        + bytes.fromhex(CACHE_KEY) + b"\x00" * 8
    (docs / "blob.bin").write_bytes(bytes(blob))
    return str(tmp_path / "run")


def test_every_search_form_of_a_uuid_and_of_a_cache_key():
    forms = {n.form for n in trace_ids.needles_for(0, ID_MAIN)}
    assert {"text", "text-utf16le", "hex", "bytes", "bytes-uuid-le", "base64"} <= forms
    # base64url is only searched where it differs from base64 (a value with '+' or '/' in it)
    assert ("base64url" in forms) == (b"+" in next(n.data for n in trace_ids.needles_for(0, ID_MAIN)
                                                   if n.form == "base64")
                                      or b"/" in next(n.data for n in trace_ids.needles_for(0, ID_MAIN)
                                                      if n.form == "base64"))
    key_forms = {n.form for n in trace_ids.needles_for(0, CACHE_KEY)}
    assert "bytes" in key_forms and "hex" not in key_forms      # dashless already: no second text
    suffixed = {n.form for n in trace_ids.needles_for(0, ID_MAIN + "~1")}
    assert "text" in suffixed and "text (without the ~N suffix)" in suffixed
    assert "bytes (without the ~N suffix)" in suffixed


def test_a_base64_identifier_is_also_searched_as_the_bytes_it_encodes():
    forms = {n.form: n.data for n in trace_ids.needles_for(0, STICKER_ID)}
    assert forms["bytes (base64-decoded)"] == STICKER_BYTES
    assert forms["hex (base64-decoded)"] == STICKER_BYTES.hex().encode()
    assert forms["base64 without padding"] == STICKER_ID.rstrip("=").encode()
    assert forms["base64url"] == STICKER_ID.rstrip("=").replace("/", "_").encode()
    # the URL-safe spelling, given unpadded, decodes to the same bytes
    url = trace_ids.needles_for(0, STICKER_ID.rstrip("=").replace("/", "_"))
    assert {n.form: n.data for n in url}["bytes (base64-decoded)"] == STICKER_BYTES
    # hex, words and usernames are made of base64 characters too, and are not read as base64
    for not_base64 in (ID_MAIN, CACHE_KEY, "1681494784166", "customSticker", "john_smith1",
                       "D7//c3ludGhldGljM===", "D7//c3ludGhldGljMR=="):
        assert trace_ids.base64_bytes(not_base64) is None, not_base64


def _sticker_run_folder(tmp_path):
    """A claim key holding a base64 id, and a chat message holding the bytes it encodes."""
    docs = tmp_path / "run" / "ExtractedData" / "Application" / "APP" / "Documents"
    docs.mkdir(parents=True)
    conn = sqlite3.connect(str(docs / "cache.sqlite"))
    conn.execute("create table claim (key text)")
    conn.execute("insert into claim values (?)", ("customSticker~" + STICKER_ID,))
    conn.commit()
    conn.close()

    def field(number, value):
        return bytes([number << 3 | 2, len(value)]) + value
    content = field(4, field(2, b"\x05") + field(4, field(14, field(1, STICKER_BYTES)
                                                             + field(3, SECRET.encode()))))
    conn = sqlite3.connect(str(docs / "chat.sqlite"))
    conn.execute("create table message (content blob)")
    conn.execute("insert into message values (?)", (content,))
    conn.commit()
    conn.close()
    (docs / "plain.txt").write_bytes(b"x" * 7 + STICKER_ID.rstrip("=").encode() + b"\n")
    return str(tmp_path / "run")


def test_a_base64_id_is_found_as_bytes_in_a_protobuf_and_its_field_is_named(tmp_path):
    payload = trace_ids.trace(_sticker_run_folder(tmp_path), [STICKER_ID])
    rows = [h for h in payload["hits"] if h["kind"] == "sqlite"]
    chat = [h for h in rows if h["table"] == "message"]
    assert [(h["form"], h.get("field"), h["cell"]) for h in chat] == \
        [("bytes (base64-decoded)", "4.4.14.1", "blob")]
    # the padded key is reported once, as text; its unpadded spelling is the same occurrence
    claim = [h for h in rows if h["table"] == "claim"]
    assert [(h["form"], h.get("field")) for h in claim] == [("text", None)]
    raw_claim = [h for h in payload["hits"] if h["kind"] == "sqlite-file"
                 and h["file"].endswith("cache.sqlite")]
    assert raw_claim and {h["form"] for h in raw_claim} == {"text"}
    # an unpadded occurrence on its own is reported under the form that found it
    plain = [h for h in payload["hits"] if h["kind"] == "file"]
    assert [(h["form"], h["offset"]) for h in plain] == [("base64 without padding", 7)]
    assert any("field 4.4.14.1" in line for line in trace_ids.describe(payload))
    assert SECRET not in json.dumps(payload)


def test_a_hit_outside_every_live_record_is_placed_hit_by_hit(tmp_path):
    """One id in a live row and on a free page; one in a freeblock; one in unallocated space. The
    first used to be marked "in rows" at both places, because some row held it."""
    id_free = "12121212-3434-4565-8787-909090909090"
    id_block = "abababab-cdcd-4efe-8a0a-1b1b1b1b1b1b"
    id_space = "c0c0c0c0-d1d1-4e2e-8f3f-404040404040"
    docs = tmp_path / "run" / "ExtractedData" / "Application" / "APP" / "Documents"
    docs.mkdir(parents=True)
    conn = sqlite3.connect(str(docs / "store.sqlite"))
    conn.execute("pragma secure_delete = 0")
    conn.execute("create table t (id integer primary key, v text)")
    conn.execute("create table gone (id integer primary key, v text)")
    for n, text in enumerate((id_free, id_block, "filler", id_space), 1):  # cells fill from the end
        conn.execute("insert into t values (?, ?)", (n, text + "-" + SECRET))
    conn.executemany("insert into gone values (?, ?)", [(n, id_free + "x" * 900) for n in range(40)])
    conn.commit()
    conn.execute("delete from t where id = 2")          # between two cells: a freeblock
    conn.execute("delete from t where id = 4")          # the first cell: back to unallocated space
    conn.execute("drop table gone")                     # its pages: the freelist
    conn.commit()
    conn.close()
    payload = trace_ids.trace(str(tmp_path / "run"), [id_free, id_block, id_space])
    raw = [h for h in payload["hits"] if h["kind"] == "sqlite-file"]

    def places(i):
        return {(h["in_rows"], h.get("where")) for h in raw if h["id"] == i}
    assert places(0) == {(True, None), (False, "free page")}
    assert places(1) == {(False, "freeblock")}
    assert places(2) == {(False, "unallocated space")}
    assert [h["row"] for h in payload["hits"] if h["kind"] == "sqlite"] == [1]
    assert any("free page - in no row either reading returns" in line
               for line in trace_ids.describe(payload))


def test_a_match_across_a_chunk_boundary_is_found_once(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * 10 + ID_LE.encode() + b"y" * 10)
    needles = trace_ids.needles_for(0, ID_LE)
    hits = trace_ids.scan_file(str(path), needles, chunk=16)
    assert [(n.form, off) for n, off, _case in hits] == [("text", 10)]


def test_trace_places_each_hit_and_never_leaks_content(tmp_path, caplog):
    run = _run_folder(tmp_path)
    payload = trace_ids.trace(run, [ID_MAIN, ID_WAL, ID_GONE, ID_LE, CACHE_KEY])
    hits = payload["hits"]

    def of(i, kind):
        return [h for h in hits if h["id"] == i and h["kind"] == kind]

    # the checkpointed row: text, in the main-only reading
    row = of(0, "sqlite")
    assert [(h["table"], h["column"], h["row"], h["reading"], h["case"]) for h in row] == \
        [("t", "name", 1, sqlite_open.MAIN_ONLY, "lower")]
    # written after the checkpoint: a blob holding the UUID's bytes, only with the -wal applied
    row = of(1, "sqlite")
    assert [(h["column"], h["form"], h["reading"], h["cell"]) for h in row] == \
        [("data", "bytes", sqlite_open.WAL_ONLY, "blob")]
    # inserted and changed within the log: no reading returns it, a superseded frame still holds it
    assert of(2, "sqlite") == []
    wal = of(2, "wal")
    assert wal and any(h["superseded"] for h in wal)
    assert all(h["case"] == "upper" for h in wal)
    # a plain file: little-endian UUID bytes, and a CACHE_KEY's raw bytes, at their offsets
    assert [(h["form"], h["offset"]) for h in of(3, "file")] == [("bytes-uuid-le", 64)]
    assert [(h["form"], h["offset"]) for h in of(4, "file")] == \
        [("bytes", 64 + 16 + len(SECRET))]
    assert all(h["device_path"].endswith(("store.sqlite", "store.sqlite-wal", "blob.bin"))
               for h in hits)

    out = trace_ids.write_report(run, payload)
    with caplog.at_level(logging.INFO):
        for line in trace_ids.describe(payload):
            logging.getLogger("t").info(line)
    assert SECRET not in open(out, encoding="utf-8").read()
    assert SECRET not in caplog.text
    assert "renamed" not in caplog.text and "gone" not in json.dumps(payload)


def test_exit_codes(tmp_path):
    run = _run_folder(tmp_path)
    assert app.run_trace_ids(["--trace-ids", run, ID_MAIN]) == trace_ids.EXIT_FOUND
    assert app.run_trace_ids(["--trace-ids", run, "deadbeef-dead-4eef-8eef-deadbeefdead"]) \
        == trace_ids.EXIT_NONE
    assert app.run_trace_ids(["--trace-ids", run]) == trace_ids.EXIT_USAGE
    assert app.run_trace_ids(["--trace-ids", str(tmp_path / "absent"), ID_MAIN]) \
        == trace_ids.EXIT_USAGE
    ids = tmp_path / "ids.txt"
    ids.write_text(f"# a comment\n{ID_LE}\n\n", encoding="utf-8")
    assert app.run_trace_ids(["--trace-ids", run, f"@{ids}"]) == trace_ids.EXIT_FOUND
    assert any(name.startswith("trace_ids_") for name in os.listdir(run))


def test_a_damaged_page_size_costs_the_placement_not_the_trace(tmp_path):
    path = tmp_path / "broken.sqlite"
    path.write_bytes(trace_ids.SQLITE_MAGIC + b"\x00" * 200 + ID_MAIN.encode())
    assert trace_ids._free_places(str(path), [216]) == {}


def test_a_hit_in_a_keyed_archive_names_the_path_of_the_text_that_holds_it(tmp_path):
    from overlay_fixture import Obj, archive
    url = "https://cf-st.example.net/d/SyntheticAsset?mo=QUJD%3D&uc=1"
    docs = tmp_path / "run" / "ExtractedData" / "Documents"
    docs.mkdir(parents=True)
    blob = archive(Obj("SynthOverlay", filters=Obj("SynthFilters", geoFilters=[
        Obj("SynthGeo", imageUrl="https://cf-st.example.net/d/Other?uc=1"),
        Obj("SynthGeo", imageUrl=url, note=SECRET)])))
    conn = sqlite3.connect(str(docs / "gallery.sqlite"))
    conn.execute("create table detail (overlay blob)")
    conn.execute("insert into detail values (?)", (blob,))
    conn.commit()
    conn.close()
    payload = trace_ids.trace(str(tmp_path / "run"), ["SyntheticAsset"])
    rows = [h for h in payload["hits"] if h["kind"] == "sqlite"]
    assert [(h["table"], h.get("field"), h["cell"]) for h in rows] == [
        ("detail", "SynthOverlay.filters.geoFilters[1].imageUrl", "blob: binary plist")]
    assert SECRET not in json.dumps(payload)
