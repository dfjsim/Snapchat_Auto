"""Checking an expansion before an extract is built, and telling the two kinds of row apart.

A selection names rows; the relations add more. Which rows the examiner chose and which the tool
brought in with them is the first thing a reader of a disclosure bundle needs to know, and until now it
was answerable only from counts in a header and a JSON manifest. Three things close that:

* ``--dry-run`` lists the rows it *would* add, with the reason for each, instead of only counting them;
* :func:`partial_report.expanded_selection` writes the closure back out **as a selection file**, so it
  can be loaded into the full report and checked row by row before anything is built;
* :func:`partial_report.pulled_config` marks those rows in the tables of the extract itself.

The trap this has to close: an expanded selection's ticks are *already* a closure, so building it under
the relations again would add a second hop from every row that was pulled in — an extract larger than
the one that was reviewed. So the file records ``minimal`` as its own policy and says it is an
expansion, and both halves are pinned here.

Every input is synthetic: placeholder ids, hand-built indexes, no extraction data.
"""
import json
import re

from scripts import partial_report as pr
from scripts import selection_file
from tests.test_partial_expansion import CONV_A, KEY_1, build_indexes, closure_for, selection


def _closure(**over):
    """The closure for one ticked Memory: its group sibling comes with it (mem_group) and so does the
    cache entry its media was recovered from (mem_cache) -- both on in the recommended set."""
    return closure_for(selection(mem=[("mem-SNAP-0001", {"snap": "SNAP-0001"})]), **over)


# --------------------------------------------------------------- D: the dry run names the rows

def test_the_dry_run_lists_each_added_row_and_why():
    text = pr.dry_run_text(_closure())

    assert "Rows added (2), and why:" in text
    assert "mem-SNAP-0002" in text
    assert "mem_group" in text
    assert text.isascii()                                  # it goes to a Windows console


def test_the_dry_run_lists_nothing_when_nothing_was_added():
    text = pr.dry_run_text(_closure(relations=dict(pr.PRESETS["minimal"])))

    assert "Rows added" not in text


def test_a_long_list_points_at_the_manifest_for_the_rest():
    indexes = build_indexes()
    for n in range(60):                                    # more rows than the dry run prints
        indexes["mem"].add(f"mem-SNAP-9{n:03}", snap=f"SNAP-9{n:03}")
        indexes["mem"].groups[f"mem-SNAP-9{n:03}"] = "group-1"
        indexes["mem"].link(pr.EDGE_MEMORY_GROUP, "mem-SNAP-0001", "mem", f"mem-SNAP-9{n:03}")
    closure = pr.expand(indexes, pr.resolve(indexes, selection(mem=["mem-SNAP-0001"])),
                        pr.default_options())

    text = pr.dry_run_text(closure)

    assert re.search(r"and \d+ more", text) and "partial_manifest.json" in text


# --------------------------------------------------------------- C: the marker in the extract

def test_the_pulled_config_names_only_the_rows_the_examiner_did_not_tick():
    closure = _closure()

    config = pr.pulled_config(closure, "mem")

    assert config.startswith("pulled:{") and config.endswith(",")
    marked = json.loads(config[len("pulled:"):-1])
    assert list(marked) == ["mem-SNAP-0002"]               # the sibling, not the ticked Memory
    assert "mem_group" in marked["mem-SNAP-0002"]


def test_a_full_run_marks_nothing_at_all():
    """`closure=None` is the full-run path, and it has to add nothing to the page — the corpus
    byte-diff is only a drift detector while that stays true."""
    assert pr.pulled_config(None, "mem") == ""


def test_a_kind_with_nothing_pulled_in_emits_nothing():
    assert pr.pulled_config(_closure(), "conv") == ""


def test_the_reason_is_escaped_because_it_lands_in_an_attribute():
    closure = _closure()
    closure.reasons[("mem", "mem-SNAP-0002")] = ['he said "look at this" <b>']

    marked = json.loads(pr.pulled_config(closure, "mem")[len("pulled:"):-1])

    assert "&quot;" in marked["mem-SNAP-0002"] and "<b>" not in marked["mem-SNAP-0002"]


def test_the_marker_is_scoped_to_one_page_when_asked():
    """A conversation page would otherwise carry every conversation's messages."""
    closure = closure_for(selection(conv=[f"conv-{CONV_A}"]),
                          relations={**pr.PRESETS["recommended"], "conv_messages": True})

    mine = pr.pulled_config(closure, "msg", prefix=f"conv-{CONV_A}|")
    other = pr.pulled_config(closure, "msg", prefix="conv-nothing|")

    assert f"conv-{CONV_A}|msg-1.0" in mine
    assert other == ""


def test_the_banner_says_what_the_marker_means():
    banner = pr.banner_html(_closure())

    assert "plegend" in banner and "were <b>not</b> selected" in banner


def test_the_banner_says_nothing_about_it_when_nothing_was_pulled_in():
    assert "plegend" not in pr.banner_html(_closure(relations=dict(pr.PRESETS["minimal"])))


# --------------------------------------------------------------- B: the closure as a selection file

def _expanded(**over):
    source = selection(mem=[("mem-SNAP-0001", {"snap": "SNAP-0001"})])
    closure = closure_for(source, **over)
    return pr.expanded_selection(closure, source, run_id="run-full", sources={"digest": "d" * 64})


def test_the_expansion_ticks_every_included_row():
    payload = _expanded()

    assert set(payload["selections"]["mem"]) == {"mem-SNAP-0001", "mem-SNAP-0002"}


def test_a_ticked_row_keeps_the_identifiers_it_arrived_with():
    """They are what finds it again if a later build moves our id for it, so an expansion must not
    quietly drop them."""
    payload = _expanded()

    assert payload["selections"]["mem"]["mem-SNAP-0001"] == {"snap": "SNAP-0001"}


def test_an_added_row_carries_the_reason_it_is_there():
    payload = _expanded()

    assert "mem_group" in payload["selections"]["mem"]["mem-SNAP-0002"]["why"]


def test_a_row_found_under_a_different_id_keeps_its_original_record():
    """A key that named a whole group resolves to several rows, none of them the ticked id — so the
    record has to follow the id it landed on rather than being lost with the id it left."""
    indexes = build_indexes()
    for row in ("mem-SNAP-0001", "mem-SNAP-0002"):
        indexes["mem"].keys[("cachekeys", KEY_1)].add(row)
    source = selection(mem=[(f"mem-by-cachekey-{KEY_1}", {"cachekeys": [KEY_1]})])
    closure = pr.expand(indexes, pr.resolve(indexes, source), pr.default_options())

    payload = pr.expanded_selection(closure, source, run_id="run-full")

    for row in ("mem-SNAP-0001", "mem-SNAP-0002"):
        assert payload["selections"]["mem"][row] == {"cachekeys": [KEY_1]}


def test_the_file_carries_the_full_reports_identity_not_this_runs():
    """It is meant to be loaded into the full report. Stamping the expanding run's id would make
    --install-selection report a mismatch every time, which teaches an examiner to force past a check
    that exists to stop a selection landing on the wrong case."""
    payload = _expanded()

    assert payload["run_id"] == "run-full"
    assert payload["sources"] == {"digest": "d" * 64}


def test_the_file_asks_to_be_built_with_containment_only():
    """Its ticks are already a closure. Following the relations again would add a second hop from every
    row that was pulled in, so the extract would hold more than was reviewed."""
    payload = _expanded()

    assert payload["relations"] == pr.EXPANDED_RELATIONS == "minimal"
    assert pr.is_expanded(payload) is True
    assert pr.is_expanded({"schema": 2, "selections": {}}) is False


def test_the_file_records_which_policy_produced_it():
    payload = _expanded()

    assert "mem_group" in payload["expanded"]["relations"]
    assert payload["expanded"]["selected"] == 1 and payload["expanded"]["added"] == 2
    assert payload["expanded"]["from_digest"] == selection_file.selection_digest(
        selection(mem=[("mem-SNAP-0001", {"snap": "SNAP-0001"})]))


def test_the_recorded_policy_names_transitive_when_it_ran():
    payload = _expanded(transitive=True)

    assert payload["expanded"]["relations"].endswith("transitive")


def test_it_is_a_selection_file_the_run_reads_back_to_the_same_rows(tmp_path):
    """The contract: written, read from disk and resolved again, an expansion yields exactly the rows
    the closure held — otherwise reviewing it in the report would not be reviewing what gets built."""
    payload = _expanded()
    path = tmp_path / "expanded.json"
    path.write_text(selection_file.selection_json_text(payload), encoding="utf-8")

    reread, unattributed = pr.load_selection(str(path))
    indexes = build_indexes()
    resolution = pr.resolve(indexes, reread)
    closure = pr.expand(indexes, resolution, {**pr.default_options(),
                                              "relations": pr.parse_relations(reread["relations"])})

    assert not unattributed
    # exactly the rows the closure held -- every one of them, and nothing else
    original = closure_for(selection(mem=[("mem-SNAP-0001", {"snap": "SNAP-0001"})]))
    for kind in pr.KINDS:
        assert closure.included.get(kind, set()) == original.included.get(kind, set()), kind


def test_building_it_under_the_relations_again_would_grow_it():
    """Why the file records `minimal` and says it is an expansion: this is the trap it closes."""
    payload = _expanded()
    indexes = build_indexes()
    # the cache entry the expansion already ticked also belongs to a message: one more hop away
    indexes["cc"].link(pr.EDGE_MESSAGE_CACHE, f"ck-{KEY_1}", "msg", f"conv-{CONV_A}|msg-1.0")

    again = pr.expand(indexes, pr.resolve(indexes, payload), pr.default_options())
    reviewed = pr.expand(indexes, pr.resolve(indexes, payload),
                         {**pr.default_options(), "relations": pr.parse_relations("minimal")})

    assert again.included["msg"] and not reviewed.included["msg"]


def test_the_provenance_says_the_selection_was_an_expansion():
    """A hand-ticked selection and a reviewed expansion are different provenances, and "selected by the
    examiner" claims more than it should for the second."""
    payload = _expanded()
    prov = {"tool_version": "1.6.0", "selection": {"name": "expanded.json",
                                                   "expanded": payload["expanded"]}}

    html = pr.provenance_html(_closure(), prov)

    assert "Selection is an expansion" in html
    assert "1 row(s) ticked in the reports plus 2 added" in html


def test_the_provenance_says_nothing_of_the_sort_for_a_hand_ticked_selection():
    prov = {"tool_version": "1.6.0", "selection": {"name": "selection.json"}}

    assert "Selection is an expansion" not in pr.provenance_html(_closure(), prov)


# ----------------------------------------- the marker survives the review, which is the point of it

def test_a_row_kept_from_an_expansion_is_still_not_a_choice(tmp_path):
    """The gap a real run exposed. An expanded selection ticks every row, so after the review every row
    is a seed — and calling them all "selected by the examiner" would lose exactly the distinction the
    expansion was written to preserve. The `why` in each row's key record is what keeps it."""
    payload = _expanded()
    indexes = build_indexes()
    closure = pr.expand(indexes, pr.resolve(indexes, payload),
                        {**pr.default_options(), "relations": pr.parse_relations("minimal")})

    assert closure.chosen("mem", "mem-SNAP-0001") is True
    assert closure.chosen("mem", "mem-SNAP-0002") is False
    assert closure.counts()["mem"] == {"selected": 1, "pulled_in": 1, "total": 2}

    marked = json.loads(pr.pulled_config(closure, "mem")[len("pulled:"):-1])
    assert list(marked) == ["mem-SNAP-0002"]
    assert "reviewed" in marked["mem-SNAP-0002"]
    assert "plegend" in pr.banner_html(closure)


def test_the_dry_run_does_not_count_a_carried_row_as_one_it_added():
    payload = _expanded()
    indexes = build_indexes()
    closure = pr.expand(indexes, pr.resolve(indexes, payload),
                        {**pr.default_options(), "relations": pr.parse_relations("minimal")})

    text = pr.dry_run_text(closure)

    assert "Rows added" not in text                        # this run added none
    assert "came from an earlier expansion rather than being chosen" in text


def test_re_expanding_keeps_the_reason_a_row_arrived_with():
    """Otherwise a second pass through the loop would launder every pulled-in row into a choice."""
    once = _expanded()
    indexes = build_indexes()
    closure = pr.expand(indexes, pr.resolve(indexes, once),
                        {**pr.default_options(), "relations": pr.parse_relations("minimal")})

    twice = pr.expanded_selection(closure, once, run_id="run-full")

    assert "why" in twice["selections"]["mem"]["mem-SNAP-0002"]
    assert "why" not in twice["selections"]["mem"]["mem-SNAP-0001"]

