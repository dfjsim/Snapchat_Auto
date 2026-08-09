"""The public selection API: the surface another tool builds a selection against.

This is a **compatibility surface**, and the thing that makes it one is that its consumer cannot be
updated in lockstep with this repository. Three properties therefore matter more than any feature:

* **It installs on its own.** ``snapchat_auto_selection`` must import nothing outside the standard
  library and nothing from Snapchat Auto, or the whole point — a tool producing a selection without
  taking on pandas, OpenCV and a GUI toolkit — is lost. Asserted by walking its ASTs, so it cannot rot
  quietly.
* **Its anchors are the generators' anchors.** ``anchor_for`` is asserted against the reports' own
  functions for the same identifiers. If the two ever disagree, a selection built through the API names
  rows that do not exist and the build refuses — which is safe, but useless.
* **A selection built here resolves like one saved from the browser.** That is the contract test at the
  bottom: same identifiers in, same rows out.

Every input is synthetic.
"""
import ast
import json
import os
import sys

import pytest

from scripts import contacts_report, partial_report
from snapchat_auto_selection import api, format as selformat


CONV = "aaaa0000-0000-4000-8000-000000000001"
SNAP = "SNAP-0001"
KEY = "0" * 32
SHA = "1" * 64

PACKAGE_DIR = os.path.dirname(os.path.abspath(api.__file__))


# --------------------------------------------------------------------------- it installs on its own

def _stdlib_names():
    """Every top-level module name the standard library provides on this interpreter."""
    names = set(sys.stdlib_module_names)
    names.discard("this")
    return names


def _imports_of(path):
    """``{module name}`` for every import in one file, top-level name only."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # a relative import, i.e. inside this package
                continue
            if node.module:
                found.add(node.module.split(".")[0])
    return found


def test_the_distribution_imports_nothing_but_the_standard_library():
    """Its dependency list is empty and has to stay empty, or it stops being installable alone."""
    stdlib = _stdlib_names()
    offenders = {}
    files = [os.path.join(PACKAGE_DIR, n) for n in sorted(os.listdir(PACKAGE_DIR))
             if n.endswith(".py")]
    assert files, "no modules found — is the package still where this test looks?"
    for path in files:
        outside = {name for name in _imports_of(path)
                   if name not in stdlib and name != "snapchat_auto_selection"}
        if outside:
            offenders[os.path.basename(path)] = sorted(outside)
    assert not offenders, f"non-stdlib imports in the selection distribution: {offenders}"


def test_the_distribution_imports_nothing_from_snapchat_auto():
    for name in sorted(os.listdir(PACKAGE_DIR)):
        if not name.endswith(".py"):
            continue
        found = _imports_of(os.path.join(PACKAGE_DIR, name))
        assert "scripts" not in found, f"{name} imports the application"
        assert "Snapchat_Auto" not in found, f"{name} imports the application"


def test_it_declares_no_dependencies():
    manifest = os.path.join(os.path.dirname(PACKAGE_DIR), "pyproject.toml")
    with open(manifest, encoding="utf-8") as fh:
        text = fh.read()
    assert "dependencies = []" in text


def test_the_application_re_exports_it_rather_than_copying_it():
    """One implementation of the format. A second copy is a divergence waiting to happen."""
    from scripts import selection_file

    assert selection_file.read_selection is selformat.read_selection
    assert selection_file.SCHEMA is selformat.SCHEMA
    source = os.path.abspath(selection_file.__file__)
    with open(source, encoding="utf-8") as fh:
        body = fh.read()
    assert "from snapchat_auto_selection.format import" in body
    assert "def parse_selection_text" not in body          # not reimplemented here


# --------------------------------------------------------------------------- anchors match the reports

def test_a_memory_anchor_is_what_the_memories_report_emits():
    assert api.anchor_for("mem", snap_id=SNAP) == f"mem-{SNAP}"


def test_a_conversation_anchor_is_what_the_conversations_report_emits():
    assert api.anchor_for("conv", conversation_id=CONV) == f"conv-{CONV}"


def test_a_message_anchor_is_qualified_exactly_as_the_index_qualifies_it():
    """The one an integrator would get wrong from prose, and the reason this function exists."""
    built = api.anchor_for("msg", conversation_id=CONV, server_message_id="12.0")
    # the composition conversations_report.index uses: the conversation's row id, a pipe, then the
    # page-local message anchor. The page anchor stays page-local; only the *store* id is qualified.
    assert built == api.anchor_for("conv", conversation_id=CONV) + "|msg-12.0"


def test_a_contact_anchor_matches_contact_anchor_including_its_fallbacks():
    """Asserted against the report's own function, for each step of its fallback chain."""
    for identifiers, contact in (
            ({"user_id": "u-0001"}, {"user_id": "u-0001", "username": "alice-test"}),
            ({"username": "alice-test"}, {"user_id": "", "username": "alice-test"}),
            ({"conversation_id": CONV}, {"user_id": "", "username": "", "conv_id": CONV}),
            ({}, {"user_id": "", "username": "", "conv_id": ""})):
        assert api.anchor_for("ct", **identifiers) == contacts_report.contact_anchor(contact), identifiers


def test_a_contact_anchor_substitutes_the_same_characters_the_report_does():
    odd = "a b/c+d"
    assert api.anchor_for("ct", username=odd) == contacts_report.contact_anchor({"username": odd})


def test_a_cache_entry_and_a_cached_file_anchor():
    assert api.anchor_for("cc", cache_key=KEY) == f"ck-{KEY}"
    assert api.anchor_for("cm", sha256=SHA) == f"cm-{SHA}"
    # a row with no decoded hash is anchored on its path, as the Library/Caches report anchors it
    assert api.anchor_for("cm", rel="Library/Caches/x/one") == "cm-Library/Caches/x/one"


def test_a_missing_identifier_is_refused_rather_than_guessed():
    for kind, identifiers in (("mem", {}), ("conv", {}), ("cc", {}), ("cm", {}),
                              ("msg", {"conversation_id": CONV})):
        with pytest.raises(ValueError):
            api.anchor_for(kind, **identifiers)


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="unknown kind"):
        api.anchor_for("nope", snap_id=SNAP)


# --------------------------------------------------------------------------- the builder

def test_the_builder_records_the_alternate_keys_it_was_given():
    builder = api.SelectionBuilder()
    builder.add_message(CONV, "12.0", ts=1700000000, sender="u-0001")
    builder.add_memory(SNAP, media_id="MEDIA-1", entry_id="ENTRY-1")
    builder.add_cached_file(sha256=SHA, raw_sha256=["ff00", "ff01"], rel="Library/Caches/x/one")

    selections = builder.to_payload()["selections"]
    assert selections["msg"][f"conv-{CONV}|msg-12.0"] == {"conv": CONV, "smid": "12.0",
                                                          "ts": 1700000000, "sender": "u-0001"}
    assert selections["mem"][f"mem-{SNAP}"] == {"snap": SNAP, "mediaid": "MEDIA-1",
                                                "entry": "ENTRY-1"}
    assert selections["cm"][f"cm-{SHA}"]["raw"] == ["ff00", "ff01"]


def test_identifiers_that_were_not_given_are_left_out_rather_than_recorded_empty():
    builder = api.SelectionBuilder()
    builder.add_memory(SNAP)
    assert builder.to_payload()["selections"]["mem"][f"mem-{SNAP}"] == {"snap": SNAP}


def test_adding_a_row_twice_keeps_whatever_either_call_knew():
    builder = api.SelectionBuilder()
    builder.add_memory(SNAP, media_id="MEDIA-1")
    builder.add_memory(SNAP, entry_id="ENTRY-1")
    assert builder.to_payload()["selections"]["mem"][f"mem-{SNAP}"] == {
        "snap": SNAP, "mediaid": "MEDIA-1", "entry": "ENTRY-1"}


def test_a_builder_payload_round_trips_through_both_file_forms(tmp_path):
    builder = api.SelectionBuilder(note="from the test")
    builder.add_conversation(CONV)
    builder.add_memory(SNAP)
    builder.set_relations("recommended")

    as_json = builder.write_json(str(tmp_path / "selection.json"))
    as_js = builder.write_js(str(tmp_path / "selection.js"))
    from_json = selformat.read_selection(as_json)
    from_js = selformat.read_selection(as_js)

    assert from_json["selections"] == from_js["selections"] == builder.to_payload()["selections"]
    # the digest is over what is selected, so both forms of one selection agree
    assert selformat.selection_digest(from_json) == selformat.selection_digest(from_js)
    assert from_json["relations"] == "recommended"
    assert from_json["note"] == "from the test"


def test_a_selection_built_from_identifiers_carries_no_provenance_and_that_is_valid():
    builder = api.SelectionBuilder()
    builder.add_memory(SNAP)
    payload = builder.to_payload()
    assert payload["sources"] is None and payload["run_id"] == "" and payload["tool_version"] == ""
    assert api.validate(payload) == []


# --------------------------------------------------------------------------- validate

def test_validate_accepts_a_good_payload():
    builder = api.SelectionBuilder()
    builder.add_conversation(CONV)
    builder.add_message(CONV, "12.0")
    builder.add_contact("u-0001", username="alice-test")
    builder.add_memory(SNAP)
    builder.add_cache_entry(KEY)
    builder.add_cached_file(sha256=SHA)
    assert api.validate(builder.to_payload()) == []


def test_validate_catches_an_unqualified_message_id():
    """The mistake the docs cannot prevent on their own, so validate() names it in full."""
    payload = {"tool": api.TOOL, "schema": 2, "selections": {"msg": {"msg-12.0": 1}}}
    problems = api.validate(payload)
    assert any("per-conversation ordinal" in p for p in problems)


def test_validate_catches_a_bad_schema_an_unknown_kind_and_an_empty_selection():
    assert any("outside what this build reads" in p
               for p in api.validate({"schema": 99, "selections": {"mem": {f"mem-{SNAP}": 1}}}))
    assert any("unknown kind" in p
               for p in api.validate({"schema": 2, "selections": {"nope": {"x": 1}}}))
    assert any("nothing is selected" in p for p in api.validate({"schema": 2, "selections": {}}))
    assert any("'selections' is missing" in p for p in api.validate({"schema": 2}))


def test_validate_catches_a_malformed_key_record_and_a_wrong_prefix():
    payload = {"schema": 2, "selections": {"mem": {f"mem-{SNAP}": "not an object",
                                                   "oops-1": 1}}}
    problems = api.validate(payload)
    assert any("should map to an object" in p for p in problems)
    assert any("does not look like a 'mem' id" in p for p in problems)


# --------------------------------------------------------------------------- describe

def test_describe_brackets_the_schema_it_writes():
    described = api.describe()
    assert described["schema_min"] <= described["schema_write"] <= described["schema_max"]
    assert described["api_version"] == api.API_VERSION


def test_describe_lists_exactly_the_kinds_the_rest_of_the_code_uses():
    """An integrator builds its UI from this, so it may not drift from the closure's own kinds."""
    assert set(api.describe()["kinds"]) == set(partial_report.KINDS)
    assert set(api.IDENTIFIERS) == set(partial_report.KINDS)


def test_describe_names_an_identifier_the_builder_actually_accepts():
    import inspect

    adders = {"conv": api.SelectionBuilder.add_conversation, "msg": api.SelectionBuilder.add_message,
              "ct": api.SelectionBuilder.add_contact, "mem": api.SelectionBuilder.add_memory,
              "cc": api.SelectionBuilder.add_cache_entry, "cm": api.SelectionBuilder.add_cached_file}
    for kind, described in api.describe()["kinds"].items():
        accepted = set(inspect.signature(adders[kind]).parameters) - {"self"}
        for name in described["primary"] + described["alternates"]:
            assert name in accepted, f"{kind}: describe() names {name}, which add_* does not take"


def test_the_executable_reports_the_relation_vocabulary_too(capsys):
    """The format half comes from the package; the relations only the installed build knows."""
    import Snapchat_Auto as app

    assert app.describe_selection_api() == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["relations"]) == set(partial_report.RELATION_KEYS)
    assert payload["relation_switches"] == list(partial_report.POLICY_TOKENS)
    assert payload["tool_version"]
    assert set(payload["relation_presets"]) == set(partial_report.PRESETS)


# --------------------------------------------------------------------------- the contract

def test_a_built_selection_and_a_browser_saved_one_resolve_to_the_same_rows():
    """The contract: same identifiers in, same rows out, whichever route produced the file.

    The browser writes the anchors the generators emit; the API writes the anchors ``anchor_for``
    derives. If those two ever disagree the API is decorative, so this resolves both against one index.
    """
    conv_index = partial_report.Index("conv")
    conv_index.add(f"conv-{CONV}", {}, conv=CONV)
    msg_index = partial_report.Index("msg")
    msg_index.add(f"conv-{CONV}|msg-12.0", {}, smid=f"{CONV}|12.0")
    mem_index = partial_report.Index("mem")
    mem_index.add(f"mem-{SNAP}", {}, snap=SNAP)
    indexes = {"conv": conv_index, "msg": msg_index, "mem": mem_index}

    builder = api.SelectionBuilder()
    builder.add_conversation(CONV)
    builder.add_message(CONV, "12.0")
    builder.add_memory(SNAP)
    from_api = partial_report.resolve(indexes, builder.to_payload())

    # what the reports' own JS would have written for the same three rows
    from_browser = partial_report.resolve(indexes, {
        "tool": "Snapchat_Auto", "schema": 2,
        "selections": {"conv": {f"conv-{CONV}": {"conv": CONV}},
                       "msg": {f"conv-{CONV}|msg-12.0": {"conv": CONV, "smid": "12.0"}},
                       "mem": {f"mem-{SNAP}": {"snap": SNAP}}}})

    assert from_api.seeds == from_browser.seeds
    assert from_api.ok and not from_api.moved
