"""Synthetic creative-tools item stores (``primary.docobjects`` › ``ctp__item_5``) for the item tests.

:func:`flatbuffer` writes a FlatBuffers buffer the way the format lays one out — a root offset, and per
table a vtable of 16-bit field offsets, the table's offset back to it, and its fields, each offset
field pointing forward to its string, ``[ubyte]`` vector or sub-table — from plain values: a
:class:`Table` of ``{slot: value}``. :func:`message` writes a protobuf message from ``(field, value)``
pairs. :func:`item_doc` and :func:`tree_doc` build the two documents the store holds, and
:func:`store` an account's store in an app folder. Every value is invented: example.net hosts,
made-up ids.
"""
import base64
import hashlib
import os
import sqlite3
import struct

import overlay_fixture as ofx

USER = "11111111-2222-4333-8444-555555555555"     # the account whose store and claims these are
HASH = hashlib.sha256(USER.encode()).hexdigest()   # its userHash: the store's folder name
OTHER_USER = "22222222-3333-4444-8555-666666666666"
OTHER_HASH = hashlib.sha256(OTHER_USER.encode()).hexdigest()

FILTER_IMAGE = "https://geofilter.example.net/png/synthetic-filter.png"
FILTER_CDN = "https://cf-st.example.net/d/SyntheticAsset1?mo=QUJD%3D&uc=7"
FONT_URL = "https://fonts.example.net/geofilter-fonts/open/Synthetic.ttf"
MUSIC_URL = "https://cf-st.example.net/d/SyntheticTrack1?bo=Q0FFU0FB&uc=37"
SHARED_BO = "Q0FFU0FB"                              # one bo= value, in an item URL and in others

OWN = b"\x5a\x1f\xfb\x10synth"                      # an item's own id: 9 bytes, '+'/'/' in base64
OWN_ID = base64.b64encode(OWN).decode()
STICKER = b"\xfb\xff\x0fsticker\x01\x02\x03"        # a custom sticker's 13-byte id
STICKER_ID = base64.b64encode(STICKER).decode()     # standard alphabet, padded ('/', '=')
STICKER_URLSAFE = base64.urlsafe_b64encode(STICKER).decode().rstrip("=")

FILTERS_ENDPOINT = "/snapchat.creativetools.filters.ComputeFeedService/ComputeFeed"
STICKERS_ENDPOINT = "/snapchat.creativetools.custom-stickers.ComputeFeedService/ComputeFeed"


class Table:
    """A FlatBuffers table: ``{slot: value}``, each value ``("str", text)``, ``("bytes", data)``,
    ``("u8", n)``, ``("i64", n)``, ``("f64", x)`` or a nested :class:`Table`."""

    def __init__(self, fields):
        self.fields = fields


def flatbuffer(root):
    """``root`` (a :class:`Table`) as a FlatBuffers buffer."""
    buf = bytearray(4)
    struct.pack_into("<I", buf, 0, _write_table(buf, root))
    return bytes(buf)


def _write_table(buf, table):
    slots = sorted(table.fields)
    nslots = slots[-1] + 1 if slots else 0
    offsets = {slot: 4 + 8 * k for k, slot in enumerate(slots)}
    vtable_pos = len(buf)
    buf += struct.pack("<HH", 4 + 2 * nslots, 4 + 8 * len(slots))
    buf += b"".join(struct.pack("<H", offsets.get(slot, 0)) for slot in range(nslots))
    buf += b"\x00" * (-len(buf) % 4)
    table_pos = len(buf)
    buf += struct.pack("<i", table_pos - vtable_pos) + b"\x00" * (8 * len(slots))
    pending = []
    for slot in slots:
        value, pos = table.fields[slot], table_pos + offsets[slot]
        if isinstance(value, Table):
            pending.append((pos, value))
        elif value[0] in ("u8", "i64", "f64"):
            struct.pack_into({"u8": "<B", "i64": "<q", "f64": "<d"}[value[0]], buf, pos, value[1])
        else:
            pending.append((pos, value))
    for pos, value in pending:
        if isinstance(value, Table):
            target = _write_table(buf, value)
        else:
            data = value[1].encode("utf-8") if value[0] == "str" else bytes(value[1])
            target = len(buf)
            buf += struct.pack("<I", len(data)) + data + (b"\x00" if value[0] == "str" else b"")
            buf += b"\x00" * (-len(buf) % 4)
        struct.pack_into("<I", buf, pos, target - pos)
    return table_pos


def _varint(n):
    out = bytearray()
    while True:
        low, n = n & 0x7F, n >> 7
        out.append(low | (0x80 if n else 0))
        if not n:
            return bytes(out)


def message(*pairs):
    """A protobuf message of ``(field, value)`` pairs: an int is a varint, ``str``/``bytes`` a
    length-delimited value, a ``list`` of pairs a nested message."""
    out = b""
    for number, value in pairs:
        if isinstance(value, int):
            out += _varint(number << 3) + _varint(value)
            continue
        if isinstance(value, list):
            value = message(*value)
        elif isinstance(value, str):
            value = value.encode("utf-8")
        out += _varint(number << 3 | 2) + _varint(len(value)) + value
    return out


def filter_payload(own=OWN, image=FILTER_IMAGE, cdn=FILTER_CDN, font=FONT_URL):
    """A payload of the filters feed's kind (field 2.16), its asset URLs where the corpus keeps them."""
    body = [(2, [(1, [(1, image), (2, cdn)])])]
    if font:
        # each level carries a number too: a message of one text alone reads as text itself
        body.append((7, [(1, [(5, [(1, font), (2, 1)])])]))
    return message((2, [(16, body)]), (4, int.from_bytes(own[:8], "little")), (6, own))


def item_doc(item_id, payload, own_id=None, feed="feed:17-2-0", slot0=None):
    """A ``ctp__item_5.p`` document: slot 0 the item_id (or ``slot0``), slot 1 a rank text, slot 2 the
    payload, slot 3 the own id, slot 4 the feed's sub-table, slots 5 and 6 small integers."""
    fields = {0: ("str", item_id if slot0 is None else slot0), 1: ("str", "000000000000001"),
              2: ("bytes", payload), 4: Table({0: ("str", feed), 1: ("u8", 1)}), 5: ("u8", 17),
              6: ("u8", 2)}
    if own_id is not None:
        fields[3] = ("str", own_id)
    return flatbuffer(Table(fields))


def feed(kind, context, name="", endpoint=None, children=None):
    """One ``CTPFeed`` of a feed tree."""
    fields = {"FEED_ID": ofx.Obj("CTPFeedIdentifiers", TYPE=kind, CONTEXT=context), "NAME": name,
              "MEDIA_CONTENT": None, "CHILD_FEEDS": children}
    fields["SOURCE"] = (ofx.Obj("CTPFeedSource", COMPUTE_ENDPOINT=endpoint) if endpoint else None)
    return ofx.Obj("CTPFeed", **fields)


def tree_doc(root=None, archive=None):
    """A ``ctp__feedtree.p`` document: slot 0 the context, slot 1 a time, slot 2 the archive."""
    if archive is None:
        root = root or feed(10, 2, "Preview", children=[
            feed(17, 2, endpoint=FILTERS_ENDPOINT),
            feed(11, 2, "StickerPicker", children=[feed(4, 2, endpoint=STICKERS_ENDPOINT),
                                                   feed(7, 2, "Emoji")])])
        archive = ofx.archive(root)
    return flatbuffer(Table({0: ("i64", 2), 1: ("f64", 700000000.0), 2: ("bytes", archive)}))


def store(app, items, user_hash=HASH, tree=True, tables=True, wal_changes=None):
    """An account's ``primary.docobjects`` in ``app``: ``items`` is ``[(item_id, document)]``.

    ``tree`` is the context-2 feed tree (:func:`tree_doc`'s by default, a document, or ``{context:
    document}``); ``wal_changes`` is a function of a connection run after the checkpoint, whose writes
    only the ``-wal`` holds. Returns the store's path.
    """
    folder = os.path.join(app, "Documents", "user_scoped", user_hash, "DocObjects")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "primary.docobjects")
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=wal")
    conn.execute("create table snapchatter (rowid integer primary key autoincrement, p blob, "
                 "userId string, unique (userId))")
    if tables:
        conn.execute("create table ctp__item_5 (rowid integer primary key autoincrement, p blob, "
                     "item_id string, unique (item_id))")
        conn.execute("create table ctp__feedtree (rowid integer primary key autoincrement, p blob, "
                     "context integer, unique (context))")
        conn.executemany("insert into ctp__item_5 (p, item_id) values (?, ?)",
                         [(doc, item_id) for item_id, doc in items])
        trees = tree if isinstance(tree, dict) else (
            {2: tree_doc() if tree is True else tree} if tree else {})
        conn.executemany("insert into ctp__feedtree (p, context) values (?, ?)",
                         [(doc, context) for context, doc in trees.items()])
    conn.commit()
    conn.close()
    if wal_changes is not None:
        checkpointed = open(path, "rb").read()
        conn = sqlite3.connect(path)
        wal_changes(conn)
        conn.commit()
        wal = open(path + "-wal", "rb").read()
        conn.close()
        with open(path, "wb") as fh:
            fh.write(checkpointed)
        with open(path + "-wal", "wb") as fh:
            fh.write(wal)
    if os.path.exists(path + "-shm"):
        os.remove(path + "-shm")
    return path


def filter_item(own=OWN, feed_id="feed:17-2-0", **payload):
    """``(item_id, document)`` of a filters-feed item whose item_id is ``<own id>-<feed>-0``."""
    own_id = base64.b64encode(own).decode()
    item_id = f"{own_id}-{feed_id}-0"
    return item_id, item_doc(item_id, filter_payload(own, **payload), own_id, feed_id)
