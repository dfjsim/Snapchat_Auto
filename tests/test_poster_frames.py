"""The two guards around poster-frame extraction from cached video.

Both come from a real cached file, and both are the difference between a missing thumbnail and a
report that never finishes: an audio container that every magic-byte identifier calls an .mp4, and
a decoder read that does not return. A thumbnail is a convenience — it must never be able to cost
the examiner their report.

Every input is synthetic. No extraction data is required or used.
"""
import time

from scripts import memories_media_report as memories_report


def _iso_bmff(tmp_path, name, brand):
    """The first 12 bytes of an ISO base media file: size, 'ftyp', major brand."""
    path = tmp_path / name
    path.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + brand + b"\x00" * 64)
    return str(path)


def test_an_audio_only_container_is_not_offered_to_the_decoder(tmp_path):
    """M4A starts with the same ....ftyp bytes as an .mp4 — and holds no frame to extract."""
    assert not memories_report.has_video_track(_iso_bmff(tmp_path, "a.mp4", b"M4A "))
    assert not memories_report.has_video_track(_iso_bmff(tmp_path, "b.mp4", b"M4B "))


def test_a_video_brand_is_left_to_the_decoder(tmp_path):
    for brand in (b"isom", b"mp42", b"qt  "):
        assert memories_report.has_video_track(_iso_bmff(tmp_path, "v.mp4", brand))


def test_an_unreadable_file_is_left_to_the_decoder(tmp_path):
    """Only a positively identified audio brand is skipped; anything else is still tried."""
    missing = str(tmp_path / "gone.mp4")
    assert memories_report.has_video_track(missing)


def test_poster_within_gives_up_instead_of_blocking(monkeypatch, tmp_path):
    """A read that never returns must cost the time bound, not the run."""
    monkeypatch.setattr(memories_report, "generate_poster",
                        lambda *a, **k: time.sleep(30) or True)
    started = time.monotonic()
    got = memories_report.poster_within(_iso_bmff(tmp_path, "v.mp4", b"isom"),
                                        str(tmp_path / "out.jpg"), timeout=0.2)
    assert got is False
    assert time.monotonic() - started < 5


def test_poster_within_returns_the_frame_when_extraction_finishes(monkeypatch, tmp_path):
    monkeypatch.setattr(memories_report, "generate_poster", lambda *a, **k: True)
    assert memories_report.poster_within(_iso_bmff(tmp_path, "v.mp4", b"isom"),
                                         str(tmp_path / "out.jpg"), timeout=5) is True


def test_poster_within_skips_audio_without_starting_a_thread(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(memories_report, "generate_poster",
                        lambda *a, **k: calls.append(1) or True)
    assert memories_report.poster_within(_iso_bmff(tmp_path, "a.mp4", b"M4A "),
                                         str(tmp_path / "out.jpg")) is False
    assert not calls
