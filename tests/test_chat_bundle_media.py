"""A chat video the cache keeps as a bundle is shown with its message on iOS too, and once.

The file named after a bundle's CACHE_KEY is a small descriptor; the video is a child file. The shared
join only copies a whole file that is media, so such a message used to show "Media (no cached file)"
unless a saved copy named after its conversation, message and part stood in for it. The rebuild the
Android run already used (``scripts/chat_media.py``) now runs on iOS as well — and where the saved copy
and the cached file are the same bytes, the message shows them once and lists both names.
Every input is synthetic.
"""
from scripts import ParseSnapchat_Android as android
from scripts import chat_media
from scripts import conversations_report as conv_report

KEY = "aaaabbbbccccdddd0000111122223333"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def test_one_rebuild_for_both_platforms(tmp_path):
    assert android.materialize_chat_media is chat_media.materialize_chat_media
    assert android.chat_cache_key is chat_media.chat_cache_key
    # a bundle: the descriptor under the key, the video in a child file — rebuilt under the key
    app = tmp_path / "app"
    folder = app / "Documents" / "com.snap.file_manager_3_SCContent_synthetic"
    folder.mkdir(parents=True)
    (folder / KEY).write_bytes(b"\x0a\x22descriptor-not-media")
    (folder / f"{KEY}_zchildvideo").write_bytes(MP4)
    import pandas as pd
    claims = pd.DataFrame({"CACHE_KEY": [KEY], "EXTERNAL_KEY": ["SYNTHETIC-MEDIA-ID"]})
    made = chat_media.materialize_chat_media(claims, str(app), str(tmp_path / "rebuilt"))
    assert made == 1
    assert (tmp_path / "rebuilt" / KEY).read_bytes() == MP4
    key, note = chat_media.chat_cache_key(KEY)
    assert key == KEY and "is a bundle" in note and f"{KEY}_zchildvideo" in note


def _row(smid, name, sha, ctype):
    att = {"name": name, "sha256": sha, "rel": f"media/{name}.mp4", "cache_key": KEY,
           "cache_key_how": "", "ext": "mp4", "kind": "video", "bytes": 10, "md5": "",
           "how": "", "meta": None}
    return {"smid": smid, "atts": [att], "types": [ctype], "raw_types": [], "text": "",
            "raw_text": "", "parse_error": False, "created_unix": 1, "created_utc": "", "created": "",
            "read_utc": "", "read": "", "sender": "", "direction": "", "cmid": ""}


def test_the_same_bytes_under_two_names_are_one_attachment():
    rows = [_row("12.0", KEY, "aa" * 32, "Temporarily stored media"),
            _row("12.0", "cm-chat-media-video-saved.mov", "aa" * 32, "Media saved in chat"),
            _row("12.0", "thumbnail-file", "bb" * 32, "Thumbnail")]
    shared = rows[0]["atts"][0]                     # the published-file cache hands this to every message
    messages, folded = conv_report._merge_rows(rows)
    [msg] = messages
    assert folded == 2
    assert [a["name"] for a in msg["atts"]] == [KEY, "thumbnail-file"]
    assert [a["name"] for a in msg["atts"][0]["same_as"]] == ["cm-chat-media-video-saved.mov"]
    assert "same_as" not in shared and msg["atts"][0] is not shared
    files = [a["name"] for a in conv_report._all_files(msg["atts"])]
    assert files == [KEY, "cm-chat-media-video-saved.mov", "thumbnail-file"]
    detail = conv_report._attachment_detail(msg["atts"][0], "../")
    assert "same bytes as" in detail and "media/cm-chat-media-video-saved.mov.mp4" in detail


def test_both_readings_of_a_message_fold_to_the_same_two_files():
    """arroyo.db is read twice: a message the -wal changed comes back as two rows, each bringing
    the cached file and the saved copy."""
    rows = [_row("12.0", KEY, "aa" * 32, "Temporarily stored media"),
            _row("12.0", "saved.mov", "aa" * 32, "Media saved in chat"),
            _row("12.0", KEY, "aa" * 32, "Temporarily stored media"),
            _row("12.0", "saved.mov", "aa" * 32, "Media saved in chat")]
    [msg], _folded = conv_report._merge_rows(rows)
    assert [a["name"] for a in conv_report._all_files(msg["atts"])] == [KEY, "saved.mov"]
