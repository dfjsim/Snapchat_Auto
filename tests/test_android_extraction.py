"""Android extraction: one canonical device path per file, whichever tool made the archive.

A full file system extraction of an Android phone shows the same app files through several of the
paths Android mounts them at — a GrayKey archive of a Pixel carries each file of an app's private data
under /data/data, /data/user/0 and /data_mirror/data_ce/null/0; a UFED archive carries the app's
shared-storage folder under Dump/data/media/0 and again under Dump/mnt/runtime/*/emulated/0. The old
extractor matched the package name anywhere in the path, wrote every copy onto one file and merged the
shared-storage folder into the private one. Every archive here is synthetic.
"""
import json
import os
import zipfile

import pytest

import android_fixture as fx
from scripts.data import extract_zip

PKG = extract_zip.ANDROID_PACKAGE


@pytest.mark.parametrize("name,expected", [
    (f"/data/data/{PKG}/databases/arroyo.db", f"data/data/{PKG}/databases/arroyo.db"),
    (f"/data/user/0/{PKG}/databases/arroyo.db", f"data/data/{PKG}/databases/arroyo.db"),
    (f"/data_mirror/data_ce/null/0/{PKG}/databases/arroyo.db",
     f"data/data/{PKG}/databases/arroyo.db"),
    (f"Dump/data/data/{PKG}/files/x", f"data/data/{PKG}/files/x"),
    (f"data/user/10/{PKG}/databases/main.db", f"data/user/10/{PKG}/databases/main.db"),
    (f"/data/user_de/0/{PKG}/shared_prefs/a.xml", f"data/user_de/0/{PKG}/shared_prefs/a.xml"),
    (f"Dump/data/media/0/Android/data/{PKG}/cache/x", f"data/media/0/Android/data/{PKG}/cache/x"),
    (f"Dump/mnt/runtime/full/emulated/0/Android/data/{PKG}/cache/x",
     f"data/media/0/Android/data/{PKG}/cache/x"),
    (f"storage/emulated/0/Android/media/{PKG}/y", f"data/media/0/Android/media/{PKG}/y"),
])
def test_every_mount_point_maps_to_one_device_path(name, expected):
    assert extract_zip.android_entry(name)[0] == expected


@pytest.mark.parametrize("name", [
    f"/data/misc/profiles/cur/0/{PKG}/primary.prof",          # the ART profile, not app data
    f"/data/data/com.google.android.gms/files/backup_chunk_listings/{PKG}",
    f"/data/system_ce/0/shortcut_service/packages/{PKG}.xml",
    f"/data/app/~~abc==/{PKG}-xyz==/base.apk",
    "/data/data/com.example.other/databases/x.db",
])
def test_paths_that_only_mention_the_package_are_not_its_data(name):
    assert extract_zip.android_entry(name) is None


def test_an_inner_path_shaped_like_a_mount_point_stays_inside_the_app():
    """One Pixel's Play services folder holds cache/data/user/0/<its own package>/…: the earliest
    match is the app's root, and the rest is a path inside it."""
    name = f"/data_mirror/data_ce/null/0/{PKG}/cache/data/user/0/{PKG}/f"
    assert extract_zip.android_entry(name)[0] == f"data/data/{PKG}/cache/data/user/0/{PKG}/f"


def test_a_bare_app_folder_is_only_accepted_when_asked_for():
    name = f"export/{PKG}/databases/arroyo.db"
    assert extract_zip.android_entry(name) is None
    assert extract_zip.android_entry(name, flat=True)[0] == f"data/data/{PKG}/databases/arroyo.db"
    assert extract_zip.android_entry(f"export/{PKG}/primary.prof", flat=True) is None


@pytest.mark.parametrize("style", ["graykey", "ufed"])
def test_each_file_is_written_once_under_its_device_path(tmp_path, style):
    archive = fx.build_zip(str(tmp_path / f"{style}.zip"), str(tmp_path / "src"), style)
    dest = tmp_path / "out"
    root = extract_zip.extract(archive, "android", dest=str(dest))
    assert os.path.samefile(root, dest)
    app = dest / "data" / "data" / PKG
    assert (app / "databases" / "arroyo.db").is_file()
    assert (app / "databases" / "native_content_manager" / "cache_controller.db").is_file()
    assert (dest / "data" / "media" / "0" / "Android" / "data" / PKG / "files" / "note.txt"
            ).read_bytes() == b"external"
    # nothing from another app, nothing flat, no mirror folders
    assert not (dest / PKG).exists()
    assert not (dest / "data" / "user").exists()
    assert not (dest / "data_mirror").exists()
    assert not (dest / "data" / "data" / "com.example.other").exists()
    manifest = json.loads((dest / "extraction_manifest.json").read_text(encoding="utf-8"))
    assert manifest["platform"] == "android"
    assert f"data/data/{PKG}" in manifest["roots"]
    if style == "graykey":
        # three views of every private file, written once
        n_private = manifest["roots"][f"data/data/{PKG}"]["files"]
        assert manifest["duplicates_skipped"] == 2 * n_private
        root = manifest["roots"][f"data/data/{PKG}"]
        assert root["archive_roots"] == [f"data/data/{PKG}"]
        assert root["also_seen_at"] == [f"data/user/0/{PKG}", f"data_mirror/data_ce/null/0/{PKG}"]
    else:
        assert manifest["duplicates_skipped"] == 1              # the /mnt/runtime copy
    assert manifest["duplicates_differing"] == 0
    # every written file has a device record keyed on its device path
    assert f"data/data/{PKG}/databases/arroyo.db" in manifest["mtimes"]


def test_the_canonical_mount_wins_when_copies_differ(tmp_path):
    """A live acquisition can read one file at two moments through two mounts; the copy read
    through /data/data is the one kept, and the difference is counted rather than hidden."""
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"/data/user/0/{PKG}/databases/main.db", b"later")
        zf.writestr(f"/data/data/{PKG}/databases/main.db", b"canonical")
    dest = tmp_path / "out"
    extract_zip.extract(str(archive), "android", dest=str(dest))
    assert (dest / "data" / "data" / PKG / "databases" / "main.db").read_bytes() == b"canonical"
    manifest = json.loads((dest / "extraction_manifest.json").read_text(encoding="utf-8"))
    assert manifest["duplicates_differing"] == 1


def test_symbolic_links_are_not_written_as_files(tmp_path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"/data/data/{PKG}/databases/main.db", b"db")
        link = zipfile.ZipInfo(f"/data/data/{PKG}/lib")
        link.external_attr = (0o120777 << 16)
        zf.writestr(link, b"/data/app/somewhere/lib/arm64")
    dest = tmp_path / "out"
    extract_zip.extract(str(archive), "android", dest=str(dest))
    assert not (dest / "data" / "data" / PKG / "lib").exists()


def test_an_extraction_without_the_app_says_so(tmp_path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/data/data/com.example.other/databases/x.db", b"x")
    with pytest.raises(extract_zip.SnapchatNotFound):
        extract_zip.extract(str(archive), "android", dest=str(tmp_path / "out"))


def test_a_second_run_reuses_the_extraction(tmp_path):
    archive = fx.build_zip(str(tmp_path / "gk.zip"), str(tmp_path / "src"), "graykey")
    dest = tmp_path / "out"
    extract_zip.extract(archive, "android", dest=str(dest))
    marker = dest / "data" / "data" / PKG / "databases" / "arroyo.db"
    before = marker.stat().st_mtime_ns
    extract_zip.extract(archive, "android", dest=str(dest))
    assert marker.stat().st_mtime_ns == before
