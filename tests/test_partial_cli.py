"""Driving a partial run: the option table, the request both front ends build, and the evidence gate.

The CLI and the GUI must produce **the same** :class:`partial_report.Request`. If they can diverge,
one of them quietly builds a different extract from the same instructions — so the GUI's relation
dialog is converted into the CLI's own ``--relations`` vocabulary and parsed back by the same code,
and that round trip is pinned here.

The other half is the gate: a partial report is a subset of a report someone ticked rows in, and the
two are only comparable if they were built from the same evidence by the same build. Refusing is the
default; proceeding is something the examiner asks for and is then stated on every page.

Every input is synthetic.
"""
import json

import pytest

import Snapchat_Auto as app
from scripts import partial_report, source_fingerprint


CONV = "aaaa0000-0000-4000-8000-00000000000a"


def _selection(tmp_path, name="selection.json", **over):
    payload = {"tool": "Snapchat_Auto", "schema": 2, "tool_version": "1.5.2+build.1",
               "run_id": "run-1", "exported": "2026-01-01T00:00:00Z",
               "sources": {"tool_version": "1.5.2+build.1", "digest": "d" * 64,
                           "artifacts": {"arroyo": {"present": True, "path": "/x/arroyo.db",
                                                    "sha256": "a" * 64, "label": "arroyo.db"}}},
               "selections": {"conv": {f"conv-{CONV}": {"conv": CONV}}}}
    payload.update(over)
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path), payload


# --------------------------------------------------------------------------- the option table

def test_a_value_may_start_with_a_single_dash():
    """``--relations -mem_group`` is the documented way to subtract one from the recommended set.

    The parser used to reject any value starting with "-" as a missing value, which made the form the
    usage text advertises impossible to type.
    """
    values, error = app._parse_options(["--relations", "-mem_group"], app._CLI_OPTIONS)
    assert error is None and values["relations"] == "-mem_group"


def test_two_options_in_a_row_is_still_a_missing_value():
    _values, error = app._parse_options(["--relations", "--tz", "utc"], app._CLI_OPTIONS)
    assert error and "requires a value" in error


def test_an_unknown_option_is_named():
    _values, error = app._parse_options(["--nope", "x"], app._CLI_OPTIONS)
    assert error and "nope" in error


# --------------------------------------------------------------------------- the request

def test_the_request_carries_the_selection_its_digest_and_the_case_reference(tmp_path):
    path, payload = _selection(tmp_path)
    request, error = app._partial_request({"selection": path, "case-ref": "EXHIBIT-7"})

    assert error is None
    assert request.prov["case_ref"] == "EXHIBIT-7"
    assert request.prov["selection"]["name"] == "selection.json"
    assert request.prov["selection"]["counts"] == {"conv": 1}
    assert len(request.prov["selection"]["sha256"]) == 64
    assert request.prov["selection"]["digest"]
    assert request.selection["selections"]["conv"]


def test_the_defaults_refuse_rather_than_proceed(tmp_path):
    path, _ = _selection(tmp_path)
    request, _error = app._partial_request({"selection": path})
    assert request.options["unresolved"] == "refuse"
    assert request.options["sources_mismatch"] == "refuse"
    assert request.options["version_mismatch"] == "refuse"
    assert request.options["transitive"] is False
    assert request.options["legacy_reports"] is False


def test_the_relation_spec_reaches_the_options(tmp_path):
    path, _ = _selection(tmp_path)
    request, error = app._partial_request({"selection": path,
                                           "relations": "mem_cache,legacy_reports,transitive"})
    assert error is None
    assert [k for k, on in request.options["relations"].items() if on] == ["mem_cache"]
    assert request.options["transitive"] and request.options["legacy_reports"]


def test_a_bad_option_value_is_reported_rather_than_silently_defaulted(tmp_path):
    path, _ = _selection(tmp_path)
    for bad in ({"unresolved": "maybe"}, {"sources-mismatch": "ignore"},
                {"version-mismatch": "whatever"}, {"max-rows": "lots"},
                {"relations": "no_such_relation"}):
        request, error = app._partial_request({"selection": path, **bad})
        assert request is None and error, bad


def test_a_missing_selection_file_is_reported(tmp_path):
    request, error = app._partial_request({"selection": str(tmp_path / "nope.json")})
    assert request is None and "not found" in error


# --------------------------------------------------------------------------- both front ends agree

def test_the_gui_dialog_and_the_cli_spec_mean_the_same_thing():
    """The contract test between the two front ends."""
    state = {"relations": {"mem_cache": True, "msg_cache": True, "participants": True},
             "transitive": True, "legacy_reports": True}
    spec = app._relations_spec(state)

    relations = partial_report.parse_relations(spec)
    policy = partial_report.parse_policy(spec)
    assert {k for k, on in relations.items() if on} == {"mem_cache", "msg_cache", "participants"}
    assert policy == {"transitive": True, "legacy_reports": True}


def test_an_empty_dialog_means_minimal_not_recommended():
    """A dialog with every box cleared must not fall back to the default set."""
    spec = app._relations_spec({"relations": {}, "transitive": False, "legacy_reports": False})
    assert spec == "minimal"
    assert not any(partial_report.parse_relations(spec).values())


def test_the_policy_switches_are_not_mistaken_for_relations():
    for token in partial_report.POLICY_TOKENS:
        assert token not in partial_report.RELATION_KEYS
        # accepted in a relation list, and does not turn a relation on
        assert not any(partial_report.parse_relations(token).values())


def test_a_preset_inside_a_list_is_refused_with_an_explanation():
    with pytest.raises(ValueError, match="stand on its own"):
        partial_report.parse_relations("all,-transitive")


# --------------------------------------------------------------------------- the evidence gate

def _sources(sha="a" * 64):
    return {"tool_version": "1.5.2+build.1", "digest": "d" * 64,
            "artifacts": {"arroyo": {"present": True, "path": "/x/arroyo.db", "sha256": sha,
                                     "label": "arroyo.db"}}}


def _request(payload, **options):
    return partial_report.Request(payload, {**partial_report.default_options(), **options})


def test_matching_evidence_and_build_proceeds_and_records_both_verdicts(tmp_path, monkeypatch):
    _path, payload = _selection(tmp_path)
    monkeypatch.setattr(source_fingerprint.app_version, "get_version", lambda: "1.5.2+build.1")
    request = _request(payload)

    version, sources = partial_report.check_evidence(request, _sources())
    assert version.ok and sources.ok
    assert request.prov["sources"]["ok"] and request.prov["version"]["ok"]
    assert request.prov["reuse"]["allowed"] is True


def test_a_differing_artifact_refuses_by_default(tmp_path, monkeypatch):
    _path, payload = _selection(tmp_path)
    monkeypatch.setattr(source_fingerprint.app_version, "get_version", lambda: "1.5.2+build.1")
    with pytest.raises(partial_report.EvidenceMismatch, match="not what the selection was made from"):
        partial_report.check_evidence(_request(payload), _sources(sha="f" * 64))


def test_a_differing_artifact_can_be_proceeded_through_and_is_then_on_record(tmp_path, monkeypatch):
    _path, payload = _selection(tmp_path)
    monkeypatch.setattr(source_fingerprint.app_version, "get_version", lambda: "1.5.2+build.1")
    request = _request(payload, sources_mismatch="proceed")

    _version, sources = partial_report.check_evidence(request, _sources(sha="f" * 64))
    assert not sources.ok
    assert request.prov["sources"]["ok"] is False
    assert request.prov["reuse"]["allowed"] is False        # nothing may be reused across a difference
    # and the page that carries it says so
    banner = partial_report.banner_html(_closure_of(request), request.prov)
    assert "differ from the run the selection was made in" in banner


def test_a_different_build_refuses_and_then_disables_reuse(tmp_path, monkeypatch):
    _path, payload = _selection(tmp_path)
    monkeypatch.setattr(source_fingerprint.app_version, "get_version", lambda: "9.9.9+build.9")
    with pytest.raises(partial_report.EvidenceMismatch, match="1.5.2"):
        partial_report.check_evidence(_request(payload), _sources())

    request = _request(payload, version_mismatch="resolve")
    partial_report.check_evidence(request, _sources())
    assert request.prov["version"]["ok"] is False
    assert request.prov["reuse"]["allowed"] is False


def test_a_selection_with_no_fingerprints_is_not_a_mismatch_to_refuse_over(tmp_path, monkeypatch):
    """An externally produced selection has none. Refusing would make the public API unusable.

    What it must never do is report "verified" — both verdicts come back as *not comparable*, which the
    pages render as "not possible to verify".
    """
    _path, payload = _selection(tmp_path, sources=None, tool_version="")
    monkeypatch.setattr(source_fingerprint.app_version, "get_version", lambda: "1.5.2+build.1")
    request = _request(payload)

    version, sources = partial_report.check_evidence(request, _sources())     # no raise
    assert not version.comparable and not sources.comparable
    assert request.prov["sources"] is None and request.prov["version"] is None
    assert request.prov["reuse"]["allowed"] is False
    assert "not possible to verify" in partial_report.banner_html(_closure_of(request), request.prov)


def _closure_of(request):
    """The smallest closure that renders: one conversation, nothing else."""
    index = partial_report.Index("conv")
    index.add(f"conv-{CONV}", {})
    indexes = {"conv": index}
    return partial_report.expand(indexes, partial_report.resolve(indexes, request.selection),
                                 request.options)
