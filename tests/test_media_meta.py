"""What a media file says about itself — `scripts.data.media_meta`.

The reader is fed synthetic files built here: a JPEG whose EXIF is written with Pillow, a PNG with
text chunks, and an ISO base media file assembled box by box. Every timestamp it returns must say
what clock it is on, and only a value whose zone the file states may become an instant — a naive
EXIF wall clock is handed back as the string it is. No extraction data is used.
"""
import struct

import pytest
from PIL import Image, ExifTags, PngImagePlugin

from scripts.data import media_meta

T_UTC = 1714557600                                # 2024-05-01 10:00:00 UTC


def _jpeg(path, *, offset=True, gps=True):
    im = Image.new("RGB", (8, 6), "red")
    ex = Image.Exif()
    ex[ExifTags.Base.Make] = "ACME"
    ex[ExifTags.Base.Model] = "Cam 1"
    ex[ExifTags.Base.Software] = "fw 1.0"
    ex[ExifTags.Base.DateTime] = "2024:05:01 12:00:05"
    ifd = ex.get_ifd(ExifTags.IFD.Exif)
    ifd[ExifTags.Base.DateTimeOriginal] = "2024:05:01 12:00:00"
    if offset:
        ifd[ExifTags.Base.OffsetTimeOriginal] = "+02:00"
    ifd[ExifTags.Base.SubsecTimeOriginal] = "123"
    ifd[ExifTags.Base.LensModel] = "wide"
    ifd[ExifTags.Base.ExposureTime] = 0.01
    if gps:
        g = ex.get_ifd(ExifTags.IFD.GPSInfo)
        g[ExifTags.GPS.GPSLatitudeRef] = "N"
        g[ExifTags.GPS.GPSLatitude] = (45.0, 30.0, 0.0)
        g[ExifTags.GPS.GPSLongitudeRef] = "W"
        g[ExifTags.GPS.GPSLongitude] = (73.0, 30.0, 0.0)
        g[ExifTags.GPS.GPSDateStamp] = "2024:05:01"
        g[ExifTags.GPS.GPSTimeStamp] = (10.0, 0.0, 0.0)
    im.save(path, format="JPEG", exif=ex.tobytes())
    return str(path)


def _box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _qt(kind, text):
    body = text.encode()
    return _box(kind, struct.pack(">HH", len(body), 0x15C7) + body)


def _mp4(path, *, creation=T_UTC, with_udta=True, moov=True):
    parts = [_box(b"ftyp", b"mp42\x00\x00\x00\x00mp42isom"), _box(b"mdat", b"\x00" * 64)]
    if moov:
        stamp = creation - media_meta._ISOBMFF_1904
        mvhd = _box(b"mvhd", b"\x00" * 4 + struct.pack(">IIII", stamp, stamp + 5, 1000, 12345)
                    + b"\x00" * 80)
        udta = b""
        if with_udta:
            keys = _box(b"keys", b"\x00" * 4 + struct.pack(">I", 1)
                        + _box(b"mdta", b"com.apple.quicktime.creationdate"))
            data = _box(b"data", b"\x00\x00\x00\x01" + b"\x00" * 4 + b"2024-05-01T12:00:00+0200")
            ilst = _box(b"ilst", _box(struct.pack(">I", 1), data))
            udta = _box(b"udta", _qt(b"\xa9xyz", "+45.5000-073.5000/") + _qt(b"\xa9mak", "Apple")
                        + _box(b"meta", b"\x00" * 4 + keys + ilst))
        parts.append(_box(b"moov", mvhd + udta))
    path.write_bytes(b"".join(parts))
    return str(path)


# --------------------------------------------------------------------------- images

def test_an_exif_time_with_an_offset_becomes_an_instant_and_keeps_its_wall_clock(tmp_path):
    meta = media_meta.extract(_jpeg(tmp_path / "a.jpg"))
    by_label = {t["label"]: t for t in meta["times"]}
    original = by_label["EXIF DateTimeOriginal"]

    assert original["wall"] == "2024-05-01 12:00:00"
    assert original["zone"] == "+02:00"
    assert original["epoch"] == T_UTC                    # 12:00 at +02:00 is 10:00 UTC
    assert "OffsetTimeOriginal" in original["note"] and ".123" in original["note"]


def test_an_exif_time_without_an_offset_is_never_turned_into_an_instant(tmp_path):
    meta = media_meta.extract(_jpeg(tmp_path / "a.jpg", offset=False))
    original = {t["label"]: t for t in meta["times"]}["EXIF DateTimeOriginal"]

    assert original["wall"] == "2024-05-01 12:00:00"
    assert original["zone"] is None and original["epoch"] is None
    assert "no timezone" in original["note"]


def test_the_gps_stamp_is_utc_and_the_fix_is_decoded(tmp_path):
    meta = media_meta.extract(_jpeg(tmp_path / "a.jpg"))
    gps_time = {t["label"]: t for t in meta["times"]}["EXIF GPSDateStamp + GPSTimeStamp"]

    assert gps_time["zone"] == "UTC" and gps_time["epoch"] == T_UTC
    assert meta["gps"] == {"lat": 45.5, "lon": -73.5}


def test_key_fields_come_first_and_the_rest_is_kept_apart(tmp_path):
    meta = media_meta.extract(_jpeg(tmp_path / "a.jpg"))

    assert meta["key"][:3] == [("Make", "ACME"), ("Model", "Cam 1"), ("Software", "fw 1.0")]
    assert ("LensModel", "wide") in meta["key"]
    assert any(name == "ExposureTime" for name, _v in meta["other"])
    assert not any(name.startswith("DateTime") for name, _v in meta["other"])
    assert meta["present"] and meta["sources"] == ["EXIF"] and meta["container"] == "JPEG"
    assert meta["pixels"] == "8×6"


def test_a_jpeg_with_no_exif_says_nothing_is_present(tmp_path):
    Image.new("RGB", (4, 4)).save(tmp_path / "plain.jpg", format="JPEG")
    meta = media_meta.extract(str(tmp_path / "plain.jpg"))

    assert meta["present"] is False and meta["times"] == [] and meta["key"] == []


def test_png_text_chunks_are_read_and_a_dated_one_is_a_time(tmp_path):
    info = PngImagePlugin.PngInfo()
    info.add_text("Creation Time", "Wed, 01 May 2024 12:00:00 +0200")
    info.add_text("Comment", "hello")
    Image.new("RGB", (4, 4)).save(tmp_path / "t.png", pnginfo=info)
    meta = media_meta.extract(str(tmp_path / "t.png"))

    assert meta["times"][0]["label"] == "PNG Creation Time"
    assert meta["times"][0]["epoch"] == T_UTC
    assert ("Comment", "hello") in meta["other"]


def test_xmp_dates_are_read_from_a_jpeg(tmp_path):
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description '
           b'xmp:CreateDate="2024-05-01T12:00:00+02:00"/></rdf:RDF></x:xmpmeta>')
    Image.new("RGB", (4, 4)).save(tmp_path / "x.jpg", format="JPEG", xmp=xmp)
    meta = media_meta.extract(str(tmp_path / "x.jpg"))
    create = {t["label"]: t for t in meta["times"]}["XMP xmp:CreateDate"]

    assert create["epoch"] == T_UTC and create["zone"] == "+02:00"
    assert "XMP" in meta["sources"]


# --------------------------------------------------------------------------- ISO BMFF

def test_mvhd_times_are_utc_since_1904(tmp_path):
    meta = media_meta.extract(_mp4(tmp_path / "v.mp4", with_udta=False))
    by_label = {t["label"]: t for t in meta["times"]}

    assert by_label["mvhd creation_time"]["epoch"] == T_UTC
    assert by_label["mvhd creation_time"]["wall"] == "2024-05-01 10:00:00"
    assert by_label["mvhd creation_time"]["zone"] == "UTC"
    assert by_label["mvhd modification_time"]["epoch"] == T_UTC + 5
    assert ("Duration (mvhd)", "12.3 s") in meta["key"]
    assert meta["container"] == "mp42"


def test_quicktime_user_data_gives_a_zoned_creation_date_a_fix_and_the_make(tmp_path):
    meta = media_meta.extract(_mp4(tmp_path / "v.mp4"))
    by_label = {t["label"]: t for t in meta["times"]}
    created = by_label["QuickTime com.apple.quicktime.creationdate"]

    assert created["wall"] == "2024-05-01 12:00:00" and created["zone"] == "+02:00"
    assert created["epoch"] == T_UTC
    assert meta["gps"] == {"lat": 45.5, "lon": -73.5}
    assert ("©mak", "Apple") in meta["key"]
    assert set(meta["sources"]) == {"mvhd", "QuickTime user data"}


def test_a_zero_mvhd_time_means_not_set_and_is_not_reported(tmp_path):
    meta = media_meta.extract(_mp4(tmp_path / "v.mp4", creation=media_meta._ISOBMFF_1904,
                                   with_udta=False))
    assert not any(t["label"] == "mvhd creation_time" for t in meta["times"])


def test_a_video_cached_without_its_header_says_so(tmp_path):
    meta = media_meta.extract(_mp4(tmp_path / "v.mp4", moov=False))

    assert meta["present"] is False and "no moov" in meta["note"]


def test_heif_is_named_as_unread_rather_than_as_empty(tmp_path):
    (tmp_path / "h.heic").write_bytes(_box(b"ftyp", b"heic\x00\x00\x00\x00mif1heic") + b"\x00" * 32)
    meta = media_meta.extract(str(tmp_path / "h.heic"))

    assert meta["present"] is False and "HEIF" in meta["note"]


# --------------------------------------------------------------------------- never raises

@pytest.mark.parametrize("payload", [b"", b"\xff\xd8\xff" + b"\x00" * 10, b"\x89PNG\r\n\x1a\n" + b"junk",
                                     b"\x00\x00\x00\x08ftypmp42" + b"\x00\x00\x00\x00moov"])
def test_truncated_or_hostile_bytes_never_raise(tmp_path, payload):
    (tmp_path / "f").write_bytes(payload)
    result = media_meta.extract(str(tmp_path / "f"))
    assert result is None or isinstance(result, dict)


def test_a_format_not_read_here_returns_none(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"WEBVTT\n\n")
    assert media_meta.extract(str(tmp_path / "f.txt")) is None


def test_pixel_size_and_orientation_alone_are_present_but_not_notable(tmp_path):
    """Every encoder writes these; on a real device most cached JPEGs carry nothing else, so a flag
    raised by them would sit on three quarters of the rows and say nothing."""
    ex = Image.Exif()
    ex[ExifTags.Base.Orientation] = 1
    ifd = ex.get_ifd(ExifTags.IFD.Exif)
    ifd[ExifTags.Base.ExifImageWidth] = 8
    ifd[ExifTags.Base.ExifImageHeight] = 6
    Image.new("RGB", (8, 6)).save(tmp_path / "s.jpg", format="JPEG", exif=ex.tobytes())
    meta = media_meta.extract(str(tmp_path / "s.jpg"))

    assert meta["present"] is True and meta["notable"] is False


def test_a_camera_make_or_a_time_makes_a_file_notable(tmp_path):
    assert media_meta.extract(_jpeg(tmp_path / "a.jpg"))["notable"] is True
    assert media_meta.extract(_mp4(tmp_path / "v.mp4", with_udta=False))["notable"] is True
