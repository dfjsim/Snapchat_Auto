"""Report associations that the test extractions do not exercise.

Two of them are only visible on a device that has group chats or duplicated cached content, and
both are the kind of thing that fails silently — a contact shown with one of their three
conversations, or a link that lands on the first of five matching rows. They are pinned here so
they cannot regress on a corpus that never shows them.

Every input is synthetic. No extraction data is required or used.
"""
from scripts import contacts_report, report_ui


def _conv(title, kind="Group", messages=0, participants=(), page=None, **kw):
    return dict({
        "page": page or f"pages/{title}.html", "title": title, "kind": kind,
        "messages": messages, "attachments": 0, "first": "", "last": "",
        "first_sort": 0, "last_sort": 0,
        "participants": [dict({"user_id": "", "username": "", "display": "", "raw": "",
                               "label": "", "is_owner": False}, **p) for p in participants],
    }, **kw)


def _contact(user_id="", username="", display="", conv_id=""):
    return {"display": display, "username": username, "user_id": user_id,
            "legacy_username": "", "conv_id": conv_id, "is_owner": False}


UID = "11111111-2222-3333-4444-555555555555"


def test_a_contact_in_group_chats_lists_every_conversation():
    """The friends artifact names one conversation; membership names the rest."""
    index = {
        "private-1": _conv("Private chat", kind="Private", messages=4,
                           participants=[{"user_id": UID, "username": "someone"}]),
        "group-a": _conv("Group A", messages=9, participants=[{"user_id": UID}]),
        "group-b": _conv("Group B", messages=2, participants=[{"user_id": UID}]),
        "unrelated": _conv("Other", participants=[{"user_id": "99999999-0000-0000-0000-0"}]),
    }
    convs = contacts_report.contact_conversations(
        _contact(user_id=UID, username="someone", conv_id="private-1"), index)

    assert [c["id"] for c in convs] == ["private-1", "group-a", "group-b"]
    # the friends artifact's own conversation leads and is labelled as such; the rest follow by size
    assert convs[0]["why"] == "friends"
    assert {c["why"] for c in convs[1:]} == {"participant"}


def test_membership_follows_the_user_id_even_when_the_names_differ():
    """The user id is permanent, so a renamed account is still the same member."""
    index = {"g": _conv("Group", participants=[{"user_id": UID, "username": "the-new-name"}])}
    convs = contacts_report.contact_conversations(
        _contact(user_id=UID, username="the-old-name", display="Something Else"), index)
    assert [c["id"] for c in convs] == ["g"]


def test_a_username_match_is_not_enough_to_claim_membership():
    """A username can be changed and reused, so it must never attribute a conversation."""
    index = {"g": _conv("Group", participants=[{"raw": "someone", "username": "someone"}])}
    assert contacts_report.contact_conversations(_contact(username="someone"), index) == []


def test_a_display_name_match_is_not_enough_either():
    """Display names are local to this device and two accounts can share one."""
    index = {"g": _conv("Group", participants=[{"display": "Chris"}])}
    assert contacts_report.contact_conversations(_contact(display="Chris"), index) == []


def test_a_contact_with_no_user_id_keeps_only_the_friends_list_conversation():
    """The deliberate cost of matching on the user id alone: incomplete, never wrong."""
    index = {"known": _conv("Private", kind="Private"),
             "g": _conv("Group", participants=[{"username": "someone"}])}
    convs = contacts_report.contact_conversations(
        _contact(username="someone", conv_id="known"), index)
    assert [c["id"] for c in convs] == ["known"]


def test_a_contact_with_no_conversation_gets_an_empty_list():
    index = {"g": _conv("Group", participants=[{"user_id": "someone-else"}])}
    assert contacts_report.contact_conversations(_contact(user_id=UID), index) == []


def test_a_conversation_id_the_chat_database_does_not_have_is_not_invented():
    """A friends-list id with no conversation behind it must not become a row that links nowhere."""
    assert contacts_report.contact_conversations(_contact(conv_id="ghost"), {}) == []


def test_contact_detail_names_every_conversation_and_its_id():
    convs = [_conv("Group A", messages=9) | {"id": "group-a", "why": "participant"},
             _conv("Group B", messages=2) | {"id": "group-b", "why": "participant"}]
    html = contacts_report._contact_detail(_contact(user_id=UID), convs, "../")
    assert "group-a" in html and "group-b" in html
    assert "Conversations (2)" in html


def test_find_fragment_carries_every_token_once():
    """A multi-target link must reach all of its targets, and say each one only once."""
    assert report_ui.find_fragment(["abc", "def"]) == "#find=abc|def"
    assert report_ui.find_fragment(["abc", "ABC", "abc"]) == "#find=abc"
    assert report_ui.find_fragment(["a b/c"]) == "#find=a%20b%2Fc"


def test_find_fragment_is_empty_when_there_is_nothing_to_find():
    """Callers fall back to a plain link rather than emitting a fragment that matches everything."""
    assert report_ui.find_fragment([]) == ""
    assert report_ui.find_fragment([None, "", "  "]) == ""


def test_the_table_search_ors_the_parts_of_a_find_query():
    """The OR split is what makes one link land on several rows — pinned on the shipped JS."""
    assert "function terms(q)" in report_ui.VTABLE_JS
    assert "split('|')" in report_ui.VTABLE_JS
    assert "findAll:findAll" in report_ui.VTABLE_JS
    # and the fragment has to be routed to it, or every such link silently does nothing
    assert "'find='" in report_ui.NAV_JS
