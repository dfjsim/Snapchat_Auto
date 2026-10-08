"""A cached file is tied to the chat message it belongs to, not only when it is the one file the chat
report displayed.

A message names its media by ids — the media id of its ``local_message_references``, a shared item's
id, a sticker's id — and a claim key names a message by conversation and number. Every claim that
carries one of those is another cached file of that message (a thumbnail, the full media, an edit's
state), and links to it; one that carries none links to nothing.

A key naming a message no row is there for still names its conversation: the entry is tied to the
conversation — a link when the Conversations report lists it, a stated fact when it does not —
and only from what arroyo.db holds in both readings, read from arroyo.db itself. Every input is
synthetic.
"""
import base64
import json
import plistlib
import sqlite3

from scripts import cache_controller_report as cc
from scripts import conversations_report as conv_report
from scripts import partial_report, report_ui
from scripts.data import arroyo_content

CONV = "1111aaaa-2222-4333-8444-55555555cccc"
OTHER_CONV = "2222bbbb-3333-4444-8555-66666666dddd"     # a conversation no report lists
MEDIA_UUID = "ABCDEF01-2345-4678-9ABC-DEF012345678"
OTHER_UUID = "0F0F0F0F-1111-4222-8333-444444444444"
SHARE_ID = base64.b64encode(bytes(range(200, 233))).decode()      # 44 characters, holds '+' and '/'
ACCT_A = "a0a0a0a0-1111-4222-8333-444444444444"         # the account arroyo.db belongs to
ACCT_B = "b0b0b0b0-1111-4222-8333-444444444444"         # another account on the same phone


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


def _damage(path, table):
    """Overwrite ``table``'s root page with 0xFF bytes: its columns still read, its rows do not."""
    conn = sqlite3.connect(path)
    size = conn.execute("pragma page_size").fetchone()[0]
    root = conn.execute("select rootpage from sqlite_master where name = ?", (table,)).fetchone()[0]
    conn.close()
    with open(path, "r+b") as fh:
        fh.seek((root - 1) * size)
        fh.write(b"\xff" * size)


def test_a_table_that_will_not_read_costs_the_ids_not_the_report(tmp_path):
    """A conversation_message table that will not read is not one that holds nothing: no content
    ids, and no message numbers to say a message is absent from — None, never empty."""
    db = str(tmp_path / "arroyo.db")
    conn = sqlite3.connect(db)
    conn.execute("create table conversation_message (client_conversation_id text, "
                 "server_message_id integer, content_type integer, message_content blob)")
    conn.executemany("insert into conversation_message values (?, ?, 1, ?)",
                     [(CONV, n, b"x" * 400) for n in range(1, 40)])
    conn.commit()
    conn.close()
    assert conv_report.load_arroyo_messages(db)[1] == {CONV.lower(): {str(n) for n in range(1, 40)}}
    _damage(db, "conversation_message")
    assert conv_report.load_arroyo_messages(db) == ({}, None)
    assert conv_report.load_content_ids(db) == {}
    # no such table, or no arroyo.db at all: not read either
    other = str(tmp_path / "other.db")
    sqlite3.connect(other).execute("create table unrelated (x)").connection.close()
    assert conv_report.load_arroyo_messages(other) == ({}, None)
    assert conv_report.load_arroyo_messages(str(tmp_path / "absent.db")) == ({}, None)


def test_one_chip_per_message_whatever_the_manifest_repeats():
    record = {"conversation_id": CONV, "server_message_id": "7.0", "anchor": "msg-7.0"}
    links = cc._chat_links_for([], "k", {"k": [record, dict(record)]}, {})
    assert len(links) == 1


# --------------------------------------------------------------------------- conversation ties

def _conversation(conv=CONV, in_arroyo=True, smids=("7.0", "9.0"), page="pages/c.html"):
    """A conversation record as the Conversations report hands it to write_cache_links."""
    return {"id": conv, "title": "Chat", "page": page, "in_arroyo": in_arroyo,
            "n_messages": len(smids),
            "messages": [{"smid": s, "anchor": f"msg-{s}", "atts": []} for s in smids]}


def _tie_manifest(tmp_path, conversations=None, arroyo="default", drop=()):
    """``(by_key, by_message, ids)`` over a manifest whose arroyo.db holds messages 7 and 9 of CONV
    and belongs to ACCT_A, unless told otherwise; ``drop`` removes sections from the JSON."""
    if arroyo == "default":
        arroyo = {"read": True, "conversations_read": True, "account": ACCT_A.upper(),
                  "held": {CONV.lower(): {"7", "9"}}}
    tmp_path.mkdir(parents=True, exist_ok=True)
    conv_report.write_cache_links(conversations or [_conversation()], str(tmp_path), arroyo=arroyo)
    path = tmp_path / "cache_links.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for section in drop:
        data.pop(section, None)
    (tmp_path / "Conversations").mkdir()
    (tmp_path / "Conversations" / "cache_links.json").write_text(json.dumps(data), encoding="utf-8")
    path.unlink()
    by_key, by_message = cc.load_chat_links(str(tmp_path))
    return by_key, by_message, cc.load_chat_ids(str(tmp_path), by_message)


def _ties(chat, *keys, user=ACCT_A, chats=None):
    by_key, by_message, ids = chat
    clist = [{"external_key": k, "user_id": user} for k in keys]
    if chats is None:
        chats = cc._chat_links_for(clist, "k", by_key, by_message, ids)
    return cc._conversation_links_for(clist, chats, ids)


def test_a_message_arroyo_does_not_hold_ties_to_its_conversation(tmp_path):
    chat = _tie_manifest(tmp_path)
    key = f"animationmedia~1:{CONV}:404:2:0"
    assert _links(key, "k", chat) == []                    # no message 404: no chat link
    [tie] = _ties(chat, key)
    assert (tie["route"], tie["listed"], tie["href"], tie["anchor"], tie["parts"], tie["number"]) \
        == ("conversation", True, "Conversations/pages/c.html", f"conv-{CONV}", ["2"], "404")
    assert tie["held"] is False and tie["in_arroyo"] is True
    basis = tie["basis"]
    for said in (key, "message 404", "part 2", "arroyo.db this run read holds no message 404 of "
                 "this conversation, in either reading (with and without its -wal)",
                 "the account that arroyo.db belongs to", "opens the conversation, not a message"):
        assert said in basis, said
    assert "no longer" not in basis
    assert "This claim was made by account" not in basis   # the same account: no such sentence


def test_another_accounts_claim_says_whose_it_is_and_never_no_longer(tmp_path):
    [tie] = _ties(_tie_manifest(tmp_path), f"animationmedia~1:{CONV}:404:2:0", user=ACCT_B)
    assert f"This claim was made by account {ACCT_B}" in tie["basis"]
    assert f"belongs to account {ACCT_A}" in tie["basis"]
    assert "may never have been in this database" in tie["basis"]
    assert "no longer" not in tie["basis"]
    # no account to compare with — the claim names none, or arroyo.db's is not known: whose claim
    # it is is not said either way
    for n, (user, account) in enumerate((("", ACCT_A), (ACCT_B, ""))):
        [tie] = _ties(_tie_manifest(tmp_path / f"u{n}", arroyo={
            "read": True, "account": account, "held": {CONV.lower(): {"7"}}}),
            f"1:{CONV}:404:0:0", user=user)
        assert "This claim was made by account" not in tie["basis"]
        assert "the account that arroyo.db belongs to" not in tie["basis"]


def test_every_claim_s_account_is_said_whichever_claim_comes_first(tmp_path):
    """One entry claimed by two accounts for the same message: arroyo.db's own account's claim is the
    stronger evidence and is never hidden behind the other's, nor the other's behind it."""
    chat = _tie_manifest(tmp_path)
    own, other = (f"1:{CONV}:404:0:0", ACCT_A.upper()), (f"thumbnail~1:{CONV}:404:1:0", ACCT_B)
    for pair in ((other, own), (own, other)):
        [tie] = cc._conversation_links_for([{"external_key": k, "user_id": u} for k, u in pair],
                                           [], chat[2])
        basis = tie["basis"]
        assert tie["user_id"] == ACCT_A.upper()                    # the own account's, as spelled
        assert f'"{own[0]}" was made by the account that arroyo.db belongs to' in basis
        assert f'"{other[0]}" was made by account {ACCT_B}' in basis
        assert f"belongs to account {ACCT_A}" in basis
        assert "its own chat database does not hold" in basis      # the own account's conclusion
        for absent in ("The claim being another account's", "may never have been",
                       "This claim was made by"):
            assert absent not in basis, absent
    # a first claim naming no account does not silence the account a later one names
    [tie] = cc._conversation_links_for([{"external_key": f"1:{CONV}:404:0:0", "user_id": ""},
                                        {"external_key": f"x~1:{CONV}:404:0:0", "user_id": ACCT_B}],
                                       [], chat[2])
    assert tie["user_id"] == ACCT_B
    assert f'"x~1:{CONV}:404:0:0" was made by account {ACCT_B}' in tie["basis"]
    assert f'"1:{CONV}:404:0:0" names no account (no USER_ID)' in tie["basis"]
    assert f"The claim of account {ACCT_B} being another account's, the message may never have " \
           f"been in this database" in tie["basis"]
    # a conversation no report lists, claimed by two other accounts: both are named
    third = "c0c0c0c0-1111-4222-8333-444444444444"
    [tie] = cc._conversation_links_for([{"external_key": f"1:{OTHER_CONV}:5:0:0", "user_id": ACCT_B},
                                        {"external_key": f"1:{OTHER_CONV}:5:1:0", "user_id": third}],
                                       [], chat[2])
    assert f"may be one of {ACCT_B}'s or {third}'s that the database this run read never held" in \
        tie["basis"]


def test_no_tie_where_a_message_is_there_or_the_entry_already_reaches_it(tmp_path):
    chat = _tie_manifest(tmp_path, arroyo={"read": True, "account": ACCT_A,
                                           "held": {CONV.lower(): {"7", "9", "11"}}})
    # message 9: held and listed, linked by the key route instead
    assert _links(f"1:{CONV}:9:0:0", "k", chat) == [("key", "9.0")]
    assert _ties(chat, f"1:{CONV}:9:0:0") == []
    # message 11: arroyo.db holds it and the report lists no such message — a missing rule, which
    # is the survey's to find, not a conversation tie
    assert _links(f"1:{CONV}:11:0:0", "k", chat) == []
    assert _ties(chat, f"1:{CONV}:11:0:0") == []
    # an entry whose chat links already reach message 404 (by the file the join attached)
    reached = [{"conversation_id": CONV.upper(), "server_message_id": "404.0", "anchor": "m"}]
    assert _ties(chat, f"1:{CONV}:404:0:0", chats=reached) == []
    # not a chat key at all
    assert _ties(chat, f"content~{CONV}", "https://example.invalid/x") == []
    assert cc._conversation_links_for([{"external_key": f"1:{CONV}:404:0:0"}], [], None) == []


def test_a_conversation_no_report_lists_is_a_stated_fact_and_a_filter(tmp_path):
    chat = _tie_manifest(tmp_path)
    key = f"1:{OTHER_CONV.upper()}:5:0:0"
    [tie] = _ties(chat, key, user=ACCT_B)
    assert (tie["route"], tie["listed"], tie["conversation_id"]) == ("named", False,
                                                                     OTHER_CONV.upper())
    assert "href" not in tie and "anchor" not in tie and tie["in_arroyo"] is False
    for said in ("no report lists the conversation", "in either reading (with and without its -wal)",
                 f"This claim was made by account {ACCT_B}",
                 f"may be one of {ACCT_B}'s that the database this run read never held",
                 "filters this report"):
        assert said in tie["basis"], said
    assert "no longer" not in tie["basis"]
    entry = {"memory": None, "chats": [], "conv_links": [tie], "cache_media": [], "claims": [],
             "cache_key": "k", "on_disk": {"found": False, "paths": []}}
    for compact in (False, True):
        chips = cc._links_html(entry, "../", compact=compact)
        assert f'href="#find={OTHER_CONV.upper()}"' in chips
        assert "Conversations/" not in chips and "in no report" in chips
        assert ('class="qm"' in chips) is (not compact)      # its "?" in the detail only
    # the listed tie is a link to the conversation's page, at its header
    [listed] = _ties(chat, f"1:{CONV}:404:0:0")
    chips = cc._links_html(dict(entry, conv_links=[listed]), "../", compact=True)
    assert f'href="../Conversations/pages/c.html#conv-{CONV}"' in chips
    assert 'target="scauto_conv_page"' in chips and "not in arroyo.db" in chips


def test_a_conversation_no_report_lists_but_arroyo_db_holds_messages_of_was_held(tmp_path):
    """No report lists the conversation, yet the arroyo.db this run read holds another message of
    it: the "?" says so, and never that the database may never have held the conversation."""
    chat = _tie_manifest(tmp_path, arroyo={"read": True, "conversations_read": True,
                                           "account": ACCT_A, "held": {OTHER_CONV: {"3"}}})
    [tie] = _ties(chat, f"1:{OTHER_CONV}:404:0:0", user=ACCT_B)
    assert (tie["route"], tie["listed"], tie["held"], tie["in_arroyo"]) == ("named", False, False,
                                                                          True)
    assert "it holds other messages of this conversation" in tie["basis"]
    assert f"This claim was made by account {ACCT_B}" in tie["basis"]
    assert "may never have been in this database" in tie["basis"]
    assert "never held" not in tie["basis"]


def test_conversation_tables_that_would_not_read_name_no_absence_of_the_conversation(tmp_path):
    """arroyo.db's conversation tables did not read in full: whether a row of them names the
    conversation is not known, so neither the listed (C) nor the unlisted (D) wording says none does
    — while the absence of the message, read from conversation_message, is still said."""
    arroyo = {"read": True, "conversations_read": False, "account": ACCT_A, "held": {}}
    chat = _tie_manifest(tmp_path, conversations=[_conversation(in_arroyo=False, smids=())],
                         arroyo=arroyo)
    for key in (f"1:{CONV}:3:0:0", f"1:{OTHER_CONV}:5:0:0"):              # C, then D
        [tie] = _ties(chat, key)
        assert "holds no message" in tie["basis"]
        assert "conversation tables would not read in full in this run" in tie["basis"]
        assert "is not known" in tie["basis"]
        for absent in ("and no conversation, feed_entry or user_conversation row",
                       "neither arroyo.db's conversation tables", "not from arroyo.db"):
            assert absent not in tie["basis"], (key, absent)
    # a manifest that does not say they were read has not said so
    chat = _tie_manifest(tmp_path / "old", arroyo={"read": True, "account": ACCT_A, "held": {}})
    assert chat[2].conversations_read is False


def test_without_the_arroyo_facts_nothing_is_tied(tmp_path):
    """An older manifest has neither section; one without `arroyo` (a partial extract's) has no
    facts to state an absence from. Either way: no tie, never a wrong statement."""
    for n, drop in enumerate((("arroyo", "conversations"), ("arroyo",))):
        chat = _tie_manifest(tmp_path / str(n), drop=drop)
        for key in (f"1:{CONV}:404:0:0", f"1:{OTHER_CONV}:5:0:0"):
            assert _ties(chat, key) == [], (drop, key)


def test_when_arroyo_db_was_not_read_only_a_listed_conversation_is_tied_and_nothing_is_absent(
        tmp_path):
    chat = _tie_manifest(tmp_path, arroyo={"read": False, "account": ACCT_A, "held": {}})
    [tie] = _ties(chat, f"1:{CONV}:404:0:0")
    assert tie["listed"] and tie["held"] is None
    assert chat[2].holds(CONV, 7) is None and chat[2].holds_any(CONV) is None
    assert "is not known" in tie["basis"]
    assert "holds no" not in tie["basis"]
    entry = {"memory": None, "chats": [], "conv_links": [tie], "cache_media": [], "claims": [],
             "cache_key": "k", "on_disk": {"found": False, "paths": []}}
    assert "not in arroyo.db" not in cc._links_html(entry, "../", compact=True)
    assert _ties(chat, f"1:{OTHER_CONV}:5:0:0") == []       # unlisted, and nothing known
    # whose claim it is is said as in every other wording: the account is read apart from messages
    assert "made by the account that arroyo.db belongs to" in tie["basis"]
    [tie] = _ties(chat, f"1:{CONV}:404:0:0", user=ACCT_B)
    assert f"This claim was made by account {ACCT_B}" in tie["basis"]
    assert "made by the account that arroyo.db belongs to" not in tie["basis"]


def test_the_parts_of_one_message_are_one_tie_and_the_manifest_spells_the_id(tmp_path):
    chat = _tie_manifest(tmp_path)
    [tie] = _ties(chat, f"thumbnail~1:{CONV.upper()}:404:1:0", f"1:{CONV.upper()}:404:0:0")
    assert tie["parts"] == ["0", "1"]
    assert tie["conversation_id"] == CONV and tie["anchor"] == f"conv-{CONV}"
    assert tie["basis"].startswith("The claim EXTERNAL_KEYs ") and "(parts 0, 1)" in tie["basis"]
    # a listed conversation of no message held: held by none of its rows
    chat = _tie_manifest(tmp_path / "c", conversations=[_conversation(in_arroyo=False, smids=())],
                         arroyo={"read": True, "conversations_read": True, "account": ACCT_A,
                                 "held": {}})
    [tie] = _ties(chat, f"1:{CONV}:3:0:0")
    assert tie["in_arroyo"] is False and "friends / groups lists" in tie["basis"]
    assert "and no conversation, feed_entry or user_conversation row" in tie["basis"]


def test_a_listed_tie_outside_a_partial_extract_is_marked_not_linked(tmp_path):
    [tie] = _ties(_tie_manifest(tmp_path), f"1:{CONV}:404:0:0")
    entry = {"memory": None, "chats": [], "conv_links": [tie], "cache_media": [], "claims": [],
             "cache_key": "k", "on_disk": {"found": False, "paths": []}}
    indexes = {"cc": partial_report.Index("cc"), "conv": partial_report.Index("conv")}
    indexes["cc"].add("ck-k", key="k")
    indexes["conv"].add(f"conv-{CONV}", conv=CONV)
    selection = {"schema": 2, "selections": {"cc": {"ck-k": 1}}}
    closure = partial_report.expand(indexes, partial_report.resolve(indexes, selection),
                                    {**partial_report.default_options(), "relations": {}})
    chips = cc._links_html(entry, "../", compact=True, closure=closure)
    assert report_ui.XOUT_MARK in chips and "Conversations/" not in chips


# --------------------------------------------------------------------------- what arroyo.db holds

def _sql(conn, statements):
    for statement in statements:
        sql, params = statement if isinstance(statement, tuple) else (statement, ())
        conn.execute(sql, params)


def _checkpoint_then_wal(path, before, after, damage=(), header=False):
    """``before`` checkpointed into the file, ``after`` left in its -wal only. The root page of each
    ``damage`` table is overwritten in the file alone, so only the checkpointed reading meets it (a
    connection opened afterwards would checkpoint the -wal away); ``header`` overwrites the file's
    own 16-byte header, so the file will not open without its -wal (``after`` must then change the
    schema, which puts page 1 in the -wal)."""
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=wal")
    _sql(conn, before)
    conn.commit()
    size = conn.execute("pragma page_size").fetchone()[0]
    roots = [conn.execute("select rootpage from sqlite_master where name = ?", (t,)).fetchone()[0]
             for t in damage]
    conn.close()                                       # checkpoints: the file holds all of it
    checkpointed = bytearray(open(path, "rb").read())
    for root in roots:
        checkpointed[(root - 1) * size:root * size] = b"\xff" * size
    if header:
        checkpointed[0:16] = b"\xff" * 16
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


def _arroyo(path):
    _checkpoint_then_wal(path, [
        "create table conversation_message (client_conversation_id text, server_message_id integer, "
        "client_message_id integer, content_type integer, message_content blob, "
        "local_message_references blob)",
        "create table conversation (client_conversation_id text, creation_timestamp integer)",
        "create table required_values (key text primary key, value text)",
        ("insert into conversation_message values (?, 5, 5, 1, x'', NULL), (?, 6, 6, 1, x'', ?), "
         "(?, NULL, 41, 1, x'', NULL), (NULL, 8, 8, 1, x'', NULL)",
         (CONV, CONV, _media_reference(MEDIA_UUID), CONV.upper())),
        ("insert into conversation values (?, 0)", (OTHER_CONV,)),
        ("insert into required_values values ('USERID', ?)", (ACCT_A.upper(),)),
    ], [
        "delete from conversation_message where server_message_id = 5",
        ("insert into conversation_message values (?, 7, 7, 1, x'', NULL)", (CONV,)),
    ])


def test_what_arroyo_db_holds_is_read_from_both_readings(tmp_path):
    import os
    db = str(tmp_path / "arroyo.db")
    _arroyo(db)
    assert os.path.getsize(db + "-wal")
    content, held = conv_report.load_arroyo_messages(db)
    # 5 only in the checkpointed reading, 7 only with the -wal; 41 by the client id of a message
    # the server never numbered; the row with no conversation id is not a message of any
    assert held == {CONV.lower(): {"5", "6", "7", "41"}}
    assert "404" not in held[CONV.lower()]
    assert content == {(CONV.lower(), "6"): [(MEDIA_UUID, "media")]}
    assert conv_report.load_content_ids(db) == content
    facts = {}
    conv_report.load_arroyo_conversations(db, facts=facts)
    assert facts == {"account": ACCT_A, "conversations_read": True}       # lower-cased
    conn = sqlite3.connect(str(tmp_path / "plain.db"))
    conn.execute("create table conversation (client_conversation_id text)")
    conn.close()
    # no required_values: no account known; no arroyo.db: nothing read
    for path, read in ((str(tmp_path / "plain.db"), True), (str(tmp_path / "absent.db"), False)):
        facts = {}
        conv_report.load_arroyo_conversations(path, facts=facts)
        assert facts == {"account": "", "conversations_read": read}


def test_a_checkpointed_copy_that_will_not_read_leaves_the_held_numbers_unknown(tmp_path):
    """The message the -wal deleted is in the checkpointed copy only: when that copy will not read,
    no message is said to be absent — while what the current reading names its media by stays."""
    db = str(tmp_path / "arroyo.db")
    _checkpoint_then_wal(db, [
        "create table conversation_message (client_conversation_id text, server_message_id integer, "
        "content_type integer, message_content blob, local_message_references blob)",
        ("insert into conversation_message values (?, 5, 1, x'', NULL), (?, 6, 1, x'', ?)",
         (CONV, CONV, _media_reference(MEDIA_UUID))),
    ], ["delete from conversation_message where server_message_id = 5"],
        damage=["conversation_message"])                   # the -wal holds a whole image of it
    content, held = conv_report.load_arroyo_messages(db)
    assert held is None
    assert content == {(CONV.lower(), "6"): [(MEDIA_UUID, "media")]}


def test_a_checkpointed_copy_that_will_not_open_leaves_what_arroyo_db_holds_unknown(tmp_path):
    """The file's own header is damaged: only the -wal reading opens, and the message the -wal
    deleted — in the checkpointed copy alone — was never read. Nothing is said to be absent, not a
    message and not a conversation row, while what the current reading names its media by stays."""
    from scripts.data import sqlite_open
    db = str(tmp_path / "arroyo.db")
    _checkpoint_then_wal(db, [
        "create table conversation_message (client_conversation_id text, server_message_id integer, "
        "content_type integer, message_content blob, local_message_references blob)",
        ("insert into conversation_message values (?, 5, 1, x'', NULL), (?, 6, 1, x'', ?)",
         (CONV, CONV, _media_reference(MEDIA_UUID))),
        "create table conversation (client_conversation_id text)",
        ("insert into conversation values (?)", (OTHER_CONV,)),
    ], ["delete from conversation_message where server_message_id = 5",
        "create table other (x)"], header=True)
    views = sqlite_open.open_views(db)
    try:
        assert views.merged is not None and views.main_only is None
    finally:
        views.close()
    content, held = conv_report.load_arroyo_messages(db)
    assert held is None
    assert content == {(CONV.lower(), "6"): [(MEDIA_UUID, "media")]}
    facts = {}
    assert OTHER_CONV in conv_report.load_arroyo_conversations(db, facts=facts)
    assert facts["conversations_read"] is False


def test_a_content_column_the_wal_added_hides_no_checkpointed_message(tmp_path):
    """A message is held by its two ids: a reading that predates message_content still says which
    messages it holds — 5, which the -wal deleted, too — and the -wal reading's media ids stay."""
    db = str(tmp_path / "arroyo.db")
    _checkpoint_then_wal(db, [
        "create table conversation_message (client_conversation_id text, server_message_id integer, "
        "content_type integer)",
        ("insert into conversation_message values (?, 5, 1), (?, 6, 1)", (CONV, CONV)),
    ], [
        "alter table conversation_message add column message_content blob",
        "alter table conversation_message add column local_message_references blob",
        ("update conversation_message set local_message_references = ? where server_message_id = 6",
         (_media_reference(MEDIA_UUID),)),
        "delete from conversation_message where server_message_id = 5",
    ])
    content, held = conv_report.load_arroyo_messages(db)
    assert held == {CONV.lower(): {"5", "6"}}
    assert content == {(CONV.lower(), "6"): [(MEDIA_UUID, "media")]}


def test_a_column_or_table_the_wal_created_is_a_reading_with_less_not_one_that_failed(tmp_path):
    """The checkpointed reading predates a column the -wal added, or a table it created: that reading
    reads what it has — it has not failed. And two readings naming two accounts name none."""
    db = str(tmp_path / "arroyo.db")
    _checkpoint_then_wal(db, [
        "create table conversation_message (client_conversation_id text, server_message_id integer, "
        "content_type integer, message_content blob)",
        ("insert into conversation_message values (?, 5, 1, x'')", (CONV,)),
        "create table required_values (key text primary key, value text)",
        ("insert into required_values values ('USERID', ?)", (ACCT_A,)),
    ], [
        "alter table conversation_message add column client_message_id integer",
        ("insert into conversation_message values (?, NULL, 1, x'', 41)", (CONV,)),
        ("update required_values set value = ? where key = 'USERID'", (ACCT_B,)),
    ])
    assert conv_report.load_arroyo_messages(db)[1] == {CONV.lower(): {"5", "41"}}
    facts = {}
    conv_report.load_arroyo_conversations(db, facts=facts)
    assert facts == {"account": "", "conversations_read": True}
    other = str(tmp_path / "other.db")
    _checkpoint_then_wal(other, ["create table unrelated (x)"], [
        "create table conversation_message (client_conversation_id text, server_message_id integer, "
        "content_type integer, message_content blob)",
        ("insert into conversation_message values (?, 7, 1, x'')", (CONV,)),
    ])
    assert conv_report.load_arroyo_messages(other)[1] == {CONV.lower(): {"7"}}


def _full_run(tmp_path):
    """The Conversations report over an arroyo.db alone: a conversation it holds messages of, and
    one only its conversation table names."""
    db = str(tmp_path / "arroyo.db")
    _arroyo(db)
    reports = tmp_path / "Reports"
    stage = conv_report.index(None, None, None, str(reports / "Conversations"),
                              str(tmp_path / "attachments"), arroyo=db, tz="utc",
                              report_dir=str(reports))
    return stage, reports


def test_the_manifest_lists_every_conversation_and_what_arroyo_db_holds(tmp_path):
    stage, reports = _full_run(tmp_path)
    conv_report.render(stage)
    manifest = json.loads((reports / "Conversations" / "cache_links.json").read_text("utf-8"))
    assert manifest["version"] == 3
    pages = json.loads((reports / "Conversations" / "conversation_pages.json").read_text("utf-8"))
    assert set(manifest["conversations"]) == set(pages) == {OTHER_CONV}
    record = manifest["conversations"][OTHER_CONV]
    assert record["anchor"] == f"conv-{OTHER_CONV}"
    assert record["href"] == f"Conversations/{pages[OTHER_CONV]}"
    assert record["in_arroyo"] is True and record["messages"] == 0
    assert manifest["arroyo"] == {"read": True, "conversations_read": True, "account": ACCT_A,
                                  "held": {CONV.lower(): [5, 6, 7, 41]}}
    # and the cache_controller report reads them back: CONV is held but not listed, OTHER_CONV listed
    by_key, by_message = cc.load_chat_links(str(reports))
    ids = cc.load_chat_ids(str(reports), by_message)
    assert ids.holds(CONV.upper(), "7") is True and ids.holds(CONV, 404) is False
    assert ids.conversation(OTHER_CONV.upper())["id"] == OTHER_CONV
    [tie] = cc._conversation_links_for([{"external_key": f"1:{OTHER_CONV}:3:0:0",
                                         "user_id": ACCT_A}], [], ids)
    assert tie["route"] == "conversation" and tie["in_arroyo"] is True


def test_a_failed_message_read_states_no_absence_anywhere(tmp_path):
    """arroyo.db's conversation_message will not read: the manifest says its messages were not
    read — not that it holds none — so a conversation no report lists gets no tie, and the one it
    lists a tie that says the absence is not known."""
    db = str(tmp_path / "arroyo.db")
    conn = sqlite3.connect(db)
    conn.execute("create table conversation_message (client_conversation_id text, "
                 "server_message_id integer, content_type integer, message_content blob)")
    conn.executemany("insert into conversation_message values (?, ?, 1, ?)",
                     [(CONV, n, b"x" * 400) for n in range(1, 40)])
    conn.execute("create table conversation (client_conversation_id text, "
                 "creation_timestamp integer)")
    conn.execute("insert into conversation values (?, 0)", (OTHER_CONV,))
    conn.commit()
    conn.close()
    _damage(db, "conversation_message")
    reports = tmp_path / "Reports"
    conv_report.render(conv_report.index(None, None, None, str(reports / "Conversations"),
                                         str(tmp_path / "attachments"), arroyo=db, tz="utc",
                                         report_dir=str(reports)))
    manifest = json.loads((reports / "Conversations" / "cache_links.json").read_text("utf-8"))
    assert manifest["arroyo"] == {"read": False, "conversations_read": True, "account": "",
                                  "held": {}}
    by_key, by_message = cc.load_chat_links(str(reports))
    chat = (by_key, by_message, cc.load_chat_ids(str(reports), by_message))
    assert _ties(chat, f"1:{CONV}:404:0:0") == []                  # listed by no report
    [tie] = _ties(chat, f"1:{OTHER_CONV}:404:0:0")
    assert "is not known" in tie["basis"] and "holds no" not in tie["basis"]


def _rendered(tmp_path, statements, damage=()):
    """``(manifest, chat)`` of the Conversations report over an arroyo.db made of ``statements``,
    with the root page of each ``damage`` table overwritten."""
    db = str(tmp_path / "arroyo.db")
    conn = sqlite3.connect(db)
    _sql(conn, statements)
    conn.commit()
    conn.close()
    for table in damage:
        _damage(db, table)
    reports = tmp_path / "Reports"
    conv_report.render(conv_report.index(None, None, None, str(reports / "Conversations"),
                                         str(tmp_path / "attachments"), arroyo=db, tz="utc",
                                         report_dir=str(reports)))
    manifest = json.loads((reports / "Conversations" / "cache_links.json").read_text("utf-8"))
    by_key, by_message = cc.load_chat_links(str(reports))
    return manifest, (by_key, by_message, cc.load_chat_ids(str(reports), by_message))


_MESSAGES = ["create table conversation_message (client_conversation_id text, server_message_id "
             "integer, content_type integer, message_content blob)",
             ("insert into conversation_message values (?, 5, 1, x'')", (CONV,))]


def test_a_feed_entry_of_another_schema_still_names_its_conversation(tmp_path):
    """A feed_entry without one of the columns read for its dates still holds the conversation's
    row: the conversation is listed, as arroyo.db's, and the tie is a link — never "no table names
    it"."""
    manifest, chat = _rendered(tmp_path, _MESSAGES + [
        "create table feed_entry (client_conversation_id text, display_timestamp integer, "
        "last_updated_timestamp integer, conversation_type integer)",
        ("insert into feed_entry values (?, 1, 2, 0)", (OTHER_CONV,))])
    assert manifest["conversations"][OTHER_CONV]["in_arroyo"] is True
    assert manifest["arroyo"]["conversations_read"] is True
    [tie] = _ties(chat, f"1:{OTHER_CONV}:3:0:0")
    assert (tie["route"], tie["in_arroyo"]) == ("conversation", True)
    assert "neither arroyo.db's conversation tables" not in tie["basis"]


def test_a_damaged_conversation_table_is_not_one_that_names_nothing(tmp_path):
    """The conversation table will not read: the conversation it names is not listed, and its tie
    says whether a conversation table names it is not known — never that none does. The survey
    counts the same file unread (``claim_link_survey._arroyo_facts``)."""
    manifest, chat = _rendered(tmp_path, _MESSAGES + [
        "create table conversation (client_conversation_id text, creation_timestamp integer)",
        ("insert into conversation values (?, 0)", (OTHER_CONV,))], damage=["conversation"])
    assert OTHER_CONV not in manifest["conversations"]
    assert manifest["arroyo"]["read"] is True and manifest["arroyo"]["conversations_read"] is False
    [tie] = _ties(chat, f"1:{OTHER_CONV}:5:0:0")
    assert tie["route"] == "named" and "holds no message 5" in tie["basis"]
    assert "is not known" in tie["basis"]
    assert "neither arroyo.db's conversation tables" not in tie["basis"]


def test_a_partial_extracts_manifest_carries_no_held_numbers(tmp_path):
    stage, reports = _full_run(tmp_path)
    indexes = stage.indexes()
    selection = {"schema": 2, "selections": {"conv": {f"conv-{OTHER_CONV}": 1}}}
    closure = partial_report.expand(indexes, partial_report.resolve(indexes, selection),
                                    {**partial_report.default_options(), "relations": {}})
    conv_report.render(stage, closure=closure, prov={})
    manifest = json.loads((reports / "Conversations" / "cache_links.json").read_text("utf-8"))
    assert "arroyo" not in manifest
    assert set(manifest["conversations"]) == {OTHER_CONV}
    assert CONV.lower() not in json.dumps(manifest).lower()


# --------------------------------------------------------------------------- the report

CK_LISTED, CK_UNLISTED, CK_MESSAGE, CK_LEAD = ("1" * 32, "2" * 32, "3" * 32, "4" * 32)


def _cache_app(tmp_path, sections=True, claims=None, arroyo=None):
    """An app folder whose cache holds a claim naming a message arroyo.db does not hold in a listed
    conversation, one naming a conversation no report lists (another account's), one naming a
    message the report lists, and a context-19 claim of the chat shape beside a Memory a minute
    off — a "possible Memory" when nothing ties it; or ``claims`` instead. The Conversations manifest
    is the current build's (its ``arroyo`` section given, or one holding messages 7 and 9), or
    (``sections`` false) one without what arroyo.db holds."""
    import overlay_fixture as ofx
    app = str(tmp_path / "app")
    ofx.scdb(app, [(ofx.SNAP_A, ofx.overlay([]), 0)])
    claims = claims or [(CK_LISTED, 3, f"1:{CONV}:404:0:0"),
                        (CK_UNLISTED, 3, f"1:{OTHER_CONV}:5:0:0", ofx.OTHER_USER),
                        (CK_MESSAGE, 3, f"1:{CONV}:7:0:0"),
                        (CK_LEAD, 19, f"1:{OTHER_CONV}:6:0:0")]
    ofx.cache_db(app, claims)
    for key in dict.fromkeys(claim[0] for claim in claims):
        ofx.cached_file(app, key)
    reports = tmp_path / "Reports"
    (reports / "Conversations").mkdir(parents=True)
    conv_report.write_cache_links(
        [_conversation()], str(reports / "Conversations"),
        arroyo=(arroyo or {"read": True, "conversations_read": True, "account": ofx.USER,
                           "held": {CONV.lower(): {"7", "9"}}})
        if sections else None)
    stage = cc.index(app, outdir=str(reports / "CacheController"), tz="utc")
    return stage, reports, {e["cache_key"]: e for e in stage.model}


def _index_rows(path):
    text = open(path, encoding="utf-8").read()
    return {r[0]: r for r in json.loads(text[text.index("(") + 1:text.rindex(")")])}


def test_the_report_ties_counts_filters_and_records_an_edge_only_for_a_listed_conversation(
        tmp_path):
    import overlay_fixture as ofx
    stage, reports, entries = _cache_app(tmp_path)
    [listed] = entries[CK_LISTED]["conv_links"]
    [unlisted] = entries[CK_UNLISTED]["conv_links"]
    assert listed["listed"] and not entries[CK_LISTED]["chats"]
    assert not unlisted["listed"] and f"This claim was made by account {ofx.OTHER_USER}" in \
        unlisted["basis"]
    assert entries[CK_MESSAGE]["chats"] and not entries[CK_MESSAGE]["conv_links"]
    assert entries[CK_LISTED]["category"] == "Chat media"          # the category is the key's
    edges = {(edge, src, dst) for edge, src, _kind, dst in stage.sel.edges
             if edge == partial_report.EDGE_CONV_CACHE}
    assert edges == {(partial_report.EDGE_CONV_CACHE, f"ck-{CK_LISTED}", f"conv-{CONV}")}
    # a file whose claim names a conversation is accounted for: never a possible Memory
    assert entries[CK_LEAD]["conv_links"] and not entries[CK_LEAD]["leads"]

    cc.render(stage)
    out = reports / "CacheController"
    page = (out / "CacheController_report.html").read_text("utf-8")
    assert '<option value="Conversation">' in page and ".chip.chat.gone{" in page
    assert "<b>3</b> tied only to a conversation" in page
    assert "<b>1</b> linked to a chat" in page                       # a tie is not a chat link
    rows = _index_rows(out / "data" / "index.js")
    assert rows[f"ck-{CK_LISTED}"][5]["link"] == "Conversation"
    assert rows[f"ck-{CK_MESSAGE}"][5]["link"] == "Chat"
    assert "#find=" in json.dumps(rows[f"ck-{CK_UNLISTED}"][1])
    assert "Possible" not in rows[f"ck-{CK_LEAD}"][5]["link"]
    _report, stats = cc.generate_report(stage.model, [], str(tmp_path / "again"), "UTC", "../",
                                        None, None, "db")
    assert (stats["conv"], stats["chat"]) == (3, 1)


def test_a_tie_beside_a_chat_link_is_drawn_dashed_though_no_entry_is_tied_only(tmp_path):
    """One cached file claimed for message 7, which the report lists, and for message 404, which
    arroyo.db does not hold: a chat link and a tie. The entry counts as linked to a chat — no header
    line or Linked option for a tie alone — but its tie chip keeps the dashed style."""
    stage, reports, entries = _cache_app(tmp_path, claims=[(CK_MESSAGE, 3, f"1:{CONV}:7:0:0"),
                                                          (CK_MESSAGE, 3, f"1:{CONV}:404:0:0")])
    entry = entries[CK_MESSAGE]
    assert entry["chats"] and [tie["number"] for tie in entry["conv_links"]] == ["404"]
    cc.render(stage)
    out = reports / "CacheController"
    page = (out / "CacheController_report.html").read_text("utf-8")
    assert ".chip.chat.gone{" in page
    for absent in ('value="Conversation"', "tied only to a conversation"):
        assert absent not in page, absent
    rows = _index_rows(out / "data" / "index.js")
    assert rows[f"ck-{CK_MESSAGE}"][5]["link"] == "Chat"
    assert "chip chat gone" in json.dumps(rows[f"ck-{CK_MESSAGE}"][1])


def test_the_linked_option_states_no_absence_when_arroyo_db_s_messages_were_not_read(tmp_path):
    """arroyo.db's messages were not read: a listed conversation is still tied, and the Linked
    option that selects it says only that there is no message row — not that arroyo.db lacks one."""
    import overlay_fixture as ofx
    stage, reports, entries = _cache_app(tmp_path, arroyo={
        "read": False, "conversations_read": True, "account": ofx.USER, "held": {}})
    [tie] = entries[CK_LISTED]["conv_links"]
    assert tie["held"] is None and "is not known" in tie["basis"]
    assert not entries[CK_UNLISTED]["conv_links"]                  # no report lists it: nothing
    cc.render(stage)
    out = reports / "CacheController"
    page = (out / "CacheController_report.html").read_text("utf-8")
    assert '<option value="Conversation">chat conversation only (no message row)</option>' in page
    assert "not in arroyo.db" not in page
    rows = _index_rows(out / "data" / "index.js")
    assert rows[f"ck-{CK_LISTED}"][5]["link"] == "Conversation"
    assert "not in arroyo.db" not in json.dumps(rows[f"ck-{CK_LISTED}"][1])


def test_without_what_arroyo_db_holds_the_report_is_what_it_was(tmp_path):
    """A manifest of an older build: no tie, no new line, option or rule on the page — and the
    file nothing ties is a possible Memory again."""
    stage, reports, entries = _cache_app(tmp_path, sections=False)
    assert not any(e.get("conv_links") for e in entries.values())
    assert entries[CK_LEAD]["leads"]
    assert partial_report.EDGE_CONV_CACHE not in {edge for edge, *_rest in stage.sel.edges}
    cc.render(stage)
    page = (reports / "CacheController" / "CacheController_report.html").read_text("utf-8")
    for absent in ('value="Conversation"', ".chip.chat.gone", "tied only to a conversation"):
        assert absent not in page, absent
