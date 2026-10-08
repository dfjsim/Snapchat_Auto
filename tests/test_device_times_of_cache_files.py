"""What the reports say about a cache file on the device stays what the archive recorded about it.

Two steps of the tool used to write into the extraction folder — the merge of split videos renamed each
``<key>_PREFETCH`` head to ``<key>_0-1``, and the legacy Memories report copied the merged videos into
the SCContent folder as whole files — and the reports then quoted names and files the device never
had, found no device record for them, and dated the file by what was left. Neither step writes there
any more, a folder an earlier version wrote into is put back where the manifest proves it, and the
device's times are shown per file on the device, with any part the archive recorded nothing for
counted rather than dropped. Everything here is synthetic; no extraction data is used.
"""
import json
import os

from scripts import memories_media_report as mr
from scripts import parseSnapvideos_PREFETCH as merge
from scripts.data import device_fs, extract_zip

NS = device_fs.NS
KEY = "0123456789abcdef0123456789abcdef"
USER = "11111111-2222-3333-4444-555555555555"
FOLDER = f"Application/APPUUID/Documents/com.snap.file_manager_3_SCContent_{USER}"


def _utc(seconds):
    return mr.make_epoch_formatter("utc")[0](seconds)


def _rec(btime, mtime, atime=None, ctime=None):
    return {"source": "zip-ut", "precision": "s", "btime": btime * NS, "mtime": mtime * NS,
            "atime": (atime or mtime) * NS, "ctime": (ctime or mtime) * NS}


def _tree(tmp_path, files, recorded):
    """An extraction folder holding ``files`` ({name: bytes}) in one SCContent folder, and a
    manifest recording ``recorded`` ({name: record})."""
    root = tmp_path / "ExtractedData"
    folder = root / FOLDER
    folder.mkdir(parents=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    fs = {f"{FOLDER}/{name}": rec for name, rec in recorded.items()}
    (root / "extraction_manifest.json").write_text(json.dumps(
        {"container_prefixes": {}, "archive": "graykey", "renamed": {},
         "mtimes": {k: r["mtime"] // NS for k, r in fs.items()}, "fs": fs}), encoding="utf-8")
    return root, folder


# --------------------------------------------------------------------------- nothing is written there

def test_the_merge_of_split_videos_leaves_the_extraction_s_names_alone(tmp_path, monkeypatch):
    app = tmp_path / "Application"
    folder = app / "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE" / "Documents" / f"com.snap.file_manager_3_SCContent_{USER}"
    folder.mkdir(parents=True)
    (folder / f"{KEY}_PREFETCH").write_bytes(b"head")
    (folder / f"{KEY}_4-9").write_bytes(b"tail!")
    monkeypatch.chdir(tmp_path)

    merge.main(str(app).replace("\\", "/"))

    assert sorted(os.listdir(folder)) == [f"{KEY}_4-9", f"{KEY}_PREFETCH"]
    # the merged copy is unchanged: the head first, at offset 0, then the parts by offset
    assert (tmp_path / "SnapFixedVideos" / f"{KEY}.mp4").read_bytes() == b"headtail!"


# --------------------------------------------------------------------------- what an earlier run wrote

def test_a_head_an_earlier_version_renamed_gets_its_device_name_back(tmp_path):
    root, folder = _tree(tmp_path, {f"{KEY}_0-1": b"head", f"{KEY}_4-9": b"tail!"},
                         {f"{KEY}_PREFETCH": _rec(10, 11), f"{KEY}_4-9": _rec(12, 13)})

    assert extract_zip.undo_earlier_writes(str(root), str(tmp_path / "none")) == (1, 0)
    assert sorted(os.listdir(folder)) == [f"{KEY}_4-9", f"{KEY}_PREFETCH"]


def test_a_real_0_1_part_is_never_renamed(tmp_path):
    # the archive itself holds a `_0-1` part: the manifest records it, so it is the device's name
    root, folder = _tree(tmp_path, {f"{KEY}_0-1": b"h"}, {f"{KEY}_0-1": _rec(10, 11)})

    assert extract_zip.undo_earlier_writes(str(root)) == (0, 0)
    assert os.listdir(folder) == [f"{KEY}_0-1"]


def test_a_merged_copy_an_earlier_version_put_in_the_cache_folder_is_removed(tmp_path):
    root, folder = _tree(tmp_path, {f"{KEY}_PREFETCH": b"head", f"{KEY}_4-9": b"tail!",
                                    KEY: b"headtail!"},
                         {f"{KEY}_PREFETCH": _rec(10, 11), f"{KEY}_4-9": _rec(12, 13)})
    merged = tmp_path / "SnapFixedVideos"
    merged.mkdir()
    (merged / f"{KEY}.mp4").write_bytes(b"headtail!")

    assert extract_zip.undo_earlier_writes(str(root), str(merged)) == (0, 1)
    assert KEY not in os.listdir(folder)
    assert (merged / f"{KEY}.mp4").is_file()                 # the bytes are kept where we made them


def test_a_whole_file_not_proven_to_be_ours_is_kept(tmp_path):
    merged = tmp_path / "SnapFixedVideos"
    merged.mkdir()
    (merged / f"{KEY}.mp4").write_bytes(b"headtail!")
    # recorded by the archive: the device's own whole file, whatever SnapFixedVideos holds
    root, folder = _tree(tmp_path, {KEY: b"headtail!", f"{KEY}_4-9": b"tail!"},
                         {KEY: _rec(10, 11), f"{KEY}_4-9": _rec(12, 13)})
    assert extract_zip.undo_earlier_writes(str(root), str(merged)) == (0, 0)
    assert KEY in os.listdir(folder)


def test_different_bytes_are_never_taken_for_the_merged_copy(tmp_path):
    root, folder = _tree(tmp_path, {f"{KEY}_4-9": b"tail!", KEY: b"something else"},
                         {f"{KEY}_4-9": _rec(12, 13)})
    merged = tmp_path / "SnapFixedVideos"
    merged.mkdir()
    (merged / f"{KEY}.mp4").write_bytes(b"other bytes!!!")

    assert extract_zip.undo_earlier_writes(str(root), str(merged)) == (0, 0)
    assert KEY in os.listdir(folder)


# --------------------------------------------------------------------------- how the times are shown

def test_a_part_without_a_record_is_counted_not_dropped():
    lines, attrs = device_fs.summarize([None, _rec(20, 20)], _utc)

    # the one recorded part's times are not the file's: the line says whose they are
    assert [(labels, shown) for labels, shown, _n, _k in lines] == [
        ("created / modified / accessed / inode changed", f"{_utc(20)} (1 of 2 parts)")]
    assert ("not recorded", "1 of 2 parts") in attrs


def _media(paths_and_records):
    f = {"out": "a.mp4", "role": "full", "src": [p for p, _r in paths_and_records]}
    m = {"snap_id": "SNAP-1", "media_files": [f], "times": {}, "entry_times": {}}
    fs = {mr.manifest_key(p): r for p, r in paths_and_records if r}
    mr.annotate_file_times({"SNAP-1": m}, "utc", {}, fs=fs)
    return m, f


def test_two_copies_of_the_media_are_dated_apart():
    one = f"/x/{FOLDER}/{KEY}"
    two = f"/x/Application/APPUUID/Documents/com.snap.file_manager_3_SCContent_OTHER/{KEY}"
    m, f = _media([(one, _rec(10, 30)), (two, _rec(20, 20))])
    rows = mr._device_time_rows(f)

    # each copy its own created and modified — never one bound over both, as if they were parts
    assert [(label, value) for label, value, _s in rows] == [
        ("Cache file created on the device", _utc(10)),
        ("Cache file modified / accessed / inode changed on the device", _utc(30)),
        ("Cache file created / modified / accessed / inode changed on the device", _utc(20))]
    assert rows[0][2].startswith(f"extraction archive › /{FOLDER}/{KEY} · ")
    assert "parts" not in " ".join(source for _l, _v, source in rows)


def test_a_file_rebuilt_from_parts_is_bounded_over_its_own_parts():
    m, f = _media([(f"/x/{FOLDER}/{KEY}_PREFETCH", _rec(10, 10)),
                   (f"/x/{FOLDER}/{KEY}_4-9", _rec(20, 25))])
    rows = mr._device_time_rows(f)

    assert rows[0][:2] == ("Cache file created on the device", f"{_utc(10)} … {_utc(20)} (2 parts)")
    assert rows[0][2].startswith(f"extraction archive › 2 parts of /{FOLDER}/{KEY}_* · ")


def test_the_chunks_of_one_caching_media_pack_are_one_file():
    item = "ab" * 32
    folder = f"/x/Application/APPUUID/Library/Caches/caching-media/{item}"
    f = {"out": "a.jpg", "role": "cached", "source": "caching-media", "item": item,
         "src": [f"{folder}/{item}-0.pack", f"{folder}/{item}-1.pack"]}
    m = {"snap_id": "SNAP-1", "media_files": [f], "times": {}, "entry_times": {}}
    fs = {mr.manifest_key(p): _rec(10 + n, 10 + n) for n, p in enumerate(f["src"])}
    mr.annotate_file_times({"SNAP-1": m}, "utc", {}, fs=fs)
    rows = mr._device_time_rows(f)

    assert [value for _label, value, _s in rows] == [f"{_utc(10)} … {_utc(11)} (2 parts)"]
    assert rows[0][2].startswith(
        f"extraction archive › 2 parts of /Application/APPUUID/Library/Caches/caching-media/"
        f"{item}/{item}-*.pack · ")


def test_a_memory_is_found_by_the_time_of_its_last_part():
    m, _f = _media([(f"/x/{FOLDER}/{KEY}_PREFETCH", _rec(10, 10)),
                    (f"/x/{FOLDER}/{KEY}_4-9", _rec(20, 25, 40, 50))])
    keys = mr._memory_time_keys(m)

    for seconds in (10, 20, 25, 40):                   # every part's created, modified, accessed
        assert seconds in keys["ts"]
    assert keys["tc"] == [50]                          # the inode change, apart


def test_a_generated_file_s_note_is_not_given_a_device_record():
    path = f"/x/{FOLDER}/{KEY}"
    m, f = _media([(path, _rec(10, 30))])
    f.update({"generated": True, "src_note": "(generated from the decrypted video — not original "
                                             "device data)"})
    out = mr._render_src_paths(f, None, None)

    assert out.startswith("(generated from the decrypted video")
    assert "not recorded" not in out
    assert out.count("on the device:") == 1               # the video's cache file, and only it


def test_a_shard_longer_than_its_range_is_not_said_to_be_short(tmp_path):
    longer = tmp_path / f"{KEY}_0-1"
    longer.write_bytes(b"x" * 100)                     # what an earlier rename of a PREFETCH head left
    shorter = tmp_path / f"{KEY}_100-300"
    shorter.write_bytes(b"x" * 50)

    coverage = mr._part_coverage([(0, str(longer)), (100, str(shorter))])
    assert coverage["short"] == [shorter.name]
