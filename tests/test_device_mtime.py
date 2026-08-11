"""The Library/Caches report's "modified" column, which used to be the moment *we* unzipped the file.

`os.path.getmtime` of an extracted copy is when this run — or an earlier one — wrote it. It was shown
under a bare `modified` heading beside the path, size, producer and stored SHA-256, which *are* facts
about the device, so a reader took it as one. It also stamped the processing date onto every row.

An extraction ZIP does record the file's real mtime, which is not obvious and worth pinning: two
extractions of one device, taken by different tools fifteen days apart, carry the same stamps for the
same Snapchat cache files — archive-creation stamping could not produce that agreement. It is in the
entry's `UT` extra field as UTC seconds; the header's DOS date/time is a local wall clock with no zone
recorded, so it cannot be turned into an instant without guessing whose clock wrote it.

`extract_zip` records those in the extraction manifest and also applies them to the extracted copies.
The report reads the **manifest**, not the files, because a file always has an mtime: from the file
alone "the device said this" cannot be told apart from "the archive recorded nothing", and an
extraction folder produced by an older build carries our unzip times with no way to say so.

Every input here is synthetic. No extraction data is required or used.
"""
import json
import os
import struct
import time
import zipfile

from scripts import cache_media_report as cm
from scripts.data import extract_zip

DEVICE_TIME = 1750000000          # a fixed instant, well before any test run


def _ut_extra(stamp):
    """A ZIP 'UT' extra field (0x5455) carrying a modification time, as a real archive writes it."""
    body = struct.pack("<B i", 1, stamp)                       # flags: mtime present
    return struct.pack("<HH", 0x5455, len(body)) + body


# --------------------------------------------------------------- reading the archive's record

def test_the_device_time_is_read_from_the_unix_extra_field(tmp_path):
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("Application/UUID/Library/Caches/x.dat", (2001, 1, 1, 0, 0, 0))
        info.extra = _ut_extra(DEVICE_TIME)
        zf.writestr(info, b"x")

    with zipfile.ZipFile(path) as zf:
        assert extract_zip.zip_mtime(zf.infolist()[0]) == DEVICE_TIME


def test_an_entry_with_no_unix_field_reports_no_time_rather_than_the_dos_one(tmp_path):
    """The DOS date/time is a local wall clock with no zone, so it cannot be presented as an instant.
    Reporting nothing is the honest answer, and the report says «not recorded»."""
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("Application/UUID/Library/Caches/y.dat",
                                    (2001, 1, 1, 0, 0, 0)), b"y")

    with zipfile.ZipFile(path) as zf:
        info = zf.infolist()[0]
        assert info.date_time[0] == 2001                       # the DOS field IS there
        assert extract_zip.zip_mtime(info) is None             # and is deliberately not used


def test_other_extra_fields_are_skipped_not_misread(tmp_path):
    """Extra fields are a sequence of (id, size, body); a reader that does not walk it properly reads
    another field's bytes as a timestamp."""
    path = tmp_path / "c.zip"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("Application/UUID/Library/Caches/z.dat", (2001, 1, 1, 0, 0, 0))
        info.extra = struct.pack("<HH", 0x000A, 8) + b"\x00" * 8 + _ut_extra(DEVICE_TIME)
        zf.writestr(info, b"z")

    with zipfile.ZipFile(path) as zf:
        assert extract_zip.zip_mtime(zf.infolist()[0]) == DEVICE_TIME


# --------------------------------------------------------------- finding a file's record again

def test_the_manifest_key_survives_either_separator():
    """The lookup key is the path from the container segment on. Matching separators as an escaped
    character class put a regex there that accepted only «/» — which matches nothing on Windows, so
    every file showed as having no recorded time. Hence both spellings, pinned."""
    expected = "Application/UUID/Library/Caches/x.dat"

    assert cm._manifest_key(r"C:\run\ExtractedData\Application\UUID\Library\Caches\x.dat") == expected
    assert cm._manifest_key("/run/ExtractedData/Application/UUID/Library/Caches/x.dat") == expected
    assert cm._manifest_key("Application/UUID/Library/Caches/x.dat") == expected


def test_an_appgroup_path_is_keyed_too():
    assert cm._manifest_key(r"C:\x\AppGroup\GUID\Library\Caches\y").startswith("AppGroup/GUID/")


def test_a_path_outside_a_container_has_no_key():
    assert cm._manifest_key(r"C:\somewhere\else\file.dat") == ""
    assert cm._manifest_key("") == ""


# --------------------------------------------------------------- what the column shows

def _ms_fmt(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000)) + " UTC"


def test_the_column_shows_the_device_time_not_the_copys_own(tmp_path):
    full = tmp_path / "Application" / "UUID" / "Library" / "Caches" / "x.dat"
    full.parent.mkdir(parents=True)
    full.write_bytes(b"x")                                     # its own mtime is now
    mtimes = {"Application/UUID/Library/Caches/x.dat": DEVICE_TIME}

    shown = cm._device_mtime(str(full), mtimes, _ms_fmt)

    assert shown == _ms_fmt(DEVICE_TIME * 1000)
    assert not shown.startswith(time.strftime("%Y-%m-%d", time.gmtime()))


def test_a_file_the_archive_recorded_no_time_for_shows_nothing(tmp_path):
    """Rather than the extracted copy's mtime, which is a fact about this run."""
    assert cm._device_mtime(str(tmp_path / "x.dat"), {}, _ms_fmt) == ""
    assert cm._device_mtime(str(tmp_path / "x.dat"), None, _ms_fmt) == ""


def test_an_absent_time_is_stated_rather_than_left_blank():
    """A blank cell reads as «nothing happened»; the truth is that we do not know."""
    assert "not recorded" in cm._NO_MTIME


# --------------------------------------------------------------- end to end through extract()

def test_extraction_records_the_times_and_stamps_the_copies(tmp_path):
    """Both halves: the manifest the report reads, and the extracted copy carrying the device's own
    time the way every ordinary unzip tool leaves it."""
    src = tmp_path / "extraction.zip"
    with zipfile.ZipFile(src, "w") as zf:
        for name in ("Application/UUID/Documents/user.plist",
                     "Application/UUID/Library/Caches/x.dat"):
            info = zipfile.ZipInfo(name, (2001, 1, 1, 0, 0, 0))
            info.extra = _ut_extra(DEVICE_TIME)
            zf.writestr(info, b"data")

    dest = tmp_path / "out"
    extract_zip.extract(str(src), "ios", str(dest))

    manifest = json.loads((dest / "extraction_manifest.json").read_text(encoding="utf-8"))
    key = "Application/UUID/Library/Caches/x.dat"
    assert manifest["mtimes"][key] == DEVICE_TIME
    assert abs(os.path.getmtime(dest / key) - DEVICE_TIME) < 2, \
        "the extracted copy should carry the device's time, not the moment it was written"
