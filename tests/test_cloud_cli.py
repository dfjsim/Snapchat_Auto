"""The cloud download from the command line: every refusal happens before anything is contacted.

A retrieval needs ``--attest yes`` and ``--authority``; it is part of a full iOS run only; its scopes,
snap ids and date rules must read. The fetcher is replaced by one that fails the test if it is ever
built, so "nothing was contacted" is checked, not assumed. The run folder's settings decide how a
later retrieval refreshes the reports.

Every input is synthetic.
"""
import json
import os

import pytest

import Snapchat_Auto as app
from scripts import cloud_download, cloud_memories, cloud_refresh, partial_report


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a fetcher was built — something would have been contacted")
    monkeypatch.setattr(cloud_download, "UrllibFetcher", refuse)


@pytest.fixture
def zip_file(tmp_path):
    path = tmp_path / "extraction.zip"
    path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    return str(path)


@pytest.mark.parametrize("extra", [
    ["--cloud", "missing"],                                            # no attestation, no note
    ["--cloud", "missing", "--attest", "yes"],                         # no note
    ["--cloud", "missing", "--authority", "Search warrant 2026-001"],   # not attested
    ["--cloud", "everything", "--attest", "yes", "--authority", "Search warrant 2026-001"],
    ["--cloud", "missing", "--attest", "yes", "--authority", "Search warrant 2026-001",
     "--cloud-dates", "ZCAPTURETIMEUTC|yesterday||include"],
    ["--cloud-snaps", "not-an-id", "--attest", "yes", "--authority", "Search warrant 2026-001"],
    ["--attest", "yes", "--authority", "Search warrant 2026-001"],     # nothing to retrieve
])
def test_refusals(zip_file, tmp_path, extra):
    code = app.run_cli(["--zip", zip_file, "--workdir", str(tmp_path / "w")] + extra)
    assert code == 2
    assert not os.path.exists(tmp_path / "w")                          # no run was even started


def test_not_with_a_selection_or_on_android(zip_file, tmp_path):
    ok = ["--cloud", "missing", "--attest", "yes", "--authority", "Search warrant 2026-001"]
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"tool": "Snapchat_Auto", "schema": 2, "selections": {}}))
    assert app.run_cli(["--zip", zip_file, "--selection", str(sel)] + ok) == 2
    assert app.run_cli(["--zip", zip_file, "--os", "android"] + ok) == 2


def test_the_request_the_options_build(tmp_path):
    ids = tmp_path / "ids.txt"
    ids.write_text("0a0b0c0d-1111-4222-8333-444455556666\n")
    values = {"cloud": "missing,incomplete", "attest": "yes",
              "authority": "Consent of the account holder", "cloud-snaps": f"@{ids}",
              "cloud-dates": "ZCAPTURETIMEUTC,ZGALLERYENTRY.ZCREATETIMEUTC|2024-03-01|2024-03-31|"
                             "include;*||2025-01-01|exclude",
              "cloud-delay": "8", "cloud-max-per-min": "3", "cloud-overlays": "no"}
    request, error = app._cloud_request(values, "run", "utc")
    assert error is None
    assert request.scopes == {"missing", "incomplete", "snaps"}
    assert request.snap_ids == {"0A0B0C0D-1111-4222-8333-444455556666"}
    assert [r.fields for r in request.date_rules] == [
        ("ZGALLERYSNAP.ZCAPTURETIMEUTC", "ZGALLERYENTRY.ZCREATETIMEUTC"), ("*",)]
    assert (request.pace.delay_s, request.pace.max_per_min, request.overlays) == (8.0, 3, False)
    assert request.authority.attested and request.authority.attested_utc


def test_post_run_refusals(tmp_path):
    assert app.run_cloud_download(["--cloud-download", str(tmp_path / "absent")]) == 2
    run = tmp_path / "run"
    run.mkdir()
    assert app.run_cloud_download(["--cloud-download", str(run), "--attest", "yes",
                                   "--authority", "Search warrant 2026-001"]) == 2
    assert app.run_cloud_download(["--cloud-download", str(run), "--cloud", "missing"]) == 2


def test_the_refresh_follows_the_build(tmp_path):
    cloud_refresh.write_settings(str(tmp_path), os="ios", tz="utc", padding="both")
    settings = cloud_refresh.load_settings(str(tmp_path))
    assert settings["tz"] == "utc" and settings["tool_version"]
    assert cloud_refresh.refresh_mode(settings)[0] == "targeted"
    assert cloud_refresh.refresh_mode(dict(settings, tool_version="0.0.1"))[0] == "full"
    assert cloud_refresh.refresh_mode({})[0] == "full"
    assert cloud_refresh.refresh_mode(settings, "full")[0] == "full"


def test_a_partial_extract_states_the_authority():
    class Closure:
        included, warnings = {}, []

        def total(self):
            return 0
    prov = {"sources": {"ok": True}, "cloud": {"memories": 2, "files": 3,
                                               "sessions": [{"note": "Warrant 2026-001"}],
                                               "first_utc": "a", "last_utc": "b"}}
    assert "Warrant 2026-001" in partial_report.banner_html(Closure(), prov)
    assert "cloud" in partial_report.PROVENANCE_KEYS
    assert cloud_memories.SCOPES == ("missing", "incomplete", "selection", "snaps")
