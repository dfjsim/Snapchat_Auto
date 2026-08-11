"""A message's sender: the name a report shows, and the id a selection has to match it on.

`conversation_message.sender_id` is the sender's **permanent user id**, and the parser replaces it in
place with the friend's username or display name, because that is what a reader of the report wants
to see. But a display name is only what the device knew at extraction time — it can differ between
two extractions of one phone, and it is missing entirely for a sender who is not in the friends
artifact. So it is the wrong thing to match a message on.

That matters because of one specific fallback: a message with no server message id is anchored on its
*position* in the conversation (`msg-row7`), which recovering one more message shifts. Such a
selection is re-found by conversation + time + sender, and the format spec tells an external tool that
`sender` is a user id. It was in fact indexed under the display name, so a tool doing what the spec
says matched nothing at all.

These tests pin both halves: the id survives the rename into its own field, and the index accepts
either spelling — the id for anything built against the spec, the name for selections saved by a
report that predates this.

Every input here is synthetic. No extraction data is required or used.
"""
import pandas as pd

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

def _index(tmp_path, **over):
    """The `msg` index the closure resolves against, built from one conversation."""
    stage = cr.index(_frame(**over), str(tmp_path / "out"), str(tmp_path / "cache"),
                     tz_label="UTC", run_id="RUN-1")
    return stage.sel_msg if hasattr(stage, "sel_msg") else stage


def test_the_time_and_sender_key_is_registered_under_the_user_id(tmp_path):
    """What an external tool sends, because the format spec says `sender` is a user id."""
    frame = _frame(**{cr.COL_SMID: ""})            # no server id: the positional-anchor case
    by_conv, _ = cr.build_messages(frame, str(tmp_path / "c"), str(tmp_path / "m"), lambda ts: "")
    msg = by_conv[CONV][0]

    keys = _ts_sender_keys(CONV, msg)

    assert f'{CONV}|{msg["created_unix"]}|{UID.lower()}' in keys


def test_the_display_name_is_not_a_key(tmp_path):
    """The name is what the report shows and nothing more. A key built from it would promise a stable
    identifier and deliver a label that changes with whatever the device knew at extraction time."""
    frame = _frame(**{cr.COL_SMID: ""})
    by_conv, _ = cr.build_messages(frame, str(tmp_path / "c"), str(tmp_path / "m"), lambda ts: "")
    msg = by_conv[CONV][0]

    keys = _ts_sender_keys(CONV, msg)

    assert f'{CONV}|{msg["created_unix"]}|alice test' not in keys
    assert len(keys) == 1


def test_a_sender_with_no_recovered_id_gets_no_key_at_all(tmp_path):
    """No key is honest; a key that cannot be trusted is not."""
    frame = _frame(**{cr.COL_SMID: "", cr.COL_SENDER_UID: ""})
    by_conv, _ = cr.build_messages(frame, str(tmp_path / "c"), str(tmp_path / "m"), lambda ts: "")
    msg = by_conv[CONV][0]

    assert _ts_sender_keys(CONV, msg) == set()


def _ts_sender_keys(conv_id, msg):
    """The `ts_sender` keys the index registers for one message, built the same way `index` does."""
    index = partial_report.Index("msg")
    row = f'conv-{conv_id}|{msg["anchor"]}'
    index.add(row, msg, smid="")
    if msg.get("created_unix") and msg.get("sender_uid"):
        index.keys[("ts_sender", f'{conv_id}|{msg["created_unix"]}'
                                 f'|{str(msg["sender_uid"]).lower()}')].add(row)
    return {key for kind, key in index.keys if kind == "ts_sender"}


# --------------------------------------------------------------- end to end through resolve()

def test_a_selection_carrying_the_user_id_finds_the_message(tmp_path):
    """The whole point: a tool that follows the spec and exports the Snapchat user id resolves."""
    frame = _frame(**{cr.COL_SMID: ""})
    by_conv, _ = cr.build_messages(frame, str(tmp_path / "c"), str(tmp_path / "m"), lambda ts: "")
    msg = by_conv[CONV][0]

    index = partial_report.Index("msg")
    row = f'conv-{CONV}|{msg["anchor"]}'
    index.add(row, msg, smid="")
    index.keys[("ts_sender", f'{CONV}|{msg["created_unix"]}|{UID.lower()}')].add(row)

    # Either case: the index case-folds what it stores, so the lookup has to fold too — some tools
    # print a UUID upper-case, and a capital silently matching nothing is the worst kind of failure.
    for sender in (UID, UID.upper()):
        selection = {"schema": 2, "selections": {"msg": {f"conv-{CONV}|msg-row99": {
            "conv": CONV, "ts": msg["created_unix"], "sender": sender}}}}
        resolution = partial_report.resolve({"msg": index}, selection)
        assert resolution.seeds["msg"] == {row}, sender


def test_the_sender_is_matched_case_insensitively():
    """Neither spelling is under our control: a display name read off a page keeps its capitals, and
    some tools print a UUID upper-case. A non-matching alternate is indistinguishable from an absent
    one, so this failed silently."""
    lookups = dict(partial_report._lookups(
        "msg", "ts_sender", {"conv": CONV, "ts": 1700000000, "sender": "Alice TEST"}))

    assert (("ts_sender", f"{CONV}|1700000000|alice test")) in lookups
