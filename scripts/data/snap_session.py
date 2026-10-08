"""The snap editor's session record — which cache file a snap being edited is held in.

The app keeps small preferences in ``Documents/user_scoped/<hash>/userPreferences/pref.docobjects``
(SQLite: ``docprefitem(rowid, p BLOB, key STRING UNIQUE)``). The row keyed
``SnapEditor-SnapSessionContext`` holds the snap the editor is working on. Its ``p`` cell carries a
``TSAF`` container (see :mod:`scripts.data.tsaf`) whose root type is ``SESnapSessionContext`` and
whose ``GPBData`` key is followed by two little-endian 32-bit words — the second the length of the
protobuf that follows — and then the protobuf itself. In it:

* ``1``          — when the record was saved (Unix seconds);
* ``2.2.4``      — one entry per media item of the snap: ``.6`` its position (1, 2, …), ``.10`` the
  item's **CACHE_KEY** in ``cache_controller.db``;
* ``2.2.17.7``   — when the snap was edited (Unix milliseconds);
* ``2.5.1`` / ``2.5.2`` — a UUID and a MEDIA_CONTEXT_TYPE: the key and the context of the
  ``cache_controller.db`` claims on those files, which read ``<UUID>~<position>`` with that context.

So the record ties a ``cache_controller.db`` claim to the file it claims, in the app's own words,
and dates the editing. Verified on a test device (iOS 18.3, app 13.4x) against the claims and the
cached files themselves. The decoder is strict: every field above must be there and the protobuf
must parse to its last byte, or nothing is returned — another app version's layout is not guessed.

Only the latest session is a live row (and an ended one is often emptied); earlier versions survive
in the ``-wal`` frames that a later write superseded. Those are carved
(:func:`sqlite_open.superseded_wal_pages`) and kept **only** when a claim in ``cache_controller.db``
corroborates them — same CACHE_KEY, a key ``<UUID>~<position>`` and the same context — so a carved
record never stands on its own.
"""
import glob
import logging
import os
import re

from scripts.data import protobuf_wire
from scripts.data import sqlite_open

logger = logging.getLogger(__name__)

SESSION_KEY = "SnapEditor-SnapSessionContext"
#: How the record starts inside a docprefitem cell (or a carved page): the TSAF root type token,
#: then the GPBData key token.
MARKER = b"SnapSessionContext\x00\x08GPBData\x00"
_HEX32 = re.compile(rb"^[0-9a-f]{32}$")
_UUID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
#: Order of preference when one record is found by more than one reading.
_READING_RANK = {sqlite_open.BOTH: 0, sqlite_open.WAL_ONLY: 1, sqlite_open.MAIN_ONLY: 2,
                 sqlite_open.CARVED: 3}


def find_stores(app):
    """Every ``pref.docobjects`` of an app folder (one per account)."""
    return sorted(glob.glob(os.path.join(app, "Documents", "user_scoped", "*", "userPreferences",
                                         "pref.docobjects")))


def decode(message):
    """The session record in one protobuf, or None (see the module docstring for the fields)."""
    try:
        saved = protobuf_wire.values(message, 1)
        media = protobuf_wire.values(message, 2, 2, 4)
        edited = protobuf_wire.values(message, 2, 2, 17, 7)
        claim = protobuf_wire.values(message, 2, 5, 1)
        context = protobuf_wire.values(message, 2, 5, 2)
        items = []
        for item in media:
            keys = protobuf_wire.values(item, 10)
            position = protobuf_wire.values(item, 6)
            if len(keys) != 1 or not _HEX32.match(bytes(keys[0])):
                return None
            items.append({"cache_key": bytes(keys[0]).decode("ascii"),
                          "position": position[0] if position and isinstance(position[0], int)
                          else None})
    except protobuf_wire.Malformed:
        return None
    if (len(saved) != 1 or not isinstance(saved[0], int) or not items or len(claim) != 1
            or len(context) != 1 or not isinstance(context[0], int)):
        return None
    try:
        claim_uuid = bytes(claim[0]).decode("ascii")
    except UnicodeDecodeError:
        return None
    if not _UUID.match(claim_uuid):
        return None
    return {"saved_unix": saved[0], "media": items,
            "edited_ms": edited[0] if edited and isinstance(edited[0], int) else None,
            "claim_uuid": claim_uuid.upper(), "context": context[0]}


def records_in(buf):
    """Every session record in ``buf`` (a docprefitem cell or a page image), in order."""
    out = []
    buf = bytes(buf or b"")
    start = buf.find(MARKER)
    while start != -1:
        pos = start + len(MARKER)
        if pos + 8 <= len(buf):
            length = int.from_bytes(buf[pos + 4:pos + 8], "little")
            body = buf[pos + 8:pos + 8 + length]
            if 0 < length == len(body):
                rec = decode(body)
                if rec:
                    out.append(rec)
        start = buf.find(MARKER, start + 1)
    return out


def corroborated(rec, claims_by_key):
    """True when a claim in ``cache_controller.db`` says the same as the record: one of its files
    is claimed under ``<UUID>~<position>`` with its context. ``claims_by_key`` is
    ``{cache_key_lower: [(EXTERNAL_KEY, MEDIA_CONTEXT_TYPE)]}``."""
    for item in rec["media"]:
        want = f"{rec['claim_uuid']}~{item['position']}".upper()
        for ek, mct in claims_by_key.get(item["cache_key"].lower(), []):
            if (ek or "").upper() == want and mct == rec["context"]:
                return True
    return False


def read(app, claims_by_key):
    """``{cache_key_lower: [record]}`` from every store of ``app``, each record marked with where it
    was read (``wal``: a :mod:`sqlite_open` reading, or ``CARVED``) and from which store."""
    found = {}
    for store in find_stores(app):
        candidates = []
        views = None
        try:
            views = sqlite_open.open_views(store)
            rows, marks = sqlite_open.read_table(views, "docprefitem")
            for row, mark in zip(rows, marks):
                if row.get("key") == SESSION_KEY:
                    candidates += [(rec, mark) for rec in records_in(row.get("p"))]
        except Exception as error:                         # noqa: BLE001 - one unreadable store
            logger.debug(f"Could not read {store}: {error}")
        finally:
            if views is not None:
                views.close()
        dropped = 0
        for _frame, _page, data in sqlite_open.superseded_wal_pages(store):
            for rec in records_in(data):
                if corroborated(rec, claims_by_key):
                    candidates.append((rec, sqlite_open.CARVED))
                else:
                    dropped += 1
        if dropped:
            logger.info(f"  {dropped} carved snap-editor session record(s) in {store} left out: no "
                        f"claim in cache_controller.db corroborates them")
        best = {}
        for rec, mark in candidates:
            ident = (rec["saved_unix"], rec["edited_ms"], rec["claim_uuid"], rec["context"],
                     tuple((i["cache_key"], i["position"]) for i in rec["media"]))
            if ident not in best or _READING_RANK[mark] < _READING_RANK[best[ident][1]]:
                best[ident] = (rec, mark)
        for rec, mark in sorted(best.values(), key=lambda rm: rm[0]["saved_unix"]):
            for item in rec["media"]:
                found.setdefault(item["cache_key"].lower(), []).append(
                    dict(rec, wal=mark, store=store, position=item["position"]))
    return found
