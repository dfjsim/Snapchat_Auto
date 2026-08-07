"""What counts as a message's own text, and what the parser must not drop silently.

Both behaviours here were bugs found by comparing a report against another tool on the same
extraction: media rows displayed the protobuf media id as if the user had typed it, and rows that
named no conversation left the report without saying so.

Every value here is invented — ids, tokens and message text alike. They only reproduce the *shape*
of what the corpus holds; nothing identifying a test device belongs in this repository.
"""
import sqlite3

from scripts import ParseSnapchat_iOS as parse_snapchat_ios
from scripts.conversations_report import _own_text

CONV = "11111111-2222-4333-8444-555555555555"
USER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CACHE_KEY = "0123456789abcdef0123456789abcdef"                 # 32 hex, like a real one
MEDIA_ID = "AbCdEfGhIjKlMnOpQrStU"                             # 21 base64url chars, like a real one


def _att(name):
    return {"name": name, "ext": "jpg", "kind": "image"}


# --------------------------------------------------------------- _own_text

def test_real_text_is_shown_whatever_it_looks_like():
    assert _own_text("Test", [], CONV, "1") == "Test"
    assert _own_text("https://link.snapchat.com/add-friends", [], CONV, "1") == \
        "https://link.snapchat.com/add-friends"


def test_a_caption_on_a_media_message_is_still_shown():
    """Gating on "is this a text message" would drop a caption the sender actually typed."""
    for raw_type in ("0", "2", "3", "5"):
        assert _own_text("nice one", [], CONV, raw_type) == "nice one"


def test_media_id_tokens_are_not_shown_as_message_text():
    """The shapes the older concatenating parser produced, which must never read as a message."""
    for token in (
        MEDIA_ID,                                              # bare media id
        MEDIA_ID + "\x04",                                     # ... with the trailing field byte
        MEDIA_ID + ".1020",                                    # ... with the content-type suffix
        CACHE_KEY,                                             # a cache key
        f"cm-chat-media-video-1:{CONV}:10:0",                  # an EXTERNAL_KEY
        USER,                                                  # a bare uuid
    ):
        assert _own_text(token, [], CONV) == "", f"{token!r} is not message text"


def test_control_characters_are_stripped_from_real_text():
    """A trailing protobuf field byte must not reach the report, but the text must survive."""
    assert _own_text("see you at eight\x04", [], CONV, "1") == "see you at eight"
    # newlines are part of the message and are kept
    assert _own_text("first\nsecond", [], CONV, "1") == "first\nsecond"


def test_a_message_that_is_only_its_attachment_name_is_not_text():
    assert _own_text(CACHE_KEY, [_att(CACHE_KEY)], CONV) == ""


# --------------------------------------------------- rows that name no conversation

def _arroyo(tmp_path, rows, with_server_column=False):
    path = tmp_path / "arroyo.db"
    conn = sqlite3.connect(path)
    extra = ", server_conversation_id TEXT" if with_server_column else ""
    conn.execute(f"""create table conversation_message (
        client_conversation_id TEXT, server_message_id INTEGER, message_content BLOB,
        creation_timestamp INTEGER, read_timestamp INTEGER, content_type INTEGER,
        sender_id TEXT{extra})""")
    conn.executemany(
        f"insert into conversation_message values (?,?,?,?,?,?,?{',?' if with_server_column else ''})",
        rows)
    conn.commit()
    conn.close()
    return str(path)


def _tag(field, wire):
    return bytes([(field << 3) | wire])


def _sub(field, payload):
    assert len(payload) < 128                                  # keeps the length a single byte
    return _tag(field, 2) + bytes([len(payload)]) + payload


def _varint(field, value):
    out, rest = b"", value
    while True:
        byte = rest & 0x7F
        rest >>= 7
        out += bytes([byte | (0x80 if rest else 0)])
        if not rest:
            return _tag(field, 0) + out


# A synthetic 16-byte user id. Deliberately not valid UTF-8, like the real ones: a value that does
# decode as text is picked up by the text parser and the row never reaches the event path at all.
_FAKE_USER_ID = b"\xff\xfe\xfd\xfc" * 4


def _saved_event(user_id=_FAKE_USER_ID, target=66):
    """A content_type 9 body: 4->4->8->7 = {1: {1: <user id>}, 2: <server_message_id>}."""
    seven = _sub(1, _sub(1, user_id)) + _varint(2, target)
    return _sub(4, _sub(4, _sub(8, _sub(7, seven))))


def _text_message(text):
    """A content_type 1 body: the message sits at 4.4.2.1."""
    return _sub(4, _sub(4, _sub(2, _sub(1, text.encode("utf-8")))))


def _media_with_caption(caption):
    """A content_type 2 body: the caption sits at 4.4.7.11.1, beside the media plumbing."""
    seven = _sub(11, _sub(1, caption.encode("utf-8")))
    return _sub(4, _sub(4, _sub(7, seven)))


# --------------------------------------------------- reading the text out of its own field

def test_a_text_message_body_is_read_from_its_field():
    assert parse_snapchat_ios.messageText(_text_message("on my way")) == "on my way"


def test_a_media_caption_is_recovered():
    """A caption typed on a photo is the message; it must not be lost with the media plumbing."""
    assert parse_snapchat_ios.messageText(_media_with_caption("look at this")) == "look at this"


def test_a_media_message_with_no_caption_yields_no_text():
    assert parse_snapchat_ios.messageText(_saved_event()) == ""
    assert parse_snapchat_ios.messageText(b"") == ""
    assert parse_snapchat_ios.messageText(None) == ""


def test_text_whose_bytes_are_also_valid_protobuf_is_still_read():
    """The regression that made a schema-guessing decoder unusable here.

    Some perfectly ordinary sentences are *also* well-formed protobuf, so a decoder without a schema
    reports the message body as a nested submessage — its letters reinterpreted as field numbers —
    and the text becomes unreachable. One message in the test corpus is like this. Reading the wire
    format directly has no such ambiguity.
    """
    import blackboxprotobuf
    text = "Absolutely not kid"                                # invented, but ambiguous the same way
    decoded, _typedef = blackboxprotobuf.decode_message(text.encode("utf-8"))
    assert isinstance(decoded, dict) and decoded, \
        "this test is pointless unless the text really is ambiguous"

    assert parse_snapchat_ios.messageText(_text_message(text)) == text


def test_emoji_survive_as_character_references():
    """The legacy report is written as cp1252, so text is stored with the rest as XML entities."""
    assert parse_snapchat_ios.messageText(_text_message("hey 😘")) == "hey &#128536;"


def test_proto_field_reads_the_last_value_of_a_repeated_field():
    """protobuf says the last one wins for a singular field."""
    blob = _sub(4, _sub(4, _sub(2, _sub(1, b"first") + _sub(1, b"second"))))
    assert parse_snapchat_ios.protoField(blob, (4, 4, 2, 1)) == b"second"


def test_proto_field_returns_none_for_a_path_that_is_not_there():
    assert parse_snapchat_ios.protoField(_text_message("hi"), (4, 4, 7, 11, 1)) is None
    assert parse_snapchat_ios.protoField(b"\xff\xff", (4,)) is None


# ------------------------------------------------- content_type 9 event messages

def test_a_saved_media_event_is_described_when_the_target_says_it_was_saved():
    blob = _saved_event(target=66)
    saved = {(CONV, 66): 1}                                    # conversation_message.is_saved
    assert parse_snapchat_ios.savedMediaEventText(blob, CONV, saved) == \
        "Saved the media of message 66 in this chat"


def test_a_saved_media_event_is_not_called_a_save_without_corroboration():
    """One decoded protobuf field is a lead; the database has to agree before it is a finding."""
    blob = _saved_event(target=66)
    for saved in ({}, {(CONV, 66): 0}):
        assert parse_snapchat_ios.savedMediaEventText(blob, CONV, saved) == \
            "Event message referring to message 66"


def test_an_unreadable_event_body_yields_no_description():
    assert parse_snapchat_ios.savedMediaEventText(b"\xff\xff\xff", CONV, {}) == ""
    assert parse_snapchat_ios.savedMediaEventText(None, CONV, {}) == ""


def test_a_text_less_message_is_not_reported_as_a_parse_failure(tmp_path):
    """The protobuf decodes; it simply holds no string. Calling that a parse error is untrue."""
    db = _arroyo(tmp_path, [(CONV, 73, _saved_event(target=66), 1784044448932, 0, 9, "sender")])

    df = parse_snapchat_ios.getChats(db)

    assert len(df) == 1
    assert not str(df.iloc[0]["message_content"]).startswith("ERROR")
    assert df.iloc[0]["message_content"] == "Event message referring to message 66"


def test_content_type_9_is_labelled_a_system_message_not_missing_media():
    assert parse_snapchat_ios.uncachedLabel(9) == "System message"
    assert parse_snapchat_ios.uncachedLabel(0) == "Media (no cached file)"
    assert parse_snapchat_ios.uncachedLabel(77) == "Unrecognised (content_type 77)"


def test_rows_naming_no_conversation_are_reported_not_silently_skipped(tmp_path, caplog):
    """A row the filter excludes has to be counted in the log, not vanish."""
    db = _arroyo(tmp_path, [
        (CONV, 1, b"", 1677004351000, 0, 1, "sender"),
        (None, 2, b"", 1677004352000, 0, 1, "sender"),        # no conversation at all
    ])
    caplog.clear()
    with caplog.at_level("WARNING"):
        excluded = parse_snapchat_ios.reportExcludedMessages(db, "client_conversation_id IS NOT NULL")
    assert excluded == 1
    assert "1 conversation_message row(s) name no conversation" in caplog.text


def test_a_row_with_only_a_server_conversation_id_is_kept(tmp_path):
    """When the app version records the server's id, that row is reported rather than dropped."""
    db = _arroyo(tmp_path, [
        (CONV, 1, b"", 1677004351000, 0, 1, "sender", None),
        (None, 2, b"", 1677004352000, 0, 1, "sender", "server-conv-id"),
    ], with_server_column=True)

    df = parse_snapchat_ios.getChats(db)

    assert len(df) == 2, "the server-identified row must not be filtered out"
    assert "server_conversation_id" in df.columns
    assert parse_snapchat_ios.reportExcludedMessages(
        db, "(client_conversation_id IS NOT NULL OR server_conversation_id IS NOT NULL)") == 0
