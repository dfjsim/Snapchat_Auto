"""The Android run, end to end, on a synthetic extraction (see android_fixture).

An archive shaped like a GrayKey one, carrying the databases the parser reads with invented rows, run
through the same entry point the GUI and the command line use. Every value is invented.
"""
import base64
import json
import os
import re
import sqlite3

import pytest

import android_fixture as fx
from scripts import ParseSnapchat_Android as android
from scripts import android_layout
from scripts import memories_android_report as memories
from scripts.data import extract_zip


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One full run: archive -> extraction -> every report. Shared by the tests below."""
    base = tmp_path_factory.mktemp("android_run")
    archive = fx.build_zip(str(base / "gk.zip"), str(base / "src"), "graykey")
    root = extract_zip.extract(archive, "android", dest=str(base / "ExtractedData"))
    reports = base / "Reports"
    cwd = os.getcwd()
    os.chdir(base)                                   # the run writes its scratch files in the cwd
    try:
        android.main(root, report_dir=str(reports), tz="utc", legacy_reports=False)
    finally:
        os.chdir(cwd)
    return base, reports


def _rows(path):
    text = open(path, encoding="utf-8").read()
    return json.loads(text[text.index("(") + 1:text.rindex(")")])


def test_every_report_is_written(run):
    _base, reports = run
    for rel in ("Conversations/Conversations_report.html", "Contacts/Contacts_report.html",
                "Memories/Memories_report.html", "CacheController/CacheController_report.html",
                "sources.json"):
        assert (reports / rel).is_file(), rel
    # the parser's staging folder is not part of the report
    assert not (reports / "_chat_attachments").exists()


def test_conversations_are_the_arroyo_ones(run):
    _base, reports = run
    rows = _rows(reports / "Conversations" / "data" / "index.js")
    by_id = {r[0]: r for r in rows}
    assert set(by_id) == {f"conv-{fx.CONV_A}", f"conv-{fx.CONV_G}", f"conv-{fx.CONV_EMPTY}"}
    # the group's title comes from arroyo's own feed_entry
    assert fx.GROUP_TITLE in by_id[f"conv-{fx.CONV_G}"][1][2]


def test_messages_are_sent_or_received_and_the_attachment_is_published(run):
    _base, reports = run
    page_dir = reports / "Conversations" / "pages" / "data" / fx.CONV_A
    rows = _rows(page_dir / "index.js")
    assert len(rows) == 6
    directions = [r[5]["dir"] for r in rows]
    assert directions.count("Sent") == 1                          # the owner's reply
    media = reports / "Conversations" / "media" / f"{fx.CHAT_KEY}.jpg"
    assert media.read_bytes() == fx.JPEG
    text = " ".join(r[2] for r in rows)
    assert "a caption" in text and "hello from a" in text


def test_sharded_and_bundled_chat_media_is_rebuilt(run):
    """The shared join takes a claim's file only when a WHOLE file named after the key is media. A
    chat video stored as a bundle and a photo stored as byte-range shards are rebuilt instead of
    being reported as having no cached file."""
    _base, reports = run
    media = reports / "Conversations" / "media"
    assert (media / f"{fx.BUNDLE_KEY}.mp4").read_bytes() == fx.MP4
    assert (media / f"{fx.SHARD_KEY}.jpg").read_bytes() == fx.JPEG
    detail = open(reports / "Conversations" / "pages" / "data" / fx.CONV_A / "detail-0.js",
                  encoding="utf-8").read()
    assert "is a bundle" in detail and "byte-range shard" in detail
    # both still link to their own cache_controller row
    assert f"#ck-{fx.BUNDLE_KEY}" in detail and f"#ck-{fx.SHARD_KEY}" in detail


def test_encrypted_chat_media_opens_with_the_key_its_message_carries(run):
    _base, reports = run
    media = reports / "Conversations" / "media"
    assert (media / f"{fx.ENC_CHAT_KEY}.jpg").read_bytes() == fx.JPEG
    detail = open(reports / "Conversations" / "pages" / "data" / fx.CONV_A / "detail-0.js",
                  encoding="utf-8").read()
    assert "carried in the content of the message" in detail


def test_a_wrong_key_pair_never_produces_media():
    from scripts import ParseSnapchat_Android as android
    raw = bytes(range(256)) * 4
    plain, pair = android._decrypt_with(raw, [(bytes(32), bytes(16)), (bytes(range(32)), bytes(16))])
    assert plain is None and pair is None


def test_contacts_come_from_the_friend_table_with_their_own_fields(run):
    _base, reports = run
    rows = _rows(reports / "Contacts" / "data" / "index.js")
    assert {r[0] for r in rows} == {f"ct-{fx.OWNER}", f"ct-{fx.FRIEND_A}", f"ct-{fx.MEMBER_B}"}
    detail = open(reports / "Contacts" / "data" / "detail-0.js", encoding="utf-8").read()
    # CombinedUsername: the changed username is the legacy one
    assert "alpha_before" in detail and "alpha_now" in detail
    # the row's own fields, and the table's own comment on the column quoted as such
    assert "+15550000000" in detail
    assert "invented comment" in detail
    assert "listed in SuggestedFriend" in detail
    html = open(reports / "Contacts" / "Contacts_report.html", encoding="utf-8").read()
    assert "main.db Friend" in html and "not the friends list" in html


def test_the_memory_is_decrypted_and_located(run):
    _base, reports = run
    rows = _rows(reports / "Memories" / "data" / "index.js")
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == f"mem-{fx.SNAP_ID}"
    assert row[5]["state"] == "decrypted" and row[5]["geo"] == "y"
    media = list((reports / "Memories" / "media").iterdir())
    assert len(media) == 1 and media[0].read_bytes() == fx.JPEG
    manifest = json.loads((reports / "Memories" / "media_by_cache_key.json").read_text())
    assert manifest[fx.MEM_KEY][0]["snap_id"] == fx.SNAP_ID


def test_the_cache_report_links_both_ways(run):
    _base, reports = run
    detail = open(reports / "CacheController" / "data" / "detail-0.js", encoding="utf-8").read()
    assert f"Memories/Memories_report.html#mem-{fx.SNAP_ID}" in detail
    assert "memories_snap._id" in detail                  # the link names the Android column
    assert "ZSNAPID" not in detail
    assert fx.CONV_A in detail                            # the chat attachment's message


def test_the_device_record_of_a_cached_file_is_shown(run):
    """The archive's UT times reach the cache report through the extraction manifest, keyed on the
    device path — the Android spelling of manifest_key."""
    from datetime import datetime, timezone
    _base, reports = run
    detail = open(reports / "CacheController" / "data" / "detail-0.js", encoding="utf-8").read()
    assert "/data/data/com.snapchat.android/files/native_content_manager/" in detail
    modified = datetime.fromtimestamp(fx.FILE_TIMES[0], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    assert modified in detail


def test_the_survey_carries_structure_and_no_values(run):
    base, _reports = run
    survey = json.loads((base / "android_survey.json").read_text(encoding="utf-8"))
    text = json.dumps(survey)
    assert "conversation_message" in text and "key_username" in text
    for secret in ("owner_name", "hello from A", fx.OWNER, "+15550000000"):
        assert secret not in text, secret
    assert any("<uuid>" in k for k in survey["folders"])


# --------------------------------------------------------------- pieces

@pytest.mark.parametrize("value,size,expected", [
    (base64.b64encode(bytes(range(32))).decode(), 32, bytes(range(32))),
    (base64.urlsafe_b64encode(bytes(range(200, 216))).decode().rstrip("="), 16,
     bytes(range(200, 216))),
    (bytes(range(16)).hex(), 16, bytes(range(16))),
    (bytes(range(32)), 32, bytes(range(32))),
])
def test_a_key_is_read_in_each_stored_form(value, size, expected):
    assert memories.decode_key(value, size)[0] == expected


@pytest.mark.parametrize("value", [None, "", "not a key", base64.b64encode(b"short").decode()])
def test_a_key_of_the_wrong_size_is_reported_not_guessed(value):
    raw, why = memories.decode_key(value, 32)
    assert raw is None and why


def test_a_hex_stored_key_decrypts_too(tmp_path):
    app = fx.build_app(str(tmp_path), key_encoding="hex")
    layout = android_layout.discover(str(tmp_path))[0]
    assert layout.app.endswith("data/data/com.snapchat.android")
    loaded, _info, _extra = memories.load_memories(layout.db("memories"))
    count = memories.collect_media(loaded, app, str(tmp_path / "out"))
    assert count == 1
    state = loaded[fx.SNAP_ID]["files"][0]["state"]
    assert state == "decrypted (memories_snap.media_key / media_iv)"


def test_a_my_eyes_only_snap_without_a_key_says_so(tmp_path):
    fx.build_app(str(tmp_path))
    layout = android_layout.discover(str(tmp_path))[0]
    conn = sqlite3.connect(layout.db("memories"))
    conn.execute("update memories_entry set is_private = 1")
    conn.execute("update memories_snap set media_key = null, media_iv = null, "
                 "encrypted_media_key = 'd3JhcHBlZA==', encrypted_media_iv = 'aXY='")
    conn.commit()
    conn.close()
    loaded, _info, _extra = memories.load_memories(layout.db("memories"))
    memories.collect_media(loaded, layout.app, str(tmp_path / "out"))
    m = loaded[fx.SNAP_ID]
    assert m["meo"]
    assert memories._state(m) == "My Eyes Only — locked"
    assert not (tmp_path / "out" / "media").exists()


def test_the_account_falls_back_to_shared_prefs(tmp_path):
    fx.build_app(str(tmp_path))
    layout = android_layout.discover(str(tmp_path))[0]
    conn = sqlite3.connect(layout.db("arroyo"))
    conn.execute("delete from required_values")
    conn.commit()
    conn.close()
    owner = android.owner_identity(layout)
    assert owner["user_id"] == fx.OWNER
    assert owner["username"] == "owner_name"
    assert any(label.startswith("shared_prefs/user_session_shared_pref.xml")
               for label, _v in owner["rows"])


def test_contacts_are_read_without_the_username_table(tmp_path):
    fx.build_app(str(tmp_path))
    layout = android_layout.discover(str(tmp_path))[0]
    conn = sqlite3.connect(layout.db("main"))
    conn.execute("drop table CombinedUsername")
    conn.commit()
    conn.close()
    from scripts.memories_media_report import make_time_formatter
    df, identifiers, _stats = android.read_contacts(layout, {}, make_time_formatter("utc")[0])
    assert len(df) == 3 and identifiers == {}
    assert set(df["Username"]) == {"owner_name", "alpha_before", "member_b"}


def test_schema_comments_are_read_from_the_create_statement(tmp_path):
    fx.build_app(str(tmp_path))
    layout = android_layout.discover(str(tmp_path))[0]
    comments = android.schema_comments(layout.db("main"), "Friend")
    assert comments == {"addedTimestamp": "when the one user added the other (invented comment)"}


def test_the_older_flat_layout_is_still_read(tmp_path):
    """An extraction folder an earlier version wrote holds the app folder with no device path."""
    fx.build_app(str(tmp_path / "old"))
    flat = tmp_path / "ExtractedData"
    os.makedirs(flat)
    os.rename(tmp_path / "old" / "data" / "data" / "com.snapchat.android",
              flat / "com.snapchat.android")
    layouts = android_layout.discover(str(flat))
    assert len(layouts) == 1
    assert layouts[0].db("arroyo").endswith("com.snapchat.android/databases/arroyo.db")
    assert layouts[0].device_path(layouts[0].db("arroyo")) == \
        "/data/data/com.snapchat.android/databases/arroyo.db"
    assert re.search(r"com\.snap\.file_manager_3_SCContent_", layouts[0].sccontent_dirs[0])


def test_every_android_report_page_has_valid_javascript(run):
    """The Memories report is new and hand-built; the others take Android words in their text. Every
    inline script they wrote must parse the way a browser parses it."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    _base, reports = run
    pages = [reports / "Memories" / "Memories_report.html", reports / "Contacts" / "Contacts_report.html",
             reports / "Conversations" / "Conversations_report.html",
             reports / "CacheController" / "CacheController_report.html"]
    for page in pages:
        doc = page.read_text(encoding="utf-8")
        blocks = [b for b in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", doc, re.S)
                  if b.strip()]
        assert blocks, page
        for block in blocks:
            proc = subprocess.run([node, "--check", "-"], input=block, text=True, encoding="utf-8",
                                  capture_output=True)
            assert proc.returncode == 0, f"{page.name}: {proc.stderr}"
