"""The device filesystem's own record of each extracted file — `scripts.data.device_fs` and the
`extract_zip` manifest that carries it.

Two acquisition tools record it two ways. A UFED (CLBX) archive keeps one map of every path's stat
record in ``metadata<N>/metadata.msgpack`` — four timestamps in nanoseconds, owner, mode, inode,
protection class, xattrs. A GrayKey archive writes four timestamps into each entry's ``UT`` extra field
(the fourth is not in the specification; it is the birth time). Both land in one record shape, times
as integer nanoseconds with the source's precision stated, so a whole-second value is never dressed
up as exact and a time the archive did not record is absent rather than filled from the extracted
copy. Every archive here is synthetic. No extraction data is used.
"""
import json
import os
import struct
import zipfile

import msgpack

from scripts import memories_media_report as mr
from scripts import report_ui
from scripts.data import device_fs, extract_zip

NS = 1_000_000_000
T_M, T_A, T_C, T_B = 1750000000, 1750001000, 1750001000, 1749990000     # mtime, atime, ctime, birth
APP = "Application/00000000-0000-0000-0000-000000000001"
_META_PLIST = ("<?xml version=\"1.0\"?><!DOCTYPE plist><plist version=\"1.0\"><dict>"
               "<key>MCMMetadataIdentifier</key><string>com.toyopagroup.picaboo</string>"
               "</dict></plist>").encode()


def _ut(flags, *stamps):
    body = struct.pack("<B", flags) + b"".join(struct.pack("<i", s) for s in stamps)
    return struct.pack("<HH", 0x5455, len(body)) + body


def _ux(uid, gid):
    body = struct.pack("<BB", 1, 4) + struct.pack("<I", uid) + struct.pack("<B", 4) + struct.pack("<I", gid)
    return struct.pack("<HH", 0x7875, len(body)) + body


def _info(name, extra=b""):
    info = zipfile.ZipInfo(name, (2001, 1, 1, 0, 0, 0))
    info.extra = extra
    return info


def _utc(seconds):
    return mr.make_epoch_formatter("utc")[0](seconds)


# --------------------------------------------------------------------------- the ZIP entry's record

def test_a_graykey_style_ut_field_yields_all_four_times_and_the_owner():
    record = device_fs.from_zip_entry(_info("x", _ut(0b1111, T_M, T_A, T_C, T_B) + _ux(501, 501)))

    assert record["source"] == "zip-ut" and record["precision"] == "s"
    assert (record["mtime"], record["atime"], record["ctime"]) == (T_M * NS, T_A * NS, T_C * NS)
    assert record["btime"] == T_B * NS and "fourth UT time" in record["btime_basis"]
    assert (record["uid"], record["gid"]) == (501, 501)


def test_a_three_time_ut_field_has_no_birth_time_and_says_nothing_about_it():
    record = device_fs.from_zip_entry(_info("x", _ut(0b111, T_M, T_A, T_C)))

    assert "btime" not in record and record["ctime"] == T_C * NS


def test_an_mtime_only_field_records_only_the_mtime():
    record = device_fs.from_zip_entry(_info("x", _ut(0b1, T_M)))

    assert record == {"source": "zip-ut", "precision": "s", "mtime": T_M * NS}


def test_an_entry_with_no_timestamp_field_yields_no_record():
    assert device_fs.from_zip_entry(_info("x")) is None


# --------------------------------------------------------------------------- the UFED record

def test_a_ufed_stat_record_is_read_at_nanosecond_precision_with_its_attributes():
    raw = {"btime": T_B * NS + 749803, "mtime": T_M * NS + 750305, "atime": T_M * NS + 750305,
           "ctime": T_M * NS + 750327, "size": 258048, "inode": 70474, "links": 1, "uid": 501,
           "gid": 501, "mode": 0o100600, "prot": 3, "xattr": {"com.apple.x": b"yes"}}
    record = device_fs.from_ufed_record(raw)

    assert record["source"] == "ufed-metadata" and record["precision"] == "ns"
    assert record["btime"] == T_B * NS + 749803 and record["prot"] == 3
    assert record["xattr"] == {"com.apple.x": "yes"}


def test_a_nanosecond_record_shows_its_fraction_and_a_second_one_does_not():
    assert device_fs.format_ns(T_M * NS + 750305, _utc, "ns") == "2025-06-15 15:06:40.000750 UTC"
    assert device_fs.format_ns(T_M * NS, _utc, "s") == "2025-06-15 15:06:40 UTC"


def test_identical_instants_are_merged_and_parts_are_bounded():
    one = {"source": "zip-ut", "precision": "s", "mtime": T_M * NS, "atime": T_M * NS,
           "ctime": T_M * NS, "btime": T_B * NS, "prot": 3}
    two = dict(one, mtime=(T_M + 9) * NS, atime=(T_M + 9) * NS, ctime=(T_M + 9) * NS, inode=7)
    lines, attrs = device_fs.summarize([one], _utc)
    assert [(labels, shown) for labels, shown, _n, _k in lines] == [
        ("created", _utc(T_B)), ("modified / accessed / inode changed", _utc(T_M))]

    lines, attrs = device_fs.summarize([one, two], _utc)
    bounded = dict((labels, shown) for labels, shown, _n, _k in lines)
    assert bounded["modified / accessed / inode changed"] == f"{_utc(T_M)} … {_utc(T_M + 9)} (2 parts)"
    assert ("protection class", device_fs.PROTECTION_CLASSES[3]) in attrs


def test_the_shared_renderer_names_the_source_and_says_not_recorded_for_nothing():
    html = report_ui.device_fs_html([{"source": "zip-ut", "precision": "s", "mtime": T_M * NS}], _utc)
    assert "<b>modified</b>" in html and "UT extra field" in html
    assert "not recorded" in report_ui.device_fs_html([None], _utc)


# --------------------------------------------------------------------------- through extract()

def _zip_with(path, entries, extra_files=()):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(_info(f"{APP}/.com.apple.mobile_container_manager.metadata.plist"), _META_PLIST)
        for name, extra, data in entries:
            zf.writestr(_info(name, extra), data)
        for name, data in extra_files:
            zf.writestr(name, data)


def test_a_graykey_archive_puts_the_four_times_in_the_manifest(tmp_path):
    src = tmp_path / "gk.zip"
    _zip_with(src, [(f"{APP}/Library/Caches/x.dat", _ut(0b1111, T_M, T_A, T_C, T_B) + _ux(501, 501), b"d")])
    dest = tmp_path / "out"
    extract_zip.extract(str(src), "ios", str(dest))

    manifest = json.loads((dest / "extraction_manifest.json").read_text(encoding="utf-8"))
    record = manifest["fs"][f"{APP}/Library/Caches/x.dat"]
    assert record["btime"] == T_B * NS and record["source"] == "zip-ut"
    assert manifest["mtimes"][f"{APP}/Library/Caches/x.dat"] == T_M       # the old field stays


def test_a_ufed_archive_s_msgpack_record_wins_over_its_ut_field(tmp_path):
    """The msgpack record has the birth time and nanoseconds; the entry's UT field has neither."""
    device_path = f"private/var/mobile/Containers/Data/{APP}/Library/Caches/y.dat"
    src = tmp_path / "ufed.zip"
    table = msgpack.packb({
        "usr/bin/other": {"mtime": 5 * NS, "atime": 5 * NS, "ctime": 5 * NS, "btime": 5 * NS,
                          "size": 1, "inode": 1, "links": 1, "uid": 0, "gid": 0, "mode": 0o100644,
                          "prot": 0, "xattr": {}},
        device_path: {"mtime": T_M * NS + 750305, "atime": T_M * NS + 750305,
                      "ctime": T_M * NS + 750327, "btime": T_B * NS + 749803, "size": 1,
                      "inode": 70474, "links": 1, "uid": 501, "gid": 501, "mode": 0o100600,
                      "prot": 3, "xattr": {}}})
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(_info(f"filesystem1/private/var/mobile/Containers/Data/{APP}/"
                          ".com.apple.mobile_container_manager.metadata.plist"), _META_PLIST)
        zf.writestr(_info(f"filesystem1/{device_path}", _ut(0b111, T_M, T_M, T_M)), b"y")
        zf.writestr("metadata1/filesystem.msgpack", msgpack.packb({"mount_point": "/"}))
        zf.writestr("metadata1/metadata.msgpack", table)
        zf.writestr("version", b"CLBX-0.3.1")
    dest = tmp_path / "out"
    extract_zip.extract(str(src), "ios", str(dest))

    manifest = json.loads((dest / "extraction_manifest.json").read_text(encoding="utf-8"))
    record = manifest["fs"][f"{APP}/Library/Caches/y.dat"]
    assert record["source"] == "ufed-metadata" and record["precision"] == "ns"
    assert record["btime"] == T_B * NS + 749803 and record["prot"] == 3 and record["inode"] == 70474
    # the extracted copy carries the sub-second mtime too, as an ordinary unzip would (NTFS keeps
    # 100 ns ticks, so the last two digits are the filesystem's, not ours)
    assert os.stat(dest / APP / "Library" / "Caches" / "y.dat").st_mtime_ns // 100 == (T_M * NS + 750305) // 100
    # the record of a path this run did not extract was not kept
    assert len(manifest["fs"]) == 1


def test_the_memories_report_lists_every_recorded_time_with_its_source():
    f = {"out": "a.jpg", "role": "full", "file_times": [], "src_mtimes": [],
         "src_fs": [("/dev/path", {"source": "ufed-metadata", "precision": "ns",
                                   "btime": T_B * NS, "mtime": T_M * NS, "atime": T_M * NS,
                                   "ctime": (T_M + 1) * NS, "prot": 3})]}
    f["device_summary"] = device_fs.summarize([f["src_fs"][0][1]], _utc)
    rows = mr._device_time_rows(f)

    # a nanosecond record shows its fraction even when it is zero: the precision is the record's
    assert rows[0] == ("Cache file created on the device", _utc(T_B).replace(" UTC", ".000000 UTC"),
                       "extraction archive › /dev/path · UFED/CLBX metadata.msgpack (nanosecond stat record)")
    assert rows[1][0] == "Cache file modified / accessed on the device"
    assert rows[2][0] == "Cache file inode changed on the device"
