"""Cached files named by an item of an account's creative-tools store (``primary.docobjects`` ›
``ctp__item_5``) — and nothing named on less than an exact identity.

Each item is a FlatBuffers document (slot 0 the item_id again, slot 2 a protobuf payload, slot 3 the
item's own id, slot 4 its feed); the feed is named only from the store's feed tree. A cache claim is
matched when its whole key, or the key after a word and ``:`` or ``~``, is a text the item holds — or,
failing that, the same bytes as the item's own id in another base64 alphabet. Never on a query value,
a path segment or a part of a text, and never to one of several items holding the same text. The
cache_controller report shows the items on the entry and, when no chat or Memory link says what the
file is, files it under its own category; it is never a link and never a lead.

Every input is synthetic (``ctp_fixture``, ``overlay_fixture``).
"""
import base64
import glob
import html
import json
import logging
import os
import tempfile

import ctp_fixture as fx
import overlay_fixture as ofx
from scripts import cache_controller_report as cc
from scripts import trace_ids
from scripts.data import base64_text, ctp_items, sqlite_open

LENS_URL = "https://lens.example.net/lens/Synthetic.lns"
OTHER_URL = "https://cf-st.example.net/d/SyntheticNobody?mo=QUJD%3D&uc=7"
NASTY = "</script><b class=\"x\">'quoted' & <i>bold</i></b>"


def _music_item(url=fx.MUSIC_URL, own=b"music-own-1"):
    """An item of a feed the tree does not list (feed:19-1-0), its URL at payload 2.7.11.1 — and a
    one-character text, which must never match anything."""
    own_id = base64.b64encode(own).decode()
    payload = fx.message((2, [(7, [(2, "Synthetic Song"), (5, "1"), (11, [(1, url), (2, 1)])])]),
                         (6, own))
    item_id = f"{own_id}-feed:19-1-0-0"
    return item_id, fx.item_doc(item_id, payload, own_id, "feed:19-1-0")


def _sticker_item(item_id=fx.STICKER_ID, decodes=True):
    """A custom sticker of the custom-stickers feed (feed:4-2-0), whose own id is 13 bytes."""
    payload = fx.message((2, [(9, [(1, "https://cf-st.example.net/d/SyntheticSticker"), (2, 1)])]),
                         (6, fx.STICKER))
    doc = fx.item_doc(item_id, payload, fx.STICKER_ID, "feed:4-2-0") if decodes \
        else b"a document of another layout"
    return item_id, doc


def _lens_item():
    own = b"lens-own-01"
    own_id = base64.b64encode(own).decode()
    item_id = f"{own_id}-feed:3-2-0-0"
    payload = fx.message((2, [(5, [(1, LENS_URL), (2, NASTY), (3, 1)])]), (6, own))
    return item_id, fx.item_doc(item_id, payload, own_id, "feed:3-2-0")


def _index(tmp_path, items, **kw):
    app = str(tmp_path / "app")
    fx.store(app, items, **kw)
    return ctp_items.read(app)


def _places(hits):
    return [(h["item_id"], h["rule"], [where for where, _text in h["where"]]) for h in hits]


# --------------------------------------------------------------------------- the documents

def test_an_item_document_is_read_slot_by_slot():
    item_id, doc = fx.filter_item()
    item = ctp_items.decode_item(item_id, doc)
    assert (item["own_id"], item["own_bytes"], item["feed"], item["kind"], item["payload_ok"]) == (
        fx.OWN_ID, fx.OWN, "feed:17-2-0", 16, True)
    assert ("2.16.2.1.2", fx.FILTER_CDN) in item["texts"]
    assert ("2.16.2.1.1", fx.FILTER_IMAGE) in item["texts"]
    assert ("2.16.7.1.5.1", fx.FONT_URL) in item["texts"]
    # the rank text is a slot of its own, not read
    assert "000000000000001" not in [text for _path, text in item["texts"]]


def test_a_document_whose_slot_0_is_not_the_row_s_item_id_is_nothing():
    item_id, doc = fx.filter_item()
    assert ctp_items.decode_item(item_id.replace("feed:17", "feed:12"), doc) is None
    for junk in (b"", None, b"\xff" * 64, b"\x04\x00\x00\x00" + b"\x00" * 8):
        assert ctp_items.decode_item(item_id, junk) is None


def test_a_payload_that_does_not_parse_to_its_last_byte_has_no_kind_and_no_texts():
    item_id = f"{fx.OWN_ID}-feed:17-2-0-0"
    item = ctp_items.decode_item(item_id, fx.item_doc(item_id, fx.filter_payload()[:-3], fx.OWN_ID))
    assert (item["payload_ok"], item["kind"], item["texts"], item["own_bytes"]) == (False, None, [], None)
    assert (item["own_id"], item["feed"]) == (fx.OWN_ID, "feed:17-2-0")       # the FlatBuffers strings
    # payload field 6 that slot 3 is not the base64 of is not the item's own id
    other = fx.item_doc(item_id, fx.filter_payload(own=b"another-own"), fx.OWN_ID)
    assert ctp_items.decode_item(item_id, other)["own_bytes"] is None


ODD_OWN = b"odd-own-01"
ODD_URL = "https://cf-st.example.net/d/SyntheticOdd1?uc=3"


def _odd_item(field2):
    """An item whose payload parses but whose field 2 is not a message: ``field2`` is the raw field
    (key and value), its URL at payload 7.1."""
    own_id = base64.b64encode(ODD_OWN).decode()
    item_id = f"{own_id}-feed:17-2-0-0"
    payload = field2 + fx.message((6, ODD_OWN), (7, [(1, ODD_URL), (2, 1)]))
    return item_id, fx.item_doc(item_id, payload, own_id)


ODD_FIELD_2 = {"a number": fx.message((2, 5)), "a text": fx.message((2, "some text value")),
               "8 bytes": b"\x11" + b"\x00" * 8, "4 bytes": b"\x15synt"}


def test_a_payload_whose_field_2_is_not_a_message_keeps_its_texts_and_has_no_kind():
    for what, field2 in ODD_FIELD_2.items():
        item = ctp_items.decode_item(*_odd_item(field2))
        assert (item["payload_ok"], item["kind"], item["own_bytes"]) == (True, None, ODD_OWN), what
        assert ("7.1", ODD_URL) in item["texts"], what
    texts = ctp_items.decode_item(*_odd_item(ODD_FIELD_2["a text"]))["texts"]
    assert ("2", "some text value") in texts
    # a field 2 holding two field numbers, or one numbered 0, is no kind either
    assert ctp_items._kind(fx.message((2, [(1, "x" * 9), (3, 1)]))) is None
    assert ctp_items._kind(fx.message((2, b"\x00" * 4))) is None
    assert ctp_items._kind(b"\xff") is None


def test_one_odd_or_unreadable_document_loses_nothing_but_itself(tmp_path, monkeypatch, caplog):
    """A field 2 that is not a message, and a document whose reading fails outright: the store's other
    items, the odd item's URL and the failed one's item_id are all still matched, and the report is
    written."""
    odd = _odd_item(ODD_FIELD_2["a number"])
    failing = fx.filter_item(own=b"failing-01", cdn="https://cf-st.example.net/d/Failing1?uc=1",
                             font=None)
    app = str(tmp_path / "app")
    fx.store(app, [fx.filter_item(), odd, failing])
    decode = ctp_items.decode_item

    def fails_once(item_id, blob):
        if item_id == failing[0]:
            raise RuntimeError("synthetic failure")
        return decode(item_id, blob)

    monkeypatch.setattr(ctp_items, "decode_item", fails_once)
    index = ctp_items.read(app)
    assert (index["stores"][0]["items"], index["stores"][0]["undecoded"]) == (3, 1)
    assert _places(ctp_items.match(ODD_URL, index)) == [(odd[0], "key", ["payload 7.1"])]
    assert ctp_items.match(ODD_URL, index)[0]["kind"] is None
    assert _places(ctp_items.match(fx.FILTER_CDN, index)) == [
        (fx.filter_item()[0], "key", ["payload 2.16.2.1.2"])]
    assert [(h["item_id"], h["decoded"]) for h in ctp_items.match(failing[0], index)] == [
        (failing[0], False)]
    # the kind cell says nothing rather than "not readable": the payload was read
    claim = {"external_key": ODD_URL, "user_id": fx.USER}
    assert "payload not readable" not in cc._ctp_items_html(
        {"ctp_items": cc._ctp_hits([claim], index)}, None, None)
    ofx.cache_db(app, [("a" * 32, 25, ODD_URL), ("b" * 32, 25, fx.FILTER_CDN)])
    out = str(tmp_path / "Reports" / "CacheController")
    stage = cc.index(app, outdir=out, tz="utc")
    assert sum(1 for e in stage.model if e.get("ctp_items")) == 2
    # and a store that cannot be read at all costs its items, never the report
    monkeypatch.setattr(ctp_items, "read", lambda app: 1 / 0)
    with caplog.at_level(logging.WARNING):
        stage = cc.index(app, outdir=str(tmp_path / "unread" / "CacheController"), tz="utc")
    assert stage["ctp_stores"] == [] and not any(e.get("ctp_items") for e in stage.model)
    assert "creative-tools item stores (primary.docobjects ctp__item_5) not read" in caplog.text
    assert cc.render(stage)


def test_the_feed_is_named_from_the_feed_tree_and_only_from_it():
    feeds = ctp_items.decode_feed_tree(fx.tree_doc())
    label = ctp_items.feed_label("feed:17-2-0", feeds)
    assert (label["short"], label["endpoint"], label["in_tree"], label["type"], label["context"]) == (
        "filters", fx.FILTERS_ENDPOINT, True, 17, 2)
    assert ctp_items.feed_label("feed:4-2-1", feeds)["short"] == "custom-stickers"
    assert ctp_items.feed_label("feed:7-2-0", feeds)["short"] == "Emoji"       # no endpoint: its NAME
    unlisted = ctp_items.feed_label("feed:16-6-0", feeds)
    assert (unlisted["in_tree"], unlisted["short"], unlisted["name"]) == (False, "", "")
    assert ctp_items.feed_label("not a feed", feeds)["in_tree"] is False
    assert (unlisted["prior"], unlisted["tree_unread"]) == (False, False)
    # no archive, an archive of another class, or no document at all: not read, never a guess — and
    # not the same as a tree that was read and lists nothing
    assert ctp_items.decode_feed_tree(fx.tree_doc(archive=b"bplist00 and then nothing")) is None
    assert ctp_items.decode_feed_tree(fx.tree_doc(archive=ofx.archive(
        ofx.Obj("SomethingElse", NAME="Preview")))) is None
    assert ctp_items.decode_feed_tree(None) is None and ctp_items.decode_feed_tree(b"") is None
    assert ctp_items.decode_feed_tree(fx.tree_doc(root=fx.feed("ten", "two", "Unnumbered"))) == {}
    # a feed the trees that were read do not list, in a store with one that was not: not known
    assert ctp_items.feed_label("feed:16-6-0", feeds, tree_unread=True)["tree_unread"] is True
    assert ctp_items.feed_label("feed:17-2-0", feeds, tree_unread=True)["tree_unread"] is False


UNREADABLE_TREE = fx.tree_doc(archive=ofx.archive(ofx.Obj("SomethingElse", NAME="Preview")))


def test_a_feed_tree_that_cannot_be_read_is_never_said_not_to_list_a_feed(tmp_path):
    index = _index(tmp_path, [fx.filter_item()], tree=UNREADABLE_TREE)
    assert index["stores"][0]["trees_undecoded"] == 1
    hits = cc._ctp_hits([{"external_key": fx.FILTER_CDN, "user_id": fx.USER}], index)
    info = hits[0]["feed_info"]
    assert (info["in_tree"], info["tree_unread"], info["short"]) == (False, True, "")
    cell = cc._ctp_feed_cell(hits[0])
    assert "ctp__feedtree not decoded" in cell and html.escape(cc.FEED_TREE_UNDECODED_BASIS) in cell
    assert "not in ctp__feedtree" not in cell and html.escape(cc.FEED_NOT_IN_TREE_BASIS) not in cell
    assert "could not be read" in hits[0]["basis"] and "does not list" not in hits[0]["basis"]
    # one tree read (context 2) and one not (context 6): a feed of neither is not known either, and
    # a feed of the tree that was read is named as before
    index = _index(tmp_path / "two", [fx.filter_item(), _ctx6_item()],
                   tree={2: fx.tree_doc(), 6: UNREADABLE_TREE})
    assert index["stores"][0]["trees_undecoded"] == 1
    assert ctp_items.match(CTX6_CDN, index)[0]["feed_info"]["tree_unread"] is True
    assert ctp_items.match(fx.FILTER_CDN, index)[0]["feed_info"]["short"] == "filters"


CTX6_CDN = "https://cf-st.example.net/d/SyntheticCtx6?uc=1"


def _ctx6_item():
    return fx.filter_item(own=b"ctx6-own-1", feed_id="feed:16-6-0", image="https://x.example.net/6.png",
                          cdn=CTX6_CDN, font=None)


def test_a_feed_only_the_checkpointed_feed_tree_lists_is_named_as_prior_state(tmp_path):
    """The -wal rewrites the context-2 tree without the custom-stickers feed."""
    current = fx.tree_doc(root=fx.feed(10, 2, "Preview", children=[
        fx.feed(17, 2, endpoint=fx.FILTERS_ENDPOINT),
        fx.feed(11, 2, "StickerPicker", children=[fx.feed(7, 2, "Emoji")])]))
    index = _index(tmp_path, [fx.filter_item(), _sticker_item()], wal_changes=lambda conn: conn.execute(
        "update ctp__feedtree set p = ? where context = 2", (current,)))
    sticker = cc._ctp_hits([{"external_key": "customSticker~" + fx.STICKER_ID, "user_id": fx.USER}],
                           index)[0]
    info = sticker["feed_info"]
    assert (info["in_tree"], info["prior"], info["short"]) == (True, True, "custom-stickers")
    cell = cc._ctp_feed_cell(sticker)
    assert "custom-stickers (prior state)" in cell
    assert html.escape("Only the checkpointed version of the store's feed tree") in cell
    assert "which only the checkpointed version of the store's feed tree names custom-stickers" \
        in sticker["basis"]
    named = ctp_items.match(fx.FILTER_CDN, index)[0]["feed_info"]
    assert (named["short"], named["prior"]) == ("filters", False)       # the current tree lists it
    assert "(prior state)" not in cc._ctp_feed_cell({"feed_info": named})


# --------------------------------------------------------------------------- the rules

def test_the_whole_key_is_a_text_of_the_item(tmp_path):
    item_id, doc = fx.filter_item()
    index = _index(tmp_path, [(item_id, doc)])
    hits = ctp_items.match(fx.FILTER_CDN, index)
    assert _places(hits) == [(item_id, "key", ["payload 2.16.2.1.2"])]
    assert (hits[0]["user_hash"], hits[0]["feed_info"]["short"], hits[0]["wal"]) == (
        fx.HASH, "filters", sqlite_open.BOTH)
    # the empty "?" a claim key can end with is the same URL — the one URL rule the reports share
    assert ctp_items.match(fx.FONT_URL + "?", index)[0]["where"] == [("payload 2.16.7.1.5.1",
                                                                       fx.FONT_URL)]
    assert ctp_items.match("HTTPS://" + fx.FILTER_IMAGE.split("://")[1], index)
    # the item_id and the own id are texts the item holds too
    assert _places(ctp_items.match(item_id, index)) == [(item_id, "key", ["its item_id"])]


def test_the_key_after_a_word_and_a_colon_or_a_tilde_is_a_text_of_the_item(tmp_path):
    music_id, music = _music_item()
    sticker_id, sticker = _sticker_item()
    index = _index(tmp_path, [(music_id, music), (sticker_id, sticker)])
    assert _places(ctp_items.match("music:" + fx.MUSIC_URL, index)) == [
        (music_id, "after_prefix", ["payload 2.7.11.1"])]
    for separator in ":~":
        hits = ctp_items.match(f"customSticker{separator}{fx.STICKER_ID}", index)
        # the id is the item_id and slot 3 alike: both places are named
        assert _places(hits) == [(sticker_id, "after_prefix",
                                  ["its item_id", "FlatBuffers slot 3 (its own id)"])]
        assert hits[0]["prefix"] == f"customSticker{separator}"
        assert hits[0]["feed_info"]["short"] == "custom-stickers"
    # never a scheme: "<word>://" is a URL, not a word and a separator
    assert ctp_items.match("customSticker://" + fx.STICKER_ID, index) == []


def test_an_item_id_with_its_feed_matches_by_the_own_id_in_slot_3(tmp_path):
    sticker_id, sticker = _sticker_item(item_id=f"{fx.STICKER_ID}-feed:4-2-0")
    index = _index(tmp_path, [(sticker_id, sticker)])
    assert _places(ctp_items.match("customSticker~" + fx.STICKER_ID, index)) == [
        (sticker_id, "after_prefix", ["FlatBuffers slot 3 (its own id)"])]


def test_the_same_id_in_another_base64_alphabet_matches_by_its_bytes(tmp_path):
    sticker_id, sticker = _sticker_item(item_id=f"{fx.STICKER_ID}-feed:4-2-0")
    index = _index(tmp_path, [(sticker_id, sticker)])
    assert len(fx.STICKER) == 13 and fx.STICKER_URLSAFE != fx.STICKER_ID
    hits = ctp_items.match("customSticker~" + fx.STICKER_URLSAFE, index)
    assert _places(hits) == [(sticker_id, "id_bytes", ["FlatBuffers slot 3 (its own id)",
                                                       "payload field 6 (its own id)"])]
    # a document that says nothing: the item_id itself, read as base64
    bare = _index(tmp_path / "bare", [_sticker_item(decodes=False)])
    assert _places(ctp_items.match("customSticker:" + fx.STICKER_URLSAFE, bare)) == [
        (fx.STICKER_ID, "id_bytes", ["its item_id"])]


def test_an_unpadded_id_that_does_not_read_as_base64_matches_by_its_text_only(tmp_path):
    """The bytes rule reads a key's id by the one rule ``--trace-ids`` reads ids by: padded, or with
    '+' or '/', or mixing upper case, lower case and digits. An unpadded urlsafe id with none of these
    is not read as base64 — the limit the report's wording states."""
    plain, padded = "Synthetic-StickerA", "Synthetic+StickerA=="       # one id, 13 bytes
    own = base64.b64decode(padded)
    assert base64_text.base64_bytes(plain) is None and base64_text.base64_bytes(padded) == own
    item = fx.filter_item(own=own, feed_id="feed:4-2-0", font=None)
    index = _index(tmp_path, [item])
    assert ctp_items.match("customSticker~" + plain, index) == []
    assert _places(ctp_items.match("customSticker~" + padded, index)) == [
        (item[0], "after_prefix", ["FlatBuffers slot 3 (its own id)"])]


def test_a_custom_sticker_whose_document_has_another_layout_is_matched_by_its_item_id(tmp_path,
                                                                                      caplog):
    with caplog.at_level(logging.INFO):
        index = _index(tmp_path, [_sticker_item(decodes=False)])
    assert index["stores"][0]["undecoded"] == 1
    assert "1 document(s) not decoded (layout differs)" in caplog.text
    for separator in ":~":
        hits = ctp_items.match(f"customSticker{separator}{fx.STICKER_ID}", index)
        assert [(h["item_id"], h["decoded"], h["feed_info"]) for h in hits] == [
            (fx.STICKER_ID, False, None)]
    claim = {"external_key": "customSticker~" + fx.STICKER_ID, "user_id": fx.USER}
    detail = cc._ctp_items_html({"ctp_items": cc._ctp_hits([claim], index)}, None, None)
    assert "document not decoded (layout differs)" in detail
    assert html.escape(cc.UNDECODED_ITEM_BASIS) in detail
    # nothing is said of what such a document holds: the "no date, no snap" is of the layout read
    assert "is not known" in cc.UNDECODED_ITEM_BASIS
    assert "On the stores examined, an item in that layout carries no date" in cc.CTP_BASIS
    assert "item record carries no date" not in detail


def test_a_query_value_a_path_segment_or_a_part_of_a_text_never_matches(tmp_path):
    index = _index(tmp_path, [_music_item(), fx.filter_item()])
    for near_miss in (fx.MUSIC_URL.replace("SyntheticTrack1", "SyntheticOther9"),   # same bo=
                      fx.MUSIC_URL.replace("uc=37", "uc=38"),
                      f"https://cf-st.example.net/d/SyntheticOther9?bo={fx.SHARED_BO}",
                      fx.SHARED_BO, "bo=" + fx.SHARED_BO, "mo=QUJD%3D",
                      "SyntheticTrack1", "music:SyntheticTrack1",                    # the id alone
                      "https://cf-st.example.net/d/SyntheticTrack1",                # without query
                      fx.MUSIC_URL[:-3], "music:" + fx.MUSIC_URL[:-1],              # a part
                      fx.MUSIC_URL + "&more=1",                                     # holding it
                      "x:1", "scale:1", "https://bitmoji.example.net/render?scale=1",  # "1" is a text
                      "Synthetic", "", None):
        assert ctp_items.match(near_miss, index) == [], near_miss


def test_a_text_two_items_of_one_store_hold_is_attributed_to_neither(tmp_path):
    first = fx.filter_item(own=b"first-own", image="https://geofilter.example.net/png/first.png")
    second = fx.filter_item(own=b"secnd-own", image="https://geofilter.example.net/png/second.png")
    index = _index(tmp_path, [first, second])            # both hold FILTER_CDN and FONT_URL
    assert ctp_items.match(fx.FILTER_CDN, index) == []
    assert ctp_items.match(fx.FONT_URL, index) == []
    assert _places(ctp_items.match("https://geofilter.example.net/png/first.png", index)) == [
        (first[0], "key", ["payload 2.16.2.1.1"])]
    # one item's own id is another's payload text: the bytes rule does not then pick the one whose
    # own id it is — a store where an earlier rule found the text is decided by that rule
    own_a = b"\xfb\xff\xbfsynthA"
    id_a = base64.b64encode(own_a).decode()
    item_a = fx.filter_item(own=own_a, image="https://geofilter.example.net/png/a.png",
                            cdn="https://cf-st.example.net/d/SyntheticA?uc=1", font=None)
    item_b = fx.filter_item(own=b"secnd-own", image="https://geofilter.example.net/png/b.png",
                            cdn=id_a, font=None)
    index = _index(tmp_path / "ab", [item_a, item_b])
    for key in (id_a, "customSticker~" + id_a, "customSticker:" + id_a):
        assert ctp_items.match(key, index) == [], key
    # the same id in the other alphabet is a text no item holds, and only A's own id is its bytes
    urlsafe = "customSticker~" + base64.urlsafe_b64encode(own_a).decode().rstrip("=")
    hits = ctp_items.match(urlsafe, index)
    assert _places(hits) == [(item_a[0], "id_bytes", ["FlatBuffers slot 3 (its own id)",
                                                      "payload field 6 (its own id)"])]
    basis = cc._ctp_hits([{"external_key": urlsafe, "user_id": fx.USER}], index)[0]["basis"]
    assert "the same id, in another base64 alphabet or padding" in basis


def test_both_readings_are_read_and_a_hit_says_which_holds_its_text(tmp_path):
    """One item the -wal rewrote (a new image, the same CDN URL), one it deleted, one it added, one it
    left alone."""
    old_image, new_image = (f"https://geofilter.example.net/png/{n}.png" for n in ("old", "new"))
    rewritten = fx.filter_item(image=old_image)
    deleted = fx.filter_item(own=b"deleted-own", cdn="https://cf-st.example.net/d/Gone1?uc=1",
                             font=None)
    added = fx.filter_item(own=b"added-own-1", cdn="https://cf-st.example.net/d/New1?uc=1",
                           font=None)
    steady = fx.filter_item(own=b"steady-own", cdn="https://cf-st.example.net/d/Steady1?uc=1",
                            font=None)
    new_doc = fx.filter_item(image=new_image)[1]

    def changes(conn):
        conn.execute("update ctp__item_5 set p = ? where item_id = ?", (new_doc, rewritten[0]))
        conn.execute("delete from ctp__item_5 where item_id = ?", (deleted[0],))
        conn.execute("insert into ctp__item_5 (p, item_id) values (?, ?)", (added[1], added[0]))

    index = _index(tmp_path, [rewritten, deleted, steady], wal_changes=changes)
    said = {}
    for key in (fx.FILTER_CDN, new_image, old_image, "https://cf-st.example.net/d/Gone1?uc=1",
                "https://cf-st.example.net/d/New1?uc=1", "https://cf-st.example.net/d/Steady1?uc=1"):
        hits = ctp_items.match(key, index)
        assert len(hits) == 1, key
        said[key] = (hits[0]["item_id"], hits[0]["wal"], hits[0]["rewritten"])
    assert said == {
        # the -wal's version is shown, and the checkpointed one holds the text too
        fx.FILTER_CDN: (rewritten[0], sqlite_open.WAL_ONLY, True),
        new_image: (rewritten[0], sqlite_open.WAL_ONLY, False),
        old_image: (rewritten[0], sqlite_open.MAIN_ONLY, False),   # prior state
        "https://cf-st.example.net/d/Gone1?uc=1": (deleted[0], sqlite_open.MAIN_ONLY, False),
        "https://cf-st.example.net/d/New1?uc=1": (added[0], sqlite_open.WAL_ONLY, False),
        "https://cf-st.example.net/d/Steady1?uc=1": (steady[0], sqlite_open.BOTH, False)}
    # the version shown for a text both readings hold is the current one
    current = ctp_items.match(fx.FILTER_CDN, index)[0]
    assert ("2.16.2.1.1", new_image) in current["texts"]
    claim = {"external_key": old_image, "user_id": fx.USER}
    assert "only without its -wal" in cc._ctp_hits([claim], index)[0]["basis"]
    claim = {"external_key": fx.FILTER_CDN, "user_id": fx.USER}
    hits = cc._ctp_hits([claim], index)
    assert "the -wal rewrote the item's row" in hits[0]["basis"]
    # the (read from) cell is the shown version's own reading: a rewritten row is never "both, so the
    # two readings agree"
    agree = html.escape(sqlite_open.MARKER_HELP[sqlite_open.BOTH])
    detail = cc._ctp_items_html({"ctp_items": hits}, None, None)
    assert '<span class="walbadge walonly">-wal only</span>' in detail and agree not in detail
    claim = {"external_key": "https://cf-st.example.net/d/Steady1?uc=1", "user_id": fx.USER}
    detail = cc._ctp_items_html({"ctp_items": cc._ctp_hits([claim], index)}, None, None)
    assert '<span class="muted">both</span>' in detail and agree in detail


def test_every_account_s_store_is_read_and_each_hit_says_whose_it_is(tmp_path):
    app = str(tmp_path / "app")
    fx.store(app, [fx.filter_item()], user_hash=fx.HASH)
    fx.store(app, [fx.filter_item(own=b"other-acct")], user_hash=fx.OTHER_HASH)  # the same URLs
    index = ctp_items.read(app)
    assert sorted(s["user_hash"] for s in index["stores"]) == sorted((fx.HASH, fx.OTHER_HASH))
    assert sorted(h["user_hash"] for h in ctp_items.match(fx.FILTER_CDN, index)) == sorted(
        (fx.HASH, fx.OTHER_HASH))
    hits = cc._ctp_hits([{"external_key": fx.FILTER_CDN, "user_id": fx.USER}], index)
    assert {h["user_hash"]: h["own_account"] for h in hits} == {fx.HASH: True, fx.OTHER_HASH: False}
    other = next(h for h in hits if h["user_hash"] == fx.OTHER_HASH)
    assert f"The claim was made by account {fx.USER}, and this store is another account's" \
        in other["basis"]
    # a store without the item table, and an app folder with no store, are read as nothing
    bare = str(tmp_path / "bare")
    fx.store(bare, [], tables=False)
    assert ctp_items.read(bare) == ctp_items.empty_index()
    assert ctp_items.read(str(tmp_path / "nothing")) == ctp_items.empty_index()
    merged = ctp_items.merge([index, ctp_items.read(bare)])
    assert len(merged["stores"]) == 2 and len(ctp_items.match(fx.FILTER_CDN, merged)) == 2


def test_whether_the_readings_differ_is_said_of_the_tables_read_never_of_the_store(tmp_path):
    """The -wal changes the store's contacts table only: the item tables agree, and the header must
    not say the whole store's readings do."""
    app = str(tmp_path / "app")
    store = fx.store(app, [fx.filter_item()], wal_changes=lambda conn: conn.execute(
        "insert into snapchatter (p, userId) values (?, ?)", (b"synthetic", fx.OTHER_USER)))
    _rows, marks, _info = sqlite_open.read_all(store, "snapchatter")
    assert sqlite_open.WAL_ONLY in marks                    # the store's readings do differ
    index = ctp_items.read(app)
    record = index["stores"][0]
    assert record["differs"] == {"ctp__item_5": False, "ctp__feedtree": False}
    state = cc._ctp_store_state(record)
    assert state.endswith("; ctp__item_5: both readings agree; ctp__feedtree: both readings agree")
    assert "bytes; both readings agree" not in state and "-wal " in state
    ofx.cache_db(app, [("a" * 32, 25, fx.FILTER_CDN)])
    out = str(tmp_path / "Reports" / "CacheController")
    cc.render(cc.index(app, outdir=out, tz="utc"))
    page = open(os.path.join(out, "CacheController_report.html"), encoding="utf-8").read()
    assert html.escape(state) in page
    # an item table the -wal changed is said to differ; a store with no -wal is one reading
    changed = _index(tmp_path / "changed", [fx.filter_item()], wal_changes=lambda conn: conn.execute(
        "insert into ctp__item_5 (p, item_id) values (?, ?)", (_music_item()[1], _music_item()[0])))
    assert cc._ctp_store_state(changed["stores"][0]).endswith(
        "; ctp__item_5: the two readings DIFFER; ctp__feedtree: both readings agree")
    plain = _index(tmp_path / "plain", [fx.filter_item()])["stores"][0]
    assert plain["differs"] == {} and cc._ctp_store_state(plain) == sqlite_open.describe(plain["info"])
    assert "no -wal" in cc._ctp_store_state(plain)


def test_reading_leaves_no_copy_of_the_store_anywhere(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = str(tmp_path / "app")
    item = fx.filter_item()
    fx.store(app, [item], wal_changes=lambda conn: conn.execute(
        "insert into ctp__item_5 (p, item_id) values (?, ?)", (_music_item()[1], _music_item()[0])))
    staged = os.path.join(tempfile.gettempdir(), "scauto_sqlite_*")
    before = set(glob.glob(staged))
    assert ctp_items.read(app)["stores"][0]["info"]["wal_bytes"]
    assert set(glob.glob(staged)) <= before
    assert glob.glob(str(tmp_path / "**" / "sqlite_views"), recursive=True) == []
    # nor does the report: its folder holds copies of cache_controller.db only
    ofx.cache_db(app, [("0" * 32, 25, fx.FILTER_CDN)])
    out = str(tmp_path / "Reports" / "CacheController")
    cc.index(app, outdir=out, tz="utc")
    assert glob.glob(os.path.join(out, "**", "primary.docobjects*"), recursive=True) == []


def test_base64_ids_are_read_by_one_rule_shared_with_trace_ids():
    assert trace_ids.base64_bytes is base64_text.base64_bytes
    assert base64_text.base64_bytes(fx.STICKER_URLSAFE) == fx.STICKER
    assert base64_text.base64_bytes(fx.STICKER_ID) == fx.STICKER


# --------------------------------------------------------------------------- the report

CK_CDN, CK_OTHER, CK_CHAT, CK_STICKER = ("a" * 32, "b" * 32, "c" * 32, "d" * 32)
CK_MEM, CK_LENS, CK_NONE, CK_X = ("e" * 32, "f" * 32, "0" * 31 + "1", "0" * 31 + "2")
CONV = "1111aaaa-2222-4333-8444-55555555cccc"


def _report_app(tmp_path, store=True, claims=None, scdb=False):
    app = str(tmp_path / "app")
    claims = claims or [(CK_CDN, 25, fx.FILTER_CDN), (CK_OTHER, 18, "music:" + fx.MUSIC_URL),
                        (CK_CHAT, 2, "customSticker~" + fx.STICKER_ID),
                        (CK_STICKER, 2, "customSticker:" + fx.STICKER_ID),
                        (CK_MEM, 25, fx.FONT_URL), (CK_LENS, 25, LENS_URL),
                        (CK_NONE, 25, OTHER_URL)]
    if scdb:                                   # a Memory a minute from the claims: a lead's partner
        ofx.scdb(app, [(ofx.SNAP_A, ofx.overlay([]), 0)])
    ofx.cache_db(app, claims)
    for claim in claims:
        ofx.cached_file(app, claim[0])
    if store:
        fx.store(app, [fx.filter_item(), _music_item(), _sticker_item(), _lens_item()])
    return app


def _entries(app, tmp_path, mem_index=None, chat_links=None):
    scfull, scparts = cc.index_sccontent(app)
    ms_fmt, _label = cc.make_ms_formatter("utc")
    entries, _virtual, _wal = cc.build_entries(
        cc.find_cache_controllers(app)[0], app, scfull, scparts, mem_index or cc.load_memory_index(app),
        chat_links or {}, ms_fmt, {}, {}, workdir=str(tmp_path / "work"),
        ctp_index=ctp_items.read(app))
    return {e["cache_key"]: e for e in entries}


CHAT = {CK_CHAT: [{"conversation_id": CONV, "server_message_id": "7.0", "anchor": "m"}]}


def test_the_category_is_given_only_in_place_of_cdn_other_and_chat_media_and_no_link(tmp_path):
    app = _report_app(tmp_path)
    mem_index = dict(cc.load_memory_index(app), url_keys={CK_MEM: (ofx.SNAP_A, fx.HASH, "ZMEDIAURL")})
    entries = _entries(app, tmp_path, mem_index, CHAT)
    got = {key: (e["category"], len(e["ctp_items"])) for key, e in entries.items()}
    assert got == {CK_CDN: (cc.CTP_CATEGORY, 1),          # was CDN media
                   CK_OTHER: (cc.CTP_CATEGORY, 1),        # was Other
                   CK_STICKER: (cc.CTP_CATEGORY, 1),      # was Chat media, and no chat links to it
                   CK_CHAT: ("Chat media", 1),            # a chat message links to it
                   CK_MEM: ("CDN media", 1),              # a Memory links to it
                   CK_LENS: ("Lens", 1),                  # a category that says what it is
                   CK_NONE: ("CDN media", 0)}             # no item names it
    # without a store, every entry is what it was
    bare = _entries(_report_app(tmp_path / "bare", store=False), tmp_path / "bare", mem_index, CHAT)
    assert {k: e["category"] for k, e in bare.items()} == {
        CK_CDN: "CDN media", CK_OTHER: "Other", CK_STICKER: "Chat media", CK_CHAT: "Chat media",
        CK_MEM: "CDN media", CK_LENS: "Lens", CK_NONE: "CDN media"}
    assert not any(e["ctp_items"] for e in bare.values())


def test_a_filter_listing_does_not_keep_the_category(tmp_path):
    """A Memory's overlay record listing the asset is a relation, not what the file is."""
    app = _report_app(tmp_path, claims=[(CK_CDN, 25, fx.FILTER_CDN)])
    ofx.scdb(app, [(ofx.SNAP_A, ofx.overlay([ofx.geofilter("1", image=fx.FILTER_CDN)]), 0)])
    entry = _entries(app, tmp_path)[CK_CDN]
    assert entry["filter_memories"] and entry["memory"] is None
    assert entry["category"] == cc.CTP_CATEGORY and entry["ctp_items"]


def test_a_file_an_item_names_is_never_a_possible_memory(tmp_path):
    """An 'Other' claim of context 19 is a lead candidate, and a Memory's time is a minute off:
    without the store it is a "possible Memory"; with an item naming it, the item explains it."""
    claims = [(CK_STICKER, 19, "customSticker~" + fx.STICKER_ID)]
    control = _report_app(tmp_path / "control", store=False, claims=claims, scdb=True)
    stage = cc.index(control, outdir=str(tmp_path / "control" / "Reports" / "CacheController"))
    entry = next(e for e in stage.model if e["cache_key"] == CK_STICKER)
    assert entry["category"] == "Other" and entry["leads"]
    named = _report_app(tmp_path / "named", claims=claims, scdb=True)
    stage = cc.index(named, outdir=str(tmp_path / "named" / "Reports" / "CacheController"))
    entry = next(e for e in stage.model if e["cache_key"] == CK_STICKER)
    assert entry["category"] == cc.CTP_CATEGORY and entry["ctp_items"] and not entry["leads"]
    assert stage.sel.edges == []                           # information on the file, never an edge


def test_a_file_an_item_names_is_never_a_lead_whatever_its_category(tmp_path):
    """A snap editor's working copy is a lead category the item does not replace: the file keeps it,
    and the item still explains the file."""
    claims = [(CK_X, 34, "11111111-2222-4333-8444-555555555555~0"), (CK_X, 25, fx.FILTER_CDN)]
    control = _report_app(tmp_path / "control", store=False, claims=claims, scdb=True)
    stage = cc.index(control, outdir=str(tmp_path / "control" / "Reports" / "CacheController"))
    entry = next(e for e in stage.model if e["cache_key"] == CK_X)
    assert entry["category"] == "Snap editor" and entry["leads"]
    named = _report_app(tmp_path / "named", claims=claims, scdb=True)
    stage = cc.index(named, outdir=str(tmp_path / "named" / "Reports" / "CacheController"))
    entry = next(e for e in stage.model if e["cache_key"] == CK_X)
    assert entry["category"] == "Snap editor" and entry["ctp_items"] and not entry["leads"]


def test_the_detail_says_what_the_item_is_and_how_the_key_names_it(tmp_path):
    app = _report_app(tmp_path)
    entries = _entries(app, tmp_path, chat_links=CHAT)
    detail = cc._detail_html(entries[CK_CDN], "../", None, None)
    for words in ("Named by a creative-tools item — primary.docobjects › ctp__item_5",
                  fx.filter_item()[0], "own id", fx.OWN_ID, "feed:17-2-0", "— filters",
                  "payload field 2.16", "the whole key", "payload 2.16.2.1.2",
                  "texts in item", html.escape(fx.FILTER_IMAGE), "userHash " + fx.HASH[:12],
                  "the claiming account&#39;s own store", html.escape(cc.CTP_BASIS)):
        assert words in detail, words
    for overstated in (" used", "is an item"):
        assert overstated not in cc.CTP_BASIS
    for rule in ("item_id", "base64", "after a word"):          # every rule the match goes by
        assert rule in cc.CTP_BASIS
    music = cc._detail_html(entries[CK_OTHER], "../", None, None)
    assert "the key after music:" in music and "not in ctp__feedtree" in music
    assert html.escape(cc.FEED_NOT_IN_TREE_BASIS) in music
    # the context named "Chat media" is explained beside a file the item names, and only there
    sticker = cc._detail_html(entries[CK_STICKER], "../", None, None)
    assert "the key after customSticker:" in sticker and html.escape(cc.MCT_CTP_NOTE) in sticker
    assert html.escape(cc.MCT_CTP_NOTE) not in cc._detail_html(entries[CK_CHAT], "../", None, None)
    assert cc._ctp_items_html(entries[CK_NONE], None, None) == ""
    assert "creative-tools" not in cc._detail_html(entries[CK_NONE], "../", None, None)
    # a version only the checkpointed file holds is badged so
    hit = dict(entries[CK_CDN]["ctp_items"][0], wal=sqlite_open.MAIN_ONLY)
    assert "no -wal only" in cc._ctp_items_html({"ctp_items": [hit]}, None, None)


def test_the_item_s_texts_are_escaped(tmp_path):
    entries = _entries(_report_app(tmp_path), tmp_path)
    detail = cc._ctp_items_html(entries[CK_LENS], None, None)
    assert html.escape(NASTY) in detail
    assert "</script>" not in detail and "<i>bold</i>" not in detail


def test_the_page_counts_and_finds_them_and_says_nothing_when_there_are_none(tmp_path):
    out = str(tmp_path / "Reports" / "CacheController")
    stage = cc.index(_report_app(tmp_path), outdir=out, tz="utc")
    assert [s["user_hash"] for s in stage["ctp_stores"]] == [fx.HASH]
    cc.render(stage)
    page = open(os.path.join(out, "CacheController_report.html"), encoding="utf-8").read()
    assert f"<option value='{cc.CTP_CATEGORY}'>" in page
    assert "<b>6</b> cached file(s) are named by an item of an account's creative-tools store" in page
    assert "primary.docobjects (" in page                  # the store, with what was read of it
    text = open(os.path.join(out, "data", "index.js"), encoding="utf-8").read()
    rows = json.loads(text[text.index("(") + 1:text.rindex(")")])
    row = next(r for r in rows if r[0] == f"ck-{CK_CDN}")
    assert row[5]["cat"] == cc.CTP_CATEGORY and row[5]["link"] == ""
    for found_by in (fx.filter_item()[0].lower(), fx.FILTER_IMAGE.lower(), "ctp__item_5", "filters",
                     "payload 2.16", cc.CTP_CATEGORY.lower()):
        assert found_by in row[2], found_by
    # a file that kept its category is found by its item, not by the category it is not in: the
    # search and the Category filter agree
    lens = next(r for r in rows if r[0] == f"ck-{CK_LENS}")
    assert lens[5]["cat"] == "Lens" and "ctp__item_5" in lens[2]
    assert cc.CTP_CATEGORY.lower() not in lens[2]
    # a store whose items name no claim: the page is what it was
    out = str(tmp_path / "none" / "Reports" / "CacheController")
    stage = cc.index(_report_app(tmp_path / "none", claims=[(CK_NONE, 25, OTHER_URL)]), outdir=out,
                     tz="utc")
    assert stage["ctp_stores"] and not any(e.get("ctp_items") for e in stage.model)
    cc.render(stage)
    page = open(os.path.join(out, "CacheController_report.html"), encoding="utf-8").read()
    for absent in ("creative-tools", cc.CTP_CATEGORY, "ctp__item_5"):
        assert absent not in page, absent


def test_an_android_app_folder_has_no_store_and_its_report_none_of_this(tmp_path):
    import android_fixture
    app = android_fixture.build_app(str(tmp_path / "android"))
    assert ctp_items.find_stores(app) == []
    assert ctp_items.read(app) == ctp_items.empty_index()
    out = str(tmp_path / "Reports" / "CacheController")
    stage = cc.index(app, outdir=out, tz="utc")
    assert stage["ctp_stores"] == [] and not any(e.get("ctp_items") for e in stage.model)
    cc.render(stage)
    page = open(os.path.join(out, "CacheController_report.html"), encoding="utf-8").read()
    assert "creative-tools" not in page and cc.CTP_CATEGORY not in page
