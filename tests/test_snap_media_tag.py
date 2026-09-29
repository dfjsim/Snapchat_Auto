"""The Snapchat app's tag in a media file's description field — `scripts.data.snap_media_tag`.

Tags are built here from synthetic values, in both encodings the lens id occurs in (packed, as the
iOS app writes it; unpacked, as the Android app does). The one rule under test: the value is the tag
only when all of it parses; anything else is not a tag and must come back as ``None``, never raise.
"""
import base64
import random

import pytest

from scripts.data import snap_media_tag

IOS_UA = "Snapchat/12.0.0.1 (iPhone15,2; iOS 17.0; gzip)"
ANDROID_UA = "Snapchat/12.0.0.2 (XY-1234; Android 9#A1B2#28; gzip) V/NAME"


def _varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _len(field, payload):
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _var(field, value):
    return _varint(field << 3) + _varint(value)


def _tag(ua=IOS_UA, lens=(12345678901,), packed=True, extra=b"", outer_extra=b""):
    body = _len(1, ua.encode())
    if lens:
        if packed:
            body += _len(2, b"".join(_varint(v) for v in lens))
        else:
            body += b"".join(_var(2, v) for v in lens)
    body += extra
    return base64.b64encode(_len(1, body) + outer_extra).decode()


def test_the_ios_shape_decodes_with_a_packed_lens_id_and_field_4_as_stored():
    tag = snap_media_tag.decode(_tag(extra=_var(4, 1)))

    assert tag["user_agent"] == IOS_UA
    assert tag["app_version"] == "12.0.0.1"
    assert tag["device"] == "iPhone15,2" and tag["os"] == "iOS 17.0"
    assert tag["ua_extra"] == ["gzip"]
    assert tag["lens_ids"] == [12345678901] and tag["lens_encoding"] == "packed"
    assert tag["stored"] == [("1.4", "1 (varint)")]


def test_the_android_shape_decodes_unpacked_and_keeps_the_os_as_written():
    tag = snap_media_tag.decode(_tag(ANDROID_UA, lens=(987654321,), packed=False))

    assert tag["device"] == "XY-1234"
    assert tag["os"] == "Android 9#A1B2#28"                 # not split, not interpreted
    assert tag["ua_extra"] == ["gzip", "V/NAME"]            # text after the parentheses is kept
    assert tag["lens_ids"] == [987654321] and tag["lens_encoding"] == "unpacked"
    assert tag["stored"] == []


def test_several_lens_ids_and_a_negative_int64_are_kept():
    tag = snap_media_tag.decode(_tag(lens=(1, (1 << 64) - 5)))
    assert tag["lens_ids"] == [1, -5]


def test_a_tag_without_a_lens_is_still_a_tag():
    tag = snap_media_tag.decode(_tag(lens=()))
    assert tag["lens_ids"] == [] and tag["lens_encoding"] == ""


def test_unknown_fields_at_either_level_are_shown_as_stored():
    tag = snap_media_tag.decode(_tag(extra=_len(9, b"\x00\xff"), outer_extra=_len(3, b"hello")))
    assert ("1.9", "2 bytes: 00ff") in tag["stored"]
    assert ("3", "hello") in tag["stored"]


def test_surrounding_whitespace_and_a_nul_terminator_are_trimmed():
    text = _tag()
    tag = snap_media_tag.decode(f"  {text}\x00\n")
    assert tag["encoded"] == text


def test_ascii_bytes_are_accepted():
    assert snap_media_tag.decode(_tag().encode())["device"] == "iPhone15,2"


def _raw(payload):
    return base64.b64encode(payload).decode()


@pytest.mark.parametrize("text", [
    None, 42, "", "short",
    "This is an ordinary caption, written by a person.",
    _tag()[:-4],                                             # cut: bad padding / truncated message
    _tag() + "AA==",                                         # trailing bytes after the message
    _tag()[:5] + "-" + _tag()[6:],                           # not the standard base64 alphabet
    _raw(b"\x0a\x40" + b"x" * 20),                           # length runs past the end
    _raw(_len(1, _len(1, IOS_UA.encode())) + b"\x08"),       # truncated varint at the top level
    _raw(_len(1, _len(1, IOS_UA.encode()) + b"\x0b")),       # wire type 3 (group start)
    _raw(_len(1, _len(1, IOS_UA.encode()) + b"\x0c")),       # wire type 4 (group end)
    _raw(_len(1, b"\x02\x01" + _len(1, IOS_UA.encode()))),   # field number 0
    _raw(_len(1, _len(1, b"Mozilla/5.0 (X11; Linux)"))),     # not the Snapchat user agent
    _raw(_len(1, _len(1, b"Snapchat/1.0 (\xff\xfe; x)"))),   # not UTF-8
    _raw(_len(1, _len(1, b"Snapchat/1.0 no parentheses"))),
    _raw(_len(1, _var(1, 5))),                               # 1.1 is a number, not text
    _raw(_var(1, 5) + b"0000"),                              # 1 is not a message
    _raw(_len(1, _len(1, IOS_UA.encode())) * 2),             # two field-1 messages
    _raw(b"\x00" * 30),
])
def test_anything_else_is_not_a_tag(text):
    assert snap_media_tag.decode(text) is None


def test_a_field_over_the_size_bound_is_not_read():
    assert snap_media_tag.decode("A" * (snap_media_tag._MAX_TEXT + 4)) is None


def test_random_bytes_never_raise():
    rng = random.Random(20260924)
    for _ in range(3000):
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 120)))
        snap_media_tag.decode(base64.b64encode(blob).decode())
        snap_media_tag.decode(blob)
        snap_media_tag.fields(blob)


def test_fields_accounts_for_every_byte_or_returns_none():
    assert snap_media_tag.fields(_var(1, 150) + _len(2, b"ab")) == [(1, 0, 150), (2, 2, b"ab")]
    assert snap_media_tag.fields(_var(1, 150) + b"\x80") is None
    assert snap_media_tag.fields(b"") == []
