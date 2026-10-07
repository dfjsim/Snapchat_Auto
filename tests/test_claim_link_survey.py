"""``--survey-claim-links``: for every cache claim, whether the reports tie it to a chat message,
and where in arroyo.db the ids its key carries occur — grouped by key shape, and nothing else.

What it is for is finding the link rules that are missing, on a case whose data cannot leave the
machine: so the shapes must group keys by kind without carrying their ids, an id must be recognised
however it is written (base64 in a key, raw bytes in a message), and no id, key or cell value may
reach the log or the JSON. Every input is synthetic.
"""
import base64
import json
import logging
import sqlite3

import Snapchat_Auto as app
from scripts import claim_link_survey as survey

CONV = "1111aaaa-2222-4333-8444-55555555cccc"
STICKER_BYTES = b"\x0f\xbf\xffsynthetic1"
STICKER_ID = base64.b64encode(STICKER_BYTES).decode()           # padded, holds a '/'
LONELY_ID = base64.b64encode(b"\x01\x02\x03nowhere123").decode()   # as long, and held nowhere
SHARED_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
FILE_UUID_NOWHERE = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
STORY_UUID = "cccccccc-dddd-4eee-8fff-000000000000"      # held by another database, not arroyo.db
UNSENT_UUID = "dddddddd-eeee-4fff-8000-111111111111"     # in a message the server never numbered
SECRET = "SENTINEL-CONTENT-MUST-NOT-LEAK"


def _f(number, value):
    """One length-delimited protobuf field (every length here is under 128)."""
    return bytes([number << 3 | 2, len(value)]) + value


def test_a_key_is_read_into_a_shape_and_its_ids():
    shape, ids = survey.read_key("customSticker~" + STICKER_ID)
    assert shape == f"customSticker~<b64:{len(STICKER_BYTES)}>"
    assert [i.label for i in ids] == ["customSticker", f"<b64:{len(STICKER_BYTES)}>"]
    assert ids[1].canon == ("bytes", STICKER_BYTES)

    shape, ids = survey.read_key(f"thumbnail~1:{CONV}:12:0:0")
    assert shape == "thumbnail~<n>:<uuid>:<n>:<n>:<n>"
    # a UUID and the same 32 digits without dashes are one id
    assert ids[0].canon == survey.read_key(CONV.replace("-", ""))[1][0].canon

    url = ("https://cf-st.sc-cdn.net/d/AbCdEf123456789xyz?bo="
           + base64.b64encode(b"\xfb\xff" * 8).decode().replace("=", "%3D") + "&uc=8")
    shape, ids = survey.read_key(url)
    assert shape == "https://cf-st.sc-cdn.net/d/<id>?bo=<b64:16>&uc=<n>"
    assert [i.label for i in ids] == ["<id>", "<b64:16>"]           # no host name is an id

    for name_key, masked in ((f"SYNTH.OWNER~{CONV}", "<NAME>~<uuid>"),
                             (f"content~SYNTH.OWNER~{CONV}", "content~<NAME>~<uuid>"),
                             (f"SnapVideoFilterState-SYNTH.OWNER~{CONV}",
                              "SnapVideoFilterState-<NAME>~<uuid>"),
                             (f"thumbnail~{CONV}", "thumbnail~<uuid>")):
        assert survey.read_key(name_key)[0] == masked, name_key
    assert survey.file_shape(f"SCPersistentMedia/cm-chat-media-video-SYNTH.OWNER~{CONV}.mov") == \
        "SCPersistentMedia/cm-chat-media-video-<NAME>~<uuid>.mov"

    shape, ids = survey.read_key(f"{CONV}_memories_backup_transcoded")
    assert shape == "<uuid>_memories_backup_transcoded"

    assert survey.file_shape(f"user_scoped/{'ab' * 32}/carousel-thumbnail-v2-{CONV}") == \
        "user_scoped/<hex64>/carousel-thumbnail-v2-<uuid>"
    assert survey.file_shape(f"x/{CONV}-(null)-12") == "x/<uuid>-(null)-<n>"


def _run_folder(tmp_path):
    run = tmp_path / "run"
    app_dir = run / "ExtractedData" / "Application" / "APP" / "Documents"
    (app_dir / "cachecontroller").mkdir(parents=True)
    (app_dir / "arroyo").mkdir()
    conn = sqlite3.connect(str(app_dir / "cachecontroller" / "cache_controller.db"))
    conn.execute("create table CACHE_FILE_CLAIM (USER_ID text, CACHE_KEY text, "
                 "MEDIA_CONTEXT_TYPE integer, EXTERNAL_KEY text)")
    conn.executemany("insert into CACHE_FILE_CLAIM values ('u', ?, ?, ?)", [
        ("k-sticker", 2, "customSticker~" + STICKER_ID),          # nothing ties it; a message holds it
        ("k-lonely", 2, "customSticker~" + LONELY_ID),            # nothing ties it; nowhere else
        ("k-attached", 3, f"1:{CONV}:7:0:0"),                     # the chat join attached the file
        ("k-named", 3, f"content~1:{CONV}:7:0:0"),                # the key names the message
        ("k-shared", 3, f"{SHARED_UUID}~{SHARED_UUID}"),          # its id is in many messages
        ("k-story", 3, f"SYNTH.OWNER~{STORY_UUID}"),               # an owner name, then the id
        ("k-unsent", 3, f"content~{UNSENT_UUID}"),
    ])
    conn.commit()
    conn.close()
    conn = sqlite3.connect(str(app_dir / "arroyo" / "arroyo.db"))
    conn.execute("create table conversation_message (client_conversation_id text, "
                 "server_message_id integer, client_message_id integer, content_type integer, "
                 "message_content blob)")
    sticker = _f(4, _f(4, _f(14, _f(2, _f(2, SECRET.encode()) + _f(6, STICKER_BYTES)))))
    rows = [(CONV, 7, 7, 5, sticker),
            (CONV, None, 99, 1, _f(4, _f(4, _f(2, _f(1, f"see {UNSENT_UUID}".encode())))))]
    for n in range(8, 20):                                    # a URL holding the shared id, as text
        text = f"https://example.invalid/x/{SHARED_UUID}?n={n}".encode()
        rows.append((CONV, n, n, 1, _f(4, _f(4, _f(2, _f(1, text))))))
    conn.executemany("insert into conversation_message values (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    conn = sqlite3.connect(str(app_dir / "stories.sqlite"))
    conn.execute("create table story (snap_id text)")
    conn.execute("insert into story values (?)", (STORY_UUID,))
    conn.commit()
    conn.close()
    caches = run / "ExtractedData" / "Application" / "APP" / "Library" / "Caches"
    (caches / "tmp").mkdir(parents=True)
    (caches / f"filtered-{FILE_UUID_NOWHERE}.mp4").write_bytes(b"x")
    (caches / "tmp" / f"{SHARED_UUID}~thumbnail-generation.mp4").write_bytes(b"x")
    (caches / "no-uuid-here.bin").write_bytes(b"x")
    links = run / "Reports" / "Conversations"
    links.mkdir(parents=True)
    record = {"conversation_id": CONV, "server_message_id": "7.0", "anchor": "m", "href": "x"}
    (links / "cache_links.json").write_text(json.dumps({
        "version": 3, "by_key": {"k-attached": [record]},
        "by_message": {f"{CONV}|7.0": [record]}}), encoding="utf-8")
    return str(run)


def test_the_survey_says_which_claims_a_message_holds_and_where(tmp_path, caplog):
    run = _run_folder(tmp_path)
    payload = survey.survey(run, progress=lambda *_: None)
    shapes = {(s["context"], s["shape"]): s for s in payload["shapes"]}
    stickers = shapes[(2, f"customSticker~<b64:{len(STICKER_BYTES)}>")]
    assert stickers["claims"] == 2 and stickers["status"] == {"none": 2}
    assert stickers["found_in_arroyo"] == {"none": 1}               # the lonely one is nowhere
    found = stickers["ids"][1]["found"]
    assert [(f["table"], f["column"], f["field"], f["content_type"], f["held_as"], f["claims"])
            for f in found] == [("conversation_message", "message_content", "4.4.14.2.6", 5,
                                 "a whole value, as bytes", 1)]
    assert stickers["ids"][1]["rows_per_id"] == {"1": 1}
    assert payload["shapes"][0]["shape"] == stickers["shape"]      # what a rule is wanted for, first

    structured = shapes[(3, "<n>:<uuid>:<n>:<n>:<n>")]
    assert structured["status"] == {"message: attached file": 1}
    assert shapes[(3, "content~<n>:<uuid>:<n>:<n>:<n>")]["status"] == {
        "message: named in the key": 1}
    shared = shapes[(3, "<uuid>~<uuid>")]
    assert shared["found_in_arroyo"] == {"none": 1}
    assert shared["ids"][0]["rows_per_id"] == {"6-50": 1}           # an id of many messages
    assert {f["held_as"] for f in shared["ids"][0]["found"]} == {"inside a text, as <uuid>"}

    # Library/Caches files whose path carries a UUID: looked up in every database
    files = {s["shape"]: s for s in payload["file_shapes"]}
    assert set(files) == {"filtered-<uuid>.mp4", "tmp/<uuid>~thumbnail-generation.mp4"}
    assert files["filtered-<uuid>.mp4"]["found_in_databases"] == 0
    tmp = files["tmp/<uuid>~thumbnail-generation.mp4"]
    assert tmp["found_in_databases"] == 1
    assert {(f["database"], f["table"], f["column"]) for f in tmp["ids"][0]["found"]} == {
        ("cache_controller.db", "CACHE_FILE_CLAIM", "EXTERNAL_KEY"),
        ("arroyo.db", "conversation_message", "message_content")}

    out = survey.write_report(run, payload)
    with caplog.at_level(logging.INFO):
        for line in survey.describe(payload):
            logging.getLogger("t").info(line)
    written = open(out, encoding="utf-8").read() + caplog.text
    for secret in (SECRET, STICKER_ID, STICKER_ID.rstrip("="), LONELY_ID, CONV, SHARED_UUID,
                   FILE_UUID_NOWHERE, "example.invalid", "k-sticker"):
        assert secret not in written, secret
    assert "field 4.4.14.2.6" in caplog.text

    # an untied claim whose id is in another database: where else it is held
    story = shapes[(3, "<NAME>~<uuid>")]                         # the owner name is not in the shape
    assert story["found_in_arroyo"] == {} and story["found_in_any_database"] == {"none": 1}
    assert [(f["database"], f["table"]) for f in story["ids"][0]["found"]] == [
        ("stories.sqlite", "story")]
    # an id in a message the server never numbered says so
    unsent = shapes[(3, "content~<uuid>")]["ids"][0]["found"]
    assert {f["server_message_id"] for f in unsent if f["table"] == "conversation_message"} == {
        "absent"}
    assert "SYNTH.OWNER" not in written


def test_exit_codes(tmp_path):
    run = _run_folder(tmp_path)
    assert app.run_survey_claim_links(["--survey-claim-links", run]) == survey.EXIT_OK
    assert app.run_survey_claim_links(["--survey-claim-links"]) == survey.EXIT_USAGE
    assert app.run_survey_claim_links(["--survey-claim-links", str(tmp_path / "absent")]) \
        == survey.EXIT_USAGE
    empty = tmp_path / "empty"
    empty.mkdir()
    assert app.run_survey_claim_links(["--survey-claim-links", str(empty)]) == survey.EXIT_NOTHING


def test_texts_the_survey_must_read_without_tripping():
    # JSON after a URL: a bracket is not an IPv6 host, and the id after the URL is still read
    shape, ids = survey.read_key('[{"u":"https://cdn.example/x","s":"' + STICKER_ID + '"}]')
    assert ("bytes", STICKER_BYTES) in {i.canon for i in ids}
    # "name=<base64>": the '=' separates, it is not padding
    shape, ids = survey.read_key("sticker=" + STICKER_ID)
    assert shape == f"sticker=<b64:{len(STICKER_BYTES)}>"
    # an upper-case snap id before '~' is an id, not an owner name
    upper = CONV.upper()
    assert survey.read_key(f"{upper}~1")[0] == "<uuid>~<n>"
    assert survey.read_key(f"{upper}~{upper}")[0] == "<uuid>~<uuid>"
    # a number long enough to be an id has its own placeholder, so it cannot share a shape
    assert survey.read_key(f"x~12345678:{CONV}")[0] != survey.read_key(f"x~1234567:{CONV}")[0]


def test_rows_per_id_are_counted_per_database():
    many = {"cache_controller.db": {("t", n) for n in range(survey.ROW_CAP + 1)},
            "arroyo.db": {("m", 1), ("m", 2), ("m", 3)}}
    assert survey._bucket({"rows": many}) == survey.MORE
    assert survey._bucket({"rows": {"arroyo.db": many["arroyo.db"]}}) == "2-5"


def test_an_untied_key_says_whether_the_message_it_names_is_still_there(tmp_path):
    run = _run_folder(tmp_path)
    db = next((tmp_path / "run" / "ExtractedData").rglob("cache_controller.db"))
    conn = sqlite3.connect(str(db))
    conn.executemany("insert into CACHE_FILE_CLAIM values ('u', ?, 3, ?)", [
        ("k-held", f"animationmedia~1:{CONV}:9:0:0"),             # arroyo.db holds message 9
        ("k-gone", f"animationmedia~1:{CONV}:404:0:0"),           # and no message 404
    ])
    conn.commit()
    conn.close()
    payload = survey.survey(run, progress=lambda *_: None)
    [shape] = [s for s in payload["shapes"] if s["shape"].startswith("animationmedia~")]
    assert shape["untied_named_message"] == {"names a message arroyo.db holds": 1,
                                             "names a message arroyo.db does not hold": 1}
    assert any("does not hold" in line for line in survey.describe(payload))


def test_an_arroyo_db_that_could_not_be_read_is_not_a_message_that_is_gone(tmp_path, monkeypatch):
    import pandas as pd
    from scripts.data import sqlite_open
    run = _run_folder(tmp_path)
    db = next((tmp_path / "run" / "ExtractedData").rglob("cache_controller.db"))
    conn = sqlite3.connect(str(db))
    conn.execute("insert into CACHE_FILE_CLAIM values ('u', 'k-x', 3, ?)",
                 (f"animationmedia~1:{CONV}:404:0:0",))
    conn.commit()
    conn.close()
    real = sqlite_open.read_sql

    def failing(path, query, *a, **k):
        if "SELECT client_conversation_id, server_message_id FROM" in query:
            return pd.DataFrame(), {}
        return real(path, query, *a, **k)
    monkeypatch.setattr(sqlite_open, "read_sql", failing)
    payload = survey.survey(run, progress=lambda *_: None)
    [shape] = [s for s in payload["shapes"] if s["shape"].startswith("animationmedia~")]
    assert shape["untied_named_message"] == {"names a message not found (arroyo.db not read)": 1}


def test_a_full_media_key_carrying_a_memory_s_snap_id_counts_as_tied():
    claim = {"EXTERNAL_KEY": f"{CONV}~1", "CACHE_KEY": "k", "MEDIA_CONTEXT_TYPE": 19}
    snaps = {CONV.upper(): (CONV, "h")}
    assert survey._link_status(claim, {}, {}, None, snaps) == "Memory: its snap id in the key"
    assert survey._link_status(dict(claim, MEDIA_CONTEXT_TYPE=34), {}, {}, None, snaps) == "none"
    assert survey._link_status(claim, {}, {}, None, {}) == "none"
