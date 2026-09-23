"""The Android Memories report: where a snap's media is found, and every key it is tried with.

All values are invented. Each test builds the synthetic app folder (android_fixture) and changes the
one thing it is about; the acceptance test for every route is the same — the bytes that come out are
the media that went in, which no wrong key and no wrong file can produce.
"""
import base64
import hashlib
import os
import sqlite3
import struct

from Crypto.Cipher import AES

import android_fixture as fx
from scripts import android_layout
from scripts import memories_android_report as memories
from scripts.data import flatbuffers_doc


def _pkcs7(data):
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _layout(tmp_path):
    fx.build_app(str(tmp_path))
    return android_layout.discover(str(tmp_path))[0]


def _run(layout, out):
    loaded, _info, extra = memories.load_memories(layout.db("memories"))
    memories.collect_media(loaded, layout.app, str(out), file_manager=layout.file_manager,
                           meo_rows=extra.get("meo") or [])
    return loaded[fx.SNAP_ID]


def _remove_native_copy(layout):
    """Leave the app's own file_manager cache as the only place the media is."""
    sc = layout.sccontent_dirs[0]
    os.remove(os.path.join(sc, fx.MEM_KEY))


def test_the_app_cache_file_is_found_by_the_md5_of_its_request_string(tmp_path):
    layout = _layout(tmp_path)
    _remove_native_copy(layout)
    digest = hashlib.md5(f"{fx.MEDIA_ID}.media".encode()).hexdigest().upper()
    folder = os.path.join(layout.app, "files", "file_manager", "memories_media")
    os.makedirs(folder)
    with open(os.path.join(folder, f"{digest}.media.0"), "wb") as fh:
        fh.write(fx.JPEG)                                   # the app keeps it decrypted
    layout = android_layout.discover(str(tmp_path))[0]
    m = _run(layout, tmp_path / "out")
    published = [f for f in m["files"] if f.get("path")]
    assert len(published) == 1
    f = published[0]
    assert f["role"] == "full media" and f["state"] == "plaintext in the cache"
    assert digest in f["basis"] and f["source"] == "files/file_manager/memories_media"
    assert (tmp_path / "out" / f["path"]).read_bytes() == fx.JPEG


def test_a_file_named_after_the_snap_is_found_too(tmp_path):
    layout = _layout(tmp_path)
    _remove_native_copy(layout)
    folder = os.path.join(layout.app, "files", "file_manager", "memories_overlay")
    os.makedirs(folder)
    with open(os.path.join(folder, f"{fx.SNAP_ID}.overlay"), "wb") as fh:
        fh.write(fx.JPEG)
    layout = android_layout.discover(str(tmp_path))[0]
    m = _run(layout, tmp_path / "out")
    assert [f["role"] for f in m["files"] if f.get("path")] == ["named after this snap"]


def test_a_thumbnail_package_is_split_into_its_images(tmp_path):
    layout = _layout(tmp_path)
    digest = hashlib.md5(f"{fx.SNAP_ID}.thumbnail".encode()).hexdigest().upper()
    folder = os.path.join(layout.app, "files", "file_manager", "memories_thumbnail")
    os.makedirs(folder)
    second = fx.JPEG[:-2] + b"\x01\xff\xd9"
    package = struct.pack("<3I", 2, len(fx.JPEG), len(second)) + fx.JPEG + second
    with open(os.path.join(folder, f"{digest}.thumbnail.0"), "wb") as fh:
        fh.write(package)
    layout = android_layout.discover(str(tmp_path))[0]
    m = _run(layout, tmp_path / "out")
    thumbs = [f for f in m["files"] if f["role"].startswith("thumbnail")]
    assert [f["role"] for f in thumbs] == ["thumbnail 1 of 2", "thumbnail 2 of 2"]
    assert (tmp_path / "out" / thumbs[1]["path"]).read_bytes() == second


def test_a_package_that_does_not_account_for_every_byte_is_not_cut_up():
    assert memories.split_package(struct.pack("<2I", 1, 10) + fx.JPEG) is None
    assert memories.split_package(struct.pack("<I", 0) + fx.JPEG) is None


def test_a_my_eyes_only_key_is_unwrapped_with_the_stored_master_key(tmp_path):
    layout = _layout(tmp_path)
    master, master_iv = bytes(range(100, 132)), bytes(range(200, 216))
    wrap = AES.new(master, AES.MODE_CBC, master_iv)
    enc_key = base64.b64encode(wrap.encrypt(_pkcs7(fx.MEM_AES_KEY))).decode()
    enc_iv = base64.b64encode(
        AES.new(master, AES.MODE_CBC, master_iv).encrypt(_pkcs7(fx.MEM_AES_IV))).decode()
    conn = sqlite3.connect(layout.db("memories"))
    conn.execute("update memories_entry set is_private = 1")
    conn.execute("update memories_snap set media_key = null, media_iv = null, "
                 "encrypted_media_key = ?, encrypted_media_iv = ?", (enc_key, enc_iv))
    # stored as the app writes base64: with a trailing newline
    conn.execute("insert into memories_meo_confidential values (?,?,?,?)",
                 ("dummy", "$2a$06$invented", base64.b64encode(master).decode() + "\n",
                  base64.b64encode(master_iv).decode() + "\n"))
    conn.commit()
    conn.close()
    m = _run(layout, tmp_path / "out")
    published = [f for f in m["files"] if f.get("path")]
    assert len(published) == 1
    assert published[0]["state"].startswith("decrypted (encrypted_media_key / encrypted_media_iv "
                                             "unwrapped with memories_meo_confidential.master_key")
    assert (tmp_path / "out" / published[0]["path"]).read_bytes() == fx.JPEG
    assert m["key_state"] == "My Eyes Only — key unwrapped"
    assert memories._state(m) == "decrypted"


def test_a_wrong_master_key_unwraps_nothing(tmp_path):
    enc = base64.b64encode(os.urandom(48)).decode()
    rows = [{"user_id": "dummy", "master_key": base64.b64encode(os.urandom(32)).decode(),
             "master_key_iv": base64.b64encode(os.urandom(16)).decode()}]
    # random bytes practically never decrypt to valid padding AND the exact key sizes
    assert memories.unwrap_meo_key(rows, enc, base64.b64encode(os.urandom(32)).decode()) is None


def test_a_key_inside_the_snapdoc_is_tried_when_the_row_has_none(tmp_path):
    layout = _layout(tmp_path)

    def sub(field, payload):
        return bytes([(field << 3) | 2, len(payload)]) + payload
    snapdoc = sub(5, sub(1, sub(1, sub(4, sub(1, fx.MEM_AES_KEY) + sub(2, fx.MEM_AES_IV)))))
    conn = sqlite3.connect(layout.db("memories"))
    conn.execute("alter table memories_snap add column snapdoc blob")
    conn.execute("update memories_snap set media_key = null, media_iv = null, snapdoc = ?",
                 (snapdoc,))
    conn.commit()
    conn.close()
    m = _run(layout, tmp_path / "out")
    published = [f for f in m["files"] if f.get("path")]
    assert published and "inside memories_snap.snapdoc" in published[0]["state"]


def _flatbuffer_strings(strings):
    """A root table whose slot 0 is a vector of strings, laid out by hand."""
    body = bytearray()
    body += struct.pack("<I", 12)                                   # root -> table at 12
    body += b"\x00\x00"                                             # pad to the vtable
    body += struct.pack("<HHH", 6, 8, 4)                            # vtable: size, table size, f0
    body += struct.pack("<i", 6)                                    # table: soffset to vtable
    body += struct.pack("<I", 4)                                    # f0 -> vector just after
    vector_pos = len(body)
    body += struct.pack("<I", len(strings))
    offsets_pos = len(body)
    body += b"\x00" * (4 * len(strings))
    for n, text in enumerate(strings):
        while len(body) % 4:
            body += b"\x00"
        string_pos = len(body)
        raw = text.encode()
        body += struct.pack("<I", len(raw)) + raw + b"\x00"
        slot = offsets_pos + 4 * n
        struct.pack_into("<I", body, slot, string_pos - slot)
    assert vector_pos == 20
    return bytes(body)


def test_snap_ids_are_read_by_following_every_offset():
    ids = [fx.SNAP_ID, "77777777-0000-4000-8000-000000000001", "88888888-0000-4000-8000-000000000002"]
    blob = _flatbuffer_strings(ids)
    assert flatbuffers_doc.string_vector_field(blob) == ids
    assert flatbuffers_doc.string_vector_field(b"not a flatbuffer") is None
