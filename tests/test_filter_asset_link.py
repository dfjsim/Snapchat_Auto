"""A cached filter asset is linked to the Memories whose overlay record lists it — and to nothing more.

A Memory's ``ZGALLERYSNAPDETAIL.ZOVERLAY`` lists the snap's geofilters with the URLs of their image,
sky image and font. A cache_controller claim keyed by exactly one of those URLs is a cached asset of a
listed filter: the cache_controller report links it to each such Memory under a relation of its own —
a dashed "filter listed" chip, a detail section, the ``Filter`` link filter, an edge of its own — and
never as the Memory's media: not ``entry["memory"]``, not the "linked to a Memory" count, not a lead.
A record lists filters it does not name as selected (often it names none), so nothing here says a
filter is on the Memory.

The fixtures hold no ``primary.docobjects``: the category asserted here is this rule's, alone. Every
input is synthetic.
"""
import html
import json
import os

import overlay_fixture as fx
from scripts import cache_controller_report as cc
from scripts import partial_report
from scripts.data import sqlite_open

CK = "0123456789abcdef0123456789abcdef"
CK_EDITOR = "fedcba9876543210fedcba9876543210"
EDITOR_UUID = "eeeeeeee-1111-4222-8333-444444444444"


def _asset(**over):
    base = {"url": fx.IMAGE_URL, "key": fx.IMAGE_URL, "role": "filter image",
            "field": "filters.geoFilters[0].imageUrl", "filter_id": "101", "filter_type": "STATIC",
            "group": "GEO_GROUP", "content": "", "selected": None, "wal": sqlite_open.BOTH,
            "has_overlay_image": 0}
    return dict(base, **over)


def _claim(ek, user=fx.USER):
    return {"external_key": ek, "user_id": user, "mct": 25}


def _urls(*listed):
    out = {}
    for sid, asset in listed:
        out.setdefault(asset["key"], []).append((sid, fx.HASH, asset))
    return out


def _closure(**included):
    """A partial run's closure holding exactly these rows, and no relation followed."""
    indexes = {}
    for kind, ids in included.items():
        indexes[kind] = partial_report.Index(kind)
        for row_id in ids:
            indexes[kind].add(row_id, {})
    selection = {"schema": 2, "selections": {k: {i: 1 for i in v} for k, v in included.items()}}
    return partial_report.expand(indexes, partial_report.resolve(indexes, selection),
                                 {**partial_report.default_options(), "relations": {}})


# --------------------------------------------------------------------------- the rule

def test_the_index_keys_every_listed_asset_by_its_whole_url(tmp_path):
    app = str(tmp_path / "app")
    fx.scdb(app, [(fx.SNAP_A, fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL),
                                          fx.geofilter("2", sky=fx.SKY_URL + "?")]), 1)])
    index = cc.load_memory_index(app)
    urls = index["overlay_urls"]
    assert set(urls) == {fx.IMAGE_URL, fx.SKY_URL}        # the empty trailing "?" dropped
    sid, user_hash, asset = urls[fx.IMAGE_URL][0]
    assert (sid, user_hash, asset["field"], asset["has_overlay_image"]) == (
        fx.SNAP_A, fx.HASH, "filters.geoFilters[0].imageUrl", 1)


def test_an_exact_url_links_and_says_what_the_record_is_and_is_not():
    links = cc._overlay_links_for([_claim(fx.IMAGE_URL)], _urls((fx.SNAP_A, _asset())))
    assert [link["snap_id"] for link in links] == [fx.SNAP_A]
    basis = links[0]["basis"]
    for words in ("ZGALLERYSNAPDETAIL.ZOVERLAY", "filters.geoFilters[0].imageUrl",
                  "not the Memory's media", "a listed filter is not shown to be on the Memory",
                  "exact match of the whole URL", "here it names none",
                  "ZGALLERYSNAP.ZHASOVERLAYIMAGE = 0"):
        assert words in basis, words
    assert not links[0]["cross_account"]


def test_an_id_a_parameter_or_a_host_in_common_is_not_a_match():
    urls = _urls((fx.SNAP_A, _asset()))
    for near_miss in (fx.IMAGE_URL.replace("cf-st.example.net", "cdn.example.net"),   # other host
                      fx.IMAGE_URL.replace("/d/", "/e/"),                            # other path
                      fx.IMAGE_URL.replace("AAAAAAAAAAAA", "BBBBBBBBBBBB"),          # same mo=
                      fx.IMAGE_URL.replace("uc=1", "uc=2"),                          # other uc=
                      fx.IMAGE_URL.replace("%3D", "="),                              # unquoted
                      fx.IMAGE_URL + "?",                     # a "?" ending a query is part of it
                      "AAAAAAAAAAAA", "snap-media-AAAAAAAAAAAA"):
        assert cc._overlay_links_for([_claim(near_miss)], urls) == [], near_miss


def test_a_claim_key_with_an_empty_query_added_links_and_says_so():
    urls = _urls((fx.SNAP_A, _asset(url=fx.SKY_URL, key=fx.SKY_URL, role="sky image")))
    links = cc._overlay_links_for([_claim(fx.SKY_URL + "?")], urls)
    assert len(links) == 1
    assert 'the empty "?" the claim key ends with' in links[0]["basis"]
    assert "the record ends with" not in links[0]["basis"]
    # each side the rule shortened is named: here the claim's empty query and the record's fragment
    urls = _urls((fx.SNAP_A, _asset(url=fx.SKY_URL + "#", key=fx.SKY_URL, role="sky image")))
    both = cc._overlay_links_for([_claim("HTTPS://" + fx.SKY_URL.split("://")[1] + "?")], urls)
    assert ('the letter case of its scheme and the empty "?" the claim key ends with and the '
            'empty "#" the record ends with') in both[0]["basis"]


def test_several_memories_give_a_link_each_sorted_by_snap_id_and_skip_its_own_media_link():
    urls = _urls((fx.SNAP_B, _asset()), (fx.SNAP_A, _asset()), (fx.SNAP_C, _asset()))
    links = cc._overlay_links_for([_claim(fx.IMAGE_URL)], urls, {fx.SNAP_A: "pages/x.html"},
                                  skip_sid=fx.SNAP_C)
    assert [link["snap_id"] for link in links] == [fx.SNAP_A, fx.SNAP_B]
    assert links[0]["page"] == "pages/x.html" and links[1]["page"] is None


def test_what_the_record_says_about_the_selected_filter_is_said_as_it_says_it():
    said = {}
    for selected in (True, False, None):
        links = cc._overlay_links_for([_claim(fx.IMAGE_URL)],
                                      _urls((fx.SNAP_A, _asset(selected=selected))))
        said[selected] = links[0]["basis"]
    assert "here it names this filter" in said[True]
    assert "here it names another filter" in said[False]
    assert "here it names none" in said[None]
    for text in said.values():
        for overstated in ("applied", " used", "placed on"):
            assert overstated not in text


def test_one_record_listing_the_asset_twice_is_one_link_naming_both_places():
    urls = _urls((fx.SNAP_A, _asset(url=fx.FONT_URL, key=fx.FONT_URL, role="font of the filter text",
                                    field="filters.geoFilters[0].geofilterMarkups[0]."
                                          "displayParameters.font")),
                 (fx.SNAP_A, _asset(url=fx.FONT_URL, key=fx.FONT_URL, role="font of the filter text",
                                    field="filters.geoFilters[1].geofilterMarkups[0]."
                                          "displayParameters.font")))
    links = cc._overlay_links_for([_claim(fx.FONT_URL)], urls)
    assert len(links) == 1
    assert links[0]["fields"] == ["filters.geoFilters[1].geofilterMarkups[0].displayParameters.font"]


def test_an_asset_listed_under_two_filters_is_linked_by_the_one_the_record_names_as_selected():
    """One font in the markups of two geofilters, the record naming the second as selected: the link
    is that filter's, so its chip and detail say what the Memory's own page says of that asset."""
    from scripts.data import snap_overlay
    record = snap_overlay.unarchive(fx.overlay([fx.geofilter("1", fonts=[fx.FONT_URL]),
                                                fx.geofilter("2", fonts=[fx.FONT_URL])], selected="2"))
    urls = _urls(*((fx.SNAP_A, asset) for asset in snap_overlay.filter_assets(record)))
    font = "geofilterMarkups[0].displayParameters.font"
    for claims in ([_claim(fx.FONT_URL)],
                   # another account's claim too: the Memory's own account still comes first
                   [_claim(fx.FONT_URL, fx.OTHER_USER), _claim(fx.FONT_URL)]):
        links = cc._overlay_links_for(claims, urls)
        assert len(links) == 1
        link = links[0]
        assert (link["selected"], link["filter_id"], link["field"], link["claim_user"]) == (
            True, "2", f"filters.geoFilters[1].{font}", fx.USER)
        assert link["fields"] == [f"filters.geoFilters[0].{font}"]
        assert "here it names this filter" in link["basis"]
    entry = {"memory": None, "chats": [], "filter_memories": links}
    assert "· filter selected" in cc._links_html(entry, "../", compact=True)
    detail = cc._filter_memories_html(entry, "../")
    assert "yes — the record names this filter" in detail and "names another filter" not in detail


def test_another_account_s_claim_is_linked_and_flagged_and_the_owner_s_claim_preferred():
    urls = _urls((fx.SNAP_A, _asset(has_overlay_image=1)))
    other = cc._overlay_links_for([_claim(fx.IMAGE_URL, fx.OTHER_USER)], urls)
    assert other[0]["cross_account"] is True
    assert f"The claim was made by account {fx.OTHER_USER}" in other[0]["basis"]
    assert "does not say which listed filter" in other[0]["basis"]       # ZHASOVERLAYIMAGE = 1
    both = cc._overlay_links_for([_claim(fx.IMAGE_URL, fx.OTHER_USER), _claim(fx.IMAGE_URL)], urls)
    assert both[0]["claim_user"] == fx.USER and both[0]["cross_account"] is False
    detail = cc._filter_memories_html({"filter_memories": other}, "../")
    assert "another account&#39;s claim" in detail


def test_a_record_only_the_checkpointed_reading_holds_says_so():
    links = cc._overlay_links_for([_claim(fx.IMAGE_URL)],
                                  _urls((fx.SNAP_A, _asset(wal=sqlite_open.MAIN_ONLY))))
    assert "only without its -wal" in links[0]["basis"]


# --------------------------------------------------------------------------- the report

def _app(tmp_path, snaps=(fx.SNAP_A,), claims=None):
    app = str(tmp_path / "app")
    fx.scdb(app, [(sid, fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL)]), 0) for sid in snaps])
    fx.cache_db(app, claims or [(CK, 25, fx.IMAGE_URL)])
    fx.cached_file(app, CK)
    return app


def _entries(app, tmp_path):
    index = cc.load_memory_index(app)
    scfull, scparts = cc.index_sccontent(app)
    ms_fmt, _label = cc.make_ms_formatter("utc")
    entries, _virtual, _wal = cc.build_entries(cc.find_cache_controllers(app)[0], app, scfull,
                                               scparts, index, {}, ms_fmt, {}, {},
                                               workdir=str(tmp_path / "work"))
    return {e["cache_key"]: e for e in entries}


def test_the_entry_carries_the_link_apart_from_its_memory_and_keeps_its_category(tmp_path):
    entry = _entries(_app(tmp_path), tmp_path)[CK]
    assert [fm["snap_id"] for fm in entry["filter_memories"]] == [fx.SNAP_A]
    assert entry["memory"] is None and entry["memory_basis"] is None
    assert entry["category"] == "CDN media"
    chip = cc._links_html(entry, "../", compact=True)
    assert "· filter listed" in chip and f"#mem-{fx.SNAP_A}" in chip and "chip mem filt" in chip
    detail = cc._detail_html(entry, "../", None, None)
    assert "Listed with a Memory&#39;s filters — not its media" in detail
    assert "filters.geoFilters[0].imageUrl" in detail
    assert "not recorded — the record names no selected geofilter" in detail


def test_several_memories_are_one_chip_that_opens_all_of_them(tmp_path):
    from scripts import report_ui
    entry = _entries(_app(tmp_path, snaps=(fx.SNAP_A, fx.SNAP_B)), tmp_path)[CK]
    chip = cc._links_html(entry, "../", compact=True)
    assert report_ui.find_fragment([fx.SNAP_A, fx.SNAP_B]) in chip
    assert "2 Memories · filter listed" in chip
    # its "?" says what the search is — the Memories' snap ids, not this entry's identifier — and
    # gives no reason for the listing that the match does not check
    full = cc._links_html(entry, "../")
    assert html.escape(cc.FILTER_MANY_LINK_BASIS) in full and "snap ids" in cc.FILTER_MANY_LINK_BASIS
    assert html.escape(cc.MULTI_TARGET_BASIS) not in full
    assert "one filter is listed" not in full
    # in a partial extract holding one of them, the chip is narrowed to that one
    closure = _closure(mem=[f"mem-{fx.SNAP_B}"], cc=[f"ck-{CK}"])
    narrowed = cc._links_html(entry, "../", compact=True, closure=closure)
    assert report_ui.find_fragment([fx.SNAP_B]) in narrowed and "1 Memory · filter listed" in narrowed
    assert fx.SNAP_A not in narrowed
    # and holding neither, it is marked absent rather than dropped
    gone = cc._links_html(entry, "../", compact=True, closure=_closure(cc=[f"ck-{CK}"]))
    assert report_ui.XOUT_MARK in gone and "href" not in gone


def _rows(path):
    text = open(path, encoding="utf-8").read()
    return json.loads(text[text.index("(") + 1:text.rindex(")")])


def test_the_page_offers_the_filter_counts_it_apart_and_records_an_edge_of_its_own(tmp_path):
    app = _app(tmp_path)
    out = str(tmp_path / "Reports" / "CacheController")
    stage = cc.index(app, outdir=out, tz="utc")
    edges = [edge for edge, _src, _kind, _dst in stage.sel.edges]
    assert edges == [partial_report.EDGE_MEMORY_FILTER_ASSET]          # never EDGE_MEMORY_CACHE
    cc.render(stage)
    html = open(os.path.join(out, "CacheController_report.html"), encoding="utf-8").read()
    assert '<option value="Filter">' in html and ".chip.mem.filt{border-style:dashed}" in html
    assert "an asset of a filter listed with a Memory — not its media" in html
    row = next(r for r in _rows(os.path.join(out, "data", "index.js")) if r[0] == f"ck-{CK}")
    assert row[5]["link"] == "Filter" and "Memory" not in row[5]["link"]
    assert fx.SNAP_A.lower() in row[2]                                  # found by the snap id
    _report, stats = cc.generate_report(stage.model, [], str(tmp_path / "again"), "UTC", "../",
                                        None, None, "db")
    assert stats["filter"] == 1 and stats["mem"] == 0


def test_a_page_with_no_such_entry_has_none_of_it(tmp_path):
    app = _app(tmp_path, claims=[(CK, 25, fx.IMAGE_URL.replace("uc=1", "uc=9"))])
    out = str(tmp_path / "Reports" / "CacheController")
    stage = cc.index(app, outdir=out, tz="utc")
    assert not any(e.get("filter_memories") for e in stage.model)
    cc.render(stage)
    html = open(os.path.join(out, "CacheController_report.html"), encoding="utf-8").read()
    for absent in ('value="Filter"', "chip mem filt", ".chip.mem.filt", "listed with a Memory"):
        assert absent not in html, absent


def test_a_file_a_filter_record_explains_is_never_a_possible_memory(tmp_path):
    """A snap-editor claim makes the file a lead candidate, and the Memory's time is a minute off:
    without the record's listing it is a "possible Memory"; with it, it is accounted for."""
    claims = [(CK, 34, f"{EDITOR_UUID}~1"), (CK, 25, fx.IMAGE_URL)]
    control = _app(tmp_path / "control", claims=[(CK, 34, f"{EDITOR_UUID}~1"),
                                                 (CK, 25, fx.IMAGE_URL.replace("uc=1", "uc=9"))])
    stage = cc.index(control, outdir=str(tmp_path / "control" / "Reports" / "CacheController"))
    entry = next(e for e in stage.model if e["cache_key"] == CK)
    assert entry["category"] == "Snap editor" and entry["leads"]
    stage = cc.index(_app(tmp_path / "listed", claims=claims),
                     outdir=str(tmp_path / "listed" / "Reports" / "CacheController"))
    entry = next(e for e in stage.model if e["cache_key"] == CK)
    assert entry["filter_memories"] and not entry["leads"]


def test_an_android_app_folder_has_no_overlay_index_and_its_report_none_of_this(tmp_path):
    import android_fixture
    app = android_fixture.build_app(str(tmp_path / "android"))
    assert "overlay_urls" not in cc.load_memory_index(app)
    out = str(tmp_path / "Reports" / "CacheController")
    stage = cc.index(app, outdir=out, tz="utc")
    assert not any(e.get("filter_memories") for e in stage.model)
    assert partial_report.EDGE_MEMORY_FILTER_ASSET not in {e[0] for e in stage.sel.edges}
    cc.render(stage)
    html = open(os.path.join(out, "CacheController_report.html"), encoding="utf-8").read()
    assert 'value="Filter"' not in html and "chip mem filt" not in html
