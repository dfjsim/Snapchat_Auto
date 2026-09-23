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
"""

import struct

SLOT_USER_ID = 0
SLOT_USERNAME = 1
SLOT_DISPLAY_NAME = 2
SLOT_MUTABLE_USERNAME = 14
SLOT_LEGACY_USERNAME = 15


def string_field(buf, slot):
    """The string in root-table ``slot``, or ``""`` when absent or not readable as one."""
    try:
        buf = bytes(buf)
        table_pos = struct.unpack_from("<I", buf, 0)[0]
        vtable_pos = table_pos - struct.unpack_from("<i", buf, table_pos)[0]
        vtable_size = struct.unpack_from("<H", buf, vtable_pos)[0]
        if slot >= (vtable_size - 4) // 2:
            return ""
        field_offset = struct.unpack_from("<H", buf, vtable_pos + 4 + slot * 2)[0]
        if field_offset == 0:
            return ""
        field_pos = table_pos + field_offset
        string_pos = field_pos + struct.unpack_from("<I", buf, field_pos)[0]
        length = struct.unpack_from("<I", buf, string_pos)[0]
        if string_pos + 4 + length > len(buf):
            return ""
        return buf[string_pos + 4:string_pos + 4 + length].decode("utf-8")
    except (struct.error, IndexError, TypeError, UnicodeDecodeError, ValueError):
        return ""


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
