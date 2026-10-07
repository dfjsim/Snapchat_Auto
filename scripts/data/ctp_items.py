"""The creative-tools item store — ``primary.docobjects`` › ``ctp__item_5`` — and the cached files its
items name.

Each account keeps a store at ``Documents/user_scoped/<userHash>/DocObjects/primary.docobjects``
(SQLite; ``userHash`` is SHA-256 of the account's user id). Beside the contacts it holds the items of
the feeds the camera's creative tools are filled from — captions, filters, stickers:

* ``ctp__item_5(rowid, p BLOB, item_id STRING UNIQUE)`` — one row per item. ``p`` is a FlatBuffers
  document (:mod:`scripts.data.flatbuffers_doc`) whose root table holds, by slot:

  ====  =============================================================================================
  0     the ``item_id`` again — the self-check: a document whose slot 0 is not its row's item_id is
        not read
  2     a ``[ubyte]`` vector holding a protobuf message, the item's payload (below)
  3     the item's own id, as standard padded base64 of payload field 6
  4     a sub-table whose slot 0 is the item's feed, ``feed:<TYPE>-<CONTEXT>-<n>``
  ====  =============================================================================================

  The other slots (a rank text that equals ``index_ctp__item_5rank_id``, small integers, a short
  base64 value many items share) are not read. On the stores examined the item_id is
  ``<own id>-<feed>-<n>``; it is read from its column, never rebuilt.
* The payload parses as a protobuf to its last byte. Its field 2 holds exactly one field, whose number
  is the item's **kind** as stored (``2.11`` on the captions feed's items, ``2.16`` on the filters
  feed's, …); field 6 is the item's own id as bytes; field 4 the same id as an integer when it is
  8 bytes. Its texts — style and font names, colours, a JSON text, and the URLs of the item's assets
  (a font file, a filter image and its CDN copy) — are read with their field paths
  (:func:`protobuf_wire.values_with_paths`) and shown as stored: the field numbers are numbers, not
  names. A field 2 that is not a message of one field gives no kind, and the payload's texts are still
  read. In this layout, on the stores examined, no item holds a creation or last-use time, and none
  names a snap.
* ``ctp__feedtree(rowid, p BLOB, context INTEGER UNIQUE)`` — per context, a FlatBuffers document whose
  slot 2 is an NSKeyedArchiver archive (:mod:`scripts.data.keyed_archive`) of a ``CTPFeed``: its
  ``FEED_ID`` (``TYPE``, ``CONTEXT``), ``NAME``, ``SOURCE`` (``COMPUTE_ENDPOINT``, the creativetools
  service the feed is fetched from) and ``CHILD_FEEDS``. A feed is named from it — the word after
  ``.creativetools.`` in its endpoint, else its NAME — only when the tree lists it; an item of a feed
  the tree does not list is said to be so, never given a name by its number. Both readings of the tree
  are read: the current one names a feed, and a feed only the checkpointed version lists is named as
  prior state (``prior``). A tree document that does not hold a ``CTPFeed`` archive is not read, and
  a feed missing from the trees that were read is then not said to be missing (``tree_unread``).

Verified on every row of three test devices' stores. Both readings of each store are
read — with and without its ``-wal`` (:mod:`scripts.data.sqlite_open`), staged in a temporary folder so
no copy of the store is left anywhere — and every version of an item is kept with the reading it came
from: the ``-wal`` rewrites these rows, and a version only the checkpointed file holds is prior state.
A hit carries the reading of the version shown (``wal``), and ``rewritten`` when the -wal's version is
shown and the checkpointed version of the same item holds the matched text too. Whether the readings
differ is known of the two tables read only (``differs`` per store), never of the whole store.

**What a cached file is matched by** (:func:`match`) — exact identity of whole texts, nothing else:

* the claim's whole ``EXTERNAL_KEY`` is a text the item holds (``key``); a URL on both sides in
  the form :func:`snap_overlay.normalise_url` gives it, the one URL rule the reports share;
* the key after a word and ``:`` or ``~`` (``music:<url>``, ``customSticker~<id>``; never ``://``) is
  a text the item holds — a payload text, its item_id or its own id (``after_prefix``);
* failing those, that part of the key read as base64 is the same bytes as the item's own id — payload
  field 6, or its item_id or slot 3 read as base64 — in either alphabet, padded or not, when that part
  reads as base64 by :func:`base64_text.base64_bytes`'s rule (padded, or with ``+`` or ``/``, or mixing
  upper case, lower case and digits); an id with none of these matches by its text only (``id_bytes``).

Never a query value (``bo=``, ``mo=``, ``uc=`` — ``bo=`` is a set of fetch options many cached files
share), never a path segment alone, never a part of a text, never a text shorter than :data:`MIN_ID`
or made of digits only. A text that more than one item of one store holds is attributed to none of
them — and no later rule matches in that store: the first rule that finds the text in a store decides
for it. The same text in two accounts' stores is two hits, each saying whose store holds it.

A document of another layout is still an item: its ``item_id`` column is indexed whatever the
document holds, so a key naming the item by it matches, and the item is shown as not decoded rather
than read on a guess. This finds the store on an iOS app folder (``Documents/``); on an Android app
folder :func:`find_stores` finds nothing, and nothing is matched.
"""
import base64
import glob
import logging
import os
import re
import sqlite3

from scripts.data import base64_text
from scripts.data import flatbuffers_doc as fb
from scripts.data import keyed_archive
from scripts.data import protobuf_wire
from scripts.data import snap_overlay
from scripts.data import sqlite_open

logger = logging.getLogger(__name__)

ITEM_TABLE = "ctp__item_5"
TREE_TABLE = "ctp__feedtree"
FEED_CLASS = "CTPFeed"

#: The item document's slots that are read (see the module docstring).
SLOT_ITEM_ID, SLOT_PAYLOAD, SLOT_OWN_ID, SLOT_FEED = 0, 2, 3, 4
#: The feed tree document's slot holding the archive.
TREE_SLOT_ARCHIVE = 2

#: A text shorter than this is never matched (nor one of digits only): it would match by chance.
MIN_ID = 8
#: Nor bytes shorter than this.
MIN_ID_BYTES = 6
#: A JSON text in a payload is a text too; the detail and the search show at most this many per item.
MAX_TEXTS = 40

#: The rules, as the detail names them.
RULE_LABELS = {"key": "the whole key", "after_prefix": "the key after {prefix}",
               "id_bytes": "the key after {prefix}, as base64 bytes"}

#: Where a matched text sits in an item, as the explanations say it.
WHERE_ITEM_ID = "its item_id"
WHERE_OWN_ID = "FlatBuffers slot 3 (its own id)"
WHERE_OWN_BYTES = "payload field 6 (its own id)"

_PREFIXED = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)([:~])(?!//)(.+)$")
_FEED = re.compile(r"^feed:(\d+)-(\d+)(?:-(\d+))?$")
_SERVICE = re.compile(r"\.creativetools\.([A-Za-z0-9_-]+)\.")
#: Order of preference among the versions of one item that hold the matched text.
_READING_RANK = {sqlite_open.BOTH: 0, sqlite_open.WAL_ONLY: 1, sqlite_open.MAIN_ONLY: 2}


def find_stores(app):
    """Every ``primary.docobjects`` of an iOS app folder (one per account); none on Android."""
    return sorted(glob.glob(os.path.join(app, "Documents", "user_scoped", "*", "DocObjects",
                                         "primary.docobjects")))


# --------------------------------------------------------------------------- reading a document

def _kind(payload):
    """The one field number inside payload field 2, or None when field 2 is not a message holding
    exactly one field number — a number, a text or bytes that do not parse as a message. Never
    raises."""
    numbers = set()
    try:
        top = protobuf_wire.fields(payload)
    except protobuf_wire.Malformed:
        return None
    for field, wire, value in top:
        if field != 2:
            continue
        if wire != 2:
            return None
        try:
            numbers.update(number for number, _wire, _value in protobuf_wire.fields(value))
        except protobuf_wire.Malformed:
            return None
    kind = numbers.pop() if len(numbers) == 1 else None
    return kind if kind else None                    # field number 0 is not a field: not a message


def decode_item(item_id, blob):
    """``{"own_id", "own_bytes", "feed", "kind", "texts", "payload_ok"}`` of one ``ctp__item_5.p``
    document, or None when its slot 0 is not ``item_id``.

    ``texts`` is every printable text of the payload, ``[(field path, text)]`` in field order
    (``"2.16.2.1.2"``); ``own_bytes`` is payload field 6 only when slot 3 is its base64. A payload that
    does not parse to its last byte has no kind and no texts (``payload_ok`` False); the FlatBuffers
    strings still come back. One that does, but whose field 2 is not a message of one field, has its
    texts and no kind.
    """
    if not item_id or not blob or fb.string_field(blob, SLOT_ITEM_ID) != item_id:
        return None
    own_id = fb.string_field(blob, SLOT_OWN_ID)
    sub = fb.table_field(blob, SLOT_FEED)
    feed = fb.string_field(blob, 0, table=sub) if sub is not None else ""
    payload = fb.bytes_field(blob, SLOT_PAYLOAD)
    out = {"own_id": own_id, "own_bytes": None, "feed": feed, "kind": None, "texts": [],
           "payload_ok": False}
    if payload is None:
        return out
    found = protobuf_wire.values_with_paths(payload)
    if found is None:
        return out
    own = protobuf_wire.values(payload, 6)          # one step: cannot fail once the payload parses
    kind = _kind(payload)
    if len(own) == 1 and isinstance(own[0], bytes) and own_id \
            and base64.b64encode(own[0]).decode("ascii") == own_id:
        out["own_bytes"] = own[0]
    out.update(kind=kind, payload_ok=True,
               texts=[(".".join(map(str, path)), text) for path, _value, text in found if text])
    return out


def decode_feed_tree(blob):
    """``{(TYPE, CONTEXT): {"name", "endpoint"}}`` of every feed in one ``ctp__feedtree.p`` document
    (``{}`` when the tree lists no numbered feed); None when it is not a FlatBuffers document holding
    an archive of a ``CTPFeed`` — the tree is not read, and what it lists is not known."""
    tree = keyed_archive.unarchive(fb.bytes_field(blob, TREE_SLOT_ARCHIVE) if blob else None,
                                   FEED_CLASS)
    if tree is None:
        return None
    feeds, stack, seen = {}, [tree], set()
    while stack:
        node = stack.pop()
        if keyed_archive.class_name(node) != FEED_CLASS or id(node) in seen:
            continue
        seen.add(id(node))
        ident = node.get("FEED_ID")
        kind, context = (ident.get("TYPE"), ident.get("CONTEXT")) if isinstance(ident, dict) \
            else (None, None)
        if _number(kind) and _number(context):
            source = node.get("SOURCE")
            endpoint = source.get("COMPUTE_ENDPOINT") if isinstance(source, dict) else None
            name = node.get("NAME")
            feeds.setdefault((kind, context), {
                "name": name.strip() if isinstance(name, str) else "",
                "endpoint": endpoint.strip() if isinstance(endpoint, str) else ""})
        children = node.get("CHILD_FEEDS")
        stack.extend(reversed(children) if isinstance(children, list) else ())
    return feeds


def _number(value):
    return isinstance(value, int) and not isinstance(value, bool)


def feed_label(feed, feeds, tree_unread=False):
    """What the feed tree says of the feed ``feed`` (``feed:<TYPE>-<CONTEXT>-<n>``):
    ``{"feed", "type", "context", "short", "name", "endpoint", "in_tree", "prior", "tree_unread"}``.
    ``short`` is the word after ``.creativetools.`` in the feed's endpoint, else its NAME, else ``""``;
    a feed the tree does not list has ``in_tree`` False and no name — never one guessed from its
    number. ``prior``: only the checkpointed version of the tree lists it (``feeds`` marks it so).
    ``tree_unread``: the feed is not in the trees that were read, and the store has a tree document
    that was not (``tree_unread`` given) — whether the tree lists it is not known."""
    out = {"feed": feed or "", "type": None, "context": None, "short": "", "name": "",
           "endpoint": "", "in_tree": False, "prior": False, "tree_unread": False}
    mo = _FEED.match(feed or "")
    if not mo:
        return out
    out["type"], out["context"] = int(mo.group(1)), int(mo.group(2))
    known = feeds.get((out["type"], out["context"]))
    if known is None:
        out["tree_unread"] = bool(tree_unread)
        return out
    service = _SERVICE.search(known["endpoint"])
    out.update(in_tree=True, name=known["name"], endpoint=known["endpoint"],
               short=service.group(1) if service else known["name"], prior=bool(known.get("prior")))
    return out


# --------------------------------------------------------------------------- reading a store

def _text(value):
    """A column value as text: a string as stored, an integer in digits (the column's affinity turns
    a numeric text into a number), UTF-8 bytes decoded; anything else ``""``."""
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return ""


def _has_table(views, table):
    for conn in (views.merged, views.main_only):
        if conn is None:
            continue
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                            (table,)).fetchone():
                return True
        except sqlite3.DatabaseError:
            continue
    return False


def _matchable(text):
    """The form a text is matched in — a URL by :func:`snap_overlay.normalise_url`, anything else as
    it is — or ``""`` when it is too short to be matched, or digits only."""
    if not isinstance(text, str):
        return ""
    form = snap_overlay.normalise_url(text) or text
    return form if len(form) >= MIN_ID and not form.isdigit() else ""


def _id_bytes(text):
    raw = base64_text.base64_bytes(text) if isinstance(text, str) and text else None
    return raw if raw is not None and len(raw) >= MIN_ID_BYTES else None


def _index_item(index, item):
    """Index one version of one item: by its texts (item_id, own id, payload texts) and by the bytes of
    its own id. Each index entry is ``(item, [(where, the text as stored)])``."""
    by_text, by_bytes = {}, {}
    places = [(WHERE_ITEM_ID, item["item_id"]), (WHERE_OWN_ID, item["own_id"])]
    places += [(f"payload {path}", text) for path, text in item["texts"]]
    for where, text in places:
        form = _matchable(text)
        if form:
            by_text.setdefault(form, []).append((where, text))
    for where, text in ((WHERE_ITEM_ID, item["item_id"]), (WHERE_OWN_ID, item["own_id"])):
        raw = _id_bytes(text)
        if raw is not None:
            by_bytes.setdefault(raw, []).append((where, text))
    if item["own_bytes"] is not None and len(item["own_bytes"]) >= MIN_ID_BYTES:
        by_bytes.setdefault(item["own_bytes"], []).append((WHERE_OWN_BYTES, item["own_id"]))
    for name, found in (("by_text", by_text), ("by_bytes", by_bytes)):
        for key, wheres in found.items():
            index[name].setdefault(key, []).append((item, list(dict.fromkeys(wheres))))


def empty_index():
    return {"stores": [], "by_text": {}, "by_bytes": {}}


def read(app):
    """The items of every account's store of ``app``, indexed for :func:`match`:
    ``{"stores", "by_text", "by_bytes"}``.

    ``stores`` says what was read: ``{"path", "user_hash", "info" (sqlite_open's), "differs", "items",
    "undecoded", "trees_undecoded"}`` per store that has the item table — a store without one (an app
    version that keeps no such feeds) is passed over quietly. ``differs`` is ``{table: bool}`` for the
    tables read, when the two readings could be compared — all that is known of whether they differ:
    ``info["differs"]`` was set from those tables alone and says nothing of the store's other tables
    (the contacts among them), which are not read. Each item carries the
    store, its ``user_hash`` (the store's folder name), the reading it came from (``wal``), whether its
    document was ``decoded``, and its feed as the tree names it (``feed_info``).
    """
    index = empty_index()
    for store in find_stores(app):
        user_hash = os.path.basename(os.path.dirname(os.path.dirname(store)))
        views = None
        try:
            views = sqlite_open.open_views(store)                  # no workdir: staged in a temp dir
            if not _has_table(views, ITEM_TABLE):
                continue
            rows, marks = sqlite_open.read_table(views, ITEM_TABLE)
            tree_rows, tree_marks = sqlite_open.read_table(views, TREE_TABLE)
            info = dict(views.info)
            differs = {}
            if views.main_only is not None and views.main_only is not views.merged:
                differs[ITEM_TABLE] = any(m != sqlite_open.BOTH for m in marks)
                if _has_table(views, TREE_TABLE):
                    differs[TREE_TABLE] = any(m != sqlite_open.BOTH for m in tree_marks)
        except Exception as error:                                 # noqa: BLE001 - one store
            logger.debug(f"{store}: creative-tools items not read ({error})")
            continue
        finally:
            if views is not None:
                views.close()
        # The tree's versions by reading, not by row order: the current reading's names a feed; a
        # version only the checkpointed file holds is prior state, and a feed only it lists says so.
        feeds, prior, trees_undecoded = {}, {}, 0
        for row, mark in zip(tree_rows, tree_marks):
            tree = decode_feed_tree(row.get("p"))
            if tree is None:                       # not read: what it lists is not known
                trees_undecoded += 1
                continue
            target = prior if mark == sqlite_open.MAIN_ONLY else feeds
            for key, feed in tree.items():
                target.setdefault(key, feed)
        for key, feed in prior.items():
            feeds.setdefault(key, dict(feed, prior=True))
        ids, undecoded = set(), 0
        for row, mark in zip(rows, marks):
            item_id = _text(row.get("item_id"))
            if not item_id:
                continue
            try:
                decoded = decode_item(item_id, row.get("p"))
                feed_info = (feed_label(decoded["feed"], feeds, tree_unread=trees_undecoded > 0)
                             if decoded and decoded["feed"] else None)
            except Exception as error:     # noqa: BLE001 - one document: still an item by its item_id
                logger.debug(f"{store}: the document of a creative-tools item not read ({error})")
                decoded, feed_info = None, None
            if decoded is None:
                undecoded += 1
            item = dict(decoded or {"own_id": "", "own_bytes": None, "feed": "", "kind": None,
                                    "texts": [], "payload_ok": False},
                        item_id=item_id, store=store, user_hash=user_hash, wal=mark,
                        decoded=decoded is not None, feed_info=feed_info)
            ids.add(item_id)
            _index_item(index, item)
        index["stores"].append({"path": store, "user_hash": user_hash, "info": info,
                                "differs": differs, "items": len(ids), "undecoded": undecoded,
                                "trees_undecoded": trees_undecoded})
        logger.info(f"  {len(ids)} creative-tools item(s) in {store} (ctp__item_5)"
                    + (f"; {undecoded} document(s) not decoded (layout differs) - matched by their "
                       f"item_id only" if undecoded else "")
                    + (f"; {trees_undecoded} feed tree document(s) not decoded (ctp__feedtree) - "
                       f"their feeds are not named" if trees_undecoded else ""))
    return index


def merge(indexes):
    """One index of several (:func:`read` of several app folders)."""
    out = empty_index()
    for index in indexes:
        out["stores"] += index.get("stores") or []
        for name in ("by_text", "by_bytes"):
            for key, found in (index.get(name) or {}).items():
                out[name].setdefault(key, []).extend(found)
    return out


# --------------------------------------------------------------------------- matching a claim

def _attributable(found):
    """The entries of one lookup, less those of a store in which more than one item holds the text."""
    by_store = {}
    for item, wheres in found or ():
        by_store.setdefault(item["store"], []).append((item, wheres))
    out = []
    for store, entries in by_store.items():
        if len({item["item_id"] for item, _wheres in entries}) == 1:
            out += entries
        else:
            logger.debug(f"{store}: a text held by several creative-tools items - not attributed")
    return out


def match(external_key, index):
    """The items of ``index`` (:func:`read`) that name a claim's ``external_key``, by the rules of the
    module docstring — the first rule that finds any, one hit per item and store, sorted by account
    and item_id. The first rule that finds the text in a store decides for that store: a text it
    cannot attribute there (several items hold it) is not matched there by a later rule either.

    Each hit is the item's version that holds the matched text (the one both readings hold, else the
    -wal's, else the checkpointed file's), plus ``rule``, ``where`` (the places in the item, with the
    text each holds as stored), ``prefix`` (the word and separator a rule skipped), ``wal`` (the
    reading of the version shown) and ``rewritten`` (the -wal's version is shown, and the
    checkpointed version of the item holds the text too).
    """
    if not index or not isinstance(external_key, str) or not external_key:
        return []
    by_text, by_bytes = index.get("by_text") or {}, index.get("by_bytes") or {}
    lookups = []
    whole = _matchable(external_key)
    if whole:
        lookups.append(("key", "", by_text.get(whole)))
    mo = _PREFIXED.match(external_key)
    if mo:
        prefix, rest = mo.group(1) + mo.group(2), mo.group(3)
        form = _matchable(rest)
        if form:
            lookups.append(("after_prefix", prefix, by_text.get(form)))
        raw = _id_bytes(rest)
        if raw is not None:
            lookups.append(("id_bytes", prefix, by_bytes.get(raw)))
    held = set()                                   # stores an earlier rule found the text in
    for rule, prefix, found in lookups:
        found = [(item, wheres) for item, wheres in found or () if item["store"] not in held]
        held |= {item["store"] for item, _wheres in found}
        best, readings = {}, {}
        for item, wheres in _attributable(found):
            ident = (item["store"], item["item_id"])
            readings.setdefault(ident, set()).add(item["wal"])
            if ident not in best or _READING_RANK.get(item["wal"], 3) < \
                    _READING_RANK.get(best[ident][0]["wal"], 3):
                best[ident] = (item, wheres)
        if best:
            return [dict(item, rule=rule, where=wheres, prefix=prefix,
                         **_held_in(readings[(item["store"], item["item_id"])]))
                    for item, wheres in sorted(best.values(),
                                               key=lambda iw: (iw[0]["user_hash"], iw[0]["item_id"]))]
    return []


def _held_in(markers):
    """``{"wal", "rewritten"}`` of a matched text from the readings of the item's versions holding it.
    ``wal`` is the reading of the version shown (both, else the -wal's, else the checkpointed file's
    — :data:`_READING_RANK`); ``rewritten`` when that is the -wal's version and the checkpointed
    version holds the text too: the -wal rewrote the row, and the two readings do not agree on it."""
    if sqlite_open.BOTH in markers:
        return {"wal": sqlite_open.BOTH, "rewritten": False}
    if sqlite_open.WAL_ONLY in markers:
        return {"wal": sqlite_open.WAL_ONLY, "rewritten": sqlite_open.MAIN_ONLY in markers}
    return {"wal": sqlite_open.MAIN_ONLY, "rewritten": False}
