"""Which cache claims could be tied to a chat message, and by what? (``--survey-claim-links``)

The cache_controller report ties a cached file to a chat message when the chat join attached that
file to the message, or when the claim's key names the conversation and the message. Every other
claim is left unlinked — and a kind of claim the join does not know yet looks exactly like one that
belongs to no message at all. ``--trace-ids`` answers the question for one identifier at a time;
this answers it for every claim at once, so one run on the case says which link rules are missing:

* every claim of ``cache_controller.db`` (both readings) is read, and its EXTERNAL_KEY cut into the
  ids it carries — UUIDs, hex, base64, other long tokens, long words — and what lies between them;
* every row of every table of ``arroyo.db`` (both readings) is read: each value of a protobuf blob
  (``message_content`` above all) with its field path, and the text and blob values of the other
  columns, with the ids inside a text cut out the same way;
* each claim id is looked up among those values — as a whole value, inside a text, and as the bytes
  a UUID, hex or base64 stands for.

The result is grouped by the claim key's **shape**: the key with each id replaced by a placeholder
(``<uuid>``, ``<hex32>``, ``<b64:13>``, ``<id>``, ``<n>``). Per shape and context it counts the
claims the reports already tie to a message (by the file the chat join attached, or by the message
the key names) or to the conversation a key names when no row is there for its message, the
Memory-scoped keys, the asset URLs a Memory's overlay record lists, the keys an
item of a creative-tools store names (``primary.docobjects`` ``ctp__item_5``), and those nothing
ties; and for each id position in the shape, where in ``arroyo.db`` the same id was found —
table, column, protobuf field, content_type, reading — and in how many rows each id occurs. An id
found in one message, or a few, is what a link rule is written from; one found in every message of
a conversation (the conversation's own id) is not. An
untied key that names a conversation and a message is checked against every ``arroyo.db``: does one
hold the message, does one hold the conversation, and was the claim made by the account one of them
belongs to — which says whether a rule is missing, or whether no rule written from these databases
could tie it.

It writes ``claim_link_survey_<stamp>.json`` into the run folder and a summary into the log: shapes,
counts and field paths — never an id, a key or a cell value. A word of letters only is kept in a
shape, because words are what tell one kind of key from another (``thumbnail``, ``customSticker``);
a key holding a name made of letters only would carry it into the shape, so the output stays with
the case like the rest of the run folder.
"""
import collections
import datetime
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import uuid
from dataclasses import dataclass

from scripts import trace_ids
from scripts.data import arroyo_content, ctp_items, protobuf_wire, sqlite_open

logger = logging.getLogger(__name__)

#: Exit codes: surveyed / nothing to survey (no claims or no arroyo.db) / bad arguments.
EXIT_OK, EXIT_NOTHING, EXIT_USAGE = 0, 1, 2

#: An id shorter than this many characters is not looked up, nor decoded bytes shorter than
#: MIN_ID_BYTES: they would match by chance.
MIN_ID_CHARS = 8
MIN_ID_BYTES = 6
#: A word of letters only is looked up too from this length — a sticker's name is one.
MIN_WORD = 10
#: Rows counted per id; past this an id is simply "in more than 50 rows".
ROW_CAP = 50
#: How the number of rows an id occurs in is reported.
ROW_BUCKETS = (("1", 1), ("2-5", 5), ("6-50", ROW_CAP))
MORE = f"more than {ROW_CAP}"
#: Progress is logged after this many rows of arroyo.db.
PROGRESS_ROWS = 50_000

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
#: A URL, up to the first character a URL cannot hold unescaped — so JSON or prose after it is
#: read as plain text, and a bracket after it cannot be taken for an IPv6 host.
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>{}|\\^`\[\]]*")
_RUN = re.compile(r"[A-Za-z0-9+/=_-]+")
_RUN_NO_SLASH = re.compile(r"[A-Za-z0-9+=_-]+")
_HEX = re.compile(r"[0-9a-fA-F]+")
_WORD = re.compile(r"[A-Za-z_-]+")
_DIGITS = re.compile(r"\d+")
_PRINTABLE = re.compile(rb"[\x20-\x7e]{8,}")


# ------------------------------------------------------------------------------- reading a key

@dataclass(frozen=True)
class Ident:
    """One id in a key or a text: the placeholder it is shown as, and what it is.

    ``canon`` is the id however it is written: ``("bytes", …)`` for a UUID, even-length hex or
    base64 — the bytes it stands for, so a UUID in a key, its dashless hex in a text and its 16
    bytes in a blob are one id — and ``("text", lower-cased)`` for anything else.
    """
    label: str
    canon: tuple


def _uuid_ident(text):
    return Ident("<uuid>", ("bytes", uuid.UUID(text).bytes))


def _hex_ident(text):
    low = text.lower()
    canon = ("bytes", bytes.fromhex(low)) if len(low) % 2 == 0 else ("text", low)
    return Ident(f"<hex{len(text)}>", canon)


def _b64_ident(raw):
    return Ident(f"<b64:{len(raw)}>", ("bytes", raw))


def _text_ident(label, text):
    return Ident(label, ("text", text.lower()))


def _read_piece(piece, shape, idents, words=True):
    """A piece with no ``+ / =`` in it: hex, a number, a word or an other token. With ``words``
    false no word is an id (a host name's are not)."""
    if len(piece) >= 16 and _HEX.fullmatch(piece) and not piece.isdigit():
        idents.append(_hex_ident(piece))
        shape.append(idents[-1].label)
    elif piece.isdigit():
        if len(piece) >= MIN_ID_CHARS:              # long enough to be an id: looked up
            idents.append(_text_ident("<num>", piece))
            shape.append("<num>")
        else:
            shape.append("<n>")
    elif _WORD.fullmatch(piece):
        if words and len(piece) >= MIN_WORD:
            idents.append(_text_ident(piece, piece))
        shape.append(piece)
    elif len(piece) >= MIN_ID_CHARS:
        idents.append(_text_ident("<id>", piece))
        shape.append("<id>")
    else:
        shape.append(_DIGITS.sub("<n>", piece))


def _read_run(run, shape, idents, words=True):
    """A run of base64 characters: base64 as a whole, or else piece by piece."""
    raw = trace_ids.base64_bytes(run)
    if raw is not None and len(raw) >= MIN_ID_BYTES:
        idents.append(_b64_ident(raw))
        shape.append(idents[-1].label)
        return
    # "name=<value>": an '=' with a character other than '=' on both sides is not padding, so the
    # two sides are read apart — the value can be base64 on its own
    sides = re.split(r"(?<=[^=])=(?=[^=])", run)
    if len(sides) > 1:
        for n, side in enumerate(sides):
            if n:
                shape.append("=")
            _read_run(side, shape, idents, words)
        return
    for piece in re.split(r"([+/=])", run):
        if piece in ("+", "/", "="):
            shape.append(piece)
        elif piece:
            _read_piece(piece, shape, idents, words)


def _read_plain(text, shape, idents, split_slash=False, words=True):
    """Text with no URL in it: UUIDs first (their dashes would join them to their neighbours), then
    runs of base64 characters. ``split_slash`` reads a URL path, whose ``/`` separates segments."""
    pos = 0
    for mo in _UUID.finditer(text):
        _read_runs(text[pos:mo.start()], shape, idents, split_slash, words)
        idents.append(_uuid_ident(mo.group(0)))
        shape.append("<uuid>")
        pos = mo.end()
    _read_runs(text[pos:], shape, idents, split_slash, words)


def _read_runs(text, shape, idents, split_slash, words):
    pos = 0
    for mo in (_RUN_NO_SLASH if split_slash else _RUN).finditer(text):
        shape.append(text[pos:mo.start()])
        _read_run(mo.group(0), shape, idents, words)
        pos = mo.end()
    shape.append(text[pos:])


def _read_url(url, shape, idents):
    """A URL: host and path segment by segment, each query value whole (base64 there may hold
    a ``/``). Text the URL parser refuses is read as plain text."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        _read_plain(url, shape, idents, split_slash=True)
        return
    shape.append(f"{parts.scheme}://")
    _read_plain(parts.netloc, shape, idents, split_slash=True, words=False)
    _read_plain(urllib.parse.unquote(parts.path), shape, idents, split_slash=True)
    if parts.query:
        shape.append("?")
        for n, pair in enumerate(parts.query.split("&")):
            name, eq, value = pair.partition("=")
            shape.append(("&" if n else "") + urllib.parse.unquote(name) + eq)
            _read_plain(urllib.parse.unquote(value), shape, idents)
    if parts.fragment:
        shape.append("#")
        _read_plain(urllib.parse.unquote(parts.fragment), shape, idents)


#: A username where a claim key holds one: the upper-case run right before a ``~`` (``<NAME>~<UUID>``,
#: ``content~<NAME>~…``, ``SnapVideoFilterState-<NAME>~…``). The type words in that position are not
#: upper case (``thumbnail~``, ``content~``), so they stay readable in a shape.
_OWNER = re.compile(r"(?:^|(?<=[~:])|(?<=[a-z]-))([A-Z0-9._-]*[A-Z][A-Z0-9._-]*)(?=~)")


def mask_names(text):
    """``text`` with each owner username a key position can hold replaced by ``<NAME>``. An id in
    that position — an upper-case UUID or hex run, as a snap id is written — stays an id."""
    def name(mo):
        found = mo.group(1)
        if _UUID.fullmatch(found) or re.fullmatch(r"[0-9A-Fa-f-]{16,}", found):
            return found
        return "<NAME>"
    return _OWNER.sub(name, text)


def read_key(text):
    """``(shape, idents)`` of a claim key (or of any text a row holds).

    ``shape`` is the text with each id replaced by its placeholder, and an owner username by
    ``<NAME>`` (:func:`mask_names`); ``idents`` the ids, in order. A URL is read part by part — inside a
    path a ``/`` separates segments, inside a query value it may belong to base64 — and whatever
    precedes it (``music:``, ``lens.data``) as plain text.
    """
    text = mask_names(str(text or ""))
    shape, idents, pos = [], [], 0
    for mo in _URL.finditer(text):
        _read_plain(text[pos:mo.start()], shape, idents)
        _read_url(mo.group(0), shape, idents)
        pos = mo.end()
    _read_plain(text[pos:], shape, idents)
    return "".join(shape), idents


# ------------------------------------------------------------------------------- the claims

#: The link status of a claim, by the route the cache_controller report's first link to a message took.
ROUTE_STATUS = {"file": "message: attached file", "key": "message: named in the key",
                "content": "message: id in the key"}


#: The status of a claim whose key names a message no row is there for, in a conversation the
#: Conversations report lists: the report ties it to the conversation (``_conversation_links_for``).
CONVERSATION_STATUS = "conversation: named in the key"
#: The status of a claim whose key is the URL of an asset of a filter a Memory's overlay record lists.
FILTER_STATUS = "Memory: listed in its filter record"
#: The status of a claim whose key an item of an account's creative-tools store names.
ITEM_STATUS = "creative-tools item (ctp__item_5)"


def _link_status(claim, by_key, by_message, chat_ids=None, snap_ids=None, *, filter_urls=None,
                 items=None):
    """How the reports tie a claim to a message — computed by the cache_controller report's own
    ``_chat_links_for``, so the survey and the report cannot disagree — or to the conversation its
    key names, for a message no row is there for, when the Conversations report lists the
    conversation (the report's own ``_conversation_links_for``, over the manifest's ``conversations``
    and ``arroyo`` sections that ``chat_ids`` carries; a tie to a conversation no report lists is a
    stated fact in the report, not a link, and leaves the claim ``none``), or that its key is a
    Memory's (a Memory-scoped shape, or a full-media key carrying a Memory's ZSNAPID, the two links
    a key alone makes), or that a Memory's overlay record lists it as the URL of a filter's asset
    (``filter_urls``, the ``overlay_urls`` of ``load_memory_index``, matched by the report's own
    ``_overlay_links_for``), or that an item of a creative-tools store names it (``items``, an index
    of ``ctp_items.read``, matched by ``ctp_items.match`` as the report matches it), or ``none``. The
    first that applies, in that order."""
    from scripts.cache_controller_report import (_chat_links_for, _conversation_links_for,
                                                 _overlay_links_for, _UUID_RE)
    from scripts.memories_media_report import classify_snap_claim
    ek, ck = str(claim.get("EXTERNAL_KEY") or ""), str(claim.get("CACHE_KEY") or "")
    user = str(claim.get("USER_ID") or "")
    links = _chat_links_for([{"external_key": ek}], ck, by_key, by_message, chat_ids)
    if links:
        return ROUTE_STATUS.get(links[0].get("route"), "message")
    if any(tie["listed"] for tie in _conversation_links_for(
            [{"external_key": ek, "user_id": user}], links, chat_ids)):
        return CONVERSATION_STATUS
    if classify_snap_claim(ek)[0]:
        return "Memory-scoped key"
    mo = _UUID_RE.search(ek)
    if mo and claim.get("MEDIA_CONTEXT_TYPE") == 19 and mo.group(0).upper() in (snap_ids or {}):
        return "Memory: its snap id in the key"
    if filter_urls and _overlay_links_for([{"external_key": ek, "user_id": user}], filter_urls):
        return FILTER_STATUS
    if items and ctp_items.match(ek, items):
        return ITEM_STATUS
    return "none"


def _apps(controllers):
    """The app folder of each cache_controller.db, once each, in the order of ``controllers``.

    The folder is the one the reports read the database from: the nearest folder above it whose
    own cache_controller.db paths (``find_cache_controllers`` — the iOS container's
    ``Documents/global_scoped/cachecontroller/``, anywhere in an Android app folder) include it, as
    ``find_app_container`` names that folder. A database no app folder reads is left out, rather
    than read against a folder guessed from its depth.
    """
    from scripts.cache_controller_report import find_app_container, find_cache_controllers
    out = []
    for path in controllers:
        target = os.path.normcase(os.path.abspath(path))
        folder, app = os.path.dirname(os.path.abspath(path)), None
        while True:
            if any(os.path.normcase(os.path.abspath(p)) == target
                   for p in find_cache_controllers(folder)):
                app = find_app_container(folder)
                break
            parent = os.path.dirname(folder)
            if parent == folder:                                   # the top: no app folder reads it
                break
            folder = parent
        if app is None:
            logger.debug(f"--survey-claim-links: no app folder reads {path}")
        elif app not in out:
            out.append(app)
    return out


def _memory_keys(apps):
    """``(snap_ids, filter_urls)`` from each app folder (:func:`_apps`), in one read: every Memory's
    ZSNAPID, and the asset URLs of the geofilters every Memory's overlay record lists (iOS; the
    Android index has none)."""
    from scripts.cache_controller_report import load_memory_index
    snap_ids, filter_urls = {}, {}
    for app in apps:
        try:
            index = load_memory_index(app, overlays=True)
        except Exception as error:                                 # noqa: BLE001 - optional
            logger.debug(f"--survey-claim-links: Memories not read for {app} ({error})")
            continue
        snap_ids.update(index.get("snap_ids") or {})
        for url, listed in (index.get("overlay_urls") or {}).items():
            filter_urls.setdefault(url, []).extend(listed)
    return snap_ids, filter_urls


def _snap_ids(apps):
    """Every Memory's ZSNAPID, from each app folder (:func:`_apps`)."""
    return _memory_keys(apps)[0]


def _ctp_index(apps):
    """The items of every account's creative-tools store of each app folder (:func:`_apps`), in one
    index for ``ctp_items.match`` — iOS; an Android app folder has no such store."""
    indexes = []
    for app in apps:
        try:
            indexes.append(ctp_items.read(app))
        except Exception as error:                                 # noqa: BLE001 - optional
            logger.debug(f"--survey-claim-links: creative-tools items not read for {app} ({error})")
    return ctp_items.merge(indexes)


#: The tables whose ``client_conversation_id`` says that arroyo.db holds a conversation. Each is
#: optional: the schema moves between app versions (``user_conversation`` is gone from the newest).
CONVERSATION_TABLES = ("conversation_message", "conversation", "feed_entry", "user_conversation")


def _columns(conn, table):
    """The columns of ``table`` in one reading; none when that reading has no such table. A schema
    that will not read raises: ``PRAGMA table_info`` answers a missing table with no rows, not an
    error, so the two are never confused."""
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _rows(views, table, needed, optional=(), distinct=False):
    """The rows of ``table``'s ``needed`` and ``optional`` columns (``NULL`` for an optional one a
    reading lacks), in both readings, each row once — or ``None`` when no reading has the table and
    its ``needed`` columns.

    The merge is :func:`sqlite_open.query_both`'s, on the whole row; the difference is that nothing
    is swallowed. A reading that has the columns and will not read them raises, so a table with a
    damaged page is never taken for an empty one. A reading without them adds no rows: a table or
    column created since the last checkpoint is not in the checkpointed reading, and one dropped
    since is not in the other. A checkpointed copy that would not open at all is no reading here;
    :func:`_arroyo_facts` counts its file unread.
    """
    readings = [views.merged]
    if views.main_only is not None and views.main_only is not views.merged:
        readings.append(views.main_only)
    rows, seen, found = [], set(), False
    for conn in readings:
        columns = _columns(conn, table)
        if not set(needed) <= columns:
            continue
        found = True
        picked = ", ".join(f'"{c}"' if c in columns else "NULL" for c in (*needed, *optional))
        query = f'SELECT {"DISTINCT " if distinct else ""}{picked} FROM "{table}"'
        for row in conn.execute(query).fetchall():
            ident = tuple(sqlite_open._hashable(v) for v in row)
            if ident not in seen:
                seen.add(ident)
                rows.append(row)
    return rows if found else None


def _arroyo_facts(arroyos):
    """``(held, convs, accounts, unread, nameless)`` from every ``arroyo.db``, each fact in either
    reading.

    * ``held``: ``{(conversation id, message number)}`` of every message — by its server message
      id, or by its ``client_message_id`` when the server never numbered it (not sent, or still
      sending), so a key naming such a message is never taken for one the database lacks;
    * ``convs``: every conversation id with a row in any of :data:`CONVERSATION_TABLES`;
    * ``accounts``: each database's own account, its ``required_values`` ``USERID``;
    * ``unread``: the files that could not be read — one with no ``conversation_message``, with a
      table of these facts that will not read in either reading, or with a -wal beside a file that
      will not open without it (its checkpointed reading was not read). A table that is missing or
      will not read is not one that holds nothing, and each fact is what an absence is said from;
    * ``nameless``: the files read in full that name no account (no ``required_values`` ``USERID``),
      so a claim may be the account of one of them without being in ``accounts``.

    Ids are lower-cased.
    """
    held, convs, accounts, unread, nameless = set(), set(), set(), [], []
    for path in arroyos:
        views = None
        try:
            views = sqlite_open.open_views(path)
            if views.merged is None:
                raise sqlite3.DatabaseError("not a database that opens")
            if views.main_only is None and views.info.get("db_bytes"):
                # a -wal beside a file that will not open on its own: the checkpointed reading, the
                # only one of a row the -wal has since deleted, was not read
                raise sqlite3.DatabaseError("the checkpointed copy does not open")
            rows = _rows(views, "conversation_message",
                         ("client_conversation_id", "server_message_id"), ("client_message_id",))
            if rows is None:
                unread.append(path)
                continue
            for conv, smid, cmid in rows:
                # numbered as the reports find a message (arroyo_content.message_key)
                number = arroyo_content.message_number(smid) or arroyo_content.message_number(cmid)
                if isinstance(conv, str) and number:
                    held.add((conv.lower(), number))
            for table in CONVERSATION_TABLES:
                rows = _rows(views, table, ("client_conversation_id",), distinct=True) or []
                convs.update(conv.lower() for (conv,) in rows if isinstance(conv, str) and conv)
            own = {str(value).strip().lower()
                   for key, value in _rows(views, "required_values", ("key", "value")) or []
                   if str(key).upper() == "USERID" and str(value or "").strip()}
            accounts.update(own)
            if not own:
                nameless.append(path)
        except Exception as error:                                 # noqa: BLE001 - one database
            logger.debug(f"--survey-claim-links: {path} not read ({error})")
            if path not in unread:
                unread.append(path)
        finally:
            if views is not None:
                views.close()
    return held, convs, accounts, unread, nameless


#: What arroyo.db says about the message an untied key names (see :func:`_named_label`).
NAMED_HELD = "names a message arroyo.db holds"
NAMED_IN_CONVERSATION = "names a message arroyo.db does not hold, in a conversation it holds"
NAMED_NO_CONVERSATION = "names a conversation arroyo.db does not hold"
NAMED_OWN_ACCOUNT = NAMED_NO_CONVERSATION + " — claimed by arroyo.db's own account"
NAMED_OTHER_ACCOUNT = NAMED_NO_CONVERSATION + " — claimed by another account"
NAMED_NOT_READ = "names a message not found (arroyo.db not read)"


def _named_label(conv, number, user, held, convs, accounts, read=True, *, every_account=True):
    """What ``arroyo.db`` holds of the message an untied key names: conversation ``conv``, message
    ``number``, claimed by account ``user`` — against the ``held``, ``convs`` and ``accounts`` of
    :func:`_arroyo_facts`. ``read`` is false unless every arroyo.db was read in full;
    ``every_account`` is false when one of them named no account (its ``nameless``).

    The message first, then its conversation, then whose claim it is — the order of the table in
    docs/claim_link_survey.md. A conversation no arroyo.db holds, claimed by an account none of them
    belongs to, every one's account read, is one only that account's own chat database could tie:
    no rule written from these can. A claim of one of their accounts is that account's whatever the
    others name; one of no account read is another account's only when no arroyo.db's is unknown.
    """
    conv, user = conv.lower(), (user or "").strip().lower()
    if (conv, number) in held:
        return NAMED_HELD
    if not read:
        return NAMED_NOT_READ
    if conv in convs:
        return NAMED_IN_CONVERSATION
    if user and user in accounts:
        return NAMED_OWN_ACCOUNT
    if user and accounts and every_account:
        return NAMED_OTHER_ACCOUNT
    return NAMED_NO_CONVERSATION


def _named_files(root, name):
    return sorted(os.path.join(d, n) for d, _dirs, names in os.walk(root) for n in names
                  if n == name)


_CACHES = re.compile(r"(?:^|/)Library/Caches/", re.I)


def cache_files(root):
    """``[(path under Library/Caches, file path)]`` for every file there whose path carries a UUID."""
    out = []
    for dirpath, _dirs, names in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/") + "/"
        mo = _CACHES.search(rel_dir)
        if not mo:
            continue
        for name in sorted(names):
            rel = rel_dir[mo.end():] + name
            if _UUID.search(rel):
                out.append((rel, os.path.join(dirpath, name)))
    return out


def file_shape(rel):
    """A path with its UUIDs, long hex runs, numbers and owner usernames replaced by placeholders."""
    shape = _UUID.sub("<uuid>", mask_names(rel))
    shape = re.sub(r"(?<![0-9A-Za-z])[0-9a-fA-F]{16,}(?![0-9A-Za-z])",
                   lambda m: f"<hex{len(m.group(0))}>", shape)
    return re.sub(r"(?<![<A-Za-z])\d+(?![>])", "<n>", shape)


def _databases(root):
    """``(searched, skipped)``: every SQLite database under ``root``, the larger ones set aside."""
    searched, skipped = [], []
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            path = os.path.join(dirpath, name)
            if name.endswith(("-wal", "-shm", "-journal")) or not trace_ids._is_sqlite(path):
                continue
            (skipped if os.path.getsize(path) > trace_ids.SQL_PASS_MAX_BYTES else searched).append(path)
    return searched, skipped


# ------------------------------------------------------------------------------- the databases

def _row_values(row):
    """``(column, field path, value, text)`` for each value of a row worth looking an id up in."""
    for column, value in row.items():
        if isinstance(value, str):
            yield column, "", value.encode("utf-8", "surrogatepass"), value
        elif isinstance(value, (bytes, bytearray, memoryview)):
            data = bytes(value)
            if not data:
                continue
            yield column, "", data, None
            walked = protobuf_wire.values_with_paths(data)
            if walked:
                for path, inner, text in walked:
                    yield column, ".".join(str(n) for n in path), bytes(inner), text
            else:                       # a binary plist or another container: its ASCII strings
                for mo in _PRINTABLE.finditer(data):
                    found = mo.group(0)
                    yield column, "(text inside the blob)", found, found.decode("ascii")


def _row_identity(table, row, n):
    if table == "conversation_message" and row.get("client_conversation_id") is not None:
        return (table, str(row.get("client_conversation_id")), str(row.get("server_message_id")),
                str(row.get("client_message_id")))
    return (table, n)


def needles(subjects):
    """``(canons, by_bytes)`` for the ids of ``subjects``: every id (:attr:`Ident.canon`), and the
    bytes each stands for (a UUID's little-endian bytes too) mapped to ``{(canon, variant)}``."""
    canons, by_bytes = set(), collections.defaultdict(set)
    for subject in subjects:
        for ident in subject["idents"]:
            canons.add(ident.canon)
            kind, value = ident.canon
            if kind == "bytes":
                by_bytes[value].add((ident.canon, ""))
                if ident.label == "<uuid>":
                    by_bytes[uuid.UUID(bytes=value).bytes_le].add((ident.canon, ", little-endian"))
    return canons, by_bytes


def scan_database(db_path, canons, by_bytes, found, progress=None, label=""):
    """Look every id up in every row of ``db_path``; fill ``found``. Returns the row count.

    A value is matched whole against the bytes an id stands for; a text is cut into ids the way a
    key is, and each matched by identity. ``label`` names the database in what is found.
    """
    views = sqlite_open.open_views(db_path)
    rows_read = 0
    try:
        tables = [r[0] for r in views.merged.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")] if views.merged else []
        for table in tables:
            try:
                rows, markers = sqlite_open.read_table(views, table)
            except Exception as error:                             # noqa: BLE001 - one table
                logger.debug(f"--survey-claim-links: {label} {table} not readable ({error})")
                continue
            for n, (row, reading) in enumerate(zip(rows, markers)):
                rows_read += 1
                if progress and rows_read % PROGRESS_ROWS == 0:
                    progress(f"--survey-claim-links: {label}: {rows_read} row(s) read")
                identity = _row_identity(table, row, n)
                content_type = row.get("content_type") if table == "conversation_message" else None
                # a message the server never numbered (not sent, or still sending) has no link target
                # in the reports by number: say which kind of message the id was found in
                numbered = ((row.get("server_message_id") is not None)
                            if table == "conversation_message" else None)
                seen = set()
                for column, path, value, text in _row_values(row):
                    hits = [(canon, "a whole value, as bytes" + variant)
                            for canon, variant in by_bytes.get(value, ())]
                    if text is not None and len(text) >= MIN_ID_CHARS:
                        shape, idents = read_key(text)
                        # a text that is one id and nothing else holds it whole, padding or not
                        how = ("a whole text" if len(idents) == 1 and shape == idents[0].label
                               else "inside a text")
                        hits += [(ident.canon, f"{how}, as {ident.label}") for ident in idents
                                 if ident.canon in canons]
                    for canon, held_as in hits:
                        where = (label, table, column, path, content_type, numbered, reading,
                                 held_as)
                        if (canon, where) in seen:
                            continue
                        seen.add((canon, where))
                        entry = found.setdefault(canon, {"where": collections.Counter(),
                                                         "rows": {}})
                        entry["where"][where] += 1
                        # per database, one past the cap: a database set aside later (a claim's
                        # own) takes its rows with it, and the count stays exact up to the cap
                        rows = entry["rows"].setdefault(label, set())
                        if len(rows) <= ROW_CAP:
                            rows.add(identity)
    finally:
        views.close()
    return rows_read


def _bucket(entry):
    count = sum(len(rows) for rows in entry["rows"].values())
    if count > ROW_CAP:
        return MORE
    return next(name for name, top in ROW_BUCKETS if count <= top)


def _claim_group(claim):
    """A claim's group: its context, its shape and the ids in it — so that every claim of a group
    has the same id positions."""
    return (claim["context"], claim["shape"], tuple(i.label for i in claim["idents"]))


def _file_group(entry):
    return (entry["shape"], tuple(i.label for i in entry["idents"]))


def claims_of(claims, group):
    """The claims of one aggregated group (those in its first subject's group)."""
    key = _claim_group(group["subject"])
    return [c for c in claims if _claim_group(c) == key]


def _aggregate(subjects, found, group_of):
    """Per group (a key shape, a path shape): how many subjects, how many with an id found, and for
    each id position where it was found and in how many rows each id occurs."""
    groups = {}
    for subject in subjects:
        group = groups.setdefault(group_of(subject), {
            "subject": subject, "count": 0, "status": collections.Counter(),
            "found": collections.Counter(),
            "ids": [{"position": i + 1, "id": ident.label, "found": collections.Counter(),
                     "rows_per_id": collections.Counter()}
                    for i, ident in enumerate(subject["idents"])]})
        status = subject.get("status", "")
        group["count"] += 1
        group["status"][status] += 1
        any_found = False
        for slot, ident in zip(group["ids"], subject["idents"]):
            hit = found.get(ident.canon)
            if not hit:
                continue
            any_found = True
            for where in hit["where"]:
                slot["found"][(status,) + where] += 1
            slot["rows_per_id"][_bucket(hit)] += 1
        if any_found:
            group["found"][status] += 1
        if subject.get("named"):
            group.setdefault("named", collections.Counter())[subject["named"]] += 1
    return list(groups.values())


def _found_json(slot, count_name, with_status):
    rows = []
    for (status, database, table, column, path, ct, numbered, reading, how), n \
            in slot["found"].most_common():
        row = {count_name: n}
        if with_status:
            row["claim_status"] = status
        row.update({"database": database, "table": table, "column": column, "field": path,
                    "content_type": ct, "reading": reading, "held_as": how})
        if numbered is not None:
            row["server_message_id"] = "present" if numbered else "absent"
        rows.append(row)
    return {"position": slot["position"], "id": slot["id"], "found": rows,
            "rows_per_id": dict(slot["rows_per_id"])}


# ------------------------------------------------------------------------------- the survey

def survey(run_folder, progress=None):
    """Survey ``run_folder``; return the report as a dict (see the module docstring)."""
    progress = progress or logger.info
    started = time.monotonic()
    root = trace_ids.extracted_root(run_folder)
    shown = trace_ids.device_path_namer(root)
    controllers = _named_files(root, "cache_controller.db")
    arroyos = _named_files(root, "arroyo.db")
    report_dir = os.path.join(run_folder, "Reports")
    from scripts.cache_controller_report import load_chat_ids, load_chat_links
    by_key, by_message = load_chat_links(report_dir)
    chat_ids = load_chat_ids(report_dir, by_message)
    apps = _apps(controllers)
    snap_ids, filter_urls = _memory_keys(apps)
    items = _ctp_index(apps)
    manifest = next((os.path.join(report_dir, r, "cache_links.json")
                     for r in ("Conversations", "Communications_legacy", "Communications")
                     if os.path.isfile(os.path.join(report_dir, r, "cache_links.json"))), None)

    # 1. the claims, looked up in arroyo.db
    claims, seen = [], set()
    for path in controllers:
        rows, _markers, _info = sqlite_open.read_all(path, "CACHE_FILE_CLAIM")
        for row in rows:
            # a claim the two readings hold in different versions is still one claim
            ident = (path, row.get("USER_ID"), row.get("CACHE_KEY"), row.get("EXTERNAL_KEY"),
                     row.get("MEDIA_CONTEXT_TYPE"))
            if ident in seen:
                continue
            seen.add(ident)
            shape, idents = read_key(row.get("EXTERNAL_KEY"))
            claims.append({"context": row.get("MEDIA_CONTEXT_TYPE"), "shape": shape,
                           "idents": idents, "_key": str(row.get("EXTERNAL_KEY") or ""),
                           "_user": str(row.get("USER_ID") or ""),
                           "status": _link_status(row, by_key, by_message, chat_ids, snap_ids,
                                                  filter_urls=filter_urls, items=items)})
    progress(f"--survey-claim-links: {len(claims)} claim(s) in {len(controllers)} "
             f"cache_controller.db, chat links from {manifest or 'no Conversations report'}")

    # A key naming a conversation and a message that no report ties: does arroyo.db hold the
    # message, or its conversation, and whose claim is it? Every arroyo.db of the extraction is read
    # (a run reads one), so "does not hold" means none of them does.
    from scripts.cache_controller_report import _CHAT_EK_RE
    held, convs, accounts, unread, nameless = _arroyo_facts(arroyos)
    if unread:
        progress(f"--survey-claim-links: {len(unread)} of {len(arroyos)} arroyo.db could not be read "
                 f"in full (its messages, conversations or account) - nothing a key names is said "
                 f"to be absent")
    if nameless:
        progress(f"--survey-claim-links: {len(nameless)} of {len(arroyos)} arroyo.db name no account "
                 f"(no required_values USERID) - no claim is said to be another account's")
    # "does not hold" only when every arroyo.db was read; otherwise all that is known is "not found"
    read = bool(arroyos) and not unread
    for claim in claims:
        mo = _CHAT_EK_RE.match(claim.pop("_key", ""))
        user = claim.pop("_user", "")
        if mo and claim["status"] == "none":
            claim["named"] = _named_label(mo.group("conv"), mo.group("msg"), user, held, convs,
                                          accounts, read, every_account=not nameless)
    # 2. Library/Caches files whose path carries a UUID
    files = [{"shape": file_shape(rel),
              "idents": [i for i in read_key(rel)[1] if i.label == "<uuid>"]}
             for rel, _path in cache_files(root)]

    # Both looked up in every database in one pass. arroyo.db is what a link to a message reads;
    # the others say what else holds an id — a Story, a preference, a Memory.
    searched, skipped = _databases(root)
    canons, by_bytes = needles(claims + files)
    found, rows_read, file_rows = {}, 0, 0
    progress(f"--survey-claim-links: {len(files)} file(s) under Library/Caches carry a UUID; looking "
             f"the ids of {len(claims)} claim(s) and those files up in {len(searched)} database(s)")
    for path in searched:
        count = scan_database(path, canons, by_bytes, found, progress, os.path.basename(path))
        file_rows += count
        if os.path.basename(path) == "arroyo.db":
            rows_read += count

    def claim_hits(entry):
        """A claim's ids are in cache_controller.db by definition — its own row, its siblings' keys,
        its file's retrieval metadata — so that database is not where else an id is held."""
        where = collections.Counter({w: n for w, n in entry["where"].items()
                                     if w[0] != "cache_controller.db"})
        rows = {db: r for db, r in entry["rows"].items() if db != "cache_controller.db"}
        return dict(entry, where=where, rows=rows) if where else None
    claim_found = {canon: hit for canon, hit in ((c, claim_hits(e)) for c, e in found.items()) if hit}
    shapes = _aggregate(claims, claim_found, _claim_group)
    for group in shapes:
        group["in_arroyo"] = collections.Counter()
        for subject in claims_of(claims, group):
            if any(w[0] == "arroyo.db" for i in subject["idents"]
                   for w in (claim_found.get(i.canon) or {}).get("where", ())):
                group["in_arroyo"][subject["status"]] += 1
    shapes.sort(key=lambda g: (-g["in_arroyo"].get("none", 0), -g["found"].get("none", 0),
                               -sum(g["found"].values()), -g["count"],
                               str(g["subject"]["context"]), g["subject"]["shape"]))
    file_shapes = _aggregate(files, found, _file_group)
    file_shapes.sort(key=lambda g: (-sum(g["found"].values()), -g["count"], g["subject"]["shape"]))

    return {
        "tool": "Snapchat_Auto --survey-claim-links",
        "version": trace_ids._version(),
        "created_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_folder": os.path.abspath(run_folder),
        "elapsed_s": round(time.monotonic() - started, 1),
        "sources": {"cache_controller": [shown(p) for p in controllers],
                    "arroyo": [shown(p) for p in arroyos],
                    "chat_links": os.path.relpath(manifest, run_folder) if manifest else None,
                    "file_databases": [shown(p) for p in searched],
                    "file_databases_skipped": [shown(p) for p in skipped]},
        "claims": len(claims),
        "arroyo_rows": rows_read,
        "shapes": [{"context": g["subject"]["context"], "shape": g["subject"]["shape"],
                    "claims": g["count"], "status": dict(g["status"]),
                    "found_in_arroyo": dict(g["in_arroyo"]),
                    "found_in_any_database": dict(g["found"]),
                    **({"untied_named_message": dict(g["named"])} if g.get("named") else {}),
                    "ids": [_found_json(slot, "claims", True) for slot in g["ids"]]}
                   for g in shapes],
        "cache_files": len(files),
        "database_rows": file_rows,
        "file_shapes": [{"shape": g["subject"]["shape"], "files": g["count"],
                         "found_in_databases": sum(g["found"].values()),
                         "ids": [_found_json(slot, "files", False) for slot in g["ids"]]}
                        for g in file_shapes],
    }


#: Shapes listed in the log in full; the JSON always has all of them.
LOG_SHAPES = 40


def _where(f):
    where = f"{f['database']} > {f['table']}.{f['column']}"
    where += f" field {f['field']}" if f["field"] else ""
    where += f", content_type {f['content_type']}" if f["content_type"] is not None else ""
    return where + (", a message the server never numbered"
                    if f.get("server_message_id") == "absent" else "")


def describe(payload):
    """Log lines: the totals, each claim shape whose untied claims carry an id arroyo.db holds, and
    each Library/Caches path shape whose UUID a database holds. Shapes and locations only."""
    lines = [f"{payload['claims']} claim(s) in {len(payload['shapes'])} key shape(s); "
             f"{payload['arroyo_rows']} arroyo.db row(s) read (both readings)"]
    if not payload["sources"]["chat_links"]:
        lines.append("  no chat link manifest in Reports/ - every claim counts as untied to a "
                     "message; run the full pipeline first for the reports' own links")
    named = [s for s in payload["shapes"] if s.get("untied_named_message")]
    if named:
        lines.append("Untied claims whose key names a conversation and message:")
    for s in named[:LOG_SHAPES]:
        lines.append(f"  context {s['context']} {s['shape']}: "
                     + ", ".join(f"{n} {what}" for what, n in s["untied_named_message"].items()))
    lead = [s for s in payload["shapes"] if s["found_in_arroyo"].get("none")]
    # in arroyo.db: only what arroyo.db holds is listed under these shapes
    lead = [dict(s, ids=[dict(slot, found=[f for f in slot["found"] if f["database"] == "arroyo.db"])
                         for slot in s["ids"]]) for s in lead]
    lines.append(f"{len(lead)} shape(s) with claims no report ties to a message whose ids "
                 f"arroyo.db holds:" if lead else
                 "No claim that no report ties to a message has an id arroyo.db holds.")
    for s in lead[:LOG_SHAPES]:
        lines.append(f"  context {s['context']} {s['shape']}: {s['found_in_arroyo']['none']} of "
                     f"{s['status'].get('none', 0)} untied claim(s) ({s['claims']} in all)")
        for slot in s["ids"]:
            for f in [f for f in slot["found"] if f["claim_status"] == "none"][:5]:
                lines.append(f"    id {slot['position']} {slot['id']}: {f['claims']} claim(s), "
                             f"{_where(f)}, held as {f['held_as']} [{f['reading']}]")
            if slot["rows_per_id"]:
                lines.append(f"    id {slot['position']} {slot['id']}: rows per id "
                             + ", ".join(f"{k}: {v}" for k, v in slot["rows_per_id"].items()))
    if len(lead) > LOG_SHAPES:
        lines.append(f"  ... and {len(lead) - LOG_SHAPES} more shape(s) - see the JSON")

    elsewhere = [s for s in payload["shapes"] if s["found_in_any_database"].get("none")
                 and not s["found_in_arroyo"].get("none")]
    if elsewhere:
        lines.append(f"{len(elsewhere)} shape(s) with untied claims whose ids another database "
                     f"holds (not arroyo.db):")
    for s in elsewhere[:LOG_SHAPES]:
        lines.append(f"  context {s['context']} {s['shape']}: {s['found_in_any_database']['none']} "
                     f"of {s['status'].get('none', 0)} untied claim(s)")
        for slot in s["ids"]:
            for f in [f for f in slot["found"] if f["claim_status"] == "none"][:5]:
                lines.append(f"    id {slot['position']} {slot['id']}: {f['claims']} claim(s), "
                             f"{_where(f)}, held as {f['held_as']} [{f['reading']}]")

    found = [s for s in payload["file_shapes"] if s["found_in_databases"]]
    lines.append(f"{payload['cache_files']} file(s) under Library/Caches carry a UUID, in "
                 f"{len(payload['file_shapes'])} path shape(s); "
                 f"{len(payload['sources']['file_databases'])} database(s) searched, "
                 f"{payload['database_rows']} row(s)"
                 + (f", {len(payload['sources']['file_databases_skipped'])} larger one(s) skipped"
                    if payload["sources"]["file_databases_skipped"] else ""))
    lines.append(f"{len(found)} path shape(s) whose UUID a database holds:" if found else
                 "No database holds the UUID of a Library/Caches file.")
    for s in found[:LOG_SHAPES]:
        lines.append(f"  {s['shape']}: {s['found_in_databases']} of {s['files']} file(s)")
        for slot in s["ids"]:
            for f in slot["found"][:5]:
                lines.append(f"    id {slot['position']} {slot['id']}: {f['files']} file(s), "
                             f"{_where(f)}, held as {f['held_as']} [{f['reading']}]")
            if slot["rows_per_id"]:
                lines.append(f"    id {slot['position']} {slot['id']}: rows per id "
                             + ", ".join(f"{k}: {v}" for k, v in slot["rows_per_id"].items()))
    unfound = [s for s in payload["file_shapes"] if not s["found_in_databases"]]
    if unfound:
        lines.append(f"  not held by any database: "
                     + ", ".join(f"{s['shape']} ({s['files']})" for s in unfound[:LOG_SHAPES])
                     + (" ..." if len(unfound) > LOG_SHAPES else ""))
    return lines


def write_report(run_folder, payload):
    """Write ``claim_link_survey_<stamp>.json`` into the run folder; return its path."""
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(run_folder, f"claim_link_survey_{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return path
