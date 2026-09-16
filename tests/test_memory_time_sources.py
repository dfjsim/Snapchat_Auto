"""Every timestamp in the Memories report says where it was read from; the search box matches the
AES key / IV, the CDN URLs and what the media files say about themselves.

Three sources of time meet in one Memory — the app's database, the media file's own header and the
device's filesystem as the extraction archive recorded it — and a value without its origin is a
number the reader cannot weigh. These tests pin that each one carries its source, that a file time
whose zone the file did not state is shown as written rather than converted, and that a poster frame
this tool generated is never read as evidence.

Everything here is synthetic: a plaintext SCContent JPEG whose EXIF is written with Pillow, and the
manifest `extract_zip` would have left. No extraction data is used.
"""
import hashlib
import json
import os
import re

from PIL import Image, ExifTags

from scripts import memories_media_report as mr

USER = "11111111-2222-3333-4444-555555555555"
TOKEN = "EXAMPLETOKEN0123456AB"
URL = f"https://cf-st.sc-cdn.net/d/{TOKEN}?bo=EXAMPLEBO&uc=00"
CACHE_KEY = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
KEY, IV = b"\x11" * 32, b"\x22" * 16
DEVICE_MTIME = 1714557700                         # 2024-05-01 10:01:40 UTC


def _exif_jpeg(path, *, offset=True):
    im = Image.new("RGB", (40, 30), "red")
    ex = Image.Exif()
    ex[ExifTags.Base.Make] = "ACME"
    ex[ExifTags.Base.Model] = "Cam 1"
    ifd = ex.get_ifd(ExifTags.IFD.Exif)
    ifd[ExifTags.Base.DateTimeOriginal] = "2024:05:01 12:00:00"
    if offset:
        ifd[ExifTags.Base.OffsetTimeOriginal] = "+02:00"
    ifd[ExifTags.Base.DateTimeDigitized] = "2024:05:01 12:00:01"
    im.save(path, format="JPEG", exif=ex.tobytes())


def _extraction(tmp_path, *, offset=True, record_mtime=True):
    """An extraction root holding one plaintext cached JPEG and the manifest for it."""
    root = tmp_path / "ExtractedData"
    app = root / "Application" / "APPUUID"
    scdir = app / "Documents" / f"com.snap.file_manager_3_SCContent_{USER}"
    scdir.mkdir(parents=True)
    _exif_jpeg(scdir / CACHE_KEY, offset=offset)
    rel = f"Application/APPUUID/Documents/com.snap.file_manager_3_SCContent_{USER}/{CACHE_KEY}"
    manifest = {"container_prefixes": {}, "renamed": {},
                "mtimes": {rel: DEVICE_MTIME} if record_mtime else {}}
    (root / "extraction_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(root), str(app)


def _memory(snap_id="SNAP-1", **over):
    m = {"snap_id": snap_id, "user_hash": "aa" * 32, "key": KEY, "iv": IV, "is_meo": False,
         "key_wrapped": False, "key_source": None, "media_url": URL, "overlay_url": None,
         "thumb_url": None, "width": 40, "height": 30, "media_files": [], "media_type": 0,
         "format": "", "duration": None, "camera": "Back", "has_location": False,
         "create_utc": "2024-05-01 10:00:00 UTC", "created_sort": 100,
         "times": {"ZCREATETIMEUTC": "2024-05-01 10:00:00 UTC",
                   "ZCAPTURETIMEUTC": "2024-05-01 09:59:50 UTC"},
         "entry_times": {"ZCREATETIMEUTC": "2024-05-01 10:00:02 UTC"},
         "snap_other": {}, "entry_other": {}, "urls": {"ZMEDIADOWNLOADURL": URL},
         "ids": {"ZMEDIAID": "MEDIA-1"}, "latitude": None, "longitude": None, "address": None,
         "wal": None, "prior_rows": []}
    m.update(over)
    return m


def _recover(tmp_path, **kw):
    root, app = _extraction(tmp_path, **kw)
    m = _memory()
    mems = {m["snap_id"]: m}
    out = str(tmp_path / "Reports" / "Memories")
    mr.collect_media(mems, app, os.path.join(out, "media"))
    for f in m["media_files"]:
        f["path"] = "media/" + f["out"]
    mr.annotate_file_times(mems, "utc", mr.load_device_mtimes(root), src_root=root)
    return mems, out, root


# --------------------------------------------------------------------------- the sources

def test_every_timestamp_names_where_it_was_read_from(tmp_path):
    mems, _out, _root = _recover(tmp_path)
    times = mr._memory_times(mems["SNAP-1"])
    by_label = {label: (value, source) for label, value, source in times}

    assert by_label["Created"] == ("2024-05-01 10:00:00 UTC", "scdb-27 › ZGALLERYSNAP.ZCREATETIMEUTC")
    assert by_label["Captured"][1] == "scdb-27 › ZGALLERYSNAP.ZCAPTURETIMEUTC"
    assert by_label["Entry created"][1] == "scdb-27 › ZGALLERYENTRY.ZCREATETIMEUTC"
    assert by_label["EXIF DateTimeOriginal"][1].startswith("inside SNAP-1_full_")
    assert by_label["Cache file modified on the device"][1].startswith("extraction archive › ")
    assert all(source for _label, _value, source in times)


def test_a_zoned_file_time_is_converted_and_a_naive_one_is_shown_as_written(tmp_path):
    mems, _out, _root = _recover(tmp_path)
    by_label = {label: (value, source) for label, value, source in mr._memory_times(mems["SNAP-1"])}

    # 12:00 at +02:00 is 10:00 UTC — the same instant as the database's Created, side by side
    assert by_label["EXIF DateTimeOriginal"][0] == "2024-05-01 10:00:00 UTC"
    # no OffsetTimeDigitized in the file: the wall clock stays as written, and the source says so
    value, source = by_label["EXIF DateTimeDigitized"]
    assert value == "2024-05-01 12:00:01"
    assert "no timezone in the file" in source


def test_the_device_mtime_comes_from_the_manifest_in_the_run_timezone(tmp_path):
    mems, _out, _root = _recover(tmp_path)
    f = mems["SNAP-1"]["media_files"][0]

    assert len(f["src_mtimes"]) == 1
    path, shown = f["src_mtimes"][0]
    assert shown == "2024-05-01 10:01:40 UTC"
    assert path.startswith("/Application/APPUUID/")


def test_an_unrecorded_mtime_is_absent_rather_than_the_copy_s_own(tmp_path):
    mems, _out, _root = _recover(tmp_path, record_mtime=False)
    f = mems["SNAP-1"]["media_files"][0]

    assert f["src_mtimes"] == [(f["src_mtimes"][0][0], "")]
    assert not any(label.startswith("Cache file modified") for label, _v, _s in
                   mr._memory_times(mems["SNAP-1"]))


def test_a_generated_poster_is_never_read_as_evidence():
    m = _memory(times={}, create_utc="", entry_times={})
    m["media_files"] = [{"out": "SNAP-1_poster.jpg", "generated": True, "role": "poster",
                         "meta": {"present": True, "key": [("Software", "ours")],
                                  "times": [{"label": "x", "wall": "2024-01-01 00:00:00",
                                             "zone": None, "epoch": None}]},
                         "file_times": [{"label": "x", "shown": "2024-01-01 00:00:00",
                                         "wall": "2024-01-01 00:00:00", "naive": True, "note": ""}],
                         "src_mtimes": []}]

    assert mr._memory_times(m) == []
    assert mr._has_embedded(m["media_files"]) is False


def test_parts_of_one_file_are_bounded_not_listed():
    m = _memory(times={}, create_utc="", entry_times={})
    m["media_files"] = [{"out": "a.mp4", "role": "full", "file_times": [],
                         "src_mtimes": [("/p/a_0-100", "2024-05-01 10:00:00 UTC"),
                                        ("/p/a_100-200", "2024-05-01 10:00:09 UTC"),
                                        ("/p/a_200-300", "2024-05-01 10:00:05 UTC")]}]
    labels = [(label, value) for label, value, _s in mr._memory_times(m)]

    assert labels == [("Cache parts modified on the device — earliest of 3", "2024-05-01 10:00:00 UTC"),
                      ("Cache parts modified on the device — latest of 3", "2024-05-01 10:00:09 UTC")]


# --------------------------------------------------------------------------- the index

def _index_js(out):
    return open(os.path.join(out, "data", "index.js"), encoding="utf-8").read()


def test_the_search_string_carries_key_iv_urls_and_the_camera(tmp_path):
    mems, out, _root = _recover(tmp_path)
    mr.generate_report(mems, out, True, tz_label="UTC", run_id="R")
    index = _index_js(out).lower()

    assert KEY.hex() in index and IV.hex() in index
    assert URL.lower() in index
    assert "acme" in index and "cam 1" in index
    assert "exif xmp embedded metadata" in index


def test_the_file_times_are_keys_for_the_time_filter(tmp_path):
    mems, out, _root = _recover(tmp_path)
    mr.generate_report(mems, out, True, tz_label="UTC", run_id="R")
    rows = json.loads(re.search(r"SCV\.setRows\((\[.*\])\);?\s*$", _index_js(out), re.S).group(1))
    ts = rows[0][5]["ts"]

    # the database's 10:00:00 UTC and the EXIF 12:00:00+02:00 are one key; the mtime and the naive
    # 12:00:01 (compared as written) are their own
    assert mr.report_ui.ts_key("2024-05-01 10:00:00") in ts
    assert mr.report_ui.ts_key("2024-05-01 10:01:40") in ts
    assert mr.report_ui.ts_key("2024-05-01 12:00:01") in ts
    assert rows[0][5]["meta"] == "y"


def test_the_expanded_row_shows_each_source_and_the_searchable_block(tmp_path):
    mems, out, _root = _recover(tmp_path)
    mr.generate_report(mems, out, True, tz_label="UTC", run_id="R")
    detail = open(os.path.join(out, "data", "detail-0.js"), encoding="utf-8").read()

    assert "scdb-27 › ZGALLERYSNAP.ZCAPTURETIMEUTC" in detail
    assert "extraction archive › /Application/APPUUID/" in detail
    assert "class='moreids'" in detail
    assert KEY.hex() in detail and "ZMEDIADOWNLOADURL" in detail and "ACME" in detail


def test_the_index_has_the_metadata_filter_and_the_created_column_names_its_field(tmp_path):
    mems, out, _root = _recover(tmp_path)
    report, _l, _g = mr.generate_report(mems, out, True, tz_label="UTC", run_id="R")
    page = open(report, encoding="utf-8").read()

    assert 'id="meta"' in page and "with embedded metadata" in page
    assert "ZGALLERYSNAP.ZCREATETIMEUTC" in page
    assert "AES key / IV" in page                          # the search box says it matches them


# --------------------------------------------------------------------------- the detail page

def test_the_detail_page_sets_the_three_kinds_of_time_apart(tmp_path):
    mems, out, _root = _recover(tmp_path)
    mr.generate_report(mems, out, True, tz_label="UTC", run_id="R")
    pages = os.path.join(out, "pages")
    page = open(os.path.join(pages, os.listdir(pages)[0]), encoding="utf-8").read()

    assert "Embedded metadata — inside the media files" in page
    assert "Cam 1" in page and "read from: EXIF" in page
    # the file's own timestamps sit in ONE place, under the file's metadata — not repeated in a
    # second table — with the value as written, the conversion, and why one is not converted
    assert page.count("<td>EXIF DateTimeOriginal</td>") == 1
    assert "2024-05-01 12:00:00 +02:00" in page             # as written
    assert "not converted — no timezone in the file" in page
    # the cache file's device mtime sits on the line of the path it dates, in the Media files table
    assert "modified on the device: 2024-05-01 10:01:40 UTC" in page
    assert "Timestamps — cache files on the device" not in page
    # the database sections say which store and which encoding they were read from
    assert "table ZGALLERYSNAP" in page and "table ZGALLERYENTRY" in page


def test_a_file_with_nothing_inside_is_said_to_have_nothing(tmp_path):
    m = _memory()
    m["media_files"] = [{"out": "SNAP-1_full_x.jpg", "path": "media/SNAP-1_full_x.jpg", "role": "full",
                         "ext": "jpg", "source": "SCContent", "bytes": 10, "src": [], "hashes": [],
                         "meta": {"present": False, "times": [], "key": [], "other": [], "gps": {}},
                         "file_times": [], "src_mtimes": []}]
    body = mr._render_group_detail([m], True, [], [], None, {}, {})

    assert "none — the file carries no EXIF, XMP or dated header" in body


def test_a_format_defined_zone_is_converted_but_marked_as_assumed():
    """An mvhd time is UTC by the format's definition, not by anything the file records, and encoders
    have written local time there — so the conversion is shown, and so is the assumption."""
    m = _memory(times={}, create_utc="", entry_times={})
    m["media_files"] = [{"out": "a.mp4", "path": "media/a.mp4", "role": "full", "ext": "mp4",
                         "source": "SCContent", "bytes": 10, "src": [], "hashes": [],
                         "meta": {"present": True, "notable": True, "key": [], "other": [], "gps": {},
                                  "sources": ["mvhd"],
                                  "times": [{"label": "mvhd creation_time", "wall": "2024-05-01 10:00:00",
                                             "zone": "UTC", "epoch": 1714557600, "basis": "format",
                                             "note": ""}]}}]
    mr.annotate_file_times({"SNAP-1": m}, "utc", {})
    t = m["media_files"][0]["file_times"][0]
    assert t["assumed"] is True and "assumed" in t["caveat"]
    body = mr._render_group_detail([m], True, [], [], None, {}, {})
    assert "UTC assumed" in body
    label, value, source = mr._memory_times(m)[0]
    assert value == "2024-05-01 10:00:00 UTC" and "assumed" in source


def test_parts_and_their_mtimes_collapse_to_one_line_each():
    lines = mr._collapse_paths_with_mtimes([
        ("/d/KEY_0-100", "2024-05-01 10:00:00 UTC"), ("/d/KEY_100-200", "2024-05-01 10:00:09 UTC"),
        ("/d/WHOLE", ""), ("/e/OTHER_0-5", "2024-05-01 11:00:00 UTC"),
        ("/e/OTHER_5-9", "2024-05-01 11:00:00 UTC")])
    assert lines == [("/d/KEY_*", "2024-05-01 10:00:00 UTC … 2024-05-01 10:00:09 UTC (2 parts)"),
                     ("/d/WHOLE", "not recorded"),
                     ("/e/OTHER_*", "2024-05-01 11:00:00 UTC (all 2 parts)")]
