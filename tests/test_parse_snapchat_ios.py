import base64
import logging
import os

import pandas as pd

from scripts import ParseSnapchat_iOS as parse_snapchat_ios


def test_merge_cache_handles_mixed_media_context_dtypes(tmp_path, monkeypatch):
    cache_df = pd.DataFrame(
        {
            "CACHE_KEY": ["abc", "def"],
            "EXTERNAL_KEY": ["ext1", "ext2"],
            "MEDIA_CONTEXT_TYPE": [2, 3],
        }
    )
    content_df = pd.DataFrame(
        {
            "CACHE_KEY": ["abc", "ghi"],
            "EXTERNAL_KEY": ["ext1", "ext3"],
            "MEDIA_CONTEXT_TYPE": ["", 19],
        }
    )

    sccontent_dir = tmp_path / "sccontent"
    output_dir = tmp_path / "output"
    os.makedirs(sccontent_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGMAAA3AAwAAHw7mqgAAAABJRU5ErkJggg=="
    )
    for name in ("abc", "def", "ghi"):
        with open(sccontent_dir / name, "wb") as handle:
            handle.write(png_bytes)

    monkeypatch.setattr(parse_snapchat_ios, "SCContentFolder", str(sccontent_dir) + os.sep)
    monkeypatch.setattr(parse_snapchat_ios, "outputDir", str(output_dir))

    result = parse_snapchat_ios.mergeCache(cache_df, content_df)

    assert isinstance(result, pd.DataFrame)
    assert list(result.columns[:3]) == ["CACHE_KEY", "EXTERNAL_KEY", "MEDIA_CONTEXT_TYPE"]
    assert len(result) == 4


def _chats_frame(rows):
    """A minimal getChats-shaped frame: one row per message, as mergeCacheChats expects it."""
    return pd.DataFrame(
        [
            {
                "client_conversation_id": conv,
                "client_message_id": cmid,
                "server_message_id": smid,
                "message_content": content,
                "Creation Timestamp": "2023-02-21 18:40:50",
                "Read Timestamp": "",
                "content_type": ctype,
                "sender_id": "someone",
            }
            for conv, cmid, smid, content, ctype in rows
        ]
    )


def _empty_cache_frames():
    cache_df = pd.DataFrame(columns=["CACHE_KEY", "EXTERNAL_KEY", "MEDIA_CONTEXT_TYPE", "USER_ID"])
    cache_arroyo_df = pd.DataFrame(
        columns=["client_conversation_id", "server_message_id", "message_content", "content_type"]
    )
    return cache_df, cache_arroyo_df


def test_messages_without_a_cache_claim_are_kept_not_dropped(monkeypatch):
    """A message whose media is not in the cache must still be reported.

    mergeCacheChats used to drop any row that no CACHE_FILE_CLAIM joined onto, which silently
    removed every message whose attachment is gone — the reports then listed fewer messages than
    arroyo.db holds, while other tools showed them all.
    """
    monkeypatch.setattr(parse_snapchat_ios, "uuid", "owner-id", raising=False)
    cache_df, cache_arroyo_df = _empty_cache_frames()
    # invented ids of the same shape as the real ones — nothing identifying a test device
    # belongs in this repository
    conv = "11111111-2222-4333-8444-555555555555"
    chats_df = _chats_frame(
        [
            (conv, 9, 6, "AbCdEfGhIjKlMnOpQrStU", 0),      # media, no claim
            (conv, 10, 10, "VwXyZ01234567890AbCdE", 2),    # media, no claim
            (conv, 16, 29, "Test", 1),                     # text
            (conv, 17, 30, "whatever", 77),                # a content_type this parser cannot name
        ]
    )

    result = parse_snapchat_ios.mergeCacheChats(cache_df, chats_df, None, cache_arroyo_df)

    assert len(result) == 4, "no message row may be dropped for want of a cached file"
    types = list(result["Content Type"])
    assert types == [
        "Media (no cached file)",
        "Media (no cached file)",
        "Text",
        "Unrecognised (content_type 77)",
    ]
    # arroyo's own value survives beside the label it was derived from
    assert list(result["Content Type (arroyo)"]) == [0, 2, 1, 77]
    assert list(result["Client Message ID"]) == [9, 10, 16, 17]


def test_messages_with_no_attachment_do_not_log_a_permission_error(tmp_path, caplog, monkeypatch):
    """A message carrying no attachment must not be reported as a failure to read one.

    getChats leaves "" in Message Content for an app event it decoded but could not describe.
    Joined onto the cache folder that named the cacheFiles *directory*, which exists, so the
    identification step opened a directory: on Windows one ERROR per such message, and the row
    rendered as " missing attachment" — an attachment that never existed.
    """
    output_dir = tmp_path / "output"
    os.makedirs(output_dir / "cacheFiles", exist_ok=True)
    monkeypatch.setattr(parse_snapchat_ios, "outputDir", str(output_dir))

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        for empty in ("", "   ", ".", ".."):
            assert parse_snapchat_ios.path_to_image_html(empty) == empty

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
        "a message with no attachment is not an error"


def test_attachment_still_renders(tmp_path, monkeypatch):
    """The guard above must not stop a real cached file from being rendered."""
    output_dir = tmp_path / "output"
    cache_files = output_dir / "cacheFiles"
    os.makedirs(cache_files, exist_ok=True)
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGMAAA3AAwAAHw7mqgAAAABJRU5ErkJggg=="
    )
    name = "0123456789abcdef0123456789abcdef"
    with open(cache_files / name, "wb") as handle:
        handle.write(png_bytes)
    monkeypatch.setattr(parse_snapchat_ios, "outputDir", str(output_dir))

    result = parse_snapchat_ios.path_to_image_html(name)

    assert result.startswith('<span id="cf-' + name + '">')
    assert "<img" in result


def test_unidentifiable_cache_file_keeps_its_name_and_is_not_silent(tmp_path, caplog, monkeypatch):
    """A cached file filetype cannot identify must still be accounted for in the report.

    filetype.guess returns None for a still-encrypted cache entry, a zero-byte file or a plist.
    `kind.extension` then raised AttributeError into the catch-all at the end of the function,
    which returned None: the row rendered as nothing at all, for a file sitting in cacheFiles, and
    nothing was logged at any level to say so.
    """
    output_dir = tmp_path / "output"
    cache_files = output_dir / "cacheFiles"
    os.makedirs(cache_files, exist_ok=True)
    monkeypatch.setattr(parse_snapchat_ios, "outputDir", str(output_dir))

    unidentifiable = {
        "encrypted_blob": os.urandom(4096),
        "zero_byte": b"",
        "not_media": b"just some bytes, not media",
    }
    for name, data in unidentifiable.items():
        with open(cache_files / name, "wb") as handle:
            handle.write(data)

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        for name in unidentifiable:
            result = parse_snapchat_ios.path_to_image_html(name)
            assert result == name, "the file is on disk; the report must still name it"

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], \
        "an unrecognised file is not an error"
    for name in unidentifiable:
        assert any(name in r.message for r in caplog.records), \
            f"{name} could not be identified and nothing said so"


def test_message_content_is_always_a_string(tmp_path, monkeypatch):
    """Whatever goes in, a string comes out — "Cleaning up messages" calls .startswith on it.

    Returning None here put a NaN in the frame, and the Sticker / Video (Unknown Source) branches
    of that loop then died with AttributeError, aborting the run before any report was written.
    """
    output_dir = tmp_path / "output"
    os.makedirs(output_dir / "cacheFiles", exist_ok=True)
    monkeypatch.setattr(parse_snapchat_ios, "outputDir", str(output_dir))

    with open(output_dir / "cacheFiles" / "unreadable", "wb") as handle:
        handle.write(os.urandom(64))

    values = ["", "   ", ".", "..", "unreadable", "not-in-the-folder", float("nan"), None]
    contents = [parse_snapchat_ios.path_to_image_html(v) for v in values]
    assert all(isinstance(c, str) for c in contents), [type(c).__name__ for c in contents]

    # the loop that used to crash, verbatim
    df = pd.DataFrame({"Content Type": ["Sticker"] * len(contents), "Message Content": contents})
    for index, row in df.iterrows():
        if not row["Message Content"].startswith("<a href"):
            df = df.drop(index)
    assert df.empty
