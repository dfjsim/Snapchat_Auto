"""How a message is identified — by a report, and by a selection that has to find it again.

Every row id in the selection scheme is meant to be a fact about the evidence. For messages that was
not quite true, in two ways that both failed silently:

* **the sender.** `conversation_message.sender_id` is the sender's permanent user id, and the parser
  replaces it in place with the friend's username or display name, because that is what a reader
  wants. The `ts_sender` alternate was then built from the *name* — while
  docs/selection_format.md told an external tool that `sender` is a user id, so a tool doing exactly
  what the spec said matched nothing. A name is also the wrong thing to match on: it is only what the
  device knew at extraction time, it differs between two extractions of one phone, and it is absent
  for a sender who is not in the friends artifact.
* **the anchor.** A message with no *server* message id was anchored on its **position** in the
  conversation, which recovering one more message shifts — and the shifted string still exists, so an
  exact match handed over a *different* message than the one ticked, reporting "its own id" as its
  reason. It now takes the device's own `client_message_id` instead, and a position — the last resort,
  for a message with neither id — is never matched on at all.

So these tests cover what a message is anchored on, that a position is never honoured, that the sender
key is the permanent id and nothing else, and that both spellings of a server message id resolve.

Every input here is synthetic. No extraction data is required or used.
"""
import pandas as pd
import pytest

from scripts import conversations_report as cr
from scripts import partial_report

CONV = "aaaa0000-0000-4000-8000-00000000000a"
UID = "11111111-2222-3333-4444-555555555555"


def _frame(**over):
    """One parsed message row, in the shape ParseSnapchat_iOS hands over."""
    row = {cr.COL_CONV: CONV,
           cr.COL_SENDER: "Alice Test",                    # what fixSenders replaced the id with
           cr.COL_SENDER_UID: UID,                         # what it kept
           cr.COL_CONTENT: "hello",
           cr.COL_TEXT: "hello",
           cr.COL_TYPE: "Text",
           cr.COL_CREATED: "2026-01-02 09:30:00",
           cr.COL_READ: "",
           cr.COL_SMID: "12.0"}
    row.update(over)
    return pd.DataFrame([row])


def _messages(frame, tmp_path):
    by_conv, _stats = cr.build_messages(frame, str(tmp_path / "cache"), str(tmp_path / "media"),
                                        lambda ts: "")
    return by_conv[CONV]


# --------------------------------------------------------------- the id survives the rename

def test_the_sender_name_is_shown_and_the_user_id_is_kept_beside_it(tmp_path):
    msg = _messages(_frame(), tmp_path)[0]

    assert msg["sender"] == "Alice Test", "the name is what the report displays"
    assert msg["sender_uid"] == UID, "and the permanent id is what a later run matches it on"


def test_a_frame_from_an_older_build_has_no_id_and_still_parses(tmp_path):
    """The column is absent from a frame produced before it existed, and the fallback to the name is
    exactly the behaviour that was there before."""
    frame = _frame().drop(columns=[cr.COL_SENDER_UID])
    msg = _messages(frame, tmp_path)[0]

    assert msg["sender"] == "Alice Test" and msg["sender_uid"] == ""


# --------------------------------------------------------------- both spellings resolve

def test_the_time_and_sender_key_is_registered_under_the_user_id(tmp_path):
    """What an external tool sends, because the format spec says `sender` is a user id."""
    frame = _frame(**{cr.COL_SMID: ""})            # no server id: the positional-anchor case
    by_conv, _ = cr.build_messages(frame, str(tmp_path / "c"), str(tmp_path / "m"), lambda ts: "")
    msg = by_conv[CONV][0]

    keys = _ts_sender_keys(CONV, msg)

    assert f'{CONV}|{int(msg["created_unix"])}|{UID.lower()}' in keys


def test_the_display_name_is_not_a_key(tmp_path):
    """The name is what the report shows and nothing more. A key built from it would promise a stable
    identifier and deliver a label that changes with whatever the device knew at extraction time."""
    frame = _frame(**{cr.COL_SMID: ""})
    by_conv, _ = cr.build_messages(frame, str(tmp_path / "c"), str(tmp_path / "m"), lambda ts: "")
    msg = by_conv[CONV][0]

    keys = _ts_sender_keys(CONV, msg)

    assert f'{CONV}|{int(msg["created_unix"])}|alice test' not in keys
    assert len(keys) == 1


def test_a_sender_with_no_recovered_id_gets_no_key_at_all(tmp_path):
    """No key is honest; a key that cannot be trusted is not."""
    frame = _frame(**{cr.COL_SMID: "", cr.COL_SENDER_UID: ""})
    by_conv, _ = cr.build_messages(frame, str(tmp_path / "c"), str(tmp_path / "m"), lambda ts: "")
    msg = by_conv[CONV][0]

    assert _ts_sender_keys(CONV, msg) == set()


def _ts_sender_keys(conv_id, msg):
    """The `ts_sender` keys the index registers for one message.

    The key is spelled by `partial_report.ts_sender_key`, which is also what the *lookup* uses -- never
    rebuilt here. Rebuilding it in the test is what hid the float: both sides of the comparison were
    written the same way, so they agreed with each other and with nothing an external tool sends.
    """
    index = partial_report.Index("msg")
    row = f'conv-{conv_id}|{msg["anchor"]}'
    index.add(row, msg, smid="")
    key = partial_report.ts_sender_key(conv_id, msg.get("created_unix"), msg.get("sender_uid"))
    if key:
        index.keys[("ts_sender", key)].add(row)
    return {key for kind, key in index.keys if kind == "ts_sender"}


# --------------------------------------------------------------- what a message is anchored on

def test_a_message_with_no_server_id_is_anchored_on_the_devices_own_id(tmp_path):
    """`client_message_id` is evidence exactly as the server id is, and unique within a conversation —
    so it keeps the anchor a fact about the row instead of a fact about how many rows were recovered
    before it."""
    frame = _frame(**{cr.COL_SMID: "", cr.COL_CMID: "7"})
    msg = _messages(frame, tmp_path)[0]

    assert msg["anchor"] == "msg-c7"


def test_position_is_only_the_last_resort(tmp_path):
    frame = _frame(**{cr.COL_SMID: "", cr.COL_CMID: ""})
    msg = _messages(frame, tmp_path)[0]

    assert msg["anchor"] == "msg-row0"


def test_a_server_id_still_wins_over_the_device_one(tmp_path):
    """Unchanged for every message that has one, which is every message in the corpus."""
    msg = _messages(_frame(**{cr.COL_CMID: "7"}), tmp_path)[0]

    assert msg["anchor"] == "msg-12.0"


# --------------------------------------------------------------- a position is never a match

def _positional_index(row_id):
    index = partial_report.Index("msg")
    index.add(row_id, {}, smid="")
    return index


def test_a_positional_id_is_not_honoured_even_when_it_still_exists():
    """The false positive this exists to stop. Recovering one more message shifts every later
    position, so the identical string names a different message — and an exact match would hand that
    over with «its own id» as its reason."""
    row = f"conv-{CONV}|msg-row7"
    index = _positional_index(row)
    selection = {"schema": 2, "selections": {"msg": {row: {"conv": CONV}}}}

    result = partial_report.resolve({"msg": index}, selection, unresolved="drop")

    assert result.seeds["msg"] == set()
    assert len(result.unresolved) == 1
    why = result.unresolved[0]["why"]
    assert "only the message's position" in why and "Re-tick" in why


def test_a_positional_id_is_still_resolved_by_what_was_recorded_with_it():
    """Refusing is the fallback's failure mode, not its purpose: the time and the sender's user id
    identify the message wherever it has moved to."""
    index = _positional_index(f"conv-{CONV}|msg-row7")
    index.keys[("ts_sender", f"{CONV}|1700000000|{UID.lower()}")].add(f"conv-{CONV}|msg-row7")
    selection = {"schema": 2, "selections": {"msg": {f"conv-{CONV}|msg-row2": {
        "conv": CONV, "ts": 1700000000, "sender": UID}}}}

    result = partial_report.resolve({"msg": index}, selection)

    assert result.seeds["msg"] == {f"conv-{CONV}|msg-row7"}


def test_an_id_that_is_evidence_is_still_honoured_exactly():
    """Only the positional form loses the exact-match shortcut. A client-message-id anchor is a fact
    about the row, so it keeps it — otherwise this change would cost every unsent message its id."""
    for row in (f"conv-{CONV}|msg-12.0", f"conv-{CONV}|msg-c7"):
        index = _positional_index(row)
        selection = {"schema": 2, "selections": {"msg": {row: {"conv": CONV}}}}

        result = partial_report.resolve({"msg": index}, selection)

        assert result.seeds["msg"] == {row}, row
        assert result.how[("msg", row)] == "its own id"


def test_the_deduplicating_suffix_does_not_hide_a_position():
    """Two messages can land on one anchor, and the second takes a «-2» suffix. That is still a
    position, so the pattern has to see through the suffix."""
    row = f"conv-{CONV}|msg-row7-2"
    index = _positional_index(row)
    selection = {"schema": 2, "selections": {"msg": {row: {"conv": CONV}}}}

    result = partial_report.resolve({"msg": index}, selection, unresolved="drop")

    assert result.seeds["msg"] == set()


# --------------------------------------------------------------- end to end through resolve()

def test_a_selection_carrying_the_user_id_finds_the_message(tmp_path):
    """The whole point: a tool that follows the spec and exports the Snapchat user id resolves."""
    frame = _frame(**{cr.COL_SMID: ""})
    by_conv, _ = cr.build_messages(frame, str(tmp_path / "c"), str(tmp_path / "m"), lambda ts: "")
    msg = by_conv[CONV][0]

    index = partial_report.Index("msg")
    row = f'conv-{CONV}|{msg["anchor"]}'
    index.add(row, msg, smid="")
    index.keys[("ts_sender",
                partial_report.ts_sender_key(CONV, msg["created_unix"], UID))].add(row)

    # Either case: the index case-folds what it stores, so the lookup has to fold too — some tools
    # print a UUID upper-case, and a capital silently matching nothing is the worst kind of failure.
    for sender in (UID, UID.upper()):
        selection = {"schema": 2, "selections": {"msg": {f"conv-{CONV}|msg-row99": {
            "conv": CONV, "ts": msg["created_unix"], "sender": sender}}}}
        resolution = partial_report.resolve({"msg": index}, selection)
        assert resolution.seeds["msg"] == {row}, sender


def test_a_whole_second_and_a_float_are_the_same_instant(tmp_path):
    """The report's own time is a float (datetime.timestamp()), and a tool exporting the integer the
    format documents is doing exactly as it is told. Both have to reach the same row: this failed
    silently, because a non-matching alternate looks exactly like one that was never sent."""
    frame = _frame(**{cr.COL_SMID: ""})
    by_conv, _ = cr.build_messages(frame, str(tmp_path / "c"), str(tmp_path / "m"), lambda ts: "")
    msg = by_conv[CONV][0]
    assert isinstance(msg["created_unix"], float), "the premise: the report's value is a float"

    index = partial_report.Index("msg")
    row = f'conv-{CONV}|{msg["anchor"]}'
    index.add(row, msg, smid="")
    index.keys[("ts_sender",
                partial_report.ts_sender_key(CONV, msg["created_unix"], UID))].add(row)

    for ts in (msg["created_unix"], int(msg["created_unix"]), str(int(msg["created_unix"]))):
        selection = {"schema": 2, "selections": {"msg": {f"conv-{CONV}|msg-row99": {
            "conv": CONV, "ts": ts, "sender": UID}}}}
        assert partial_report.resolve({"msg": index}, selection).seeds["msg"] == {row}, ts


def test_the_real_index_records_the_senders_contact_row(tmp_path):
    """The `msg_sender` relation follows an edge, and an edge no generator records finds nothing —
    silently. So this goes through the real `index()` rather than a hand-built one, and checks the
    anchor is the Contacts report's own (taken from contact_link_index, not derived a second time).
    """
    friends = pd.DataFrame([{"Display name": "Alice Test", "Username": "alice-test",
                             "User ID": UID, "Conversation ID": ""}])
    stage = cr.index(_frame(), friends, None, str(tmp_path / "Conversations"),
                     str(tmp_path / "cache"), report_dir=str(tmp_path))
    sel_msg = stage["indexes"]["msg"]

    senders = [(src, dst) for edge, src, kind, dst in sel_msg.edges
               if edge == partial_report.EDGE_MESSAGE_SENDER and kind == "ct"]

    assert senders and senders[0][1] == f"ct-{UID}"
    assert senders[0][0].startswith(f"conv-{CONV}|msg-")


def test_a_message_whose_sender_id_was_not_recovered_links_to_no_contact(tmp_path):
    """Rather than to a contact matched on the display name, which is not an identifier."""
    friends = pd.DataFrame([{"Display name": "Alice Test", "Username": "alice-test",
                             "User ID": UID, "Conversation ID": ""}])
    stage = cr.index(_frame(**{cr.COL_SENDER_UID: ""}), friends, None,
                     str(tmp_path / "Conversations"), str(tmp_path / "cache"),
                     report_dir=str(tmp_path))

    assert not [e for e in stage["indexes"]["msg"].edges if e[0] == partial_report.EDGE_MESSAGE_SENDER]


def test_the_key_carries_no_fractional_second():
    """Pinned on the spelling itself, so the float cannot come back through either side."""
    assert partial_report.ts_sender_key(CONV, 1700000000.0, UID) == f"{CONV}|1700000000|{UID.lower()}"
    assert partial_report.ts_sender_key(CONV, 1700000000, UID) == f"{CONV}|1700000000|{UID.lower()}"


def test_a_key_with_a_missing_or_unusable_part_is_no_key():
    """No key is honest; a key that cannot be trusted is not."""
    assert partial_report.ts_sender_key(CONV, None, UID) == ""
    assert partial_report.ts_sender_key(CONV, "Unknown", UID) == ""
    assert partial_report.ts_sender_key(CONV, 1700000000, "") == ""
    assert partial_report.ts_sender_key("", 1700000000, UID) == ""


def test_the_sender_is_matched_case_insensitively():
    """Neither spelling is under our control: a display name read off a page keeps its capitals, and
    some tools print a UUID upper-case. A non-matching alternate is indistinguishable from an absent
    one, so this failed silently."""
    lookups = dict(partial_report._lookups(
        "msg", "ts_sender", {"conv": CONV, "ts": 1700000000, "sender": "Alice TEST"}))

    assert (("ts_sender", f"{CONV}|1700000000|alice test")) in lookups


# ------------------------------------------------- naming a row the way another tool can

def test_a_bare_server_message_id_resolves_as_well_as_the_rendered_one():
    """The report renders `<message>.<part>`, and the part is ours to add: arroyo.db holds the number.
    A tool reading it out of the database has no reason to know which spelling we chose."""
    row = f"conv-{CONV}|msg-12.0"
    index = partial_report.Index("msg")
    index.add(row, {}, smid=f"{CONV}|12.0")
    index.keys[("smid", f"{CONV}|12")].add(row)

    for smid in ("12.0", "12"):
        selection = {"schema": 2, "selections": {"msg": {f"conv-{CONV}|msg-whatever": {
            "conv": CONV, "smid": smid}}}}
        assert partial_report.resolve({"msg": index}, selection).seeds["msg"] == {row}, smid


def test_a_bare_id_matching_two_parts_of_a_message_names_neither():
    """Two parts share the message number, so the bare form identifies no single row — and a part
    chosen for the examiner is a part they did not select. Ambiguity refuses whatever `unresolved`
    says: dropping a row loses evidence, but including the wrong one over-discloses."""
    index = partial_report.Index("msg")
    for part in ("12.0", "12.1"):
        row = f"conv-{CONV}|msg-{part}"
        index.add(row, {}, smid=f"{CONV}|{part}")
        index.keys[("smid", f"{CONV}|12")].add(row)

    selection = {"schema": 2, "selections": {"msg": {f"conv-{CONV}|msg-gone": {
        "conv": CONV, "smid": "12"}}}}

    with pytest.raises(partial_report.AmbiguousSelection, match="matches 2 rows"):
        partial_report.resolve({"msg": index}, selection, unresolved="drop")
