"""The FlatBuffers root-table reader behind the ``*.docobjects`` display names.

A root table is built by hand here — root offset, vtable, table, strings — so the reader is
checked against the format rather than against a blob copied from a device. The property that
matters is the self-check: a name comes back only when slot 0 is the user id the caller already
knows, so a document of another shape yields nothing instead of a wrong name.

Every input is synthetic.
"""
import struct

from scripts.data import flatbuffers_doc as fb


def build(fields, nslots=16):
    """A FlatBuffers buffer whose root table has string ``fields`` (``{slot: text}``)."""
    vtable = struct.pack("<HH", 4 + 2 * nslots, 4 + 4 * nslots)
    vtable += b"".join(struct.pack("<H", 4 + 4 * slot if slot in fields else 0)
                       for slot in range(nslots))
    vtable_pos = 4
    table_pos = vtable_pos + len(vtable)
    strings, positions = b"", {}
    strings_start = table_pos + 4 + 4 * nslots
    for slot, text in fields.items():
        positions[slot] = strings_start + len(strings)
        encoded = text.encode("utf-8")
        strings += struct.pack("<I", len(encoded)) + encoded + b"\x00"
    table = struct.pack("<i", table_pos - vtable_pos)
    for slot in range(nslots):
        if slot in fields:
            table += struct.pack("<I", positions[slot] - (table_pos + 4 + 4 * slot))
        else:
            table += b"\x00\x00\x00\x00"
    return struct.pack("<I", table_pos) + vtable + table + strings


UID = "11111111-2222-4333-8444-555555555555"


def test_string_fields_read_by_slot():
    buf = build({0: UID, 1: "alice", 2: "Alice A. 😀", 14: "alice", 15: "alice_old"})
    assert fb.string_field(buf, 0) == UID
    assert fb.string_field(buf, 2) == "Alice A. 😀"
    assert fb.string_field(buf, 3) == ""                        # slot present, field absent
    assert fb.string_field(buf, 40) == ""                       # beyond the vtable


def test_snapchatter_names_only_when_slot_0_is_the_row_id():
    buf = build({0: UID, 1: "alice", 2: "Alice", 14: "alice", 15: "alice_old"})
    assert fb.snapchatter_names(buf, UID) == {"display_name": "Alice", "username": "alice",
                                              "mutable_username": "alice",
                                              "legacy_username": "alice_old"}
    assert fb.snapchatter_names(buf, "another-id") == {}
    assert fb.snapchatter_names(build({0: "x", 1: "alice"}), UID) == {}


def test_displaymetadata_name_is_slot_1_behind_the_same_check():
    buf = build({0: UID, 1: "Alice", 2: "Alice", 3: "A"}, nslots=4)
    assert fb.displaymetadata_name(buf, UID) == "Alice"
    assert fb.displaymetadata_name(buf, "other") == ""


def test_garbage_reads_as_nothing():
    for junk in (b"", b"\x00", b"\xff" * 64, bytes(range(256)), None):
        assert fb.string_field(junk, 0) == "" if junk is not None else True
        assert fb.bytes_field(junk, 0) is None and fb.table_field(junk, 0) is None
    assert fb.snapchatter_names(None, UID) == {}
    assert fb.snapchatter_names(b"\x10\x00\x00\x00" + b"\xff" * 12, UID) == {}


# --------------------------------------------------------------------------- one level deeper

def _nested(vector=b"\x08\x01\x12\x03abc"):
    """The shape of a creative-tools item document: a string in slot 0, a ``[ubyte]`` vector in
    slot 2, a sub-table in slot 4 whose slot 0 is a string, a byte in slot 5."""
    import ctp_fixture
    return ctp_fixture.flatbuffer(ctp_fixture.Table({
        0: ("str", UID), 2: ("bytes", vector),
        4: ctp_fixture.Table({0: ("str", "feed:17-2-0"), 1: ("u8", 3)}), 5: ("u8", 17)}))


def test_a_vector_and_a_sub_table_are_read_by_slot():
    buf = _nested()
    assert fb.bytes_field(buf, 2) == b"\x08\x01\x12\x03abc"
    sub = fb.table_field(buf, 4)
    assert sub is not None and fb.string_field(buf, 0, table=sub) == "feed:17-2-0"
    assert fb.string_field(buf, 1, table=sub) == ""            # the sub-table's byte is no string
    assert fb.string_field(buf, 0) == UID                       # without table=: the root, as before
    for absent in (1, 3, 40):                                   # not set, or past the vtable
        assert fb.bytes_field(buf, absent) is None and fb.table_field(buf, absent) is None
    assert fb.bytes_field(_nested(b""), 2) == b""               # an empty vector is one


def test_a_vector_or_a_sub_table_that_does_not_hold_together_is_nothing():
    import ctp_fixture
    tail = ctp_fixture.flatbuffer(ctp_fixture.Table({2: ("bytes", b"x" * 20)}))
    assert fb.bytes_field(tail, 2) == b"x" * 20
    assert fb.bytes_field(tail[:-6], 2) is None                 # its length runs past the buffer
    buf = bytearray(_nested())
    sub = fb.table_field(bytes(buf), 4)
    struct.pack_into("<i", buf, sub, 1 << 20)                   # its vtable outside the buffer
    assert fb.table_field(bytes(buf), 4) is None
    assert fb.string_field(bytes(buf), 0, table=sub) == ""
