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
ACCT_A = "a0a0a0a0-1111-4222-8333-444444444444"          # arroyo.db's own account (its USERID)
ACCT_B = "b0b0b0b0-1111-4222-8333-444444444444"          # another account's claims, same cache
ACCT_C = "c1c1c1c1-1111-4222-8333-444444444444"          # an account no arroyo.db is shown to be
ROW_CONV = "c0c0c0c0-1111-4222-8333-444444444444"        # held only by a conversation row
FEED_CONV = "d0d0d0d0-1111-4222-8333-444444444444"       # only by a feed_entry row
MEMBER_CONV = "e0e0e0e0-1111-4222-8333-444444444444"     # only by user_conversation
OTHER_CONV = "eeeeeeee-ffff-4000-8111-222222222222"      # held by no table of arroyo.db
OWN_CONV = "ffffffff-0000-4111-8222-333333333333"        # nor this one


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
    (app_dir / "global_scoped" / "cachecontroller").mkdir(parents=True)
    (app_dir / "arroyo").mkdir()
    conn = sqlite3.connect(str(app_dir / "global_scoped" / "cachecontroller" / "cache_controller.db"))
    conn.execute("create table CACHE_FILE_CLAIM (USER_ID text, CACHE_KEY text, "
                 "MEDIA_CONTEXT_TYPE integer, EXTERNAL_KEY text)")
    conn.executemany(f"insert into CACHE_FILE_CLAIM values ('{ACCT_A}', ?, ?, ?)", [
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
    # whose database it is, and conversations it holds with no message in them
    conn.execute("create table required_values (key text primary key, value text)")
    conn.execute("insert into required_values values ('USERID', ?)", (ACCT_A,))
    conn.execute("create table conversation (client_conversation_id text, creation_timestamp integer)")
    conn.execute("insert into conversation values (?, 0)", (ROW_CONV,))
    conn.execute("create table feed_entry (client_conversation_id text, display_timestamp integer)")
    conn.execute("insert into feed_entry values (?, 0)", (FEED_CONV,))
    conn.execute("create table user_conversation (client_conversation_id text, user_id text)")
    conn.execute("insert into user_conversation values (?, ?)", (MEMBER_CONV, ACCT_A))
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
                   FILE_UUID_NOWHERE, "example.invalid", "k-sticker", ACCT_A, ROW_CONV, FEED_CONV,
                   MEMBER_CONV):
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


def _add_claims(tmp_path, claims):
    db = next((tmp_path / "run" / "ExtractedData").rglob("cache_controller.db"))
    conn = sqlite3.connect(str(db))
    conn.executemany("insert into CACHE_FILE_CLAIM values (?, ?, 3, ?)", claims)
    conn.commit()
    conn.close()


def _arroyo_sql(tmp_path, *statements):
    conn = sqlite3.connect(str(next((tmp_path / "run" / "ExtractedData").rglob("arroyo.db"))))
    for statement in statements:
        sql, params = statement if isinstance(statement, tuple) else (statement, ())
        conn.execute(sql, params)
    conn.commit()
    conn.close()


def _named(payload):
    """``{type word: untied_named_message}`` — each case below gets a type word of its own, so a shape
    of its own."""
    return {s["shape"].split("~")[0]: s["untied_named_message"] for s in payload["shapes"]
            if s.get("untied_named_message")}


#: The cases of the ladder: (type word, the claim's account, conversation, message number).
_LADDER = [
    ("held", ACCT_A, CONV, 9),             # arroyo.db holds message 9
    ("unsent", ACCT_A, CONV, 99),          # 99 is the client_message_id of a message never numbered
    ("gone", ACCT_A, CONV, 404),           # no message 404 in a conversation it holds
    ("client", ACCT_A, CONV, 3000),        # a client id of a NUMBERED message is not its number
    ("row", ACCT_A, ROW_CONV, 5),          # a conversation held by a row with no message ...
    ("feed", ACCT_A, FEED_CONV, 5),
    ("member", ACCT_A, MEMBER_CONV, 5),
    ("other", ACCT_B, OTHER_CONV, 5),      # a conversation it does not hold, another account's claim
    ("own", ACCT_A.upper(), OWN_CONV, 6),  # ... arroyo.db's own account's (in any letter case)
    ("anon", "", OTHER_CONV, 7),           # ... a claim that names no account
]


def test_an_untied_key_says_what_arroyo_db_holds_of_what_it_names(tmp_path, caplog):
    run = _run_folder(tmp_path)
    _arroyo_sql(tmp_path, ("insert into conversation_message values (?, 30, 3000, 1, x'')", (CONV,)))
    _add_claims(tmp_path, [(user, f"k-{word}", f"{word}~1:{conv}:{n}:0:0")
                           for word, user, conv, n in _LADDER])
    payload = survey.survey(run, progress=lambda *_: None)
    in_conversation = "names a message arroyo.db does not hold, in a conversation it holds"
    assert _named(payload) == {
        "held": {"names a message arroyo.db holds": 1},
        "unsent": {"names a message arroyo.db holds": 1},
        "gone": {in_conversation: 1},
        "client": {in_conversation: 1},
        "row": {in_conversation: 1},
        "feed": {in_conversation: 1},
        "member": {in_conversation: 1},
        "other": {"names a conversation arroyo.db does not hold — claimed by another account": 1},
        "own": {"names a conversation arroyo.db does not hold — claimed by arroyo.db's own "
                "account": 1},
        "anon": {"names a conversation arroyo.db does not hold": 1},
    }
    # the statuses do not move: a label explains an untied claim, it does not tie it
    assert {s["shape"].split("~")[0]: s["status"] for s in payload["shapes"]
            if s["shape"].split("~")[0] in {w for w, *_ in _LADDER}} == {
        w: {"none": 1} for w, *_ in _LADDER}
    assert any("claimed by another account" in line for line in survey.describe(payload))

    out = survey.write_report(run, payload)
    with caplog.at_level(logging.INFO):
        for line in survey.describe(payload):
            logging.getLogger("t").info(line)
    written = open(out, encoding="utf-8").read() + caplog.text
    for secret in (CONV, ROW_CONV, FEED_CONV, MEMBER_CONV, OTHER_CONV, OWN_CONV, ACCT_A, ACCT_B,
                   ACCT_A.upper(), "k-other", "k-own"):
        assert secret not in written, secret


def test_with_no_account_to_compare_whose_claim_it_is_is_not_said(tmp_path):
    """No USERID read means no account to compare with: the conversation is still not held, but
    whose claim it is is not said."""
    run = _run_folder(tmp_path)
    _arroyo_sql(tmp_path, "delete from required_values")
    _add_claims(tmp_path, [(ACCT_B, "k-other", f"other~1:{OTHER_CONV}:5:0:0"),
                           (ACCT_A, "k-own", f"own~1:{OWN_CONV}:6:0:0")])
    payload = survey.survey(run, progress=lambda *_: None)
    assert _named(payload) == {"other": {"names a conversation arroyo.db does not hold": 1},
                               "own": {"names a conversation arroyo.db does not hold": 1}}


def _sql(conn, statements):
    for statement in statements:
        sql, params = statement if isinstance(statement, tuple) else (statement, ())
        conn.execute(sql, params)


def _root_pages(conn, tables):
    size = conn.execute("pragma page_size").fetchone()[0]
    return size, [conn.execute("select rootpage from sqlite_master where name = ?", (t,)).fetchone()[0]
                  for t in tables]


def _damage(path, table):
    """Overwrite ``table``'s root page with 0xFF bytes: the schema still reads — a probe of its
    columns passes — and its rows do not. ``path`` has no -wal."""
    conn = sqlite3.connect(path)
    size, [root] = _root_pages(conn, [table])
    conn.close()
    with open(path, "r+b") as fh:
        fh.seek((root - 1) * size)
        fh.write(b"\xff" * size)


def test_an_arroyo_db_that_will_not_read_is_not_one_that_holds_nothing(tmp_path):
    """Each fact an absence is said from — the messages, the conversations, the account — makes
    the database unread when its table is missing or its pages will not read, never one that holds
    nothing."""
    from scripts.data import sqlite_open
    claims = [(ACCT_A, "k-gone", f"gone~1:{CONV}:404:0:0"),
              (ACCT_B, "k-other", f"other~1:{OTHER_CONV}:5:0:0")]
    not_read = "names a message not found (arroyo.db not read)"
    damaged = {"conversation_message": "client_conversation_id", "conversation":
               "client_conversation_id", "required_values": "value"}
    for case in (*damaged, "missing"):
        run = _run_folder(tmp_path / case)
        _add_claims(tmp_path / case, claims)
        db = str(next((tmp_path / case / "run" / "ExtractedData").rglob("arroyo.db")))
        if case == "missing":                  # no conversation_message table at all
            _arroyo_sql(tmp_path / case, "alter table conversation_message rename to other_table")
        else:                                  # a damaged page: the columns still read
            _damage(db, case)
            assert damaged[case] in sqlite_open.table_columns(db, case)
        said = []
        payload = survey.survey(run, progress=said.append)
        assert _named(payload) == {"gone": {not_read: 1}, "other": {not_read: 1}}, case
        assert any("could not be read in full" in line for line in said), case


def _checkpoint_then_wal(path, before, after, damage=()):
    """Build ``path`` from the ``before`` statements, checkpointed into the file, and leave the
    ``after`` statements in its -wal only. The root page of each ``damage`` table is overwritten
    in the file alone, so only the checkpointed reading meets it."""
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=wal")
    _sql(conn, before)
    conn.commit()
    size, roots = _root_pages(conn, damage)
    conn.close()                                       # checkpoints: the file holds all of it
    checkpointed = bytearray(open(path, "rb").read())
    for root in roots:
        checkpointed[(root - 1) * size:root * size] = b"\xff" * size
    conn = sqlite3.connect(path)
    conn.execute("pragma wal_autocheckpoint=0")
    _sql(conn, after)
    conn.commit()
    wal = open(path + "-wal", "rb").read()
    conn.close()
    with open(path, "wb") as fh:
        fh.write(checkpointed)
    with open(path + "-wal", "wb") as fh:
        fh.write(wal)


def _wal_arroyo(path):
    """An arroyo.db whose -wal deletes a message and a conversation row and adds a message."""
    _checkpoint_then_wal(path, [
        "create table conversation_message (client_conversation_id text, server_message_id integer, "
        "client_message_id integer, primary key (client_conversation_id, client_message_id))",
        "create table conversation (client_conversation_id text primary key)",
        "create table required_values (key text primary key, value text)",
        ("insert into conversation_message values (?, 5, 5), (?, 6, 6), (?, NULL, 41)",
         (CONV, CONV, CONV.upper())),
        ("insert into conversation values (?), (?)", (ROW_CONV, FEED_CONV)),
        ("insert into required_values values ('userid', ?)", (ACCT_A.upper(),)),
    ], [
        "delete from conversation_message where server_message_id = 5",
        ("insert into conversation_message values (?, 7, 7)", (CONV,)),
        ("delete from conversation where client_conversation_id = ?", (ROW_CONV,)),
    ])


def test_arroyo_facts_come_from_both_readings(tmp_path):
    import os
    path = str(tmp_path / "arroyo.db")
    _wal_arroyo(path)
    assert os.path.getsize(path + "-wal")              # the -wal really is there to be applied
    held, convs, accounts, unread, nameless = survey._arroyo_facts([path])
    conv = CONV.lower()
    # 5 only in the checkpointed reading, 7 only once the -wal is applied; 41 never numbered
    assert held == {(conv, "5"), (conv, "6"), (conv, "7"), (conv, "41")}
    assert convs == {conv, ROW_CONV, FEED_CONV}        # ROW_CONV's row is gone from the -wal reading
    assert accounts == {ACCT_A} and unread == [] and nameless == []


def test_a_table_or_column_the_wal_created_is_read_not_taken_for_one_that_will_not(tmp_path):
    """A table created since the last checkpoint is not in the checkpointed reading at all: that is
    a reading with nothing to add, not one that failed."""
    from scripts.data import sqlite_open
    path = str(tmp_path / "arroyo.db")
    _checkpoint_then_wal(path, [
        "create table conversation_message (client_conversation_id text, server_message_id integer)",
        ("insert into conversation_message values (?, 5)", (CONV,)),
    ], [
        "alter table conversation_message add column client_message_id integer",
        ("insert into conversation_message values (?, NULL, 41)", (CONV,)),
        "create table conversation (client_conversation_id text)",
        ("insert into conversation values (?)", (ROW_CONV,)),
        "create table required_values (key text, value text)",
        ("insert into required_values values ('USERID', ?)", (ACCT_A,)),
    ])
    views = sqlite_open.open_views(path)
    try:                                               # the checkpointed reading lacks all three
        assert survey._columns(views.main_only, "conversation_message") == {
            "client_conversation_id", "server_message_id"}
        assert not survey._columns(views.main_only, "conversation")
        assert not survey._columns(views.main_only, "required_values")
    finally:
        views.close()
    held, convs, accounts, unread, nameless = survey._arroyo_facts([path])
    conv = CONV.lower()
    assert held == {(conv, "5"), (conv, "41")}
    assert convs == {conv, ROW_CONV} and accounts == {ACCT_A}
    assert unread == [] and nameless == []


def test_a_checkpointed_copy_that_will_not_read_is_not_one_that_holds_nothing(tmp_path):
    """A message the -wal deleted is only in the checkpointed reading, so when that copy will not
    read, a message the key names is not said to be absent — though the current reading is whole."""
    path = str(tmp_path / "arroyo.db")
    _checkpoint_then_wal(path, [
        "create table conversation_message (client_conversation_id text, server_message_id integer)",
        ("insert into conversation_message values (?, 5), (?, 6)", (CONV, CONV)),
        "create table required_values (key text, value text)",
        ("insert into required_values values ('USERID', ?)", (ACCT_A,)),
    ], ["delete from conversation_message where server_message_id = 5"],
        damage=["conversation_message"])
    import pytest
    from scripts.data import sqlite_open
    views = sqlite_open.open_views(path)
    try:                                               # the -wal holds a whole image of that page
        assert views.merged.execute("select * from conversation_message").fetchall() == [(CONV, 6)]
        with pytest.raises(sqlite3.DatabaseError):
            views.main_only.execute("select * from conversation_message").fetchall()
    finally:
        views.close()
    held, convs, accounts, unread, _nameless = survey._arroyo_facts([path])
    assert unread == [path]
    for number in ("5", "404"):
        assert survey._named_label(CONV, number, ACCT_A, held, convs, accounts,
                                   read=not unread) == survey.NAMED_NOT_READ


def _second_arroyo(tmp_path, userid=None):
    """Another app folder's arroyo.db, holding a message of CONV and, given ``userid``, an account."""
    folder = tmp_path / "run" / "ExtractedData" / "Application" / "APP2" / "Documents" / "arroyo"
    folder.mkdir(parents=True)
    conn = sqlite3.connect(str(folder / "arroyo.db"))
    _sql(conn, ["create table conversation_message (client_conversation_id text, "
                "server_message_id integer)",
                ("insert into conversation_message values (?, 3)", (CONV,))])
    if userid:
        _sql(conn, ["create table required_values (key text, value text)",
                    ("insert into required_values values ('USERID', ?)", (userid,))])
    conn.commit()
    conn.close()


def test_another_account_s_claim_is_said_only_when_every_arroyo_db_names_its_account(tmp_path):
    """An arroyo.db that names no account may be the account of a claim no other one names: that
    claim is not called another account's. A claim of an account that is read stays its own."""
    claims = [(ACCT_C, "k-other", f"other~1:{OTHER_CONV}:5:0:0"),
              (ACCT_A, "k-own", f"own~1:{OWN_CONV}:6:0:0")]
    own = "names a conversation arroyo.db does not hold — claimed by arroyo.db's own account"
    run = _run_folder(tmp_path / "nameless")
    _add_claims(tmp_path / "nameless", claims)
    _second_arroyo(tmp_path / "nameless")                  # no required_values at all
    said = []
    payload = survey.survey(run, progress=said.append)
    assert _named(payload) == {"other": {"names a conversation arroyo.db does not hold": 1},
                               "own": {own: 1}}
    assert any("1 of 2 arroyo.db name no account" in line for line in said)

    run = _run_folder(tmp_path / "named")                  # both accounts read: a third is another
    _add_claims(tmp_path / "named", claims)
    _second_arroyo(tmp_path / "named", ACCT_B)
    said = []
    payload = survey.survey(run, progress=said.append)
    assert _named(payload) == {
        "other": {"names a conversation arroyo.db does not hold — claimed by another account": 1},
        "own": {own: 1}}
    assert not any("name no account" in line for line in said)


def test_the_labels_are_decided_in_the_order_the_docs_give():
    """The message, then whether every arroyo.db was read, then the conversation, then whose claim
    it is — so a conversation arroyo.db holds does not make an unread message absent."""
    import os
    conv, acct = CONV.lower(), {ACCT_A}
    label = survey._named_label
    assert label(CONV, "5", ACCT_A, {(conv, "5")}, set(), acct, read=False) == survey.NAMED_HELD
    assert label(CONV, "5", ACCT_A, set(), {conv}, acct, read=False) == survey.NAMED_NOT_READ
    assert label(CONV, "5", ACCT_A, set(), {conv}, acct) == survey.NAMED_IN_CONVERSATION
    assert label(CONV, "5", ACCT_A, set(), set(), acct, every_account=False) == \
        survey.NAMED_OWN_ACCOUNT
    assert label(CONV, "5", ACCT_B, set(), set(), acct) == survey.NAMED_OTHER_ACCOUNT
    assert label(CONV, "5", ACCT_B, set(), set(), acct, every_account=False) == \
        survey.NAMED_NO_CONVERSATION
    assert label(CONV, "5", "", set(), set(), acct) == survey.NAMED_NO_CONVERSATION
    assert label(CONV, "5", ACCT_B, set(), set(), set()) == survey.NAMED_NO_CONVERSATION
    # and the docs' table lists them in that order
    doc = open(os.path.join(os.path.dirname(__file__), "..", "docs", "claim_link_survey.md"),
               encoding="utf-8").read()
    table = doc.split("### A key that names a message", 1)[1].split("\n\n")[2]
    rows = [line.split("`")[1] for line in table.splitlines()[2:]]    # after the header rows
    assert rows == [survey.NAMED_HELD, survey.NAMED_NOT_READ, survey.NAMED_IN_CONVERSATION,
                    survey.NAMED_OWN_ACCOUNT, survey.NAMED_OTHER_ACCOUNT,
                    survey.NAMED_NO_CONVERSATION]


def test_the_app_folder_is_the_one_the_reports_read_the_database_from(tmp_path):
    """iOS and Android keep cache_controller.db at different depths; a database somewhere else is
    not read against a folder guessed from where it sits."""
    import android_fixture as fx
    android = fx.build_app(str(tmp_path / "android"))
    controller = f"{android}/databases/native_content_manager/cache_controller.db"
    assert survey._apps([controller]) == [android]
    # on Android the Memories are memories.db's: the old climb of four folders read none of them
    assert fx.SNAP_ID.upper() in survey._snap_ids(survey._apps([controller]))

    ios = tmp_path / "ios" / "Application" / "APP"
    (ios / "Documents" / "gallery_data_object").mkdir(parents=True)
    db = ios / "Documents" / "global_scoped" / "cachecontroller" / "cache_controller.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    stray = tmp_path / "ios" / "copies" / "a" / "b" / "c" / "cache_controller.db"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"")
    assert survey._apps([str(db), str(stray), str(db)]) == [str(ios)]


def test_a_full_media_key_carrying_a_memory_s_snap_id_counts_as_tied():
    claim = {"EXTERNAL_KEY": f"{CONV}~1", "CACHE_KEY": "k", "MEDIA_CONTEXT_TYPE": 19}
    snaps = {CONV.upper(): (CONV, "h")}
    assert survey._link_status(claim, {}, {}, None, snaps) == "Memory: its snap id in the key"
    assert survey._link_status(dict(claim, MEDIA_CONTEXT_TYPE=34), {}, {}, None, snaps) == "none"
    assert survey._link_status(claim, {}, {}, None, {}) == "none"
    # filter-record URLs and an index that is not ctp_items.read's change nothing for a key that is
    # not one of them
    for status, snap_ids in (("Memory: its snap id in the key", snaps), ("none", {})):
        assert survey._link_status(claim, {}, {}, snap_ids=snap_ids, filter_urls={"x": 1},
                                   items={"x": 1}) == status


FILTER_URL = "https://cf-st.example.net/d/AAAAAAAAAAAA?mo=QUJD%3D&uc=1"


def _filter_urls(url=FILTER_URL):
    asset = {"url": url, "key": url, "role": "filter image", "field": "filters.geoFilters[0].imageUrl",
             "filter_id": "1", "filter_type": "STATIC", "group": "GEO_GROUP", "selected": None,
             "wal": "main+wal", "has_overlay_image": 0}
    return {url: [(CONV, "ab" * 32, asset)]}


def test_a_key_a_memory_s_filter_record_lists_is_said_so_by_the_report_s_own_rule():
    claim = {"EXTERNAL_KEY": FILTER_URL, "CACHE_KEY": "k", "MEDIA_CONTEXT_TYPE": 25,
             "USER_ID": ACCT_A}
    assert survey._link_status(claim, {}, {}, filter_urls=_filter_urls()) == survey.FILTER_STATUS
    assert survey._link_status(claim, {}, {}) == "none"
    for near_miss in (FILTER_URL.replace("uc=1", "uc=2"), FILTER_URL.replace("/d/", "/e/")):
        assert survey._link_status(dict(claim, EXTERNAL_KEY=near_miss), {}, {},
                                   filter_urls=_filter_urls()) == "none"
    # the empty "?" a claim key can carry is the same URL, as in the report; a "?" that ends a query
    # with something in it is part of that query, and is not
    bare = "https://geofilter.example.net/png/synthetic-sky"
    assert survey._link_status(dict(claim, EXTERNAL_KEY=bare + "?"), {}, {},
                               filter_urls=_filter_urls(bare)) == survey.FILTER_STATUS
    assert survey._link_status(dict(claim, EXTERNAL_KEY=FILTER_URL + "?"), {}, {},
                               filter_urls=_filter_urls()) == "none"
    # a message route comes first
    record = {"conversation_id": CONV, "server_message_id": "7.0", "anchor": "m", "href": "x"}
    assert survey._link_status(claim, {"k": [record]}, {}, filter_urls=_filter_urls()) == \
        "message: attached file"


def test_the_survey_reads_the_filter_records_of_the_app_folder(tmp_path):
    import overlay_fixture as ofx
    run = _run_folder(tmp_path)
    app_dir = tmp_path / "run" / "ExtractedData" / "Application" / "APP"
    ofx.scdb(str(app_dir), [(ofx.SNAP_A, ofx.overlay([ofx.geofilter("1", image=FILTER_URL)]), 0)])
    conn = sqlite3.connect(str(app_dir / "Documents" / "global_scoped" / "cachecontroller"
                               / "cache_controller.db"))
    conn.execute("insert into CACHE_FILE_CLAIM values (?, 'k-filter', 25, ?)", (ACCT_A, FILTER_URL))
    conn.commit()
    conn.close()
    payload = survey.survey(run, progress=lambda *_: None)
    assert [s["status"] for s in payload["shapes"] if s["context"] == 25] == [
        {survey.FILTER_STATUS: 1}]


def test_a_key_a_creative_tools_item_names_is_said_so_by_the_report_s_own_rule(tmp_path):
    import ctp_fixture as cfx
    from scripts.data import ctp_items
    app = str(tmp_path / "app")
    cfx.store(app, [cfx.filter_item()])
    items = ctp_items.read(app)
    claim = {"EXTERNAL_KEY": cfx.FILTER_CDN, "CACHE_KEY": "k", "MEDIA_CONTEXT_TYPE": 25,
             "USER_ID": ACCT_A}
    assert survey._link_status(claim, {}, {}, items=items) == survey.ITEM_STATUS
    assert survey._link_status(claim, {}, {}) == "none"
    for near_miss in (cfx.FILTER_CDN.replace("uc=7", "uc=8"), cfx.FILTER_CDN.split("?")[0]):
        assert survey._link_status(dict(claim, EXTERNAL_KEY=near_miss), {}, {}, items=items) == "none"
    # a message route and a filter record come first
    record = {"conversation_id": CONV, "server_message_id": "7.0", "anchor": "m", "href": "x"}
    assert survey._link_status(claim, {"k": [record]}, {}, items=items) == "message: attached file"
    assert survey._link_status(claim, {}, {}, filter_urls=_filter_urls(cfx.FILTER_CDN),
                               items=items) == survey.FILTER_STATUS


def test_the_survey_reads_every_creative_tools_store_of_the_app_folder(tmp_path):
    """A custom sticker's claim, whose key names the item by its item_id — a document of another
    layout than the one the report reads, matched by the item_id column alone."""
    import ctp_fixture as cfx
    run = _run_folder(tmp_path)
    app_dir = str(tmp_path / "run" / "ExtractedData" / "Application" / "APP")
    cfx.store(app_dir, [(LONELY_ID, b"a document of another layout")])
    payload = survey.survey(run, progress=lambda *_: None)
    stickers = next(s for s in payload["shapes"]
                    if s["shape"] == f"customSticker~<b64:{len(STICKER_BYTES)}>")
    assert stickers["status"] == {"none": 1, survey.ITEM_STATUS: 1}
