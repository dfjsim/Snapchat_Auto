"""The device filesystem's own record of each extracted file — `scripts.data.device_fs` and the
`extract_zip` manifest that carries it.

Two acquisition tools record it two ways. A UFED (CLBX) archive keeps one map of every path's stat
record in ``metadata<N>/metadata.msgpack`` — four timestamps in nanoseconds, owner, mode, inode,
protection class, xattrs — and puts the access time, and only it, into every slot of its entries' ``UT``
field. A GrayKey archive writes four timestamps into each entry's ``UT`` extra field (the fourth is not
in the specification; it is the birth time). Both land in one record shape, times as integer
nanoseconds with the source's precision stated, so a whole-second value is never dressed up as exact
and a time the archive did not record is absent rather than filled from the extracted copy. Every
archive here is synthetic. No extraction data is used.
"""
import json
import os
import struct
import time
import zipfile

import msgpack
import pytest

from scripts import cache_media_report as cm
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


def test_a_clbx_entry_s_ut_field_is_the_access_time_and_nothing_else():
    """A UFED/CLBX archive flags mtime / atime / ctime and writes the access time into all three."""
    record = device_fs.from_zip_entry(_info("x", _ut(0b111, T_A, T_A, T_A) + _ux(501, 501)),
                                      ut_access_only=True)

    assert record == {"source": "zip-ut-clbx", "precision": "s", "atime": T_A * NS,
                      "uid": 501, "gid": 501}
    assert "access time only" in device_fs.source_label(record)


def test_the_archive_kind_is_the_stat_table_then_graykey_s_own_fields():
    plain = _info("x", _ut(0b111, T_M, T_A, T_C))
    graykey = _info("y", _ut(0b1111, T_M, T_A, T_C, T_B))
    graykey_android = _info("z", _ut(0b111, T_M, T_A, T_C) + struct.pack("<HH", 0x3253, 32) + bytes(32))

    assert device_fs.archive_kind(["a/x"], [plain]) == "zip"
    assert device_fs.archive_kind(["a/y"], [plain, graykey]) == "graykey"
    assert device_fs.archive_kind(["a/z"], [graykey_android]) == "graykey"
    # the table decides, with or without a `version` member
    assert device_fs.archive_kind(["filesystem1/x", "metadata1/metadata.msgpack"], [graykey]) == "clbx"


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
    assert manifest["archive"] == "graykey"


_OTHER_RECORD = {"mtime": 5 * NS, "atime": 5 * NS, "ctime": 5 * NS, "btime": 5 * NS, "size": 1,
                 "inode": 1, "links": 1, "uid": 0, "gid": 0, "mode": 0o100644, "prot": 0, "xattr": {}}


def _ufed_zip(path, device_path, data, table, version=True):
    """A UFED/CLBX-shaped archive: one file under ``filesystem1/`` whose ``UT`` field carries the access
    time in all three slots, as UFED writes it, and ``table`` as ``metadata1/metadata.msgpack``."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(_info(f"filesystem1/private/var/mobile/Containers/Data/{APP}/"
                          ".com.apple.mobile_container_manager.metadata.plist"), _META_PLIST)
        zf.writestr(_info(f"filesystem1/{device_path}", _ut(0b111, T_A, T_A, T_A)), data)
        zf.writestr("metadata1/filesystem.msgpack", msgpack.packb({"mount_point": "/"}))
        zf.writestr("metadata1/metadata.msgpack", table)
        if version:
            zf.writestr("version", b"CLBX-0.3.1")


def test_a_ufed_archive_s_msgpack_record_wins_over_its_ut_field(tmp_path):
    """The msgpack record has the four named times at nanoseconds; the entry's UT field has the access
    time in every slot, which must not become the modification time anywhere — not in ``fs``, not in
    ``mtimes``, not on the extracted copy."""
    device_path = f"private/var/mobile/Containers/Data/{APP}/Library/Caches/y.dat"
    src = tmp_path / "ufed.zip"
    table = msgpack.packb({
        "usr/bin/other": _OTHER_RECORD,
        device_path: {"mtime": T_M * NS + 750305, "atime": T_A * NS + 120,
                      "ctime": T_M * NS + 750327, "btime": T_B * NS + 749803, "size": 1,
                      "inode": 70474, "links": 1, "uid": 501, "gid": 501, "mode": 0o100600,
                      "prot": 3, "xattr": {}}})
    _ufed_zip(src, device_path, b"y", table)
    dest = tmp_path / "out"
    extract_zip.extract(str(src), "ios", str(dest))

    manifest = json.loads((dest / "extraction_manifest.json").read_text(encoding="utf-8"))
    key = f"{APP}/Library/Caches/y.dat"
    record = manifest["fs"][key]
    assert manifest["archive"] == "clbx"
    assert record["source"] == "ufed-metadata" and record["precision"] == "ns"
    assert (record["mtime"], record["atime"]) == (T_M * NS + 750305, T_A * NS + 120)
    assert record["btime"] == T_B * NS + 749803 and record["prot"] == 3 and record["inode"] == 70474
    # the plain mtime older readers use is the table's, floored to the second
    assert manifest["mtimes"][key] == T_M
    # the extracted copy carries the table's sub-second mtime, as an ordinary unzip would (NTFS keeps
    # 100 ns ticks, so the last two digits are the filesystem's, not ours)
    assert os.stat(dest / APP / "Library" / "Caches" / "y.dat").st_mtime_ns // 100 == (T_M * NS + 750305) // 100
    # the record of a path this run did not extract was not kept
    assert len(manifest["fs"]) == 1


@pytest.mark.parametrize("table", [msgpack.packb({"usr/bin/other": _OTHER_RECORD}), b"\xc1 not msgpack"],
                         ids=["file-absent-from-the-table", "table-unreadable"])
def test_a_clbx_file_without_a_table_record_keeps_its_access_time_only(tmp_path, table):
    """The fallback to the entry's own UT field: in a CLBX archive that is the access time, so the
    record holds ``atime`` alone, ``mtimes`` has no entry and the extracted copy is given no device
    time. The archive is known by its table even when the table cannot be read, and without a
    ``version`` member, which an older UFED archive does not have."""
    device_path = f"private/var/mobile/Containers/Data/{APP}/Library/Caches/z.dat"
    src = tmp_path / "ufed.zip"
    _ufed_zip(src, device_path, b"z", table, version=False)
    dest = tmp_path / "out"
    extract_zip.extract(str(src), "ios", str(dest))

    manifest = json.loads((dest / "extraction_manifest.json").read_text(encoding="utf-8"))
    key = f"{APP}/Library/Caches/z.dat"
    assert manifest["archive"] == "clbx"
    assert manifest["fs"][key] == {"source": "zip-ut-clbx", "precision": "s", "atime": T_A * NS}
    assert key not in manifest["mtimes"]
    assert os.stat(dest / APP / "Library" / "Caches" / "z.dat").st_mtime > time.time() - 3600


def test_the_android_extraction_reads_a_clbx_ut_field_the_same_way(tmp_path):
    """No UFED archive of an Android phone seen so far has a stat table; the rule is keyed on the
    table, which is what was measured, so one that has one is read like an iOS CLBX archive."""
    pkg = "com.snapchat.android"
    src = tmp_path / "android.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(_info(f"filesystem1/data/data/{pkg}/databases/arroyo.db",
                          _ut(0b111, T_A, T_A, T_A)), b"db")
        zf.writestr("metadata1/metadata.msgpack", msgpack.packb({}))
    dest = tmp_path / "out"
    extract_zip.extract(str(src), "android", str(dest))

    manifest = json.loads((dest / "extraction_manifest.json").read_text(encoding="utf-8"))
    key = f"data/data/{pkg}/databases/arroyo.db"
    record = manifest["fs"][key]
    assert manifest["archive"] == "clbx"
    assert record["source"] == "zip-ut-clbx" and record["atime"] == T_A * NS
    assert not {"mtime", "ctime", "btime"} & set(record)
    assert key not in manifest["mtimes"]


# --------------------------------------------------------------------------- an earlier build's manifest

_CLBX_PREFIXES = {f"{APP}": "filesystem1/private/var/mobile/Containers/Data"}
_GRAYKEY_PREFIXES = {f"{APP}": "private/var/mobile/Containers/Data"}
_KEY = f"{APP}/Library/Caches/x.dat"


def test_a_manifest_that_records_its_archive_is_read_as_written():
    manifest = {"archive": "clbx", "container_prefixes": _CLBX_PREFIXES, "mtimes": {_KEY: T_M},
                "fs": {_KEY: {"source": "ufed-metadata", "precision": "ns", "mtime": T_M * NS}}}

    assert device_fs.manifest_times(manifest) == (manifest["mtimes"], manifest["fs"])


def test_an_earlier_clbx_folder_s_mtimes_are_shown_as_the_access_times_they_are():
    """1.6.0-beta.2 to 1.6.1-beta.1 wrote only ``mtimes``, from a CLBX entry's UT field. The container
    prefix under filesystem<N>/ says which archive it was."""
    mtimes, fs = device_fs.manifest_times({"container_prefixes": _CLBX_PREFIXES, "mtimes": {_KEY: T_A}})

    assert mtimes == {}
    assert fs == {_KEY: {"source": "zip-ut-clbx", "precision": "s", "atime": T_A * NS}}
    html = report_ui.device_fs_html([fs[_KEY]], _utc)
    assert "<b>accessed</b>" in html and "<b>modified" not in html


def test_an_earlier_clbx_folder_with_table_records_keeps_the_table_s_mtime():
    """1.6.2-beta.1 to 1.8.0-beta.1 wrote ``fs`` too: the table's records are right, ``mtimes`` beside
    them held the access time, and a fallback record carried it as all three times."""
    other = f"{APP}/Library/Caches/y.dat"
    manifest = {"container_prefixes": {}, "mtimes": {_KEY: T_A, other: T_A},
                "fs": {_KEY: {"source": "ufed-metadata", "precision": "ns", "mtime": T_M * NS + 5,
                              "atime": T_A * NS},
                       other: {"source": "zip-ut", "precision": "s", "mtime": T_A * NS,
                               "atime": T_A * NS, "ctime": T_A * NS, "uid": 501, "gid": 501}}}
    mtimes, fs = device_fs.manifest_times(manifest)

    assert mtimes == {_KEY: T_M}
    assert fs[_KEY] == manifest["fs"][_KEY]
    assert fs[other] == {"source": "zip-ut-clbx", "precision": "s", "atime": T_A * NS,
                         "uid": 501, "gid": 501}


def test_an_earlier_graykey_folder_is_read_as_written():
    manifest = {"container_prefixes": _GRAYKEY_PREFIXES, "mtimes": {_KEY: T_M}}

    assert device_fs.manifest_times(manifest) == ({_KEY: T_M}, {})


def test_an_earlier_folder_that_cannot_say_shows_both_readings():
    mtimes, fs = device_fs.manifest_times({"mtimes": {_KEY: T_M}})

    assert fs[_KEY] == {"source": "zip-ut-unclassified", "precision": "s", "mtime": T_M * NS}
    label = device_fs.source_label(fs[_KEY])
    assert "modification time if GrayKey" in label and "access time if UFED" in label


def test_the_reports_read_an_earlier_clbx_folder_through_the_same_correction(tmp_path):
    (tmp_path / "extraction_manifest.json").write_text(json.dumps(
        {"container_prefixes": _CLBX_PREFIXES, "mtimes": {_KEY: T_A}}), encoding="utf-8")

    assert mr.load_device_mtimes(str(tmp_path)) == {}
    assert mr.load_fs_records(str(tmp_path))[_KEY]["atime"] == T_A * NS
    assert cm.load_device_mtimes(str(tmp_path), "") == {}


def test_the_memories_report_lists_every_recorded_time_with_its_source():
    f = {"out": "a.jpg", "role": "full", "file_times": [], "src_mtimes": [],
         "src_fs": [("/dev/path", {"source": "ufed-metadata", "precision": "ns",
                                   "btime": T_B * NS, "mtime": T_M * NS, "atime": T_M * NS,
                                   "ctime": (T_M + 1) * NS, "prot": 3})]}
    f["device_groups"] = [("/dev/path", [f["src_fs"][0][1]],
                           device_fs.summarize([f["src_fs"][0][1]], _utc))]
    rows = mr._device_time_rows(f)

    # a nanosecond record shows its fraction even when it is zero: the precision is the record's
    assert rows[0] == ("Cache file created on the device", _utc(T_B).replace(" UTC", ".000000 UTC"),
                       "extraction archive › /dev/path · UFED/CLBX metadata.msgpack (nanosecond stat record)")
    assert rows[1][0] == "Cache file modified / accessed on the device"
    assert rows[2][0] == "Cache file inode changed on the device"
