"""A Memory's page lists the cached assets of the filters its overlay record lists — apart from its
media, and said to be.

The Memories report reads the same record (``ZGALLERYSNAPDETAIL.ZOVERLAY``) through the same module
as the cache_controller report, and finds the cached files by the same whole-URL rule, so both ends
of the link make one match. The files are not the Memory's media: they are not decrypted, not in
Media files, not among the cache keys a selection names the Memory by. On a page several Memories
share, each asset is given under the Memory whose record lists it. Every input is synthetic.
"""
import overlay_fixture as fx
from scripts import memories_media_report as mr
from scripts import partial_report, report_ui

CK = "0123456789abcdef0123456789abcdef"
CK_GONE = "11111111111111111111111111111111"
CK_FONT = "22222222222222222222222222222222"


def test_the_claim_index_reads_both_readings_and_keys_urls_by_the_one_rule(tmp_path):
    app = str(tmp_path / "app")
    fx.cache_db(app, [(CK, 25, fx.IMAGE_URL), (CK_GONE, 25, fx.SKY_URL + "?"),
                      (CK_FONT, 2, "customSticker~c3ludGhldGlj")], wal_delete=CK_GONE)
    urls = mr.index_claim_urls(app)
    assert urls == {fx.IMAGE_URL: [(CK, 25, fx.USER, fx.IMAGE_URL)],
                    # deleted by the -wal: still a claim the checkpointed reading holds
                    fx.SKY_URL: [(CK_GONE, 25, fx.USER, fx.SKY_URL + "?")]}
    uuids, urls_again = mr.index_claims(app)
    assert urls_again == urls and uuids == mr.index_claim_uuids(app)


def _profile(app, details):
    db = fx.scdb(app, details)
    return {"userHash": fx.HASH, "scdb": db, "gallery": None, "search": None}


def test_load_memories_reads_each_memory_s_record(tmp_path):
    app = str(tmp_path / "app")
    record = fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL),
                         fx.geofilter("2", fonts=[fx.FONT_URL], kind="DYNAMIC", group="DAY_GROUP")],
                        selected="2")
    mems, _stats = mr.load_memories(_profile(app, [(fx.SNAP_A, record, 0), (fx.SNAP_B, None, 0)]),
                                    "", "", str(tmp_path / "work"))
    a = mems[fx.SNAP_A]
    assert [(x["role"], x["selected"]) for x in a["filter_assets"]] == [
        ("filter image", False), ("font of the filter text", True)]
    assert a["filter_record"]["geofilters"] == 2
    assert a["filter_record"]["selected"][0]["filter_id"] == "2"
    assert mems[fx.SNAP_B]["filter_assets"] == []                    # no record: nothing listed
    assert mr._bare_memory("X", {"userHash": fx.HASH})["filter_assets"] == []


def _memories(tmp_path, claims, details):
    app = str(tmp_path / "app")
    profile = _profile(app, details)
    fx.cache_db(app, claims)
    for ck, _ctx, _ek, *_rest in claims:
        fx.cached_file(app, ck)
    mems, _stats = mr.load_memories(profile, "", "", str(tmp_path / "work"))
    mr.collect_media(mems, app, str(tmp_path / "media"))
    return mems


def test_only_assets_with_a_cached_file_are_shown_and_none_becomes_media(tmp_path):
    record = fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL), fx.geofilter("2", sky=fx.SKY_URL)])
    mems = _memories(tmp_path, [(CK, 25, fx.IMAGE_URL)], [(fx.SNAP_A, record, 0)])
    m = mems[fx.SNAP_A]
    assert [(a["url"], a["claims"]) for a in m["filter_cached"]] == [
        (fx.IMAGE_URL, [(CK, 25, fx.USER, fx.IMAGE_URL)])]
    assert m["media_files"] == []                                    # not decrypted, not published
    assert CK not in mr._cache_tokens(m)                              # not a key that names it
    assert "filter_assets" not in m              # the full list is not kept once the cached are found
    page = mr._filter_assets_html([m])
    assert f"CacheController_report.html#ck-{CK}" in page
    assert "filters.geoFilters[0].imageUrl" in page and "filter image" in page
    assert "not recorded — the record names no selected geofilter" in page
    assert "Cached assets of filters listed with this Memory — not its media" in page
    assert "lists 2 geofilter(s); it names none as selected" in page
    assert "<th>Snap</th>" not in page                                # one Memory: no Snap column


def test_the_section_says_what_the_record_selects_and_whose_claim_it_is(tmp_path):
    record = fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL),
                         fx.geofilter("9", kind="DYNAMIC", group="POST_CAPTURE_LENS_DEFAULT_GROUP")],
                        selected="9")
    mems = _memories(tmp_path, [(CK, 25, fx.IMAGE_URL, fx.OTHER_USER)], [(fx.SNAP_A, record, 0)])
    page = mr._filter_assets_html([mems[fx.SNAP_A]])
    assert "no — the record names another filter" in page
    assert "it names idValue 9 (DYNAMIC, POST_CAPTURE_LENS_DEFAULT_GROUP) as selected" in page
    assert "another account&#39;s claim" in page


def test_an_asset_only_the_checkpointed_record_lists_says_so(tmp_path):
    app = str(tmp_path / "app")
    db = fx.scdb(app, [(fx.SNAP_A, fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL)]), 0)],
                 wal_delete=fx.SNAP_A)
    fx.cache_db(app, [(CK, 25, fx.IMAGE_URL)])
    profile = {"userHash": fx.HASH, "scdb": db, "gallery": None, "search": None}
    mems, _stats = mr.load_memories(profile, "", "", str(tmp_path / "work"))
    mr.collect_media(mems, app, str(tmp_path / "media"))
    page = mr._filter_assets_html([mems[fx.SNAP_A]])
    assert f"#ck-{CK}" in page and "only in scdb-27 without its -wal" in page


def test_a_memory_with_no_cached_asset_gets_no_section(tmp_path):
    record = fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL)])
    mems = _memories(tmp_path, [(CK, 25, fx.IMAGE_URL.replace("uc=1", "uc=2"))],
                     [(fx.SNAP_A, record, 0)])
    assert mems[fx.SNAP_A]["filter_cached"] == []
    assert mr._filter_assets_html([mems[fx.SNAP_A]]) == ""
    assert mr._filter_assets_html([mr._bare_memory("X", {"userHash": fx.HASH})]) == ""


def test_in_a_partial_extract_a_cache_row_it_lacks_is_marked_absent(tmp_path):
    record = fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL)])
    m = _memories(tmp_path, [(CK, 25, fx.IMAGE_URL)], [(fx.SNAP_A, record, 0)])[fx.SNAP_A]
    index = partial_report.Index("mem")
    index.add(f"mem-{fx.SNAP_A}", {})
    selection = {"schema": 2, "selections": {"mem": {f"mem-{fx.SNAP_A}": 1}}}
    closure = partial_report.expand({"mem": index}, partial_report.resolve({"mem": index}, selection),
                                    {**partial_report.default_options(), "relations": {}})
    page = mr._filter_assets_html([m], closure=closure)
    assert report_ui.XOUT_LABEL in page and f"#ck-{CK}" not in page


def test_a_shared_page_gives_each_asset_under_the_memory_that_lists_it(tmp_path):
    a_record = fx.overlay([fx.geofilter("1", image=fx.IMAGE_URL)])
    b_record = fx.overlay([fx.geofilter("2", fonts=[fx.FONT_URL])], selected="2")
    mems = _memories(tmp_path, [(CK, 25, fx.IMAGE_URL), (CK_FONT, 25, fx.FONT_URL)],
                     [(fx.SNAP_A, a_record, 0), (fx.SNAP_B, b_record, 0)])
    members = [mems[fx.SNAP_A], mems[fx.SNAP_B]]
    page = mr._filter_assets_html(members)
    assert "<th>Snap</th>" in page
    rows = page.split("<tr>")[2:]                                     # past the header row
    assert len(rows) == 2
    assert f"#mem-{fx.SNAP_A}" in rows[0] and f"#ck-{CK}" in rows[0] and fx.FONT_URL not in rows[0]
    assert f"#mem-{fx.SNAP_B}" in rows[1] and f"#ck-{CK_FONT}" in rows[1]
    assert "yes — the record names this filter" in rows[1]
    # one line per Memory that lists an asset, each naming its Memory
    assert page.count("<div class='mrefs'>") == 2
    # and the section sits between the Media files and the Library/Caches placeholder
    body = mr._render_group_detail(members, False, [], [], None, None, {})
    assert body.index("Media files") < body.index("Cached assets of filters") < body.index('id="memcm"')


def test_a_page_with_no_cached_asset_is_as_it_was():
    """Nothing is written for a Memory with none — not even a line break — so its page is byte for
    byte what it was."""
    m = mr._bare_memory(fx.SNAP_A, {"userHash": fx.HASH})
    m["filter_cached"] = []
    body = mr._render_group_detail([m], False, [], [], None, None, {})
    assert "Cached assets of filters" not in body
    assert 'no cached media recovered</div>\n          <div id="memcm"' in body
