"""What a conversation_message row is: its content type, and what its message_content body holds.

Every value here is invented — ids, names, coordinates and text alike. The blobs only reproduce the
*shape* of message_content (4 = envelope, 4.2 = content type, 4.4 = body); nothing identifying a test
device belongs in this repository.
"""
import struct
import uuid

import pandas as pd

from scripts import ParseSnapchat_iOS as parse_snapchat_ios
from scripts import conversations_report as cr
from scripts.data import arroyo_content as ac

USER = uuid.UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
OTHER = uuid.UUID("11111111-2222-4333-8444-555555555555")
CONV = "99999999-8888-4777-8666-555555555555"


def _key(number, wire):
    return _raw_varint((number << 3) | wire)


def _raw_varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _v(number, value):
    return _key(number, 0) + _raw_varint(value)


def _b(number, payload):
    return _key(number, 2) + _raw_varint(len(payload)) + payload


def _d(number, value):
    return _key(number, 1) + struct.pack("<d", value)


def _u(number, user):
    return _b(number, _b(1, user.bytes))


def _row(body, content_type=None):
    """message_content: 4 = envelope {2: content type (left out when 0), 4: body}."""
    envelope = (_v(2, content_type) if content_type else b"") + _b(4, body)
    return _v(1, 42) + _b(4, envelope)


def _event(number, fields, content_type=6):
    return _row(_b(8, _b(number, fields)), content_type)


# --------------------------------------------------------------------------- content_type

def test_every_content_type_has_a_name_and_a_category():
    for value, (label, category) in ac.CONTENT_TYPES.items():
        assert label and category in ("media", "text", "share", "sticker", "event", "other"), value
    assert ac.content_type_label(1) == "Text"
    assert ac.content_type_label("13") == "Missed audio call"
    assert ac.content_type_label(7) == "" and ac.content_type_label(None) == ""


def test_the_type_stored_in_the_blob_is_read_and_zero_when_absent():
    assert ac.embedded_content_type(_row(_b(2, _b(1, b"hi")), 1)) == 1
    assert ac.embedded_content_type(_row(_b(11, b""))) == 0           # a Snap: the field is left out
    assert ac.embedded_content_type(b"\xff\xff") is None
    assert ac.embedded_content_type(None) is None


# --------------------------------------------------------------------------- app events

def test_a_deleted_message():
    assert ac.describe(_event(5, _u(1, USER) + _v(2, 1))) == f"{USER} deleted a chat message"
    assert ac.describe(_event(5, _u(1, USER) + _v(2, 2))) == f"{USER} deleted a Snap"


def test_calls_name_their_kind_status_and_duration():
    ended = _v(1, 1) + _v(2, 1) + _u(3, USER) + _v(4, 125_000)
    assert ac.describe(_event(2, ended)) == f"Video call ended — {USER}, duration 2 min 5 s"
    missed = _v(1, 4) + _u(3, OTHER)                          # callType 0 = audio, left out
    assert ac.describe(_event(2, missed, 13)) == f"Audio call missed — {OTHER}"


def test_a_save_to_the_camera_roll_names_what_and_from_which_message():
    counts = _b(3, _v(1, 1) + _v(2, 1)) + _b(3, _v(1, 2) + _v(2, 2))
    text = ac.describe(_event(7, _u(1, OTHER) + _v(2, 66) + counts, 9))
    assert text == f"{OTHER} saved 1 photo, 2 videos from message 66 to the camera roll"


def test_screen_capture():
    shot = _u(1, OTHER) + _v(2, 0) + _v(3, 0)
    assert ac.describe(_event(1, shot, 10)) == f"{OTHER} took a screenshot of the chat"
    record = _u(1, OTHER) + _v(2, 1) + _v(3, 2)
    assert ac.describe(_event(1, record, 11)) == f"{OTHER} screen-recorded the group profile"


def test_group_membership_and_names():
    added = _b(1, _u(1, OTHER) + _v(2, 0) + _v(3, 2)) + _u(3, USER)
    assert ac.describe(_event(3, added)) == \
        f"{OTHER} was added to the group (invite link) — by {USER}"
    removed = _b(1, _u(1, OTHER) + _v(2, 2) + _v(4, 2))
    assert ac.describe(_event(3, removed)) == f"{OTHER} was removed from the group"
    created = _u(1, USER) + _u(2, USER) + _u(2, OTHER) + _b(3, "Trip 🏔".encode())
    assert ac.describe(_event(6, created)) == \
        f"{USER} created the group “Trip 🏔” with 2 participant(s)"


def test_a_change_to_when_messages_delete():
    dynamic = _v(4, 86400) + _v(3, 7 * 86400)
    text = ac.describe(_event(8, _u(1, USER) + _b(2, _b(1, dynamic))))
    assert text == (f"{USER} changed when this chat's messages delete: viewed messages delete "
                    f"after 1 day, unviewed messages after 7 days")


def test_fixed_wording_events_and_an_unknown_event():
    assert ac.describe(_event(22, b"")) == "My AI welcome message"
    assert ac.describe(_event(21, _v(1, 2) + _v(2, 40))) == "Streak ended (40 days)"
    assert ac.describe(_event(99, b"")) == "App event 4.4.8.99 (not described)"
    assert ac.event_number(_event(99, b"")) == 99
    assert ac.event_number(_row(_b(2, _b(1, b"hi")), 1)) is None


# --------------------------------------------------------------------------- other bodies

def test_plain_text_media_and_snaps_need_no_description():
    assert ac.describe(_row(_b(2, _b(1, b"hi")), 1)) == ""
    assert ac.describe(_row(_b(3, b""), 2)) == ""
    assert ac.describe(_row(_b(11, b""))) == ""


def test_a_reply_says_whose_media_it_carries_and_keeps_its_text():
    blob = _row(_b(7, _b(3, b"") + _b(11, _b(1, b"so funny"))), 2)
    assert ac.describe(blob).startswith("Reply to a Snap or Story — the media is the Snap replied to")
    assert ac.message_text(blob) == "so funny"


def test_shares():
    assert ac.describe(_row(_b(5, _b(5, _b(1, b"story-id"))), 3)) == "Shared a Story"
    pin = _d(1, 48.858370) + _d(2, 2.294481) + _b(6, b"Tower")
    assert ac.describe(_row(_b(5, _b(18, pin)), 3)) == \
        "Shared a map pin at 48.858370, 2.294481 “Tower”"
    assert ac.describe(_row(_b(5, _b(24, _b(2, b""))), 3)) == "Shared a saved Story"


def test_a_saved_story_names_who_posted_it():
    """.1 and the Snap's own .2.18.1 both name the poster; stated only when they agree."""
    snap = _b(18, _b(1, str(OTHER).encode()))
    agree = _b(24, _u(1, OTHER) + _b(2, snap))
    assert ac.describe(_row(_b(5, agree), 3)) == f"Shared a saved Story posted by {OTHER}"
    only_share = _b(24, _u(1, OTHER))
    assert ac.describe(_row(_b(5, only_share), 3)) == f"Shared a saved Story posted by {OTHER}"
    disagree = _b(24, _u(1, USER) + _b(2, snap))
    assert ac.describe(_row(_b(5, disagree), 3)) == \
        f"Shared a saved Story (story id names {USER}, the Snap names {OTHER})"
    # and the name, as for any user id in a description
    assert ac.name_users(ac.describe(_row(_b(5, agree), 3)), {str(OTHER): "bob"}) == \
        "Shared a saved Story posted by bob"
    assert ac.describe(_row(_b(5, _b(50, b"")), 3)) == "Shared content (4.4.5.50, not described)"


def test_poll_voice_note_and_bot_text():
    poll = _b(1, b"Pizza?") + _b(2, b"yes") + _b(2, b"no")
    assert ac.describe(_row(_b(26, poll), 37)) == "Poll: “Pizza?” — options: yes / no"
    assert ac.describe(_row(_b(6, _b(1, b"")), 4)) == "Voice note"
    bot = _v(1, 4) + _b(2, _b(1, _b(1, b"first part"))) + _b(2, _b(1, _b(1, b"second")))
    assert ac.message_text(_row(_b(24, bot), 34)) == "first part\nsecond"
    assert ac.message_text(_row(_b(19, _b(1, _b(1, b"tiny"))), 26)) == "tiny"


def test_an_unreadable_blob_is_not_described():
    for blob in (None, "", b"", b"\xff\xff\xff"):
        assert ac.describe(blob) == ""
        assert ac.message_text(blob) == ""


def test_user_ids_are_named_when_known():
    text = f"{USER} deleted a chat message — {OTHER}"
    names = {str(USER): "alice"}
    assert ac.name_users(text, names) == f"alice deleted a chat message — {OTHER}"
    assert ac.name_users(text.upper(), names).startswith("alice ")
    assert ac.name_users(text, {}) == text


# --------------------------------------------------------------------------- the parser and reports

def test_fix_senders_keeps_the_ids_an_event_names():
    """Names change; the permanent id must survive. The body keeps it, the legacy copy shows both."""
    event = f"{USER} deleted a chat message"
    df = pd.DataFrame({"sender_id": [str(USER), "x"], "message_content": [event, "hello"],
                       "message_body": [event, ""]})
    friends = pd.DataFrame({"User ID": [str(USER)], "Username": ["<b>alice</b>"]})

    out = parse_snapchat_ios.fixSenders(df, friends, pd.DataFrame({}))

    assert list(out["message_body"]) == [event, ""]
    assert list(out["message_content"]) == [f"alice ({USER}) deleted a chat message", "hello"]
    assert list(out["sender_user_id"]) == [str(USER), "x"]


def test_the_report_names_each_person_and_keeps_their_full_id():
    links = {str(USER): {"href": "Contacts/Contacts_report.html#ct-a", "anchor": "ct-a",
                         "display": "Alice", "username": "alice", "user_id": str(USER),
                         "is_owner": False}}
    named, people = cr._body_people(f"{USER} added {OTHER} — by {USER}", links)

    assert named == f"Alice (alice) added {OTHER} — by Alice (alice)"
    assert [(p["label"], p["user_id"]) for p in people] == \
        [("Alice (alice)", str(USER)), (str(OTHER), str(OTHER))]

    conv = {"id": CONV, "server_id": ""}
    msg = {"text": "", "parse_error": False, "atts": [], "body": f"{USER} deleted a Snap",
           "body_named": named, "body_people": people[:1], "sender": "Alice", "sender_uid": str(USER),
           "direction": "Received", "smid": "7", "cmid": "", "types": ["System message"],
           "raw_types": ["6"], "raw_text": "", "created_utc": "", "created": "", "read_utc": "",
           "read": ""}
    detail = cr._message_detail(msg, conv, contact_links=links)
    assert detail.count(str(USER)) >= 2, "the full id is shown for the person and for the sender"
    assert "Contacts/Contacts_report.html#ct-a" in detail


def test_a_share_is_labelled_shared_content(monkeypatch):
    monkeypatch.setattr(parse_snapchat_ios, "uuid", "owner-id", raising=False)
    cache_df = pd.DataFrame(columns=["CACHE_KEY", "EXTERNAL_KEY", "MEDIA_CONTEXT_TYPE", "USER_ID"])
    cache_arroyo_df = pd.DataFrame(
        columns=["client_conversation_id", "server_message_id", "message_content", "content_type"])
    chats_df = pd.DataFrame([{"client_conversation_id": CONV, "client_message_id": 1,
                              "server_message_id": 7, "message_content": "x",
                              "Creation Timestamp": "2023-02-21 18:40:50", "Read Timestamp": "",
                              "content_type": 3, "sender_id": "someone"}])

    result = parse_snapchat_ios.mergeCacheChats(cache_df, chats_df, None, cache_arroyo_df)

    assert list(result["Content Type"]) == ["Shared content"]


def _frame(rows):
    return pd.DataFrame([{
        cr.COL_CONV: CONV, cr.COL_SENDER: "someone", cr.COL_CONTENT: content, cr.COL_TYPE: ctype,
        cr.COL_BODY: body, cr.COL_CREATED: "2023-02-21 18:40:50", cr.COL_READ: "",
        cr.COL_SMID: smid, cr.COL_TEXT: "",
    } for smid, ctype, content, body in rows])


def test_a_share_with_no_file_is_kept_when_it_is_the_whole_message(tmp_path):
    """A map pin has no media at all; it used to vanish from the report as a "duplicate"."""
    frame = _frame([("7", "Shared content", "not-a-file", "Shared a map pin at 1.000000, 2.000000")])

    by_conv, stats = cr.build_messages(frame, str(tmp_path / "c"), str(tmp_path / "m"),
                                       lambda ts: "")

    assert stats["dropped"] == 0
    [message] = by_conv[CONV]
    assert message["body"] == "Shared a map pin at 1.000000, 2.000000"


def test_a_fileless_share_row_is_dropped_only_beside_another_row_of_its_message(tmp_path):
    frame = _frame([("7.0", "Shared content", "not-a-file", "Shared a Story"),
                    ("7.0", "Media saved in chat", "also-not-a-file", "Shared a Story")])

    by_conv, stats = cr.build_messages(frame, str(tmp_path / "c"), str(tmp_path / "m"),
                                       lambda ts: "")

    assert stats["dropped"] == 1
    assert len(by_conv[CONV]) == 1


def test_a_camera_roll_save_links_to_the_message_it_saved():
    blob = _event(7, _u(1, OTHER) + _v(2, 66), 9)
    assert ac.referenced_message(blob) == 66
    assert ac.referenced_message(_event(5, _u(1, USER))) is None
    assert ac.referenced_message(_row(_b(2, _b(1, b"hi")), 1)) is None

    target = {"smid": "66.0", "anchor": "msg-66.0"}
    event = {"text": "", "parse_error": False, "atts": [], "body": "saved", "ref": "66",
             "sender": "", "sender_uid": "", "direction": "", "smid": "73.0", "cmid": "",
             "types": [], "raw_types": [], "raw_text": "", "created_utc": "", "created": "",
             "read_utc": "", "read": ""}
    listed = cr._message_detail(event, {"id": CONV, "server_id": "", "messages": [target, event]})
    assert 'href="#msg-66.0"' in listed
    absent = cr._message_detail(event, {"id": CONV, "server_id": "", "messages": [event]})
    assert "not listed in this report" in absent and "#msg-66" not in absent


def test_the_raw_content_type_is_shown_with_its_name():
    assert cr._raw_type_named("6") == "6 (App event)"
    assert cr._raw_type_named("77") == "77"
