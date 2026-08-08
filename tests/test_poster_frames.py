"""Identifying what an "....ftyp" container holds, and the guards around poster extraction.

All of this comes from one real cached file: 1,859 bytes whose ftyp brand is `M4A `. It was
reported to the examiner as a video, and then handed to a video decoder, which blocked inside a
single read indefinitely. So: the brand is read, so an audio recording or a photo is not called a
video; and extraction is bounded, because a thumbnail is a convenience and no convenience may cost
the examiner their report.

Every input is synthetic. No extraction data is required or used.
"""
import os
import subprocess
import sys
import time

from scripts import memories_media_report as memories_report
from scripts.data import poster_worker, sniff


def _iso_bmff(tmp_path, name, brand):
    """The first 12 bytes of an ISO base media file: size, 'ftyp', major brand."""
    path = tmp_path / name
    path.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + brand + b"\x00" * 64)
    return str(path)


def test_an_audio_only_container_is_not_offered_to_the_decoder(tmp_path):
    """M4A starts with the same ....ftyp bytes as an .mp4 — and holds no frame to extract."""
    assert not memories_report.has_video_track(_iso_bmff(tmp_path, "a.mp4", b"M4A "))
    assert not memories_report.has_video_track(_iso_bmff(tmp_path, "b.mp4", b"M4B "))


def test_a_still_image_container_is_not_offered_to_the_decoder(tmp_path):
    """HEIF and AVIF are ISO base media files too, and hold no video frames."""
    assert not memories_report.has_video_track(_iso_bmff(tmp_path, "i.mp4", b"heic"))
    assert not memories_report.has_video_track(_iso_bmff(tmp_path, "j.mp4", b"avif"))


def test_a_video_brand_is_left_to_the_decoder(tmp_path):
    for brand in (b"isom", b"mp42", b"qt  ", b"M4V "):
        assert memories_report.has_video_track(_iso_bmff(tmp_path, "v.mp4", brand))


def test_an_unreadable_file_is_left_to_the_decoder(tmp_path):
    """Only a positively identified audio brand is skipped; anything else is still tried."""
    missing = str(tmp_path / "gone.mp4")
    assert memories_report.has_video_track(missing)


# ------------------------------------------------------------------ the pass survives a hung file

def _fake_worker(tmp_path, script):
    """A stand-in worker process, so the contract can be tested without OpenCV or a real video."""
    path = tmp_path / "fake_worker.py"
    path.write_text(script, encoding="utf-8")
    return [sys.executable, str(path)]


HANGS_ON_SECOND = """
import sys, time
for line in sys.stdin:
    src = line.split("\\t")[0]
    sys.stdout.write("START %s\\n" % src); sys.stdout.flush()
    if src.endswith("b"):
        time.sleep(600)                      # the file that blocks the decoder for good
    sys.stdout.write("OK %s\\n" % src); sys.stdout.flush()
"""


def test_a_hung_file_is_skipped_and_the_rest_still_run(monkeypatch, tmp_path):
    """The whole point: one undecodable video costs its timeout, not the pass."""
    monkeypatch.setattr(poster_worker, "_spawn",
                        lambda: subprocess.Popen(_fake_worker(tmp_path, HANGS_ON_SECOND),
                                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE, text=True, bufsize=1))
    jobs = [(n, n + ".jpg", False) for n in ("a", "b", "c")]
    started = time.monotonic()
    results, _ = poster_worker.run_jobs(jobs, file_timeout=0.5, budget=30)
    elapsed = time.monotonic() - started

    assert results.get("a") is True                 # before the hang
    assert results.get("b") is False                # the hung one is answered for, not waited out
    assert results.get("c") is True                 # after it — the worker was restarted
    assert elapsed < 20                             # it cost the timeout, not ten minutes


def test_the_budget_stops_a_pass_that_would_run_too_long(monkeypatch, tmp_path):
    """A case where most of the cached video is undecodable must not hold the report."""
    monkeypatch.setattr(poster_worker, "_spawn",
                        lambda: subprocess.Popen(_fake_worker(tmp_path, HANGS_ON_SECOND),
                                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE, text=True, bufsize=1))
    jobs = [("b", "b.jpg", False)] * 50             # every one of them hangs
    started = time.monotonic()
    poster_worker.run_jobs(jobs, file_timeout=0.4, budget=2.0)
    assert time.monotonic() - started < 25          # bounded by the budget, not by 50 x timeout


ECHOES_THE_PATH = """
import os, sys
for line in sys.stdin:
    src, dst = line.rstrip("\\n").split("\\t")[:2]
    sys.stdout.write("START %s\\n" % src); sys.stdout.flush()
    # the worker runs from a different directory than the caller, so it can only find the file if
    # the path it was given was resolved before it was sent
    sys.stdout.write("%s %s\\n" % ("OK" if os.path.isfile(src) else "NO", src)); sys.stdout.flush()
"""


def test_relative_paths_reach_the_worker_as_the_caller_meant_them(monkeypatch, tmp_path):
    """The worker's working directory is not the caller's, so a relative path is a different file.

    The reports pass paths relative to the run folder ("./Reports/CacheController/files/x.mp4"),
    and against the worker's own directory those resolve to nothing at all — every video silently
    got no thumbnail.
    """
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 16)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(poster_worker, "_spawn",
                        lambda: subprocess.Popen(_fake_worker(tmp_path, ECHOES_THE_PATH),
                                                 cwd=str(tmp_path.parent),   # not the caller's cwd
                                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE, text=True, bufsize=1))

    results, _ = poster_worker.run_jobs([("clip.mp4", "clip.jpg", False)], file_timeout=10,
                                        budget=30)
    assert results == {"clip.mp4": True}, "a relative path did not survive the trip to the worker"


def test_a_packaged_build_re_enters_itself_rather_than_looking_for_python(monkeypatch, tmp_path):
    """A build's sys.executable is a phantom python.exe — following it cost every thumbnail.

    Nuitka standalone sets ``sys.executable`` to the *build machine's* interpreter basename joined
    onto the installation directory, so an app built from a uv venv reports
    ``<install dir>\\python.exe`` — a file that is not there. Reading that as an interpreter both
    chose the "-m" branch in a build and spawned nothing, which is exactly what happened: every
    packaged run logged ``[WinError 2]`` and produced no posters at all.
    """
    install = tmp_path / "Snapchat_Auto"
    install.mkdir()
    binary = install / "Snapchat_Auto.exe"
    binary.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "executable", str(install / "python.exe"))   # never exists in a build
    monkeypatch.setattr(sys, "argv", [str(binary)])
    monkeypatch.setitem(poster_worker.__dict__, "__compiled__", object())

    command, cwd = poster_worker._worker_command()
    assert command == [str(binary), "--poster-worker"], "a build must re-enter its own binary"
    assert "-m" not in command
    assert os.path.isfile(command[0]), "the spawn was aimed at a file that does not exist"


def test_running_from_source_still_uses_the_interpreter(monkeypatch):
    """The other half: a real interpreter runs the module, from the directory it imports from."""
    monkeypatch.delitem(poster_worker.__dict__, "__compiled__", raising=False)
    monkeypatch.setattr(sys, "executable", sys.executable)
    command, cwd = poster_worker._worker_command()
    assert command == [sys.executable, "-m", "scripts.data.poster_worker"]
    assert cwd and os.path.isdir(cwd)


def test_a_video_that_was_never_attempted_is_not_called_undecodable(monkeypatch):
    """"It did not decode" is a finding about the evidence; a failed spawn establishes nothing."""
    def no_worker():
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(poster_worker, "_spawn", no_worker)
    results, _ = poster_worker.run_jobs([("a.mp4", "a.jpg", False), ("b.mp4", "b.jpg", False)])
    assert results == {}, "a file the worker never opened must not carry a verdict"


def test_a_video_that_hung_the_decoder_is_recorded_as_undecodable(monkeypatch, tmp_path):
    """The other side of the same distinction: a hang IS an answer about that file."""
    monkeypatch.setattr(poster_worker, "_spawn",
                        lambda: subprocess.Popen(_fake_worker(tmp_path, HANGS_ON_SECOND),
                                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE, text=True, bufsize=1))
    results, _ = poster_worker.run_jobs([("a", "a.jpg", False), ("b", "b.jpg", False)],
                                        file_timeout=0.4, budget=20)
    assert results.get("a") is True
    assert results.get("b") is False, "the file that hung the decoder must be reported as such"


def test_the_pass_leaves_no_worker_behind(monkeypatch, tmp_path):
    """Abandoned work is what made a run take 82 minutes and exit 120 — nothing may survive."""
    spawned = []
    real_spawn = poster_worker._spawn

    def spy():
        proc = subprocess.Popen(_fake_worker(tmp_path, HANGS_ON_SECOND),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(poster_worker, "_spawn", spy)
    poster_worker.run_jobs([("a", "a.jpg", False), ("b", "b.jpg", False)],
                           file_timeout=0.4, budget=10)
    assert spawned
    for proc in spawned:
        assert proc.poll() is not None, "a worker was left running"


# --------------------------------------------------------------- what the container actually holds

def test_an_ftyp_container_is_typed_by_its_brand():
    """An audio recording, a photo and a video share the .mp4 magic bytes; the brand separates them."""
    def ext(brand):
        return sniff.guess_media(b"\x00\x00\x00\x1c" + b"ftyp" + brand + b"\x00" * 32)

    assert ext(b"M4A ") == "m4a"                   # a voice note reported as a video, until now
    assert ext(b"M4B ") == "m4a"
    assert ext(b"qt  ") == "mov"
    assert ext(b"M4V ") == "m4v"
    assert ext(b"heic") == "heic"
    assert ext(b"avif") == "avif"
    assert ext(b"3gp4") == "3gp"


def test_generic_brands_are_still_mp4():
    """Only brands with an unambiguous meaning are mapped; the rest do mean .mp4."""
    for brand in (b"isom", b"mp42", b"iso5", b"avc1", b"dash", b"\x00\x00\x00\x00"):
        assert sniff.guess_media(b"\x00\x00\x00\x1c" + b"ftyp" + brand + b"\x00" * 32) == "mp4"


def test_reading_the_brand_does_not_change_what_counts_as_media():
    """guess_media is the Memories linker's acceptance test — it must accept exactly what it did."""
    for brand in (b"M4A ", b"heic", b"isom", b"qt  "):
        assert sniff.guess_media(b"\x00\x00\x00\x1c" + b"ftyp" + brand + b"\x00" * 32)
    assert sniff.guess_media(b"\xff\xd8\xff" + b"\x00" * 32) == "jpg"
    assert sniff.guess_media(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32) == "png"
    assert sniff.guess_media(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 32) == "webp"
    assert sniff.guess_media(b"not media at all") is None
    assert sniff.guess_media(b"short") is None


def test_an_audio_container_is_still_grouped_as_media():
    """kind drives the report's category; only the extension changes, as for mp3 and ogg."""
    head = b"\x00\x00\x00\x1c" + b"ftyp" + b"M4A " + b"\x00" * 32
    assert sniff.sniff_content(head) == ("media", "m4a")
    kind, ext, label, encrypted = sniff.classify(head, 1859)
    assert (kind, ext, encrypted) == ("media", "m4a", False)
    assert label == "m4a"
