"""A cached file is tied to the chat message it belongs to, not only when it is the one file the chat
report displayed.

A message names its media by ids — the media id of its ``local_message_references``, a shared item's
id, a sticker's id — and a claim key names a message by conversation and number. Every claim that
carries one of those is another cached file of that message (a thumbnail, the full media, an edit's
state), and links to it; one that carries none links to nothing. Every input is synthetic.
"""
import base64
import plistlib

from scripts import cache_controller_report as cc
from scripts import conversations_report as conv_report
from scripts.data import arroyo_content

CONV = "1111aaaa-2222-4333-8444-55555555cccc"
MEDIA_UUID = "ABCDEF01-2345-4678-9ABC-DEF012345678"
OTHER_UUID = "0F0F0F0F-1111-4222-8333-444444444444"
SHARE_ID = base64.b64encode(bytes(range(200, 233))).decode()      # 44 characters, holds '+' and '/'


def _f(number, value):
    """One length-delimited protobuf field (every length here is under 128)."""
    return bytes([number << 3 | 2, len(value)]) + value


def _media_reference(media_id):
    """``local_message_references`` as the app writes it: 8 bytes, then a keyed archive."""
    archive = {"$archiver": "NSKeyedArchiver", "$version": 100000,
               "$top": {"root": plistlib.UID(1)},
               "$objects": ["$null", {"MEDIA_ID": plistlib.UID(2), "$class": plistlib.UID(3)},
                            media_id, {"$classname": "SCRef", "$classes": ["SCRef", "NSObject"]}]}
    return b"\x00" * 8 + plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def test_the_ids_a_message_names_its_media_by():
    ref = _media_reference("WORD.TAG~" + MEDIA_UUID)
    assert arroyo_content.media_reference(ref) == "WORD.TAG~" + MEDIA_UUID
    share = _f(4, _f(4, _f(5, _f(5, _f(1, SHARE_ID.encode())))))
    assert arroyo_content.content_ids(3, share, ref) == [("WORD.TAG~" + MEDIA_UUID, "media"),
                                                         (SHARE_ID, "share")]
    sticker = _f(4, _f(4, _f(14, _f(2, _f(6, b"\x0f\xbf\xffsynthetic1")))))
    assert arroyo_content.content_ids(5, sticker) == [
        (base64.b64encode(b"\x0f\xbf\xffsynthetic1").decode(), "sticker")]
    assert arroyo_content.content_ids(1, _f(4, _f(4, _f(2, b"hello")))) == []
    assert arroyo_content.media_reference(b"not a plist at all") is None
    assert [arroyo_content.message_number(v) for v in (12, "12", 12.0, "12.0", "12.3", None, "x")] \
        == ["12", "12", "12", "12", "12", "", ""]


def _manifest(tmp_path):
    """A Conversations manifest: message 7 with an attachment, message 9 without, and ids."""
    conversation = {"id": CONV, "title": "Chat", "page": "pages/c.html", "messages": [
        {"smid": "7.0", "anchor": "msg-7.0", "atts": [{"cache_key": "k-attached"}],
         "content_ids": [("WORD.TAG~" + MEDIA_UUID, "media")]},
        {"smid": "7.1", "anchor": "msg-7.1", "atts": [],                # another part of message 7
         "content_ids": [("WORD.TAG~" + MEDIA_UUID, "media")]},
        {"smid": "9.0", "anchor": "msg-9.0", "atts": [], "content_ids": [(SHARE_ID, "share")]},
    ]}
    manifest = conv_report.write_cache_links([conversation], str(tmp_path))
    assert set(manifest["messages"][CONV]["anchors"]) == {"7.0", "7.1", "9.0"}
    assert len(manifest["by_content_id"]["WORD.TAG~" + MEDIA_UUID]) == 1     # one per message
    (tmp_path / "Conversations").mkdir()
    (tmp_path / "cache_links.json").replace(tmp_path / "Conversations" / "cache_links.json")
    by_key, by_message = cc.load_chat_links(str(tmp_path))
    return by_key, by_message, cc.load_chat_ids(str(tmp_path), by_message)


def _links(ek, key, chat):
    by_key, by_message, ids = chat
    return [(link["route"], link["server_message_id"])
            for link in cc._chat_links_for([{"external_key": ek}], key, by_key, by_message, ids)]


def test_a_claim_links_to_the_message_it_belongs_to(tmp_path):
    chat = _manifest(tmp_path)
    # the attached file, as before
    assert _links("whatever", "k-attached", chat) == [("file", "7.0")]
    # a key naming message 9, which has no attachment: it is in the manifest now
    assert _links(f"animationmedia~1:{CONV}:9:0:0", "k1", chat) == [("key", "9.0")]
    # a key naming a part the report does not list: still message 9
    assert _links(f"animationmedia~1:{CONV}:9:2:0", "k2", chat) == [("key", "9.0")]
    # the media id of the message's local_message_references, in any letter case, any prefix
    assert _links("content~WORD.TAG~" + MEDIA_UUID.lower(), "k3", chat) == [("content", "7.0")]
    assert _links("SnapVideoFilterState-WORD.TAG~" + MEDIA_UUID, "k4", chat) == [("content", "7.0")]
    # the shared item's id: exactly, base64 is case-sensitive
    assert _links("content~" + SHARE_ID, "k5", chat) == [("content", "9.0")]
    assert _links("content~" + SHARE_ID.swapcase(), "k6", chat) == []
    # nothing for a key that only resembles one: the whole id must be in it
    assert _links("content~" + OTHER_UUID, "k7", chat) == []
    assert _links("content~OTHER.TAG~" + MEDIA_UUID, "k8", chat) == []
    assert _links(f"1:{CONV}:404:0:0", "k9", chat) == []                  # no such message


def test_each_link_says_how_it_was_made(tmp_path):
    by_key, by_message, ids = _manifest(tmp_path)
    [link] = cc._chat_links_for([{"external_key": "thumbnail~" + SHARE_ID}], "k", by_key,
                                by_message, ids)
    assert "4.4.5.5.1" in link["basis"] and SHARE_ID in link["basis"]
    assert link["href"] == "Conversations/pages/c.html#msg-9.0"


def test_a_message_the_server_never_numbered_is_found_by_its_client_id(tmp_path):
    assert arroyo_content.message_key(None, 1004) == "c1004"
    assert arroyo_content.message_key("12.0", 1004) == "12"
    assert arroyo_content.message_key(None, None) == ""
    lower = _media_reference(MEDIA_UUID.lower())
    assert arroyo_content.media_reference(lower) == MEDIA_UUID.lower()     # any letter case
    conversation = {"id": CONV, "title": "Chat", "page": "pages/c.html", "messages": [
        {"smid": "", "cmid": "1004", "anchor": "msg-c1004", "atts": [],
         "content_ids": [(MEDIA_UUID, "media")]}]}
    conv_report.write_cache_links([conversation], str(tmp_path))
    (tmp_path / "Conversations").mkdir()
    (tmp_path / "cache_links.json").replace(tmp_path / "Conversations" / "cache_links.json")
    by_key, by_message = cc.load_chat_links(str(tmp_path))
    ids = cc.load_chat_ids(str(tmp_path), by_message)
    [link] = cc._chat_links_for([{"external_key": "thumbnail~" + MEDIA_UUID}], "k", by_key,
                                by_message, ids)
    assert (link["route"], link["anchor"], link["server_message_id"]) == ("content", "msg-c1004",
                                                                          "c1004")


def test_a_query_that_fails_inside_read_sql_costs_the_ids_not_the_report(tmp_path, monkeypatch):
    import sqlite3
    import pandas as pd
    from scripts.data import sqlite_open
    db = str(tmp_path / "arroyo.db")
    conn = sqlite3.connect(db)
    conn.execute("create table conversation_message (client_conversation_id text, "
                 "server_message_id integer, content_type integer, message_content blob)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(sqlite_open, "read_sql", lambda *a, **k: (pd.DataFrame(), {}))
    assert conv_report.load_content_ids(db) == {}


def test_one_chip_per_message_whatever_the_manifest_repeats():
    record = {"conversation_id": CONV, "server_message_id": "7.0", "anchor": "msg-7.0"}
    links = cc._chat_links_for([], "k", {"k": [record, dict(record)]}, {})
    assert len(links) == 1
