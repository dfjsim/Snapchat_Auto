"""The run window: what it shows from the progress module, and that a run behind it really runs.

The window itself needs a display and a Tk root of its own (a second root in one pytest process
fails), so that part runs in a subprocess and is skipped where there is no GUI.

Every input is synthetic.
"""
import os
import queue
import subprocess
import sys
import textwrap

import pytest

from scripts import cloud_download, progress, run_window

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_stages_read_as_done_failed_and_running():
    snap = {"done_stages": [{"label": "Extraction", "seconds": 4.8, "depth": 0, "failed": False},
                            {"label": "Chats", "seconds": 2.0, "depth": 0, "failed": True}],
            "stack": [{"label": "Memories", "seconds": 70, "depth": 0}],
            "detail": "decrypting SCContent media", "done": 340, "total": 1320,
            "finished": False, "quiet": 42.0, "elapsed": 90}
    assert run_window.stage_text(snap).splitlines() == [
        "✓ Extraction — 4.8 s", "✗ Chats — 2.0 s", "▶ Memories — 1 min 10 s"]
    assert run_window.step_text(snap) == "decrypting SCContent media — 340 of 1,320"
    assert "nothing logged for 42.0 s" in run_window.quiet_text(snap)
    assert run_window.quiet_text(dict(snap, quiet=3)) == ""


def test_the_retrieval_runs_on_the_run_thread_and_reports_to_the_window():
    class Request:
        pace = cloud_download.Pace()
        control = None
        on_event = None
    request, events = Request(), queue.Queue()
    runner = run_window.cloud_runner(request, events)
    assert isinstance(request.control, cloud_download.Control)
    result = runner(lambda: (request.on_event(cloud_download.Event("start", total=2)), "summary")[1])
    seen = []
    while not events.empty():
        seen.append(events.get())
    assert result == "summary"
    assert seen[0] == "begin" and seen[-1] == "end" and seen[1].kind == "start"


def test_a_run_behind_the_window():
    pytest.importorskip("FreeSimpleGUI")
    code = textwrap.dedent("""
        import logging, sys, time
        sys.path.insert(0, ROOT)
        import Snapchat_Auto as app
        from scripts import progress, run_window
        def fake_run(n=3):
            progress.start_run(heartbeat=False)
            for i in range(n):
                with progress.stage(f"Stage {i}"):
                    progress.step("working", i, n)
                    logging.getLogger("x").info(f"step {i}")
                    time.sleep(0.3)
            logging.getLogger("x").warning("one warning")
            progress.finish_run()
            return "the run folder"
        try:
            app.apply_theme("light")
            result, error = run_window.run_in_window(app._cloud_ui(), fake_run, {"n": 3},
                                                     auto_close=True)
        except Exception as e:
            if "display" in str(e).lower() or "tcl" in str(e).lower():
                print("NO-GUI"); sys.exit(0)
            raise
        assert result == "the run folder" and error is None, (result, error)
        # a run that ends itself is reported, not lost
        def ends_itself():
            raise SystemExit(5)
        result, error = run_window.run_in_window(app._cloud_ui(), ends_itself, {}, auto_close=True)
        assert result is None and isinstance(error, SystemExit) and error.code == 5
        print("OK")
    """).replace("ROOT", repr(ROOT))
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         cwd=ROOT)
    if "NO-GUI" in res.stdout:
        pytest.skip("no GUI available here")
    assert res.returncode == 0 and "OK" in res.stdout, res.stderr[-2000:]
