"""The schema-less protobuf reader every decode in the project shares.

``strings`` is what the chat parser's ``proto_to_msg`` reads every text value of a message with: a
length-delimited value is text when it is printable UTF-8 (line breaks and tabs allowed), a nested
message when it parses as one to its last byte, and skipped otherwise (raw ids, packed numbers).

Every input is synthetic.
"""
import pytest

from scripts.data import protobuf_wire as pw


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _f(number, value):
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def test_fields_and_paths():
    msg = _f(1, 5) + _f(2, _f(3, b"abc") + _f(3, b"def")) + _f(4, b"\x00\x01\x02\x03\x04\x05\x06")
    assert pw.fields(msg)[0] == (1, 0, 5)
    assert pw.values(msg, 2, 3) == [b"abc", b"def"]
    with pytest.raises(pw.Malformed):
        pw.values(msg, 1, 1)                                # field 1 is a number, not a message
    with pytest.raises(pw.Malformed):
        pw.fields(msg[:-1])


def test_strings_depth_first_in_field_order():
    inner = _f(1, "nested text".encode()) + _f(2, 7)
    msg = _f(1, "first".encode()) + _f(2, inner) + _f(3, "last line\nsecond".encode())
    assert pw.strings(msg) == ["first", "nested text", "last line\nsecond"]


def test_bytes_that_are_not_text_or_a_message_are_skipped():
    uuid_bytes = bytes.fromhex("9f1e2d3c4b5a69788796a5b4c3d2e1f0")
    msg = _f(1, uuid_bytes) + _f(2, "\x01".encode()) + _f(3, "kept".encode())
    assert pw.strings(msg) == ["kept"]


def test_a_short_word_is_text_even_when_it_would_parse():
    """"hi" is 0x68 0x69: field 13, wire type 0, value 105 — a valid message. Without a schema the
    printable reading is the one taken."""
    assert pw.fields(b"hi") == [(13, 0, 105)]
    assert pw.strings(_f(1, b"hi")) == ["hi"]


def test_not_a_message_at_all():
    assert pw.strings(b"\xff\xff") is None
    assert pw.strings(b"") == []
