"""The Cloud download window: its date-rule table, and that it asks for what the command line asks.

The table's logic is plain functions: *Add for checked* and *Copy range to…* go through the same
``add_for_fields``, so the same answers give the same rules, and the rows become the command line's
``--cloud-dates`` text, which the command line's own parser reads. The window's answers are turned
into the command line's options and built by ``_cloud_request`` itself — the two front ends cannot
ask for different things.

Building the windows needs a display and a Tk root of its own (a second root in one pytest process
fails), so that part runs in a subprocess and is skipped where there is no GUI.

Every input is synthetic.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

import Snapchat_Auto as app
from scripts import cloud_gui

CAP, CRE = "ZGALLERYSNAP.ZCAPTURETIMEUTC", "ZGALLERYSNAP.ZCREATETIMEUTC"
ENTRY = "ZGALLERYENTRY.ZCREATETIMEUTC"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_one_range_for_several_timestamps():
    rows = cloud_gui.add_for_fields([], [CAP, CRE], "2024-03-01", "2024-03-31", "include")
    assert [(r["field"], r["from"], r["to"]) for r in rows] == [
        (CAP, "2024-03-01", "2024-03-31"), (CRE, "2024-03-01", "2024-03-31")]
    # a field that has a row of the same mode is updated, not duplicated; another mode is added
    rows = cloud_gui.add_for_fields(rows, [CAP, ENTRY], "2024-04-01", "", "include")
    assert [(r["field"], r["from"]) for r in rows] == [(CAP, "2024-04-01"), (CRE, "2024-03-01"),
                                                       (ENTRY, "2024-04-01")]
    rows = cloud_gui.add_for_fields(rows, [CAP], "", "2024-02-29", "exclude")
    assert len(rows) == 4 and rows[-1]["mode"] == "exclude"


def test_rows_become_the_command_lines_rules():
    rows = [{"field": CAP, "from": "2024-03-01", "to": "2024-03-31", "mode": "include"},
            {"field": "", "from": "", "to": "", "mode": "include"},              # an empty row
            {"field": "*", "from": "", "to": "2025-01-01", "mode": "exclude"}]
    spec = cloud_gui.rules_spec(rows)
    assert spec == f"{CAP}|2024-03-01|2024-03-31|include;*||2025-01-01|exclude"


def test_the_window_asks_what_the_command_line_asks(tmp_path):
    values = {"attest": True, "authority": "Search warrant 2026-001, Court of Somewhere",
              "scope_missing": True, "scope_incomplete": False, "scope_snaps": True,
              "snaps": "0a0b0c0d-1111-4222-8333-444455556666\n", "overlays": False,
              "redownload": False, "delay": "6", "jitter": "1", "maxpm": "4", "maxfail": "3",
              "timeout": "30"}
    rows = [{"field": CAP, "from": "2024-03-01", "to": "2024-03-31", "mode": "include"}]
    request, error = app._cloud_request(cloud_gui.cli_values(values, rows, "existing"),
                                        "post-run", "utc")
    assert error is None
    assert request.scopes == {"missing", "snaps"} and not request.overlays
    assert request.date_rules[0].fields == (CAP,) and request.pace.max_per_min == 4
    # unticked attestation: refused by the same function, with the same words
    refused = app._cloud_request(cloud_gui.cli_values(dict(values, attest=False), rows, "run"),
                                 "run", "utc")
    assert refused[0] is None and "Nothing was contacted" in refused[1]


def test_counts_come_from_the_runs_memories_index(tmp_path):
    data = tmp_path / "Reports" / "Memories" / "data"
    data.mkdir(parents=True)
    rows = [["mem-A", [], "", {}, 0, {"cl": "missing"}], ["mem-B", [], "", {}, 0, {}],
            ["mem-C", [], "", {}, 0, {"cl": "missing"}], ["mem-D", [], "", {}, 0, {"cl": "incomplete"}]]
    (data / "index.js").write_text("SCV.setRows(" + json.dumps(rows) + ");\n", encoding="utf-8")
    assert cloud_gui.candidate_counts(str(tmp_path)) == {"missing": 2, "incomplete": 1}
    assert cloud_gui.candidate_counts(str(tmp_path / "nothing")) == {}


def test_the_windows_build():
    pytest.importorskip("FreeSimpleGUI")
    code = textwrap.dedent("""
        import sys
        sys.path.insert(0, ROOT)
        import Snapchat_Auto as app
        from scripts import cloud_gui, cloud_download, cloud_memories
        try:
            app.apply_theme("light")
            ui = app._cloud_ui()
            for mode in ("run", "existing"):
                w = cloud_gui.build_cloud_window(ui, mode=mode, run_folder="", keychain="")
                assert w["start"].Disabled, "Start must wait for the authority"
                assert w["attest"].get() is False and w["authority"].get() == ""
                w.close()
            req = cloud_memories.CloudRequest(cloud_download.Authority("Warrant 2026-001", True, "t"))
            p = cloud_gui.build_progress_window(ui, req)
            job = cloud_download.Job("S-1", "media", [], "missing")
            for ev in (cloud_download.Event("start", total=2),
                       cloud_download.Event("item_done", job=job, done=1, total=2, text="200"),
                       cloud_download.Event("wait", job=job, wait_s=3.0, text="pace")):
                cloud_gui._show_event(p, ev)
            p.close()
        except Exception as error:
            if "display" in str(error).lower() or "tcl" in str(error).lower():
                print("NO-GUI"); sys.exit(0)
            raise
        print("OK")
    """).replace("ROOT", repr(ROOT))
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         cwd=ROOT)
    if "NO-GUI" in res.stdout:
        pytest.skip("no GUI available here")
    assert res.returncode == 0 and "OK" in res.stdout, res.stderr[-2000:]
