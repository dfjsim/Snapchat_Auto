"""The two cache reports show what a cached media file says about itself, and when the device last
wrote it — through the same renderer the Memories report uses, so a file reads the same way wherever
the examiner meets it.

Synthetic throughout: a JPEG whose EXIF is written with Pillow, placed where each report looks for
files, plus the manifest `extract_zip` would have left. No extraction data is used.
"""
import json
import os

from PIL import Image, ExifTags

from scripts import cache_controller_report as cc
from scripts import cache_media_report as cm
from scripts import memories_media_report as mr
from scripts import report_ui

USER = "11111111-2222-3333-4444-555555555555"
CACHE_KEY = "0123456789abcdef0123456789abcdef"
DEVICE_MTIME = 1714557700                         # 2024-05-01 10:01:40 UTC


def _exif_jpeg(path):
    ex = Image.Exif()
    ex[ExifTags.Base.Make] = "ACME"
    ex[ExifTags.Base.Model] = "Cam 1"
    ifd = ex.get_ifd(ExifTags.IFD.Exif)
    ifd[ExifTags.Base.DateTimeOriginal] = "2024:05:01 12:00:00"
    ifd[ExifTags.Base.OffsetTimeOriginal] = "+02:00"
    Image.new("RGB", (40, 30), "red").save(path, format="JPEG", exif=ex.tobytes())


def _app(tmp_path):
    root = tmp_path / "ExtractedData"
    app = root / "Application" / "APPUUID"
    (app / "Library" / "Caches").mkdir(parents=True)
    (app / "Documents" / f"com.snap.file_manager_3_SCContent_{USER}").mkdir(parents=True)
    return str(root), str(app)


def _manifest(root, rel):
    with open(os.path.join(root, "extraction_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"container_prefixes": {}, "renamed": {}, "mtimes": {rel: DEVICE_MTIME}}, fh)


# --------------------------------------------------------------------------- cache_controller

def _cc_entry(tmp_path):
    root, app = _app(tmp_path)
    scdir = os.path.join(app, "Documents", f"com.snap.file_manager_3_SCContent_{USER}")
    _exif_jpeg(os.path.join(scdir, CACHE_KEY))
    _manifest(root, f"Application/APPUUID/Documents/com.snap.file_manager_3_SCContent_{USER}/{CACHE_KEY}")
    scfull, scparts = mr.index_sccontent(app)
    ms_fmt, _label = cc.make_ms_formatter("utc")
    entry = cc.orphan_entries(scfull, scparts, set(), ms_fmt)[0]
    mtimes = mr.load_device_mtimes(root)
    epochfmt = lambda seconds: ms_fmt(int(seconds) * 1000)   # noqa: E731
    entry["ondisk_mtimes"] = {p: ms_fmt(mtimes[mr.manifest_key(p)] * 1000)
                              for p in entry["on_disk"]["paths"]}
    # what index() derives when the manifest holds only mtimes: a record with the one time
    entry["ondisk_fs"] = {p: {"source": "zip-ut", "precision": "s",
                              "mtime": mtimes[mr.manifest_key(p)] * 1_000_000_000}
                          for p in entry["on_disk"]["paths"]}
    entry["_epochfmt"] = epochfmt
    out = str(tmp_path / "Reports" / "CacheController")
    cc.materialize_ondisk([entry], scfull, scparts, os.path.join(out, "files"), out,
                          epochfmt=epochfmt)
    return entry, root


def test_the_published_cache_file_is_read_for_what_it_says_about_itself(tmp_path):
    entry, _root = _cc_entry(tmp_path)

    assert entry["embedded"]["notable"] is True
    assert ("Make", "ACME") in entry["embedded"]["key"]
    times = {t["label"]: t for t in entry["embedded_times"]}
    assert times["EXIF DateTimeOriginal"]["shown"] == "2024-05-01 10:00:00 UTC"   # in the run's zone
    assert times["EXIF DateTimeOriginal"]["wall"] == "2024-05-01 12:00:00 +02:00"  # as written


def test_the_cache_controller_detail_shows_the_block_and_dates_each_path(tmp_path):
    entry, root = _cc_entry(tmp_path)
    detail = cc._detail_html(entry, "../", root, {})

    assert "Embedded metadata — inside the cached file" in detail
    assert "Cam 1" in detail and "<td>EXIF DateTimeOriginal</td>" in detail
    assert "<b>modified</b> <span class='ts'>2024-05-01 10:01:40 UTC</span>" in detail
    assert "read from: EXIF" in detail


def test_the_cache_controller_search_matches_the_camera_and_the_device_mtime(tmp_path):
    entry, root = _cc_entry(tmp_path)
    out = str(tmp_path / "Reports" / "CacheController")
    cc.generate_report([entry], [], out, "UTC", "../", root, {}, "db", "R", [])
    index = open(os.path.join(out, "data", "index.js"), encoding="utf-8").read().lower()

    assert "acme" in index and "cam 1" in index
    assert "2024-05-01 10:01:40 utc" in index
    assert "exif xmp embedded metadata" in index


def test_an_unrecorded_mtime_reads_not_recorded_not_a_blank(tmp_path):
    entry, root = _cc_entry(tmp_path)
    entry["ondisk_fs"] = {p: None for p in entry["on_disk"]["paths"]}
    detail = cc._detail_html(entry, "../", root, {})

    assert "device filesystem record: not recorded" in detail


# --------------------------------------------------------------------------- Library/Caches

def _cm_entries(tmp_path):
    root, app = _app(tmp_path)
    _exif_jpeg(os.path.join(app, "Library", "Caches", "render.jpg"))
    ms_fmt, _label = cm._ms_formatter("utc")
    entries, _stats = cm.build_entries(app, {}, ms_fmt, root, {})
    return [e for e in entries if e["kind"] == "media"], root


def test_a_library_caches_media_file_carries_its_embedded_metadata(tmp_path):
    entries, _root = _cm_entries(tmp_path)
    assert len(entries) == 1
    entry = entries[0]

    assert ("Model", "Cam 1") in entry["embedded"]["key"]
    assert entry["embedded_times"][0]["shown"] == "2024-05-01 10:00:00 UTC"
    detail = cm._detail_html(entry, "../")
    assert "Embedded metadata — inside the recovered file" in detail
    assert "<td>EXIF DateTimeOriginal</td>" in detail and "ACME" in detail


def test_the_shared_renderer_says_none_for_a_file_with_nothing_inside():
    meta = {"present": False, "notable": False, "times": [], "key": [], "other": [], "gps": {},
            "sources": []}
    block = report_ui.embedded_meta_html(meta, [], label="plain.jpg")

    assert "none — the file carries no EXIF, XMP or dated header" in block
    assert report_ui.embedded_meta_html(None, [], label="x.bin").count("not a format") == 1


def test_a_format_defined_zone_is_marked_as_assumed_in_the_shared_table():
    times = report_ui.file_time_rows(
        {"times": [{"label": "mvhd creation_time", "wall": "2024-05-01 10:00:00", "zone": "UTC",
                    "epoch": 1714557600, "basis": "format", "note": ""}]},
        lambda seconds: "2024-05-01 06:00:00 EDT")
    table = report_ui.file_times_table(times)

    assert "UTC assumed" in table and "2024-05-01 06:00:00 EDT" in table
    assert times[0]["assumed"] and "the file states no zone" in times[0]["caveat"]
