"""
A minimal FlatBuffers root-table reader for Snapchat's ``*.docobjects`` stores.

Every ``p`` column in ``primary.docobjects`` is a FlatBuffers document: a root offset, a table
whose first word points back to its vtable, and in the vtable one 16-bit field offset per slot.
No schema for these documents is published, so this reads **only** what a root table can be read
without one — the string a given slot points at — and returns ``""`` on any structural mismatch
rather than guessing.

The ``snapchatter`` document's slot layout was established by iLEAPP (Alexis Brignoni,
``scripts/artifacts/snapchat.py``, MIT): slot 0 is the user id (equal to the row's ``userId``
column), slot 2 the display name, slots 1, 14 and 15 the username, mutable username and legacy
username (equal to the store's own index tables). That layout is not assumed here: a name is only
handed back when slot 0 equals the user id the caller already knows, the same self-check iLEAPP
applies, so a document of another shape yields nothing instead of a wrong name.

The creative-tools item documents (``ctp__item_5.p``, :mod:`scripts.data.ctp_items`) are read with
the same discipline, one level deeper: :func:`table_field` follows a slot to a sub-table, whose own
slots :func:`string_field` and :func:`bytes_field` read when handed its position (``table=``), and
:func:`bytes_field` reads a ``[ubyte]`` vector — the bytes of a message embedded in the document. A
document is only read that way behind the same self-check: its slot 0 must be the row's ``item_id``.
"""

import struct

SLOT_USER_ID = 0
SLOT_USERNAME = 1
SLOT_DISPLAY_NAME = 2
SLOT_MUTABLE_USERNAME = 14
SLOT_LEGACY_USERNAME = 15

_READ_ERRORS = (struct.error, IndexError, TypeError, UnicodeDecodeError, ValueError)


def _root(buf):
    """The position of the root table: the buffer's first word."""
    return struct.unpack_from("<I", buf, 0)[0]


def _field_pos(buf, table_pos, slot):
    """The position of ``slot``'s field in the table at ``table_pos``, or None when the slot lies past
    the table's vtable or the field is absent. Raises on a buffer too short to hold what it points at
    (the callers catch it): a position outside the buffer is never read as a wrapped-around one."""
    if not 0 <= table_pos < len(buf):
        raise ValueError("a table outside the buffer")
    vtable_pos = table_pos - struct.unpack_from("<i", buf, table_pos)[0]
    if not 0 <= vtable_pos < len(buf):
        raise ValueError("a vtable outside the buffer")
    vtable_size = struct.unpack_from("<H", buf, vtable_pos)[0]
    if slot >= (vtable_size - 4) // 2:
        return None
    field_offset = struct.unpack_from("<H", buf, vtable_pos + 4 + slot * 2)[0]
    if field_offset == 0:
        return None
    return table_pos + field_offset


def _target(buf, field_pos):
    """Where the offset stored at ``field_pos`` points (FlatBuffers offsets are relative to it)."""
    return field_pos + struct.unpack_from("<I", buf, field_pos)[0]


def string_field(buf, slot, table=None):
    """The string in ``slot`` of the root table — or of the sub-table at position ``table`` (from
    :func:`table_field`) — or ``""`` when absent or not readable as one."""
    try:
        buf = bytes(buf)
        field_pos = _field_pos(buf, _root(buf) if table is None else table, slot)
        if field_pos is None:
            return ""
        string_pos = _target(buf, field_pos)
        length = struct.unpack_from("<I", buf, string_pos)[0]
        if string_pos + 4 + length > len(buf):
            return ""
        return buf[string_pos + 4:string_pos + 4 + length].decode("utf-8")
    except _READ_ERRORS:
        return ""


def table_field(buf, slot, table=None):
    """The position of the sub-table ``slot`` points at (in the root table, or in the table at
    ``table``), or None when the field is absent or what it points at is not a table: its vtable must
    lie inside the buffer, be at least the 4 bytes of its own header, and describe a table that does
    too."""
    try:
        buf = bytes(buf)
        field_pos = _field_pos(buf, _root(buf) if table is None else table, slot)
        if field_pos is None:
            return None
        sub = _target(buf, field_pos)
        if not 0 <= sub < len(buf):
            return None
        vtable_pos = sub - struct.unpack_from("<i", buf, sub)[0]
        if not 0 <= vtable_pos <= len(buf) - 4:
            return None
        vtable_size, table_size = struct.unpack_from("<HH", buf, vtable_pos)
        if (vtable_size < 4 or vtable_size % 2 or vtable_pos + vtable_size > len(buf)
                or table_size < 4 or sub + table_size > len(buf)):
            return None
        return sub
    except _READ_ERRORS:
        return None


def bytes_field(buf, slot, table=None):
    """The bytes of the ``[ubyte]`` vector in ``slot`` (of the root table, or of the table at
    ``table``), or None when the field is absent or its length runs past the buffer."""
    try:
        buf = bytes(buf)
        field_pos = _field_pos(buf, _root(buf) if table is None else table, slot)
        if field_pos is None:
            return None
        vector_pos = _target(buf, field_pos)
        length = struct.unpack_from("<I", buf, vector_pos)[0]
        if vector_pos + 4 + length > len(buf):
            return None
        return buf[vector_pos + 4:vector_pos + 4 + length]
    except _READ_ERRORS:
        return None


def snapchatter_names(blob, user_id):
    """``{"display_name", "username", "mutable_username", "legacy_username"}`` from a
    ``snapchatter.p`` document, or ``{}`` when its slot 0 is not ``user_id``.

    The self-check is the whole guarantee: it ties the layout to this row. The username fields
    duplicate the store's index tables and are returned so a caller can confirm they agree.
    """
    if not blob or not user_id or string_field(blob, SLOT_USER_ID) != user_id:
        return {}
    return {"display_name": string_field(blob, SLOT_DISPLAY_NAME),
            "username": string_field(blob, SLOT_USERNAME),
            "mutable_username": string_field(blob, SLOT_MUTABLE_USERNAME),
            "legacy_username": string_field(blob, SLOT_LEGACY_USERNAME)}


def string_vector_field(buf, slot=0):
    """The vector of strings in root-table ``slot``, or ``None`` when the buffer is not one.

    Every offset is followed rather than assumed: with more than one string the strings are not laid
    out in order at fixed positions, so reading "the text at byte 0x20" (which works for a
    one-element vector) returns the wrong bytes for any other. ``None`` on any structural mismatch.
    """
    try:
        buf = bytes(buf)
        table_pos = struct.unpack_from("<I", buf, 0)[0]
        vtable_pos = table_pos - struct.unpack_from("<i", buf, table_pos)[0]
        vtable_size = struct.unpack_from("<H", buf, vtable_pos)[0]
        if slot >= (vtable_size - 4) // 2:
            return None
        field_offset = struct.unpack_from("<H", buf, vtable_pos + 4 + slot * 2)[0]
        if field_offset == 0:
            return []
        field_pos = table_pos + field_offset
        vector_pos = field_pos + struct.unpack_from("<I", buf, field_pos)[0]
        count = struct.unpack_from("<I", buf, vector_pos)[0]
        if count > 100000 or vector_pos + 4 + 4 * count > len(buf):
            return None
        out = []
        for n in range(count):
            element_pos = vector_pos + 4 + 4 * n
            string_pos = element_pos + struct.unpack_from("<I", buf, element_pos)[0]
            length = struct.unpack_from("<I", buf, string_pos)[0]
            if string_pos + 4 + length > len(buf):
                return None
            out.append(buf[string_pos + 4:string_pos + 4 + length].decode("utf-8"))
        return out
    except (struct.error, IndexError, TypeError, UnicodeDecodeError, ValueError):
        return None


# The snapchatters__displaymetadata document: slot 0 is again the user id, and slot 1 the display
# name — the string the byte-offset carve this replaced used to find at offset 56 (it agreed on
# every row of three stores; slot 2 held the same string and slot 3 its first letter, neither
# relied on).
SLOT_DM_DISPLAY_NAME = 1


def displaymetadata_name(blob, user_id):
    """The display name in a ``snapchatters__displaymetadata.p`` document, or ``""`` when the
    document's slot 0 is not ``user_id``."""
    if not blob or not user_id or string_field(blob, SLOT_USER_ID) != user_id:
        return ""
    return string_field(blob, SLOT_DM_DISPLAY_NAME)
