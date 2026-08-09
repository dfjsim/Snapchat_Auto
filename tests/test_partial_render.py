"""What a partial report must say about itself, and what it must not leave behind.

A subset of a report is only usable if the reader can tell it is a subset, of what, and what was cut.
So four properties are pinned here:

* **It says it is partial**, on every page, with the denominator next to every figure — "3 of 412",
  not "3". A page handed on by itself has to carry that.
* **It states the mismatches.** If the examiner built it from evidence, or a build, that differs from
  the run the selection was made in, the banner says so rather than only ``partial_manifest.json``.
* **No figure describes rows that are not there.** A conversation page that lists four messages under
  a header saying 137 is worse than one that says nothing, because the header is what gets quoted.
* **Nothing is published that no included row references.** Attachment publishing and Memory
  decryption both happen before the filter (for reasons in the generators), so a partial run has to
  prune what it inherited — otherwise a disclosure folder holds decrypted media of Memories the
  examiner did not select.

Every input is synthetic.
"""
import html
import json
import os

from scripts import conversations_report, memories_media_report, partial_report, report_ui


def _closure(totals=None, **included):
    """A closure over the given ids, with each kind's extraction-wide total padded out to *totals*."""
    indexes = {}
    for kind, ids in included.items():
        index = partial_report.Index(kind)
        for row_id in ids:
            index.add(row_id, {})
        for n in range((totals or {}).get(kind, 0) - len(ids)):
            index.add(f"{kind}-filler-{n}", {})
        indexes[kind] = index
    selection = {"schema": 2, "selections": {k: {i: 1 for i in v} for k, v in included.items()}}
    return partial_report.expand(indexes, partial_report.resolve(indexes, selection),
                                 {**partial_report.default_options(), "relations": {}})


# --------------------------------------------------------------------------- figures and banner

def test_the_figures_carry_the_extractions_own_total():
    closure = _closure({"conv": 412}, conv=["conv-1", "conv-2"])
    out = partial_report.figures_html(closure, "conv")
    assert "<b>2</b> of 412 conversation(s) in the extraction" in out
    assert "2 selected" in out


def test_the_figures_separate_what_was_selected_from_what_was_pulled_in():
    seeds = partial_report.Index("mem")
    for row_id in ("mem-A", "mem-B", "mem-C"):
        seeds.add(row_id, {})
    seeds.link(partial_report.EDGE_MEMORY_GROUP, "mem-A", "mem", "mem-B")
    indexes = {"mem": seeds}
    resolution = partial_report.resolve(indexes, {"schema": 2, "selections": {"mem": {"mem-A": 1}}})
    closure = partial_report.expand(indexes, resolution, partial_report.default_options())

    out = partial_report.figures_html(closure, "mem")
    assert "<b>2</b> of 3 memory/memories in the extraction" in out
    assert "1 selected, 1 pulled in by a relation" in out


def test_a_report_with_no_rows_of_a_kind_states_no_figure_for_it():
    assert partial_report.figures_html(_closure(conv=["conv-1"]), "cm") == ""


def test_the_banner_names_the_extract_as_partial_and_points_at_the_manifest():
    banner = partial_report.banner_html(_closure(conv=["conv-1"]), {"case_ref": "EXHIBIT-7"})
    assert "PARTIAL REPORT" in banner and "not the complete report" in banner
    assert "partial_manifest.json" in banner
    assert "EXHIBIT-7" in banner


def test_the_banner_states_a_source_mismatch_the_examiner_chose_to_proceed_through():
    banner = partial_report.banner_html(
        _closure(conv=["conv-1"]),
        {"sources": {"ok": False, "text": "cache_controller.db-wal differs"}})
    assert "differ from the run the selection was made in" in banner
    assert "cache_controller.db-wal differs" in banner


def test_the_banner_states_a_version_mismatch():
    banner = partial_report.banner_html(_closure(conv=["conv-1"]),
                                        {"version": {"ok": False, "text": "1.5.1 vs 1.5.2"}})
    assert "tool version" in banner and "1.5.1 vs 1.5.2" in banner


def test_a_selection_with_no_fingerprints_says_it_could_not_be_verified():
    """Never "verified" when nothing was checked — that is the one claim a report must not imply."""
    banner = partial_report.banner_html(_closure(conv=["conv-1"]), {"sources": None})
    assert "not possible to verify" in banner


def test_a_matching_source_verdict_raises_no_alarm():
    banner = partial_report.banner_html(_closure(conv=["conv-1"]),
                                        {"sources": {"ok": True, "text": "12 of 12 identical"}})
    assert "differ from the run" not in banner and "not possible to verify" not in banner


def test_a_closure_warning_reaches_the_banner():
    closure = _closure(conv=["conv-1"])
    closure.warnings.append("the transitive closure was stopped after 25 passes")
    assert "stopped after 25 passes" in partial_report.banner_html(closure)


def test_page_chrome_is_three_empty_strings_for_a_full_run():
    assert partial_report.page_chrome(None, "conv") == ("", "", "")


# --------------------------------------------------------------------------- provenance

def test_the_provenance_lists_the_relations_that_were_not_followed():
    closure = _closure({"conv": 9}, conv=["conv-1"])          # default_options minus every relation
    out = partial_report.provenance_html(closure)
    for relation in partial_report.RELATIONS:
        assert relation.key in out and html.escape(relation.basis) in out
    assert "&#10008;" in out                                  # each one marked as not followed


def test_the_provenance_states_the_legacy_reports_were_left_out():
    out = partial_report.provenance_html(_closure(conv=["conv-1"]))
    assert "left out</b> of this extract entirely" in out


def test_the_provenance_carries_the_selection_identity_and_the_source_table():
    prov = {"tool_version": "1.5.2+build.9", "built": "2026-01-02 03:04:05", "tz_label": "UTC",
            "selection": {"name": "selection.json", "sha256": "a" * 64, "digest": "b" * 64,
                          "exported": "2026-01-01T00:00:00Z", "schema": 2,
                          "tool_version": "1.5.2+build.9"},
            "sources": {"ok": True, "rows": [{"role": "arroyo", "detail": "12 bytes",
                                              "verdict": "match", "ok": True}]}}
    out = partial_report.provenance_html(_closure(conv=["conv-1"]), prov)
    assert "1.5.2+build.9" in out and "a" * 64 in out and "b" * 64 in out
    assert "arroyo" in out and "match" in out


def test_the_provenance_says_verification_was_not_possible_rather_than_nothing():
    out = partial_report.provenance_html(_closure(conv=["conv-1"]), {"sources": None})
    assert "Not checked" in out and "carried no fingerprints" in out


def test_the_provenance_counts_the_links_that_were_cut():
    closure = _closure(mem=["mem-A"])
    report_ui.xref("<a href='x'>y</a>", [("mem", "mem-Z")], closure=closure)
    out = partial_report.provenance_html(closure)
    assert "<b>1</b> cross-reference(s) point at 1 item(s)" in out


def test_the_provenance_reports_how_a_moved_selection_resolved():
    index = partial_report.Index("cm")
    index.add("cm-new", {}, raw="ff00")
    indexes = {"cm": index}
    resolution = partial_report.resolve(
        indexes, {"schema": 2, "selections": {"cm": {"cm-old": {"raw": ["ff00"]}}}})
    closure = partial_report.expand(indexes, resolution, partial_report.default_options())
    out = partial_report.provenance_html(closure)
    assert "cm-old" in out and "cm-new" in out and "raw SHA-256" in out


# --------------------------------------------------------------------------- the manifest

def test_the_manifest_records_every_row_its_reason_and_every_cut_link(tmp_path):
    closure = _closure({"conv": 5}, conv=["conv-1"])
    report_ui.xref("<a href='x'>y</a>", [("mem", "mem-Z")], closure=closure)
    path = partial_report.write_manifest(closure, str(tmp_path),
                                         {"case_ref": "EXHIBIT-7", "withheld": ["mem.urls"]})

    payload = json.loads(open(path, encoding="utf-8").read())
    assert os.path.basename(path) == "partial_manifest.json"
    assert payload["kind"] == "partial_manifest" and payload["tool"] == "Snapchat_Auto"
    assert payload["included"]["conv"] == ["conv-1"]
    assert payload["reasons"]["conv/conv-1"][0].startswith("Selected by the examiner")
    assert payload["counts"]["conv"] == {"selected": 1, "pulled_in": 0, "total": 5}
    assert payload["excluded_refs"] == [{"kind": "mem", "id": "mem-Z", "links": 1}]
    assert payload["provenance"]["case_ref"] == "EXHIBIT-7"
    assert payload["provenance"]["withheld"] == ["mem.urls"]
    # the policy, so the manifest answers "what was not followed" without the tool that wrote it
    assert {r["key"] for r in payload["relations"]} == set(partial_report.RELATION_KEYS)
    assert payload["containment"] == list(partial_report.CONTAINMENT)


def test_a_partial_report_never_lands_in_the_folder_it_is_a_subset_of():
    assert partial_report.partial_dir("run", "20260101_000000").endswith(
        os.path.join("run", "Reports_partial_20260101_000000"))


# --------------------------------------------------------------------------- conversations: figures

def _msg(anchor, smid, sender="u-1", atts=()):
    return {"anchor": anchor, "smid": smid, "sender": sender, "types": ["TEXT"], "atts": list(atts),
            "created_unix": 1700000000, "wal": None}


def _conv(messages):
    return {"id": "CONV-1", "messages": list(messages), "n_messages": len(messages),
            "n_files": 0, "n_attachments": 0, "n_missing": 0, "n_wal_gone": 0,
            "senders": {"u-1": len(messages)}, "types": {"TEXT": len(messages)}}


def test_a_narrowed_conversation_recomputes_every_figure_from_the_rows_it_keeps():
    conv = _conv([_msg("msg-1.0", "1.0"), _msg("msg-2.0", "2.0", "u-2"),
                  _msg("msg-3.0", "3.0", atts=[{"rel": "media/a.jpg"}])])
    conv["n_files"], conv["n_attachments"] = 1, 1

    out = conversations_report._narrowed(conv, {"conv-CONV-1|msg-1.0", "conv-CONV-1|msg-2.0"})
    assert [m["smid"] for m in out["messages"]] == ["1.0", "2.0"]
    assert out["n_messages"] == 2 and out["n_messages_full"] == 3
    assert out["n_attachments"] == 0 and out["n_files"] == 0   # the one with a file is not here
    assert out["senders"] == {"u-1": 1, "u-2": 1}
    assert conv["n_messages"] == 3                             # the model itself is untouched


def test_narrowing_leaves_the_model_alone_so_the_other_reports_still_read_it():
    conv = _conv([_msg("msg-1.0", "1.0")])
    out = conversations_report._narrowed(conv, set())
    assert out["messages"] == [] and conv["messages"] != []
    assert out["types"] == {} and conv["types"] != {}


# --------------------------------------------------------------------------- pruning

def test_conversations_media_is_pruned_to_what_the_rendered_messages_reference(tmp_path):
    outdir = tmp_path / "Conversations"
    media = outdir / "media"
    media.mkdir(parents=True)
    for name in ("kept.jpg", "shared.jpg", "orphan.mp4"):
        (media / name).write_bytes(b"x")

    conv = _conv([_msg("msg-1.0", "1.0", atts=[{"rel": "media/kept.jpg"},
                                               {"rel": "media/shared.jpg"}]),
                  _msg("msg-2.0", "2.0", atts=[{"rel": None}])])
    removed = conversations_report._prune_media(str(outdir), [conv])

    assert removed == 1
    assert sorted(os.listdir(media)) == ["kept.jpg", "shared.jpg"]


def test_pruning_a_folder_with_no_media_is_not_an_error(tmp_path):
    assert conversations_report._prune_media(str(tmp_path), []) == 0


def test_decrypted_memory_media_of_an_excluded_memory_does_not_stay_in_the_folder(tmp_path):
    """The one thing a partial extract must never do: carry plaintext of what was not selected."""
    outdir = tmp_path / "Memories"
    media = outdir / "media"
    media.mkdir(parents=True)
    for name in ("included.jpg", "excluded.jpg"):
        (media / name).write_bytes(b"x")

    memories = {"SNAP-1": {"media_files": [{"out": "included.jpg"}]}}
    removed = memories_media_report._prune_media(str(outdir), memories)

    assert removed == 1
    assert os.listdir(media) == ["included.jpg"]


# --------------------------------------------------------------------------- Memory groups

def _memory(snap_id, media_id="MEDIA-1"):
    return {"snap_id": snap_id, "user_hash": "u" * 12,
            "media_type": None, "format": "", "media_format": None,
            "media_url": None, "overlay_url": None, "thumb_url": None,
            "create_utc": "", "created_sort": 0,
            "duration": None, "width": None, "height": None, "camera": "",
            "has_location": False, "times": {}, "entry_times": {},
            "snap_other": {}, "entry_other": {}, "urls": {},
            "ids": {"ZMEDIAID": media_id, "ZSNAPID": snap_id},
            "key": None, "iv": None, "is_meo": False, "key_wrapped": False, "key_source": None,
            "media_refs": [], "latitude": None, "longitude": None, "address": None,
            "media_files": [], "wal": None, "prior_rows": [], "map": None}


def _group_detail(members, group_of=None, closure=None):
    return memories_media_report._render_group_detail(
        members, True, [], [], None, {}, {}, closure=closure, group_of=group_of)


def test_a_group_page_showing_part_of_a_group_states_the_groups_real_size():
    """A group exists because its members are the same media under another snap row.

    Rendering one of a pair and calling it "Group of 1" would deny the relationship the grouping
    asserts — so the page has to name the real size and the snap that is absent.
    """
    members = [_memory("SNAP-1")]
    group_of = {"SNAP-1": ["SNAP-1", "SNAP-2"], "SNAP-2": ["SNAP-1", "SNAP-2"]}
    out = _group_detail(members, group_of, _closure(mem=["mem-SNAP-1"]))

    assert "1 of 2</b> memories grouped here" in out
    assert "mem-SNAP-2" in out and report_ui.XOUT_MARK in out
    assert "of 2 grouped here" in out                          # the id band, next to the count


def test_a_whole_group_renders_exactly_as_it_always_did():
    members = [_memory("SNAP-1"), _memory("SNAP-2")]
    group_of = {"SNAP-1": ["SNAP-1", "SNAP-2"], "SNAP-2": ["SNAP-1", "SNAP-2"]}
    partial = _group_detail(members, group_of, _closure(mem=["mem-SNAP-1", "mem-SNAP-2"]))
    assert "2 memories are grouped here" in partial
    assert report_ui.XOUT_MARK not in partial


def test_a_sibling_pulled_in_by_the_group_relation_is_badged_as_not_selected():
    seeds = partial_report.Index("mem")
    seeds.add("mem-SNAP-1", {})
    seeds.add("mem-SNAP-2", {})
    seeds.link(partial_report.EDGE_MEMORY_GROUP, "mem-SNAP-1", "mem", "mem-SNAP-2")
    indexes = {"mem": seeds}
    closure = partial_report.expand(
        indexes, partial_report.resolve(indexes, {"schema": 2,
                                                  "selections": {"mem": {"mem-SNAP-1": 1}}}),
        partial_report.default_options())

    out = _group_detail([_memory("SNAP-1"), _memory("SNAP-2")], closure=closure)
    assert out.count('class="psib"') == 1                      # the sibling, not the selected one
    assert "mem_group" in out                                  # the relation that brought it in


def test_a_full_run_group_page_has_no_partial_furniture_at_all():
    out = _group_detail([_memory("SNAP-1"), _memory("SNAP-2")])
    assert report_ui.XOUT_MARK not in out and 'class="psib"' not in out
    assert "2 memories are grouped here" in out
