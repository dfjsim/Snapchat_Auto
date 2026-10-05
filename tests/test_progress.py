"""Where a run is: stages, counts, the heartbeat into a silence, and the timing summary.

Every input is synthetic.
"""
import logging
import threading
import time

from scripts import progress


def test_stages_nest_and_the_summary_says_how_long_each_took(caplog):
    progress.start_run(heartbeat=False)
    with caplog.at_level(logging.INFO):
        with progress.stage("Reports"):
            with progress.stage("Memories"):
                progress.step("decrypting SCContent media", 3, 10)
                assert progress.describe().startswith(
                    "Reports › Memories: decrypting SCContent media (3 of 10) — ")
            with progress.stage("cache_controller"):
                pass
        stages = progress.finish_run()
    assert [(s["label"], s["depth"]) for s in stages] == [
        ("Memories", 1), ("cache_controller", 1), ("Reports", 0)]
    text = caplog.text
    assert "Stage timings (total" in text and "    Memories: " in text
    # the caller's finally calls it again: nothing is logged twice
    caplog.clear()
    progress.finish_run()
    assert "Stage timings" not in caplog.text


def test_a_stage_an_error_left_open_is_closed_and_said_to_have_stopped(caplog):
    progress.start_run(heartbeat=False)
    try:
        with progress.stage("Chats"):
            raise ValueError("boom")
    except ValueError:
        pass
    opened = progress.begin("Memories")                         # never ended
    with caplog.at_level(logging.INFO):
        stages = progress.finish_run()
    assert [s["failed"] for s in stages] == [True, True] and opened
    assert "(stopped by an error)" in caplog.text


def test_the_heartbeat_speaks_only_into_a_silence(caplog, monkeypatch):
    monkeypatch.setattr(progress, "HEARTBEAT_S", 0.5)
    with caplog.at_level(logging.INFO):
        progress.start_run()
        with progress.stage("Library/Caches"):
            progress.step("reading and decoding Library/Caches files", 12)
            time.sleep(2.2)
        progress.finish_run()
    beats = [r.getMessage() for r in caplog.records if "still working" in r.getMessage()]
    assert beats and "Library/Caches: reading and decoding Library/Caches files (12)" in beats[0]
    assert len(beats) <= 3                                    # one per silence, not one per second


def test_a_counter_several_threads_advance():
    progress.start_run(heartbeat=False)
    with progress.stage("Library/Caches"):
        counter = progress.Counter(400, "hashing")
        threads = [threading.Thread(target=lambda: [counter.tick() for _ in range(100)])
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = progress.snapshot()
    progress.finish_run()
    assert (snap["detail"], snap["done"], snap["total"]) == ("hashing", 400, 400)
    assert snap["stack"][0]["label"] == "Library/Caches"


def test_a_skip_reaches_the_work_and_a_new_run_forgets_it():
    progress.start_run(heartbeat=False)
    progress.request_skip("thumbnails")
    assert progress.skip_requested("thumbnails")
    progress.start_run(heartbeat=False)
    assert not progress.skip_requested("thumbnails")
    progress.finish_run()


def test_durations_read_as_a_person_would_say_them():
    assert progress.duration(4.84) == "4.8 s"
    assert progress.duration(130) == "2 min 10 s"
    assert progress.duration(3 * 3600 + 5 * 60) == "3 h 05 min"
