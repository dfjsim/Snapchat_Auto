"""The retrieval engine: authority first, then pace, retries, stops and the record.

The network is replaced by a scripted fetcher and time by a fake clock, so every rule can be checked
exactly: the delay between requests, the per-minute cap, Retry-After and back-off, the next recorded
address after a refusal, stopping after failures or when the examiner says so, a pace changed while
waiting, nothing requested twice, and a hash-chained manifest that notices an edit.

Every input is synthetic.
"""
import json
import os
import random

import pytest

from scripts import cloud_download as cd

AUTH = cd.Authority("Search warrant 2026-001, Court of Somewhere", True, "2026-10-02T10:00:00Z")


class Clock:
    def __init__(self):
        self.t = 0.0
        self.hooks = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        for hook in list(self.hooks):
            hook(self.t)


class Fetcher:
    """Scripted outcomes per URL: bytes (a 200) or a FetchError, consumed in order."""
    user_agent = "test"

    def __init__(self, script, clock):
        self.script = {url: list(outcomes) for url, outcomes in script.items()}
        self.calls = []
        self.clock = clock

    def fetch(self, url, *, timeout, max_bytes, dest):
        self.calls.append((self.clock(), url))
        outcome = self.script[url].pop(0)
        if isinstance(outcome, cd.FetchError):
            raise outcome
        with open(dest, "wb") as fh:
            fh.write(outcome)
        import hashlib
        return cd.Response(200, url + "?final", [], {"Content-Type": "application/octet-stream"},
                           dest, len(outcome), hashlib.sha256(outcome).hexdigest(),
                           hashlib.md5(outcome).hexdigest())


def _decrypt(job, raw):
    return {"result": "ok", "ext": "mp4", "tail_ok": True, "data": b"PLAIN:" + raw}


def _engine(tmp_path, script, pace=None, authority=AUTH, events=None):
    clock = Clock()
    fetcher = Fetcher(script, clock)
    control = cd.Control(pace or cd.Pace(delay_s=5, jitter_s=0, max_per_min=100))
    engine = cd.Engine(cd.Store(str(tmp_path)), fetcher, control, authority,
                       on_event=(events.append if events is not None else None), clock=clock,
                       sleep=clock.sleep, rng=random.Random(1))
    return engine, fetcher, control, clock


def _job(n, urls=None):
    return cd.Job(f"SNAP-{n}", "media", urls or [("ZMEDIADOWNLOADURL", f"https://u/{n}")], "missing")


def test_nothing_is_requested_without_the_authority(tmp_path):
    for authority in (cd.Authority("Search warrant 2026-001", False), cd.Authority("short", True)):
        engine, fetcher, _c, _clock = _engine(tmp_path, {"https://u/1": [b"x"]}, authority=authority)
        with pytest.raises(ValueError):
            engine.run([_job(1)], decrypt=_decrypt)
        assert fetcher.calls == []


def test_requests_are_spaced_and_recorded(tmp_path):
    engine, fetcher, _c, _clock = _engine(tmp_path, {"https://u/1": [b"one"], "https://u/2": [b"two"]})
    summary = engine.run([_job(1), _job(2)], decrypt=_decrypt, scope={"missing": True})
    assert (summary.done, summary.failed, summary.stopped) == (2, 0, "completed")
    assert [t for t, _u in fetcher.calls] == [0.0, 5.0]
    records = engine.store.records()
    assert [r["type"] for r in records] == ["session", "request", "request", "end"]
    session = records[0]
    assert session["authority"]["note"].startswith("Search warrant") and session["scope"] == {"missing": True}
    req = records[1]
    assert req["http_status"] == 200 and req["bytes"] == 3 and req["decrypt"]["result"] == "ok"
    stored = os.path.join(engine.store.base, req["decrypt"]["stored"])
    assert open(stored, "rb").read() == b"PLAIN:one"
    assert open(os.path.join(engine.store.base, req["stored_encrypted"]), "rb").read() == b"one"
    assert engine.store.verify() == []
    assert not os.path.exists(os.path.join(engine.store.base, ".lock"))


def test_the_per_minute_cap(tmp_path):
    script = {f"https://u/{n}": [b"x"] for n in range(3)}
    engine, fetcher, _c, _clock = _engine(tmp_path, script,
                                          pace=cd.Pace(delay_s=0, jitter_s=0, max_per_min=2))
    engine.run([_job(n) for n in range(3)], decrypt=_decrypt)
    assert [round(t) for t, _u in fetcher.calls] == [0, 0, 60]


def test_retry_after_then_back_off(tmp_path):
    script = {"https://u/1": [cd.FetchError("HTTP 429", status=429, retry_after_s=7),
                              cd.FetchError("HTTP 503", status=503), b"ok"]}
    engine, fetcher, _c, _clock = _engine(tmp_path, script, pace=cd.Pace(
        delay_s=0, jitter_s=0, backoff_base_s=30, retries=3, max_per_min=100))
    summary = engine.run([_job(1)], decrypt=_decrypt)
    assert summary.done == 1
    assert [round(t) for t, _u in fetcher.calls] == [0, 7, 7 + 60]   # 30 * 2 ** (2 - 1)


def test_a_refused_address_moves_to_the_next_recorded_one(tmp_path):
    urls = [("ZMEDIADOWNLOADURL", "https://u/a"), ("ZMEDIAREDIRECTURI", "https://u/b")]
    script = {"https://u/a": [cd.FetchError("HTTP 403", status=403, permanent=True)],
              "https://u/b": [b"ok"]}
    engine, fetcher, _c, _clock = _engine(tmp_path, script)
    summary = engine.run([_job(1, urls)], decrypt=_decrypt)
    assert summary.done == 1 and [u for _t, u in fetcher.calls] == ["https://u/a", "https://u/b"]
    columns = [(r["url_column"], r["http_status"]) for r in engine.store.records()
               if r["type"] == "request"]
    assert columns == [("ZMEDIADOWNLOADURL", 403), ("ZMEDIAREDIRECTURI", 200)]


def test_it_stops_after_failures_in_a_row(tmp_path):
    script = {f"https://u/{n}": [cd.FetchError("network error")] for n in range(5)}
    engine, fetcher, _c, _clock = _engine(tmp_path, script, pace=cd.Pace(
        delay_s=0, jitter_s=0, retries=1, max_failures=2, max_per_min=100))
    summary = engine.run([_job(n) for n in range(5)], decrypt=_decrypt)
    assert summary.stopped == "failures" and len(fetcher.calls) == 2
    assert engine.store.records()[-1]["stopped"] == "failures"


def test_the_examiner_stops_it(tmp_path):
    script = {f"https://u/{n}": [b"x"] for n in range(3)}
    engine, fetcher, control, _clock = _engine(tmp_path, script)

    def on_event(event):
        if event.kind == "item_done":
            control.stop()
    engine.on_event = on_event
    summary = engine.run([_job(n) for n in range(3)], decrypt=_decrypt)
    assert summary.stopped == "examiner" and summary.done == 1 and len(fetcher.calls) == 1


def test_a_pace_change_applies_while_waiting(tmp_path):
    script = {"https://u/1": [b"x"], "https://u/2": [b"y"]}
    engine, fetcher, control, _clock = _engine(tmp_path, script, pace=cd.Pace(
        delay_s=300, jitter_s=0, max_per_min=100))

    def on_event(event):
        if event.kind == "wait" and control.pace.delay_s == 300:
            assert control.update(delay_s=1) == []
    engine.on_event = on_event
    engine.run([_job(1), _job(2)], decrypt=_decrypt)
    assert fetcher.calls[1][0] < 5
    assert control.update(delay_s=-1)                     # a bad value is refused, not applied


def test_pause_and_resume(tmp_path):
    script = {"https://u/1": [b"x"], "https://u/2": [b"y"]}
    engine, fetcher, control, clock = _engine(tmp_path, script)
    paused = []

    def on_event(event):
        if event.kind == "item_done" and not paused:
            control.pause()
            paused.append(clock())
    clock.hooks.append(lambda t: control.resume() if paused and t >= paused[0] + 100 else None)
    engine.on_event = on_event
    engine.run([_job(1), _job(2)], decrypt=_decrypt)
    assert fetcher.calls[1][0] >= 100


def test_nothing_is_requested_twice_unless_asked(tmp_path):
    engine, fetcher, _c, _clock = _engine(tmp_path, {"https://u/1": [b"x", b"x2"]})
    engine.run([_job(1)], decrypt=_decrypt)
    second = engine.run([_job(1)], decrypt=_decrypt)
    assert second.skipped == 1 and len(fetcher.calls) == 1
    third = engine.run([_job(1)], decrypt=_decrypt, redownload=True)
    assert third.done == 1 and len(fetcher.calls) == 2
    names = sorted(os.listdir(os.path.join(engine.store.base, "encrypted", "SNAP-1")))
    assert names == ["media-1.bin", "media-2.bin"]          # never overwritten


def test_an_edited_manifest_is_noticed(tmp_path):
    engine, _f, _c, _clock = _engine(tmp_path, {"https://u/1": [b"x"]})
    engine.run([_job(1)], decrypt=_decrypt)
    path = engine.store.manifest
    lines = open(path, encoding="utf-8").read().splitlines()
    rec = json.loads(lines[1])
    rec["http_status"] = 404
    lines[1] = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    assert cd.Store(str(tmp_path)).verify() != []


def test_a_second_writer_is_refused(tmp_path):
    store = cd.Store(str(tmp_path))
    store.lock()
    with pytest.raises(RuntimeError):
        cd.Store(str(tmp_path)).lock()
    store.unlock()
