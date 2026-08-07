"""Media recovered for a Memory that has no usable key.

Locating a cache file needs no key — only reading an encrypted one does. A Memory whose key is
still wrapped (My Eyes Only without the account's persistedkey) can still have its media cached on
disk **in the clear**, and it is then fully recoverable. Those Memories used to be filtered out of
the media phase entirely, so such a Memory showed "no cached media" — and no link to its
cache_controller entry — while the cache_controller report happily played the very same bytes.

Every input here is synthetic. No extraction data is required or used.
"""
import hashlib
import os

from Crypto.Cipher import AES

from scripts import memories_media_report as memories_report

TOKEN = "EXAMPLETOKEN0123456AB"          # shaped like a CDN token; not one from any extraction
URL = f"https://cf-st.sc-cdn.net/d/{TOKEN}?bo=EXAMPLEBO&uc=00"
CACHE_KEY = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4 + b"mp42isom" + b"\x55" * 4000
USER = "11111111-2222-3333-4444-555555555555"


def _app(tmp_path):
    d = tmp_path / "app" / "Documents" / f"com.snap.file_manager_3_SCContent_{USER}"
    d.mkdir(parents=True)
    return str(tmp_path / "app"), d


def _memory(**over):
    """A My Eyes Only Memory whose key is still wrapped: is_meo, but no key at all."""
    m = {"snap_id": "SNAP-1", "user_hash": "aa" * 32, "key": None, "iv": None, "is_meo": True,
         "key_wrapped": True, "key_source": None, "media_url": URL, "overlay_url": None,
         "thumb_url": None, "width": 720, "height": 1280, "media_files": []}
    m.update(over)
    return {"SNAP-1": m}


def test_plaintext_cache_is_recovered_without_a_key(tmp_path):
    app, scdir = _app(tmp_path)
    (scdir / CACHE_KEY).write_bytes(MP4)
    mems = _memory()
    memories_report.collect_media(mems, app, str(tmp_path / "out"))
    files = mems["SNAP-1"]["media_files"]
    assert len(files) == 1 and files[0]["ext"] == "mp4"
    assert os.path.getsize(os.path.join(str(tmp_path / "out"), files[0]["out"])) == len(MP4)


def test_the_basis_does_not_claim_a_decryption_that_never_happened(tmp_path):
    app, scdir = _app(tmp_path)
    (scdir / CACHE_KEY).write_bytes(MP4)
    mems = _memory()
    memories_report.collect_media(mems, app, str(tmp_path / "out"))
    how = mems["SNAP-1"]["media_files"][0]["how"]
    assert "stored in the clear" in how and "Decrypted with" not in how


def test_plaintext_cache_split_into_byte_range_parts_is_reassembled(tmp_path):
    """The reported case: TYPE=2 (sharded), seven parts, playable from the cache report."""
    app, scdir = _app(tmp_path)
    cut = 1024
    (scdir / f"{CACHE_KEY}_0-{cut - 1}").write_bytes(MP4[:cut])
    (scdir / f"{CACHE_KEY}_{cut}-{len(MP4) - 1}").write_bytes(MP4[cut:])
    mems = _memory()
    memories_report.collect_media(mems, app, str(tmp_path / "out"))
    files = mems["SNAP-1"]["media_files"]
    assert len(files) == 1 and files[0]["ext"] == "mp4"
    assert "rebuilt from 2 parts" in files[0]["source"]


def test_an_encrypted_cache_stays_unrecovered_without_a_key(tmp_path):
    """The other half of the rule: no key, not plaintext — nothing is produced, and nothing is
    guessed at from the file merely being there."""
    app, scdir = _app(tmp_path)
    block_aligned = MP4[:len(MP4) // 16 * 16]
    cipher = AES.new(bytes(range(32)), AES.MODE_CBC, bytes(range(16))).encrypt(block_aligned)
    (scdir / CACHE_KEY).write_bytes(cipher)
    mems = _memory()
    memories_report.collect_media(mems, app, str(tmp_path / "out"))
    assert mems["SNAP-1"]["media_files"] == []


def test_a_shard_set_missing_its_first_range_is_not_read_as_media(tmp_path):
    """Without the offset-0 shard nothing can say what the file is, so it is left alone rather
    than judged on whatever byte range happens to be cached."""
    app, scdir = _app(tmp_path)
    (scdir / f"{CACHE_KEY}_1024-{len(MP4) - 1}").write_bytes(MP4[1024:])
    mems = _memory()
    memories_report.collect_media(mems, app, str(tmp_path / "out"))
    assert mems["SNAP-1"]["media_files"] == []


def test_the_locked_meo_notice_stops_implying_the_media_is_unreadable():
    member = {"snap_id": "SNAP-1", "user_hash": "aa" * 32, "key": None, "iv": None,
              "is_meo": True, "key_wrapped": True, "key_source": None,
              "media_files": [{"out": "x.mp4"}]}
    out = memories_report._enc_html([member], {}, meo_owners=[])
    assert "recovered even so" in out
    member["media_files"] = []
    assert "may still be present on disk" in memories_report._enc_html([member], {}, meo_owners=[])
