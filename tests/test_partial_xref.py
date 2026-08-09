"""Cross-report links in a partial report: live, narrowed, or marked absent.

A report folder that holds only part of a run is full of links whose other end is not there. The two
dishonest ways to handle that are a link that goes nowhere and a link that was silently deleted — the
first misleads the reader, the second hides that the association exists at all. So every cross-report
link goes through :func:`report_ui.xref`, and this file pins what it does:

* with no closure (a full run) it returns the caller's markup **untouched**, which is what makes the
  five generators' full output byte-identical to before this feature existed;
* to an included row, the same live link;
* to an excluded row, a marker that keeps the label and names the identifier it would have reached;
* to a *set* of rows (a ``#find=`` fragment), the set narrowed to what is actually there.

The last four tests go through the real link builders of the four reports that have them, so a call
site that stops consulting the closure fails here rather than in a disclosure bundle.

Every input is synthetic.
"""
from scripts import (cache_controller_report, cache_media_report, contacts_report,
                     conversations_report, partial_report, report_ui)

CHIP = '<a class="chip mem" href="../Memories/Memories_report.html#mem-SNAP-1">Memory SNAP-1…</a>'


def _closure(**included):
    """A closure holding exactly the ids given, e.g. ``_closure(mem=["mem-SNAP-1"])``."""
    indexes = {}
    for kind, ids in included.items():
        index = partial_report.Index(kind)
        for row_id in ids:
            index.add(row_id, {})
        indexes[kind] = index
    selection = {"schema": 2, "selections": {k: {i: 1 for i in v} for k, v in included.items()}}
    return partial_report.expand(indexes, partial_report.resolve(indexes, selection),
                                 {**partial_report.default_options(), "relations": {}})


# --------------------------------------------------------------------------- xref itself

def test_a_full_run_gets_back_exactly_what_it_passed_in():
    """The identity property the whole design rests on: no closure, no rewriting."""
    assert report_ui.xref(CHIP, [("mem", "mem-SNAP-1")]) == CHIP
    assert report_ui.xref(CHIP, [("mem", "mem-SNAP-1")], closure=None) == CHIP


def test_a_link_to_an_included_row_stays_live():
    assert report_ui.xref(CHIP, [("mem", "mem-SNAP-1")], closure=_closure(mem=["mem-SNAP-1"])) == CHIP


def test_a_link_to_an_excluded_row_keeps_its_label_and_names_the_target():
    out = report_ui.xref(CHIP, [("mem", "mem-SNAP-1")], closure=_closure(mem=["mem-SNAP-9"]))
    assert "<a " not in out and "href" not in out          # never a link that goes nowhere
    assert "Memory SNAP-1…" in out                          # the label survives as text
    assert "mem-SNAP-1" in out                              # so does the identifier, in the tooltip
    assert report_ui.XOUT_LABEL in out and 'class="xout"' in out


def test_a_link_reaching_several_rows_is_live_when_any_of_them_is_here():
    closure = _closure(cc=["ck-0001"])
    out = report_ui.xref(CHIP, [("cc", "ck-0001"), ("cc", "ck-0002")], closure=closure)
    assert out == CHIP


def test_a_row_id_that_is_empty_is_not_a_target():
    """A link with nothing to check must not be marked absent — half the chips have no row id."""
    closure = _closure(mem=["mem-SNAP-9"])
    assert report_ui.xref(CHIP, [("mem", "")], closure=closure) == CHIP
    assert report_ui.xref(CHIP, [], closure=closure) == CHIP


def test_the_brief_form_is_the_marker_alone():
    """An index cell is one fixed height; a sentence in it would be sliced through the middle."""
    closure = _closure(mem=["mem-SNAP-9"])
    brief = report_ui.xref(CHIP, [("mem", "mem-SNAP-1")], closure=closure, brief=True)
    title, visible = brief.split('">', 1)
    assert report_ui.XOUT_LABEL not in visible              # the marker alone in the cell...
    assert report_ui.XOUT_LABEL in title                    # ...and the sentence in its tooltip
    assert report_ui.XOUT_MARK in visible

    full = report_ui.xref(CHIP, [("mem", "mem-SNAP-1")], closure=closure)
    assert report_ui.XOUT_LABEL in full.split('">', 1)[1]


def test_markup_that_is_not_an_anchor_is_stripped_rather_than_re_emitted():
    """Whatever a caller passes, the marker must never contain a live link."""
    out = report_ui.xref('<span>plain <a href="x">nested</a></span>', [("mem", "mem-SNAP-1")],
                         closure=_closure(mem=["mem-SNAP-9"]))
    assert "<a " not in out and "plain nested" in out


def test_every_excluded_target_is_recorded_for_the_manifest():
    closure = _closure(mem=["mem-SNAP-9"])
    report_ui.xref(CHIP, [("mem", "mem-SNAP-1")], closure=closure)
    report_ui.xref(CHIP, [("mem", "mem-SNAP-1")], closure=closure)     # a second link, same target
    report_ui.xref(CHIP, [("mem", "mem-SNAP-2")], closure=closure)
    assert closure.excluded_refs == {("mem", "mem-SNAP-1"): 2, ("mem", "mem-SNAP-2"): 1}
    assert closure.excluded_ref_count() == 3
    assert [r["id"] for r in closure.as_dict()["excluded_refs"]] == ["mem-SNAP-1", "mem-SNAP-2"]


# --------------------------------------------------------------------------- narrow

def test_narrow_keeps_the_rows_that_are_here_and_counts_the_rest():
    closure = _closure(cc=["ck-0001", "ck-0003"])
    kept, dropped = report_ui.narrow(closure, "cc", ["0001", "0002", "0003"], lambda k: f"ck-{k}")
    assert (kept, dropped) == (["0001", "0003"], 1)


def test_narrow_is_a_no_op_for_a_full_run():
    kept, dropped = report_ui.narrow(None, "cc", ["0001", "0002"], lambda k: f"ck-{k}")
    assert (kept, dropped) == (["0001", "0002"], 0)


# --------------------------------------------------------------------------- the real call sites

def _cm_entry(**over):
    entry = {"links": [], "rel": "a/b.dat", "sha256": "f" * 64, "kind": "empty",
             "recovered": True, "category": "media", "ext": "jpg", "copies": []}
    entry.update(over)
    return entry


def test_library_caches_row_marks_a_cache_entry_that_is_not_in_the_extract():
    entry = _cm_entry(links=[{"kind": "cache", "key": "0001", "basis": "same bytes"}])
    live = cache_media_report._links_cell(entry, "../")
    out = cache_media_report._links_cell(entry, "../", closure=_closure(cc=["ck-9999"]))
    assert "CacheController_report.html#ck-0001" in live
    assert "CacheController_report.html" not in out and report_ui.XOUT_MARK in out


def test_library_caches_row_narrows_a_multi_entry_link_to_what_is_here():
    entry = _cm_entry(links=[{"kind": "cache", "key": "0001", "basis": "b"},
                             {"kind": "cache", "key": "0002", "basis": "b"},
                             {"kind": "cache", "key": "0003", "basis": "b"}])
    out = cache_media_report._links_cell(entry, "../", closure=_closure(cc=["ck-0002"]))
    assert "cache (1)" in out                               # not "cache (3)"
    assert "find=0002" in out and "0001" not in out and "0003" not in out


def test_library_caches_row_marks_a_memory_and_its_detail_page_together():
    entry = _cm_entry(links=[{"kind": "memory", "snap_id": "SNAP-1", "page": "pages/abc.html",
                              "basis": "claim"}])
    out = cache_media_report._links_cell(entry, "../", closure=_closure(mem=["mem-SNAP-9"]))
    assert out.count(report_ui.XOUT_MARK) == 2              # the index row link and the detail link
    assert "Memories_report.html" not in out


def test_cache_controller_row_marks_a_message_that_is_not_in_the_extract():
    entry = {"memory": None, "chats": [{"conversation_id": "CONV-1", "server_message_id": "12.0",
                                        "href": "Conversations/pages/p.html#msg-12.0",
                                        "title": "chat", "basis": "external key"}],
             "cache_media": [], "cache_key": "0001", "on_disk": {"found": True}, "claims": [],
             "tombstones": []}
    live = cache_controller_report._links_html(entry, "../", compact=True)
    out = cache_controller_report._links_html(entry, "../", compact=True,
                                             closure=_closure(msg=["conv-CONV-1|msg-99.0"]))
    assert "Conversations/pages/p.html" in live
    assert "Conversations/pages/p.html" not in out and report_ui.XOUT_MARK in out


def test_cache_controller_keeps_a_legacy_chat_link_it_cannot_name_a_row_for():
    """The legacy report has no row selection, so its links name no target and must stay as they are."""
    entry = {"memory": None, "chats": [{"conversation_id": "CONV-1", "server_message_id": "12.0",
                                        "base": "Communications/Communications_report.html",
                                        "anchor": "cf-x", "basis": "external key"}],
             "cache_media": [], "cache_key": "0001", "on_disk": {"found": True}, "claims": [],
             "tombstones": []}
    out = cache_controller_report._links_html(entry, "../", compact=True, closure=_closure(msg=[]))
    assert "Communications/Communications_report.html" in out


def test_cache_controller_states_how_many_library_caches_copies_are_here():
    entry = {"memory": None, "chats": [], "cache_key": "0001",
             "cache_media": [{"anchor": "cm-a", "basis": "same key"},
                             {"anchor": "cm-b", "basis": "same key"}],
             "on_disk": {"found": True, "paths": ["x"]}, "claims": [], "tombstones": []}
    out = cache_controller_report._links_html(entry, "../", closure=_closure(cm=["cm-a"]))
    assert "Library/Caches (1)" in out
    assert "not part of this partial report" in out


def test_a_contact_row_marks_a_conversation_that_is_not_in_the_extract():
    contact = {"display": "Alice Test", "username": "alice-test", "user_id": "u-1",
               "legacy_username": "", "conv_id": "CONV-1", "is_owner": False}
    convs = [{"id": "CONV-1", "page": "pages/p.html", "title": "Alice", "kind": "Private",
              "messages": 3, "first": "", "last": "", "first_sort": 0, "last_sort": 0,
              "date_source": "", "why": "participant"}]
    out = contacts_report._contact_detail(contact, convs, "../", _closure(conv=["conv-CONV-9"]))
    assert "pages/p.html" not in out and report_ui.XOUT_MARK in out


def test_a_participant_chip_marks_a_contact_that_is_not_in_the_extract():
    part = {"label": "Alice Test (alice-test)", "display": "Alice Test", "username": "alice-test",
            "user_id": "u-1", "href": "Contacts/Contacts_report.html#ct-u-1", "anchor": "ct-u-1",
            "is_owner": False, "raw": "u-1"}
    live = conversations_report._participant_html(part, "../../")
    out = conversations_report._participant_html(part, "../../", closure=_closure(ct=["ct-u-9"]))
    assert "Contacts_report.html" in live
    assert "Contacts_report.html" not in out and "Alice Test (alice-test)" in out
