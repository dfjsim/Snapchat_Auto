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
    assert fb.snapchatter_names(None, UID) == {}
    assert fb.snapchatter_names(b"\x10\x00\x00\x00" + b"\xff" * 12, UID) == {}
