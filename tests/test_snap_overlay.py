"""A Memory's overlay record (scdb-27 ``ZGALLERYSNAPDETAIL.ZOVERLAY``) and the asset URLs it lists.

The record is an NSKeyedArchiver archive of ``SOJUGallerySnapOverlay``. Its geofilters each give the
URL of an image, a sky image and the font of their text; those URLs are what a cached file's claim
key is matched against, whole, by the one rule :func:`snap_overlay.normalise_url`. A record commonly
lists several filters and names the selected one separately (often none) — so the reader reports
``selected`` as the record states it and nothing more. Every input is synthetic.
"""
import base64
import os
import plistlib
import sqlite3

import overlay_fixture as fx
from scripts.data import snap_overlay as so
from scripts.data import sqlite_open


def _two_filters(**kwargs):
    a = fx.geofilter("101", image=fx.IMAGE_URL)
    b = fx.geofilter("202", sky=fx.SKY_URL, fonts=[fx.FONT_URL], kind="DYNAMIC", group="DAY_GROUP")
    return so.unarchive(fx.overlay([a, b], **kwargs))


def test_every_asset_url_of_every_listed_geofilter_with_its_place_in_the_record():
    assets = so.filter_assets(_two_filters(selected="202"))
    assert [(a["role"], a["field"], a["url"]) for a in assets] == [
        ("filter image", "filters.geoFilters[0].imageUrl", fx.IMAGE_URL),
        ("sky image", "filters.geoFilters[1].arSegmentation.sky.replacementSkyUrl", fx.SKY_URL),
        ("font of the filter text",
         "filters.geoFilters[1].geofilterMarkups[0].displayParameters.font", fx.FONT_URL)]
    # the record names filter 202 as selected: filter 101's asset is "another", 202's are "this one"
    assert [a["selected"] for a in assets] == [False, True, True]
    assert assets[1]["filter_type"] == "DYNAMIC" and assets[1]["group"] == "DAY_GROUP"
    assert assets[0]["filter_id"] == "101" and assets[0]["key"] == fx.IMAGE_URL


def test_selected_is_what_the_record_says_and_none_when_it_says_nothing():
    assert {a["selected"] for a in so.filter_assets(_two_filters())} == {None}
    listed = so.filter_assets(_two_filters(selected_ids=["202"]))
    assert [a["selected"] for a in listed] == [False, True, True]
    # an id the record names that no listed filter has: every listed one is "another"
    assert {a["selected"] for a in so.filter_assets(_two_filters(selected="999"))} == {False}
    empties = so.unarchive(fx.overlay([], selected="", selected_ids=["", "7"]))
    assert so.selected_ids(empties) == ["7"]


def test_the_shared_address_of_a_filter_chosen_by_its_parameters_is_never_an_asset():
    with_params = fx.geofilter("303", image=fx.SHARED_ADDRESS, params={"json": "{\"id\": 1}"})
    no_image = fx.geofilter("404")
    record = so.unarchive(fx.overlay([with_params, no_image]))
    assert so.filter_assets(record) == []
    # an empty parameter dictionary is the ordinary case: the URL is the image's own
    plain = so.unarchive(fx.overlay([fx.geofilter("505", image=fx.IMAGE_URL, params={})]))
    assert [a["url"] for a in so.filter_assets(plain)] == [fx.IMAGE_URL]


def test_a_listed_item_of_another_class_and_a_non_url_give_no_asset():
    other = fx.Obj("SOJUGallerySomethingElse", imageUrl=fx.IMAGE_URL)
    not_url = fx.geofilter("606", image="cf-st.example.net/d/no-scheme")
    assert so.filter_assets(so.unarchive(fx.overlay([other, not_url]))) == []


def test_anything_but_an_overlay_archive_is_none_and_lists_nothing():
    protobuf = bytes([0x0a, 0x05]) + b"hello"
    another_root = fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL)], root="SOJUSomethingElse")
    another_archiver = fx.archive(fx.Obj(so.ROOT_CLASS, filters=fx.Obj(
        "SOJUGalleryFilters", geoFilters=[fx.geofilter("1", image=fx.IMAGE_URL)])),
        archiver="NRKeyedArchiver")
    for blob in (None, b"", b"bplist00 not really", protobuf, another_root, another_archiver):
        assert so.unarchive(blob) is None
        assert so.filter_assets(so.unarchive(blob)) == []
    assert so.filter_assets({"filters": "not a filters object"}) == []


def test_the_url_rule_is_the_whole_url_less_one_empty_trailing_query_or_fragment():
    n = so.normalise_url
    assert n(fx.SKY_URL + "?") == n(fx.SKY_URL) == fx.SKY_URL
    assert n(fx.SKY_URL + "#") == fx.SKY_URL
    assert n(fx.IMAGE_URL + "#") == fx.IMAGE_URL                    # an empty fragment after a query
    # an empty query and an empty fragment: one is dropped, not both
    assert n(fx.SKY_URL + "?#") == fx.SKY_URL + "?" != n(fx.SKY_URL)
    # a "?" or "#" that ends a query or fragment with something in it is part of it, and stays
    assert n(fx.SKY_URL + "??") == fx.SKY_URL + "??"                # the query is "?"
    assert n(fx.IMAGE_URL + "?") == fx.IMAGE_URL + "?" != n(fx.IMAGE_URL)
    assert n(fx.SKY_URL + "#x#") == fx.SKY_URL + "#x#" != n(fx.SKY_URL + "#x")
    assert n(fx.SKY_URL + "#x?") == fx.SKY_URL + "#x?"              # a "?" inside the fragment
    # nothing is trimmed: a text with a space around it is another text
    assert n(" " + fx.SKY_URL) == ""
    assert n(fx.SKY_URL + " ") == fx.SKY_URL + " " != n(fx.SKY_URL)
    assert n("HTTPS://cf-st.example.net/d/X") == "https://cf-st.example.net/d/X"
    # nothing else changes case, and nothing is unquoted: '%3D' and '=' are different texts
    assert n("https://CF-ST.example.net/d/X") != n("https://cf-st.example.net/d/X")
    assert n(fx.IMAGE_URL) != n(fx.IMAGE_URL.replace("%3D", "="))
    assert n(fx.IMAGE_URL) != n(fx.IMAGE_URL.replace("uc=1", "uc=2"))
    for text in ("", "cf-st.example.net/d/X", "ftp://example.net/x", "file:///x", "https://",
                 None, 12, "customSticker~" + base64.b64encode(b"synthetic").decode()):
        assert n(text) == ""


def test_the_summary_counts_the_geofilters_and_names_the_selected_one():
    record = _two_filters(selected="202")
    found = so.summary(record)
    assert found["geofilters"] == 2
    assert found["selected"] == [{"filter_id": "202", "filter_type": "DYNAMIC", "group": "DAY_GROUP",
                                  "content": "UNRECOGNIZED_VALUE"}]
    unlisted = so.summary(_two_filters(selected="999"))["selected"]
    assert unlisted == [{"filter_id": "999", "filter_type": "", "group": "", "content": ""}]


def test_both_readings_are_joined_to_their_own_snap_and_the_evidence_is_untouched(tmp_path):
    app = str(tmp_path / "app")
    db = fx.scdb(app, [(fx.SNAP_A, fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL)]), 1),
                       (fx.SNAP_B, fx.overlay([fx.geofilter("2", fonts=[fx.FONT_URL])]), 0)],
                 wal_delete=fx.SNAP_B)
    before = sorted(os.listdir(os.path.dirname(db)))
    views = sqlite_open.open_views(db)
    try:
        records = so.read_overlays(views)
    finally:
        views.close()
    assert sorted(os.listdir(os.path.dirname(db))) == before          # no -shm, nothing written
    by_snap = {r["snap_id"]: r for r in records}
    assert [r["snap_id"] for r in records] == [fx.SNAP_A, fx.SNAP_B]  # the current reading first
    assert by_snap[fx.SNAP_A]["wal"] == sqlite_open.BOTH
    assert by_snap[fx.SNAP_A]["has_overlay_image"] == 1
    assert [a["url"] for a in by_snap[fx.SNAP_A]["assets"]] == [fx.IMAGE_URL]
    # the -wal deleted snap B and its record: recovered from the checkpointed reading, joined there
    assert by_snap[fx.SNAP_B]["wal"] == sqlite_open.MAIN_ONLY
    assert by_snap[fx.SNAP_B]["has_overlay_image"] == 0
    assert [(a["url"], a["wal"]) for a in by_snap[fx.SNAP_B]["assets"]] == [
        (fx.FONT_URL, sqlite_open.MAIN_ONLY)]


def _damaged(blob):
    """``blob`` with its root's class hierarchy holding a dictionary where a class name belongs."""
    archive = plistlib.loads(blob)
    for entry in archive["$objects"]:
        if isinstance(entry, dict) and entry.get("$classname") == so.ROOT_CLASS:
            entry["$classes"] = [so.ROOT_CLASS, {"damaged": 1}]
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def test_a_damaged_record_is_none_and_the_other_records_are_still_read(tmp_path):
    damaged = _damaged(fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL)]))
    assert so.unarchive(damaged) is None
    db = fx.scdb(str(tmp_path / "app"), [(fx.SNAP_A, damaged, 1),
                                         (fx.SNAP_B, fx.overlay([fx.geofilter("2", fonts=[fx.FONT_URL])]),
                                          0)])
    views = sqlite_open.open_views(db)
    try:
        records = so.read_overlays(views)
    finally:
        views.close()
    assert [(r["snap_id"], [a["url"] for a in r["assets"]]) for r in records] == [
        (fx.SNAP_B, [fx.FONT_URL])]


def test_a_schema_without_the_flag_or_the_table_reads_what_is_there(tmp_path):
    record = fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL)])
    db = fx.scdb(str(tmp_path / "a"), [(fx.SNAP_A, record, None)], has_flag=False)
    views = sqlite_open.open_views(db)
    try:
        assert [r["has_overlay_image"] for r in so.read_overlays(views)] == [None]
        views.merged.execute("select 1")                  # still open: nothing was closed under it
    finally:
        views.close()
    bare = tmp_path / "b.sqlite3"
    conn = sqlite3.connect(str(bare))
    conn.execute("create table ZGALLERYSNAP (Z_PK integer primary key, ZSNAPID varchar)")
    conn.commit()
    conn.close()
    views = sqlite_open.open_views(str(bare))
    try:
        assert so.read_overlays(views) == []
    finally:
        views.close()
