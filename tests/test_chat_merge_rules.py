"""Chat parsing: lookups instead of every-row-against-every-row loops, and a stated rule wherever a
value has several candidates.

Each value used to come from whichever candidate matched **last** — the friends row that came last,
the cache claim that came last in the table — which no one chose and the database's row order
decided. The loops were also every message against every friend and every claim, which on a phone
with hundreds of thousands of messages is hours. Every input is synthetic.
"""
import pandas as pd

from scripts import ParseSnapchat_iOS as parser

ALICE, BOB, CAROL = ("11111111-0000-4000-8000-00000000000a", "11111111-0000-4000-8000-00000000000b",
                     "11111111-0000-4000-8000-00000000000c")


def _messages(*senders):
    return pd.DataFrame({"sender_id": list(senders), "message_content": ["hi"] * len(senders),
                         "message_body": [""] * len(senders)})


def test_each_sender_is_named_once_looked_up():
    friends = pd.DataFrame({"User ID": [ALICE, BOB], "Username": ["alice-test", "bob-test"]})
    cached = pd.DataFrame({"User ID": [CAROL], "Username": ["carol-test"]})
    out = parser.fixSenders(_messages(ALICE, CAROL, "unknown-id", None), friends, cached)
    assert out["sender_id"].tolist()[:3] == ["alice-test", "carol-test", "unknown-id"]
    assert out["sender_user_id"].tolist()[:3] == [ALICE, CAROL, "unknown-id"]   # the id is kept


def test_an_id_with_two_names_shows_both_rather_than_the_last(caplog):
    friends = pd.DataFrame({"User ID": [ALICE, ALICE], "Username": ["alice-old", "alice-new"]})
    out = parser.fixSenders(_messages(ALICE), friends, pd.DataFrame())
    assert out["sender_id"].tolist() == ["alice-old / alice-new"]
    assert "more than one name" in caplog.text


def test_the_friends_list_wins_over_the_cached_snapchatters():
    friends = pd.DataFrame({"User ID": [ALICE], "Username": ["alice-friend"]})
    cached = pd.DataFrame({"User ID": [ALICE], "Username": ["alice-cached"]})
    assert parser.fixSenders(_messages(ALICE), friends, cached)["sender_id"].tolist() == [
        "alice-friend"]


def test_a_shares_media_comes_before_its_thumbnail_then_the_databases_order():
    claims = [("thumbnail~1:story-media-X", "k-thumb"), ("story-media-X", "k-media"),
              ("story-media-X~2", "k-other")]
    assert [ck for _ek, ck in parser._share_claim_order(claims)] == ["k-media", "k-other",
                                                                      "k-thumb"]


def test_lookup_keys_match_what_equality_matched():
    assert parser._id_key(float("nan")) is None and parser._id_key(None) is None
    assert parser._id_key(["not", "hashable"]) is None
    assert parser._names_by_id([[ALICE, "a"], [ALICE, "a"], [BOB, "b"], [None, "x"]]) == {
        ALICE: ["a"], BOB: ["b"]}


def _f(number, value):
    """One length-delimited protobuf field (every length here is under 128)."""
    return bytes([number << 3 | 2, len(value)]) + value


#: A sticker's id as bytes, the way a creative-tool-item body carries it, and in base64.
STICKER_BYTES = b"\x0f\xbf\xffsynthetic1"
STICKER_B64 = "D7//c3ludGhldGljMQ=="


def test_a_sticker_is_matched_by_the_id_its_body_carries():
    custom = _f(4, _f(2, b"\x05") + _f(4, _f(14, _f(2, _f(2, _f(3, b"")) + _f(6, STICKER_BYTES)))))
    pack = _f(4, _f(4, _f(4, _f(1, _f(1, b"pack") + _f(2, b"pack-sticker-name")))))
    assert parser._sticker_key_text(custom) == STICKER_B64
    assert parser._sticker_key_text(pack) == "pack-sticker-name"
    assert parser._sticker_key_text(_f(4, _f(4, _f(2, b"hello")))) is None
    assert parser._sticker_key_text(None) is None


def test_a_custom_sticker_message_takes_the_claim_holding_its_id(tmp_path):
    import sqlite3
    db = str(tmp_path / "arroyo.db")
    conn = sqlite3.connect(db)
    conn.execute("create table conversation_message (client_conversation_id text, "
                 "server_message_id integer, local_message_references blob, content_type integer, "
                 "message_content blob)")
    custom = _f(4, _f(4, _f(14, _f(2, _f(6, STICKER_BYTES)))))
    conn.executemany("insert into conversation_message values (?, ?, null, 5, ?)",
                     [(ALICE, 1, custom), (ALICE, 2, _f(4, _f(4, _f(2, b"no sticker"))))])
    conn.commit()
    conn.close()
    claims = pd.DataFrame({"EXTERNAL_KEY": ["customSticker-synthetic-" + STICKER_B64, "other"],
                           "CACHE_KEY": ["k-sticker", "k-other"]})
    out = parser.getCacheArroyo(db, claims)
    assert out.set_index("server_message_id")["message_content"].to_dict()[1] == "k-sticker"
    assert out.set_index("server_message_id")["message_content"].to_dict()[2] != "k-other"
