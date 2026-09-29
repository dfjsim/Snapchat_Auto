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


# --------------------------------------------------------------------------- more of ISO BMFF, and XMP
#
# The carriers the Snapchat app's tag was found in, and the metadata stores around them: a 3GPP asset
# box, QuickTime metadata directly in moov (not a FullBox), an ffmpeg-style iTunes list in udta/meta
# (a FullBox), an XMP packet in a uuid box after moov. Tag values are synthetic (test_snap_media_tag).

from tests.test_snap_media_tag import ANDROID_UA, _tag as _snap_tag  # noqa: E402


def _asset(kind, text, *, lang=0x55C4, utf16=False):
    body = b"\xfe\xff" + text.encode("utf-16-be") + b"\x00\x00" if utf16 else text.encode() + b"\x00"
    return _box(kind, b"\x00" * 4 + struct.pack(">H", lang) + body)


def _data(value, type_=1):
    body = value.encode() if isinstance(value, str) else value
    return _box(b"data", struct.pack(">I", type_) + b"\x00" * 4 + body)


def _hdlr(handler):
    return _box(b"hdlr", b"\x00" * 8 + handler + b"\x00" * 13)


def _keyed_meta(pairs, *, fullbox):
    """QuickTime metadata: ``keys`` names, ``ilst`` items indexed 1..n, typed ``data`` atoms."""
    keys = _box(b"keys", b"\x00" * 4 + struct.pack(">I", len(pairs))
                + b"".join(_box(b"mdta", name.encode()) for name, _v, _t in pairs))
    ilst = _box(b"ilst", b"".join(_box(struct.pack(">I", i), _data(value, type_))
                                  for i, (_n, value, type_) in enumerate(pairs, 1)))
    return _box(b"meta", (b"\x00" * 4 if fullbox else b"") + _hdlr(b"mdta") + keys + ilst)


def _mvhd_box(creation=T_UTC):
    stamp = creation - media_meta._ISOBMFF_1904
    return _box(b"mvhd", b"\x00" * 4 + struct.pack(">IIII", stamp, stamp + 5, 1000, 12345)
                + b"\x00" * 80)


def _trak(track_id, creation, modification=None):
    stamp = creation - media_meta._ISOBMFF_1904
    mod = (modification or creation) - media_meta._ISOBMFF_1904
    tkhd = _box(b"tkhd", b"\x00" * 4 + struct.pack(">IIII", stamp, mod, track_id, 0) + b"\x00" * 64)
    mdhd = _box(b"mdhd", b"\x00" * 4 + struct.pack(">IIII", stamp, mod, 1000, 100) + b"\x00" * 4)
    return _box(b"trak", tkhd + _box(b"mdia", mdhd))


def _video(path, *children, brand=b"mp42", before=b"", after=b""):
    data = (_box(b"ftyp", brand + b"\x00\x00\x00\x00" + brand) + before
            + _box(b"moov", _mvhd_box() + b"".join(children)) + after)
    path.write_bytes(data)
    return str(path)


def test_a_3gpp_description_box_holding_the_snapchat_tag_is_decoded(tmp_path):
    tag = _snap_tag()
    path = _video(tmp_path / "a.mp4", _box(b"udta", _asset(b"dscp", tag)))
    meta = media_meta.extract(path)
    found = meta["snapchat"][0]

    assert found["device"] == "iPhone15,2" and found["lens_ids"] == [12345678901]
    assert found["field"] == "moov › udta › dscp"
    assert found["offset"] == (tmp_path / "a.mp4").read_bytes().find(tag.encode())
    assert meta["other"][0] == ("dscp", tag)                  # the field itself, as stored, first
    assert not any(name == "dscp" for name, _v in meta["key"])
    assert meta["notable"] and "QuickTime user data" in meta["sources"]


def test_quicktime_metadata_directly_in_moov_is_read(tmp_path):
    """iOS writes a MOV's metadata to moov › meta, which is not a FullBox: hdlr comes first."""
    meta = media_meta.extract(_video(tmp_path / "v.mov", _keyed_meta([
        ("com.apple.quicktime.description", _snap_tag(), 1),
        ("com.apple.quicktime.make", "Apple", 1),
        ("com.apple.quicktime.creationdate", "2024-05-01T12:00:00+0200", 1),
        ("com.apple.quicktime.location.ISO6709", "+45.5000-073.5000/", 1),
        ("com.example.count", b"\x00\x00\x00\x07", 22),
        ("com.example.score", struct.pack(">f", 1.5), 23),
        ("com.example.cover", b"\xff\xd8\xff\xe0", 13)], fullbox=False), brand=b"qt  "))
    created = {t["label"]: t for t in meta["times"]}["QuickTime com.apple.quicktime.creationdate"]

    assert created["epoch"] == T_UTC and meta["gps"] == {"lat": 45.5, "lon": -73.5}
    assert ("com.apple.quicktime.make", "Apple") in meta["key"]
    assert ("com.example.count", "7") in meta["other"] and ("com.example.score", "1.5") in meta["other"]
    assert ("com.example.cover", "<4 bytes>") in meta["other"]
    assert meta["snapchat"][0]["field"] == "moov › meta › ilst › com.apple.quicktime.description"
    assert "QuickTime metadata (moov › meta)" in meta["sources"]
    assert "QuickTime user data" not in meta["sources"]


def test_an_ffmpeg_description_item_beside_the_encoder_name(tmp_path):
    tag = _snap_tag(ANDROID_UA, lens=(987654321,), packed=False)
    ilst = _box(b"ilst", _box(b"\xa9too", _data("Lavf60.16.100")) + _box(b"desc", _data(tag)))
    udta = _box(b"udta", _box(b"meta", b"\x00" * 4 + _hdlr(b"mdir") + ilst))
    meta = media_meta.extract(_video(tmp_path / "a.mp4", udta, brand=b"isom"))
    found = meta["snapchat"][0]

    assert ("©too", "Lavf60.16.100") in meta["key"]
    assert found["os"] == "Android 9#A1B2#28" and found["lens_encoding"] == "unpacked"
    assert found["field"] == "moov › udta › meta › ilst › desc"
    assert meta["other"][0] == ("desc", tag)


def test_a_description_that_is_not_a_tag_stays_as_stored(tmp_path):
    meta = media_meta.extract(_video(tmp_path / "a.mp4",
                                     _box(b"udta", _asset(b"dscp", "Holiday at the lake"))))
    assert ("dscp", "Holiday at the lake") in meta["key"] and "snapchat" not in meta


def test_3gpp_text_in_utf16_and_a_quicktime_text_atom_under_a_3gpp_name(tmp_path):
    meta = media_meta.extract(_video(tmp_path / "a.mp4", _box(b"udta", _asset(b"titl", "Grüße",
                                                                              utf16=True))))
    assert ("titl", "Grüße") in meta["key"]
    meta = media_meta.extract(_video(tmp_path / "b.mp4", _box(b"udta", _qt(b"titl", "Hello"))))
    assert ("titl", "Hello") in meta["key"]                   # not read as a FullBox: nothing lost


def test_an_ilst_index_with_no_key_name_is_named_by_number(tmp_path):
    ilst = _box(b"ilst", _box(struct.pack(">I", 5), _data("five")))
    meta = media_meta.extract(_video(tmp_path / "a.mp4", _box(b"meta", _hdlr(b"mdta") + ilst)))
    assert ("key #5", "five") in meta["other"]


def test_track_headers_that_agree_are_noted_and_those_that_differ_are_listed(tmp_path):
    same = media_meta.extract(_video(tmp_path / "a.mp4", _trak(1, T_UTC, T_UTC + 5)))
    created = {t["label"]: t for t in same["times"]}["mvhd creation_time"]
    assert "every track header (tkhd, mdhd) states the same time" in created["note"]
    assert not any(t["label"].startswith("track header") for t in same["times"])

    differ = media_meta.extract(_video(tmp_path / "b.mp4", _trak(2, T_UTC + 60, T_UTC + 5)))
    extra = {t["label"]: t for t in differ["times"]}
    assert extra["track header creation_time (tkhd track 2, mdhd track 2)"]["epoch"] == T_UTC + 60
    assert "tkhd/mdhd" in differ["sources"]


XMP_NS = ('xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
          'xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
          'xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/" '
          'xmlns:stEvt="http://ns.adobe.com/xap/1.0/sType/ResourceEvent#" '
          'xmlns:stRef="http://ns.adobe.com/xap/1.0/sType/ResourceRef#" '
          'xmlns:exif="http://ns.adobe.com/exif/1.0/" '
          'xmlns:xmpDM="http://ns.adobe.com/xmp/1.0/DynamicMedia/"')


def _edit_xmp(description):
    """An exported edit: its own dates, tool, description and history, then the files that went
    into it — ingredients (one used twice, one with no pantry item) and pantry items, one with its
    own GPS fix and a nested pantry, one whose creation date is the zero QuickTime time."""
    return f"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta {XMP_NS}><rdf:RDF><rdf:Description rdf:about=""
   xmp:CreateDate="2024-05-01T12:00:00+02:00" xmp:CreatorTool="Editor 1.0"
   xmpMM:InstanceID="xmp.iid:own">
  <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{description}</rdf:li></rdf:Alt></dc:description>
  <xmpMM:History><rdf:Seq>
   <rdf:li stEvt:action="created" stEvt:when="2024-05-01T12:00:00+02:00" stEvt:softwareAgent="Editor 1.0"/>
   <rdf:li stEvt:action="saved" stEvt:when="2024-05-01T12:00:05+02:00" stEvt:softwareAgent="Editor 1.0"/>
  </rdf:Seq></xmpMM:History>
  <xmpMM:Ingredients><rdf:Bag>
   <rdf:li stRef:instanceID="xmp.iid:clip" stRef:filePath="clip.mov" stRef:fromPart="time:0"/>
   <rdf:li stRef:instanceID="xmp.iid:clip" stRef:filePath="clip.mov" stRef:fromPart="time:9"/>
   <rdf:li stRef:instanceID="xmp.iid:song" stRef:filePath="song.mp3"/>
   <rdf:li stRef:instanceID="xmp.iid:unlisted" stRef:filePath="logo.png"/>
  </rdf:Bag></xmpMM:Ingredients>
  <xmpMM:Pantry><rdf:Bag>
   <rdf:li><rdf:Description xmp:CreateDate="2020-01-02T03:04:05Z" xmpMM:InstanceID="xmp.iid:clip"
      exif:GPSLatitude="45,30.0N" exif:GPSLongitude="73,30.0W" xmp:CreatorTool="Phone camera">
     <xmpDM:duration xmpDM:value="3000" xmpDM:scale="1/1000"/>
     <xmpMM:Pantry><rdf:Bag><rdf:li><rdf:Description xmp:CreateDate="2019-06-07T08:09:10Z"
        xmpMM:InstanceID="xmp.iid:nested"/></rdf:li></rdf:Bag></xmpMM:Pantry>
   </rdf:Description></rdf:li>
   <rdf:li><rdf:Description xmp:CreateDate="1904-01-01T00:00:00Z" xmpMM:InstanceID="xmp.iid:song"/></rdf:li>
  </rdf:Bag></xmpMM:Pantry>
</rdf:Description></rdf:RDF></x:xmpmeta>
<?xpacket end="w"?>""".encode()


def _xmp_uuid(xmp):
    return _box(b"uuid", media_meta._XMP_UUID + xmp)


def test_xmp_after_moov_is_read_and_the_source_files_are_kept_apart(tmp_path):
    tag = _snap_tag()
    path = _video(tmp_path / "e.mp4", after=_xmp_uuid(_edit_xmp(tag)))
    meta = media_meta.extract(path)
    labels = {t["label"]: t for t in meta["times"]}

    # the file's own
    assert labels["XMP xmp:CreateDate"]["epoch"] == T_UTC
    assert "XMP history · saved (Editor 1.0)" in labels
    assert ("XMP xmp:CreatorTool", "Editor 1.0") in meta["key"]
    assert meta["snapchat"][0]["field"] == "uuid XMP › dc:description"
    assert meta["snapchat"][0]["offset"] == (tmp_path / "e.mp4").read_bytes().find(tag.encode())
    assert "XMP" in meta["sources"]
    # the files that went into it: never this file's times or fix
    walls = {t["wall"] for t in meta["times"]}
    assert not walls & {"2020-01-02 03:04:05", "2019-06-07 08:09:10", "1904-01-01 00:00:00"}
    assert meta["gps"] == {}
    sources = {s["instance_id"]: s for s in meta["xmp_sources"]}
    assert set(sources) == {"xmp.iid:clip", "xmp.iid:nested", "xmp.iid:song", "xmp.iid:unlisted"}
    clip = sources["xmp.iid:clip"]
    assert clip["file_path"] == "clip.mov" and clip["uses"] == 2
    assert clip["gps"] == {"lat": 45.5, "lon": -73.5} and clip["duration"] == "3.00 s"
    assert clip["creator_tool"] == "Phone camera"
    assert clip["times"][0]["wall"] == "2020-01-02 03:04:05"
    assert sources["xmp.iid:song"]["not_set"] == ["xmp:CreateDate"]
    assert sources["xmp.iid:unlisted"]["file_path"] == "logo.png"


def test_xmp_in_a_jpeg_description_can_hold_the_tag(tmp_path):
    Image.new("RGB", (4, 4)).save(tmp_path / "x.jpg", format="JPEG", xmp=_edit_xmp(_snap_tag()))
    meta = media_meta.extract(str(tmp_path / "x.jpg"))

    assert meta["snapchat"][0]["field"] == "XMP › dc:description"
    assert meta["other"][0][0] == "XMP dc:description"
    assert len(meta["xmp_sources"]) == 4


def test_an_exif_image_description_can_hold_the_tag(tmp_path):
    ex = Image.Exif()
    ex[ExifTags.Base.ImageDescription] = _snap_tag()
    Image.new("RGB", (4, 4)).save(tmp_path / "d.jpg", format="JPEG", exif=ex.tobytes())
    meta = media_meta.extract(str(tmp_path / "d.jpg"))

    assert meta["snapchat"][0]["field"] == "EXIF ImageDescription"
    assert meta["other"][0][0] == "ImageDescription"
    assert not any(name == "ImageDescription" for name, _v in meta["key"])


def test_xmp_that_is_not_well_formed_falls_back_to_its_own_dates_only(tmp_path):
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description '
           b'xmp:ModifyDate="2024-05-01T12:00:00+02:00"><xmpMM:Pantry><rdf:li '
           b'xmp:CreateDate="2020-01-02T03:04:05Z"/></xmpMM:Pantry></rdf:Description></rdf:RDF>'
           b'</x:xmpmeta>')                                   # prefixes never declared
    Image.new("RGB", (4, 4)).save(tmp_path / "x.jpg", format="JPEG", xmp=xmp)
    labels = {t["label"] for t in media_meta.extract(str(tmp_path / "x.jpg"))["times"]}
    assert labels == {"XMP xmp:ModifyDate"}


def test_xmp_with_a_document_type_is_refused_without_raising(tmp_path):
    xmp = (b'<!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;">]>'
           b'<x:xmpmeta ' + XMP_NS.encode() + b'><rdf:RDF><rdf:Description dc:title="&b;"/>'
           b'</rdf:RDF></x:xmpmeta>')
    meta = media_meta.extract(_video(tmp_path / "a.mp4", after=_xmp_uuid(xmp)))
    assert isinstance(meta, dict) and "xmp_sources" not in meta
    assert not any("aaaa" in str(v) for _k, v in meta["key"] + meta["other"])


class _CountingReads:
    """A file object that records the size of every read, to prove the media data is skipped."""
    def __init__(self, data):
        import io
        self._fh, self.reads = io.BytesIO(data), []

    def read(self, n=-1):
        out = self._fh.read(n)
        self.reads.append(len(out))
        return out

    def seek(self, *args):
        return self._fh.seek(*args)


def test_the_media_data_is_skipped_not_read(tmp_path):
    path = _video(tmp_path / "big.mp4", _box(b"udta", _asset(b"dscp", _snap_tag())),
                  before=_box(b"mdat", b"\x00" * (8 * 1024 * 1024)))
    fh = _CountingReads((tmp_path / "big.mp4").read_bytes())
    meta = media_meta._extract(fh, path)

    assert meta["snapchat"] and max(fh.reads) < 64 * 1024


def test_user_data_after_a_large_track_is_still_read(tmp_path):
    big_trak = _box(b"trak", _box(b"free", b"\x00" * (5 * 1024 * 1024)))
    meta = media_meta.extract(_video(tmp_path / "a.mp4", big_trak,
                                     _box(b"udta", _asset(b"dscp", "after the track"))))
    assert ("dscp", "after the track") in meta["key"]


@pytest.mark.parametrize("children,after", [
    (_box(b"udta", _box(b"dscp", b"\x00\x00")), b""),
    (_box(b"meta", b"\x00" * 3), b""),
    (b"", _box(b"uuid", media_meta._XMP_UUID[:8])),
    (b"", _box(b"uuid", media_meta._XMP_UUID + b"<x:xmpmeta")),
    (_box(b"trak", _box(b"tkhd", b"\x01")), b""),
])
def test_truncated_metadata_boxes_never_raise(tmp_path, children, after):
    meta = media_meta.extract(_video(tmp_path / "t.mp4", children, after=after))
    assert isinstance(meta, dict) and "could not be read" not in meta.get("note", "")


def test_a_file_of_tiny_boxes_is_walked_only_so_far(tmp_path):
    path = tmp_path / "tiny.mp4"
    path.write_bytes(_box(b"ftyp", b"mp42\x00\x00\x00\x00mp42") + _box(b"free", b"") * 20000
                     + _box(b"moov", _mvhd_box()))
    meta = media_meta.extract(str(path))
    assert "no moov" in meta["note"]                           # the cap stopped the walk first


def test_xmp_that_only_restates_the_orientation_is_not_notable(tmp_path):
    xmp = (f'<x:xmpmeta {XMP_NS} xmlns:tiff="http://ns.adobe.com/tiff/1.0/"><rdf:RDF>'
           f'<rdf:Description tiff:Orientation="1"/></rdf:RDF></x:xmpmeta>').encode()
    Image.new("RGB", (4, 4)).save(tmp_path / "o.jpg", format="JPEG", xmp=xmp)
    meta = media_meta.extract(str(tmp_path / "o.jpg"))

    assert ("XMP tiff:Orientation", "1") in meta["other"]
    assert meta["present"] is True and meta["notable"] is False
