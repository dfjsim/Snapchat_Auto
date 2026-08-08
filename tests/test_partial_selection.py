"""The examiner's selection file: its format, its migration, and installing it.

The selection had no test coverage at all, while being the one piece of examiner *work* the tool
stores — and it is about to be the input to partial reports, so a silent misreading would put the
wrong evidence in a disclosure bundle.

The sharp edge pinned hardest here is the message id. Before schema 2 a message selection was a bare
page anchor (``msg-12.0``), and message numbers restart in every conversation — so ticking one
message marked the same number in every chat. Such an id cannot be attributed to a conversation
after the fact, and this file asserts that it is quarantined rather than guessed at.

Every input is synthetic: placeholder ids only, no extraction data.
"""
import json
import os

import pytest

from scripts import report_ui, selection_file
from scripts.selection_file import SelectionFormatError


CONV = "aaaa0000-0000-4000-8000-000000000001"
CONV_B = "aaaa0000-0000-4000-8000-000000000002"
RUN = "20260101-000000-0a0b0c0d"


def _payload(selections, **kw):
    return dict({"tool": "Snapchat_Auto", "schema": selection_file.SCHEMA, "run_id": RUN,
                 "tool_version": "9.9.9+build.1", "exported": "2026-01-01T00:00:00.000Z",
                 "selections": selections}, **kw)


def _simple():
    return _payload({"mem": {"mem-SNAP-0001": {"snap": "SNAP-0001"}},
                     "cc": {"ck-" + "0" * 31 + "1": 1}})


# --------------------------------------------------------------------------- parsing

def test_parses_the_wrapped_js_form():
    text = selection_file.selection_js_text(_simple())
    assert selection_file.parse_selection_text(text)["selections"]["mem"]


def test_parses_bare_json():
    text = selection_file.selection_json_text(_simple())
    assert selection_file.parse_selection_text(text)["selections"]["cc"]


@pytest.mark.parametrize("wrap", [
    lambda s: "﻿" + s,                                  # a BOM, as an editor may leave one
    lambda s: s.replace("\n", "\r\n"),                       # CRLF
    lambda s: "// a comment an examiner added\n" + s,
    lambda s: s + "\n\n",                                    # trailing blank lines
])
def test_tolerates_what_an_editor_or_browser_may_add(wrap):
    text = wrap(selection_file.selection_json_text(_simple()))
    assert selection_file.parse_selection_text(text)["selections"]["mem"]


def test_a_bare_selections_map_is_accepted_as_schema_1():
    # what a hand-written schema-1 file could look like: no envelope at all
    payload = selection_file.parse_selection_text('{"mem": {"mem-SNAP-0001": 1}}')
    assert payload["schema"] == 1
    assert payload["selections"]["mem"] == {"mem-SNAP-0001": 1}


@pytest.mark.parametrize("text", ["", "not a selection at all", "[1,2,3]", "{{{"])
def test_rejects_what_is_not_a_selection_file(text):
    with pytest.raises(SelectionFormatError):
        selection_file.parse_selection_text(text)


def test_a_future_schema_is_refused_by_number_not_misread():
    text = json.dumps(_payload({}, schema=selection_file.SCHEMA_MAX + 1))
    with pytest.raises(SelectionFormatError, match="reads up to"):
        selection_file.parse_selection_text(text)


def test_read_selection_names_the_file_in_its_error(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("nonsense", encoding="utf-8")
    with pytest.raises(SelectionFormatError, match="broken.json"):
        selection_file.read_selection(str(bad))


# --------------------------------------------------------------------------- digest

def test_the_digest_is_the_same_for_both_forms_of_one_selection():
    payload = _simple()
    as_js = selection_file.parse_selection_text(selection_file.selection_js_text(payload))
    as_json = selection_file.parse_selection_text(selection_file.selection_json_text(payload))
    assert selection_file.selection_digest(as_js) == selection_file.selection_digest(as_json)


def test_the_digest_ignores_when_the_file_was_saved_but_not_what_is_in_it():
    same = _payload(_simple()["selections"], exported="2030-06-06T06:06:06.000Z")
    assert selection_file.selection_digest(same) == selection_file.selection_digest(_simple())
    more = _simple()
    more["selections"]["mem"]["mem-SNAP-0002"] = 1
    assert selection_file.selection_digest(more) != selection_file.selection_digest(_simple())


def test_the_digest_does_not_depend_on_key_order():
    a = _payload({"mem": {"mem-SNAP-0001": 1, "mem-SNAP-0002": 1}})
    b = _payload({"mem": {"mem-SNAP-0002": 1, "mem-SNAP-0001": 1}})
    assert selection_file.selection_digest(a) == selection_file.selection_digest(b)


# --------------------------------------------------------------------------- migration

def test_migration_keeps_a_qualified_message_id_and_quarantines_a_bare_one():
    payload = _payload({"msg": {f"conv-{CONV}|msg-12.0": {"smid": "12.0"},
                                "msg-13.0": 1}}, schema=1)
    out, unattributed = selection_file.migrate_selection(payload)
    assert list(out["selections"]["msg"]) == [f"conv-{CONV}|msg-12.0"]
    assert unattributed == ["msg-13.0"]
    assert out["schema"] == selection_file.SCHEMA


def test_migration_never_promotes_a_bare_message_id_to_a_conversation():
    """A bare ``msg-12.0`` matches message 12.0 in *every* chat.

    Attributing it to whichever conversation happens to be at hand would invent a fact, and would
    put a message the examiner never ticked into a partial report. It has to come out.
    """
    payload = _payload({"conv": {f"conv-{CONV}": 1}, "msg": {"msg-12.0": 1}}, schema=1)
    out, unattributed = selection_file.migrate_selection(payload)
    assert out["selections"]["msg"] == {}
    assert unattributed == ["msg-12.0"]
    # and the one conversation that *was* ticked is untouched
    assert list(out["selections"]["conv"]) == [f"conv-{CONV}"]


def test_migration_leaves_every_other_kind_and_a_bare_1_alone():
    payload = _payload({"mem": {"mem-SNAP-0001": 1},
                        "cm": {"cm-" + "0" * 64: {"raw": ["ab"]}}}, schema=1)
    out, unattributed = selection_file.migrate_selection(payload)
    assert unattributed == []
    assert out["selections"]["mem"]["mem-SNAP-0001"] == 1
    assert out["selections"]["cm"]["cm-" + "0" * 64] == {"raw": ["ab"]}


def test_migration_does_not_mutate_its_input():
    payload = _payload({"msg": {"msg-1.0": 1}}, schema=1)
    selection_file.migrate_selection(payload)
    assert payload["selections"]["msg"] == {"msg-1.0": 1}
    assert payload["schema"] == 1


# --------------------------------------------------------------------------- installing

def _reports(tmp_path, run=RUN):
    d = tmp_path / "Reports"
    d.mkdir()
    if run:
        (d / "run_id.txt").write_text(run + "\n", encoding="utf-8")
    return d


def test_install_writes_the_drop_in_form_a_report_can_load(tmp_path):
    src = tmp_path / "selection.json"
    src.write_text(selection_file.selection_json_text(_simple()), encoding="utf-8")
    reports = _reports(tmp_path)

    target, info = selection_file.install_selection(str(reports), str(src))

    assert os.path.basename(target) == "selection.js"
    text = open(target, encoding="utf-8").read()
    # a report loads this with <script src>, so it must be a call and not bare JSON
    assert text.lstrip().startswith("/*") and "SCSel.preload(" in text
    assert selection_file.parse_selection_text(text)["selections"]["mem"]
    assert info["counts"] == {"mem": 1, "cc": 1}
    assert info["digest"] == selection_file.selection_digest(_simple())
    assert info["source_sha256"]


def test_install_sets_an_existing_file_aside_rather_than_overwriting_the_examiners_work(tmp_path):
    reports = _reports(tmp_path)
    (reports / "selection.js").write_text("SCSel.preload({\"selections\": {}});\n", encoding="utf-8")
    src = tmp_path / "selection.json"
    src.write_text(selection_file.selection_json_text(_simple()), encoding="utf-8")

    _target, info = selection_file.install_selection(str(reports), str(src))

    assert info["backup"] and os.path.isfile(info["backup"])
    assert "bak-" in os.path.basename(info["backup"])


def test_install_refuses_a_selection_saved_for_a_different_run(tmp_path):
    reports = _reports(tmp_path, run="THIS-RUN")
    src = tmp_path / "selection.json"
    src.write_text(selection_file.selection_json_text(_simple()), encoding="utf-8")  # run_id = RUN

    with pytest.raises(SelectionFormatError, match="saved for run"):
        selection_file.install_selection(str(reports), str(src))
    assert not os.path.isfile(reports / "selection.js")


def test_install_honours_force_and_says_the_run_did_not_match(tmp_path):
    reports = _reports(tmp_path, run="THIS-RUN")
    src = tmp_path / "selection.json"
    src.write_text(selection_file.selection_json_text(_simple()), encoding="utf-8")

    _target, info = selection_file.install_selection(str(reports), str(src), force=True)

    assert info["run_id_mismatch"] is True
    assert info["run_id"] == RUN and info["report_run_id"] == "THIS-RUN"


def test_install_into_a_folder_with_no_run_id_does_not_invent_one(tmp_path):
    reports = _reports(tmp_path, run=None)
    src = tmp_path / "selection.json"
    src.write_text(selection_file.selection_json_text(_simple()), encoding="utf-8")

    _target, info = selection_file.install_selection(str(reports), str(src))

    assert info["run_id_mismatch"] is False
    assert not os.path.exists(reports / "run_id.txt")


def test_install_reports_the_message_ids_it_had_to_drop(tmp_path):
    reports = _reports(tmp_path)
    src = tmp_path / "old.js"
    src.write_text(selection_file.selection_js_text(
        _payload({"msg": {"msg-12.0": 1}}, schema=1)), encoding="utf-8")

    _target, info = selection_file.install_selection(str(reports), str(src))

    assert info["unattributed_msg_ids"] == ["msg-12.0"]
    assert selection_file.parse_selection_text(
        open(_target, encoding="utf-8").read())["selections"]["msg"] == {}


def test_install_refuses_a_file_claiming_another_tool(tmp_path):
    reports = _reports(tmp_path)
    src = tmp_path / "selection.json"
    src.write_text(json.dumps(_payload({}, tool="SomethingElse")), encoding="utf-8")
    with pytest.raises(SelectionFormatError, match="written by"):
        selection_file.install_selection(str(reports), str(src))


# --------------------------------------------------------------------------- the stub

def test_the_generated_stub_is_readable_by_our_own_reader_and_carries_the_schema(tmp_path):
    path = report_ui.write_selection_stub(str(tmp_path), RUN)
    payload = selection_file.parse_selection_text(open(path, encoding="utf-8").read())
    assert payload["schema"] == selection_file.SCHEMA
    assert payload["run_id"] == RUN
    assert payload["selections"] == {}
    assert payload["tool_version"]


def test_the_stub_is_never_overwritten(tmp_path):
    path = report_ui.write_selection_stub(str(tmp_path), RUN)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('SCSel.preload({"selections": {"mem": {"mem-SNAP-0001": 1}}});\n')
    report_ui.write_selection_stub(str(tmp_path), RUN)
    assert "mem-SNAP-0001" in open(path, encoding="utf-8").read()


def test_the_stub_warns_against_renaming_a_json_to_js(tmp_path):
    """The failure it prevents is silent: bare JSON loaded as a script is a syntax error the
    browser discards, so the reports would open with nothing selected and no message."""
    text = open(report_ui.write_selection_stub(str(tmp_path), RUN), encoding="utf-8").read()
    assert "rename" in text.lower()
    assert "--install-selection" in text


# --------------------------------------------------------------------------- the browser side
#
# Pinned as source, in the style of tests/test_report_links.py: these are the JS contracts the
# Python side depends on, and nothing else would notice if one of them disappeared.

def test_the_store_carries_the_current_schema_and_the_run_provenance():
    js = report_ui.SELECT_JS
    assert f"var SCHEMA={selection_file.SCHEMA};" in js
    assert "schema:SCHEMA" in js
    assert "tool_version:(window.SCAUTO_VERSION||'')" in js
    assert "sources:(window.SCAUTO_SOURCES||null)" in js


def test_a_bare_message_id_is_quarantined_in_the_browser_too():
    js = report_ui.SELECT_JS
    assert "if(k==='msg'&&id.indexOf('|')<0){" in js
    assert "legacy[k][id]=" in js


def test_the_store_id_is_prefixed_and_the_anchor_is_not():
    vt = report_ui.VTABLE_JS
    assert "function selId(id){return (C&&C.selPrefix?C.selPrefix:'')+id;}" in vt
    assert 'data-id="\'+selId(id)+\'"' in vt
    assert "SCSel.get(C.selKind,selId(id))" in vt
    # selectShown must store the same prefixed ids, or "select all shown" and a row tick disagree
    assert "selId(rows[i][0])" in vt
    assert "selId:selId" in vt and "selKeys:selKeys" in vt


def test_both_save_forms_are_offered_and_json_is_the_default():
    assert "function saveFile(format)" in report_ui.SELECT_JS
    assert "a.download=js?'selection.js':'selection.json'" in report_ui.SELECT_JS
    bar = report_ui.selection_toolbar("memory")
    assert "scSelSaveJson()" in bar and "scSelSaveJs()" in bar
    assert ".json" in bar
    # loading still accepts either form
    assert 'accept=".js,.json,application/json"' in bar


def test_the_toolbar_warns_about_an_unattributable_message_selection():
    assert 'id="sellegacy"' in report_ui.selection_toolbar("conversation")
    assert "scSelLegacyCheck" in report_ui.SELECT_TOOLBAR_JS
    assert "SCSel.legacy('msg')" in report_ui.SELECT_TOOLBAR_JS


def test_clear_and_the_count_can_be_scoped_to_one_page():
    assert "function clear(kind,prefix)" in report_ui.SELECT_JS
    assert "function count(kind,prefix)" in report_ui.SELECT_JS
    assert "window.SCAUTO_SELPREFIX" in report_ui.SELECT_TOOLBAR_JS
    assert "SCSel.count(o.selKind,o.selPrefix)" in report_ui.VTABLE_JS


def test_a_cross_run_load_asks_first():
    assert "o.run_id!==window.SCAUTO_RUN" in report_ui.SELECT_JS
