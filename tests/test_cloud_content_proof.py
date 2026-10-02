"""Proven by content: a cached file identical to a Memory's server copy links to that Memory.

The cache file a snap editor wrote — or any file no claim connects to its Memory — can still be the
Memory's media. The only proof without an identifier is the bytes: when the copy retrieved from
Snapchat's servers, decrypted with the Memory's own key, has the same SHA-256 as the cached file, the
cache reports link the two and say how. That link comes last: any recorded identifier wins.

Every input is synthetic.
"""
import os
import sqlite3

from scripts import cache_controller_report as cc
from scripts import cache_media_report as cmr

USER = "11111111-2222-3333-4444-555555555555"
SNAP = "77777777-aaaa-4bbb-8ccc-dddddddddddd"
OTHER = "88888888-aaaa-4bbb-8ccc-dddddddddddd"
KEY_EDITOR = "e" * 32
KEY_LINKED = "f" * 32
PROOF = {"snap_id": SNAP, "role": "media", "what": "decrypted", "sha256": "ab" * 32,
         "bytes": 10, "retrieved_utc": "2026-10-02T10:00:00Z", "session": "s",
         "authority_note": "Search warrant 2026-001"}


def _db(tmp_path):
    path = str(tmp_path / "cache_controller.db")
    conn = sqlite3.connect(path)
    conn.execute("create table CACHE_FILE_CLAIM (USER_ID text, CACHE_KEY text, MEDIA_CONTEXT_TYPE "
                 "integer, EXTERNAL_KEY text, IS_AUTHORITATIVE integer, EXPIRATION_TIMESTAMP_MILLIS "
                 "integer, DELETED_TIMESTAMP_MILLIS integer, CONTENT_CLAIM_METADATA blob, "
                 "CONTENT_ATTRIBUTION integer, CREATION_TIMESTAMP_MILLIS integer)")
    conn.execute("create table CACHE_FILE_METADATA (USER_ID text, CACHE_KEY text, STORAGE_TYPE "
                 "integer, TYPE integer, FILE_SIZE_BYTES integer, TOTAL_DISK_USED_BYTES integer, "
                 "KNOWN_CONTENT_LENGTH_BYTES integer, LAST_READ_TIMESTAMP_MILLIS integer, "
                 "DELETED_TIMESTAMP_MILLIS integer, CHILDREN blob, CONTENT_RETRIEVAL_METADATA blob, "
                 "SHARD_INDEX integer, CONTENT_LIFECYCLE blob)")
    conn.execute("create table CACHE_FILE_SAMPLED_TOMBSTONE (USER_ID text, CACHE_KEY text, "
                 "MEDIA_CONTEXT_TYPE integer, IS_SHARED integer, DELETION_REASON integer, "
                 "BYTES_DELETED integer, DELETED_TIMESTAMP_MILLIS integer)")
    conn.execute("create table CACHE_KEY_VIRTUALIZATION (USER_ID text, VIRTUAL_CACHE_KEY text, "
                 "CACHE_KEY text)")
    conn.execute("insert into CACHE_FILE_CLAIM (USER_ID, CACHE_KEY, MEDIA_CONTEXT_TYPE, EXTERNAL_KEY, "
                 "CREATION_TIMESTAMP_MILLIS) values (?, ?, 34, ?, 0)",
                 (USER, KEY_EDITOR, "3c3c3c3c-1111-4222-8333-444455556666~1"))
    conn.execute("insert into CACHE_FILE_CLAIM (USER_ID, CACHE_KEY, MEDIA_CONTEXT_TYPE, EXTERNAL_KEY, "
                 "CREATION_TIMESTAMP_MILLIS) values (?, ?, 19, ?, 0)",
                 (USER, KEY_LINKED, f"snap-media-{OTHER}"))
    conn.commit()
    conn.close()
    return path


def _entries(tmp_path, content):
    mem_index = {"snap_ids": {SNAP.upper(): (SNAP, "h"), OTHER.upper(): (OTHER, "h")}, "url_keys": {},
                 "media_ids": {}, "snap_urls": {}}
    entries, _virt, _wal = cc.build_entries(_db(tmp_path), str(tmp_path), {}, {}, mem_index,
                                            {}, lambda ms: str(ms), workdir=str(tmp_path / "w"),
                                            memory_content=content)
    return {e["cache_key"]: e for e in entries}


def test_an_identical_file_links_with_its_proof(tmp_path):
    by_key = _entries(tmp_path, {KEY_EDITOR: [PROOF]})
    editor = by_key[KEY_EDITOR]
    assert editor["memory"]["snap_id"] == SNAP and editor["memory"]["by_content"]
    assert "Proven by content" in editor["memory_basis"]
    assert "Search warrant 2026-001" in editor["memory_basis"]
    chip = cc._links_html(editor, "../", compact=True)
    assert "☁" in chip


def test_a_recorded_identifier_still_wins(tmp_path):
    by_key = _entries(tmp_path, {KEY_LINKED: [dict(PROOF, snap_id=SNAP)]})
    linked = by_key[KEY_LINKED]
    assert linked["memory"]["snap_id"] == OTHER and not linked["memory"].get("by_content")
    assert linked["content_proof"]                       # the proof is still shown in the detail


def test_without_a_retrieval_nothing_changes(tmp_path):
    by_key = _entries(tmp_path, None)
    assert by_key[KEY_EDITOR]["memory"] is None and by_key[KEY_EDITOR]["content_proof"] == []


def test_a_library_caches_file_is_found_by_its_hash():
    entry = {"name": "x.jpg", "rel": "sub/x.jpg", "sha256": "ab" * 32, "bytes": 10}
    links = cmr.attribute(entry, {}, {}, {}, {"url_keys": {}}, {}, {}, {},
                          content={"ab" * 32: [PROOF]})
    assert [(link["kind"], link["snap_id"]) for link in links] == [("memory", SNAP)]
    assert "Proven by content" in links[0]["basis"]
    assert cmr.attribute(entry, {}, {}, {}, {"url_keys": {}}, {}, {}, {}) == []


# ------------------------------------------------------- the device's own copy of the media

DEVICE = {"snap_id": SNAP, "role": "full", "what": "device", "sha256": "cd" * 32, "bytes": 10,
          "source": "SCContent", "from": "9" * 32}


def test_the_devices_own_copy_proves_it_first(tmp_path):
    by_key = _entries(tmp_path, {KEY_EDITOR: [DEVICE, PROOF]})
    editor = by_key[KEY_EDITOR]
    assert editor["memory"]["snap_id"] == SNAP and editor["memory"]["by_content"] == "device"
    basis = editor["memory_basis"]
    assert "recovered it from the device" in basis and "9" * 32 in basis
    assert "Search warrant" not in basis                  # the server copy is not what proved it
    chip = cc._links_html(editor, "../", compact=True)
    assert "≡" in chip and "☁" not in chip
    detail = cc._detail_html(editor, "../", "", {})
    assert "recovered on this device" in detail and "retrieved from Snapchat" in detail


def test_a_pack_source_is_named_in_the_basis():
    rec = dict(DEVICE, source="caching-media", **{"from": "ab/" + "c" * 64})
    assert "caching-media pack ab/" in cc.content_basis(rec)


def test_a_library_caches_file_identical_to_device_media():
    entry = {"name": "x.mp4", "rel": "x.mp4", "sha256": "cd" * 32, "bytes": 10}
    links = cmr.attribute(entry, {}, {}, {}, {"url_keys": {}}, {}, {}, {},
                          content={"cd" * 32: [{k: v for k, v in DEVICE.items() if k != "sha256"}]})
    assert [(link["kind"], link["how"], link["by_content"]) for link in links] == [
        ("memory", "device", "device")]
    assert "cd" * 32 in links[0]["basis"]                 # the digest it matched on, not a blank
    assert "≡" in cmr._links_cell(dict(entry, links=links, copies=[{}]), "../")
