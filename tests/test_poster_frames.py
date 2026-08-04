"""Identifying what an "....ftyp" container holds, and the guards around poster extraction.

All of this comes from one real cached file: 1,859 bytes whose ftyp brand is `M4A `. It was
reported to the examiner as a video, and then handed to a video decoder, which blocked inside a
single read indefinitely. So: the brand is read, so an audio recording or a photo is not called a
video; and extraction is bounded, because a thumbnail is a convenience and no convenience may cost
the examiner their report.

Every input is synthetic. No extraction data is required or used.
"""
import time

from scripts import memories_media_report as memories_report
from scripts.data import sniff


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
