"""
Snapchat conversations report — an index of every conversation plus one detail page per conversation.

``Reports/Conversations/``::

    Conversations_report.html      the index: one row per conversation
    assets/ui.css, assets/ui.js    the shared UI, loaded by the index and every detail page
    data/index.js                  the index rows
    pages/<key>.html               one detail page per conversation (its message table)
    pages/data/<key>/index.js      that conversation's message rows
    pages/data/<key>/detail-<n>.js the per-message detail, fetched only when a row is expanded
    media/<name>.<ext>             the chat attachments, hard-linked from the parser's cacheFiles
    conversation_pages.json        conversation id -> detail page (for other reports/tools)
    cache_links.json               the attachment manifest the cache_controller report links back with

Both tables are the shared **virtual table** (:mod:`scripts.report_ui`), which is what makes this
scale: the documents stay a few KB whatever the number of conversations *or* the number of messages
in one conversation, and search / sort / paging still run over the whole set. That matters here even
more than in the other reports — a single active conversation can hold tens of thousands of
messages, which is exactly the shape that made the old single-document reports unusable.

Where the data comes from
-------------------------
This report does **not** re-parse the chat database. It renders the message table
``ParseSnapchat_iOS`` has already assembled (``arroyo.db`` → ``conversation_message``, joined to
``cache_controller.db`` → ``CACHE_FILE_CLAIM`` and to the ``SCPersistentMedia`` copies — see
:doc:`report_communications`), so both the legacy Communications report and this one describe the
same rows. What it adds is structure (per-conversation pages), the timezone the examiner chose,
attachment hashes, and provenance for every derived value.

Conversation identity comes from three places, and each conversation records which one named it:
the groups list (``GROUP_NAME``), the friends list (a friend's ``CONVERSATION_ID``), and — when
``arroyo.db`` still has the table — ``user_conversation`` for the conversation type and its
participant user ids.
"""

import os
import re
import json
import html
import shutil
import sqlite3
import hashlib
import logging
from datetime import datetime, timezone

from scripts import report_ui
from scripts import app_version
from scripts import partial_report
from scripts.data import sqlite_open
from scripts.contacts_report import (normalize_contacts, normalize_groups, apply_identifiers,
                                     load_identifiers, contact_link_index, contact_anchor,
                                     text_html, cell)
# Reused so every report of a run labels and converts timestamps identically (DST-aware).
from scripts.memories_media_report import make_time_formatter, guess_media

try:
    import filetype                                            # already a project dependency
except Exception:                                              # pragma: no cover
    filetype = None

logger = logging.getLogger(__name__)

# Cocoa epoch (2001-01-01) as Unix seconds — the shared timestamp formatter takes a Cocoa
# timestamp, so the Unix times parsed out of the parser's UTC strings are shifted onto it.
_COCOA_EPOCH = 978307200

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

# Attachment kinds the report can display inline. Anything else is still listed, with its detected
# type — a file that cannot be rendered must be visible, not silently dropped.
_VIDEO_EXT = {"mp4", "mov", "m4v", "webm"}
_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif"}

# Content Type values that mergeCacheChats assigns to a row *because of the cache claim it merged
# in*. A message with several claims produces one row per claim, and the rows whose claim has no
# renderable file are the duplicates — the legacy report drops exactly these two (see
# `_drop_unrenderable`).
_MEDIA_ONLY_TYPES = {"Video (Unknown Source)": "video", "Sticker": "image"}

# Columns of the message frame ParseSnapchat_iOS hands over (after its final rename). The two id
# columns marked optional only exist when this app version's conversation_message has them.
COL_CONV = "Client Conversation ID"
COL_SCONV = "Server Conversation ID"                           # optional
COL_SENDER = "Sender ID"
COL_CONTENT = "Message Content"
COL_TYPE = "Content Type"
COL_RAWTYPE = "Content Type (arroyo)"                          # optional: the numeric value the
                                                               # label above was derived from
COL_CREATED = "Creation Timestamp UTC+0"
COL_READ = "Read Timestamp UTC+0"
COL_SMID = "Server Message ID"
COL_CMID = "Client Message ID"                                 # optional
COL_TEXT = "Message Text"                                      # the parsed text, kept by the parser
COL_WAL = "WAL View"                                           # which reading of arroyo.db (optional)
                                                               # before the attachment replaced it

# What getChats writes when a message's protobuf could not be parsed.
_PARSE_ERROR = "ERROR - Something went wrong when parsing this message"

# An EXTERNAL_KEY-shaped value: "<type>:<conversation uuid>:<message>:<part>".
_EXTKEY_RE = re.compile(r"^[^:]*:[0-9a-fA-F-]{36}:\d+")

# C0 control characters. A value that came through the concatenating fallback carries the protobuf
# field byte that followed the string (a media id arrives as "<mediaId>\x04"). Real chat text keeps
# its newlines and tabs; nothing else in this range belongs in a message.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _own_text(raw, atts, conv_id, raw_type=""):
    """The message's **own** text, or "" when the parsed value is not text at all.

    The parser reads a message's text out of the protobuf field that holds it
    (``ParseSnapchat_iOS.messageText``), so what arrives here is normally either the message or
    nothing. The tests below are the screen for a value that came through the older path instead,
    which concatenated every string in the protobuf and so produced a media id or a cache claim's
    EXTERNAL_KEY where the text should be. The raw value is always listed in the row's detail, so
    nothing is hidden either way.

    ``raw_type`` — arroyo's own ``content_type`` — is deliberately **not** used to decide this. A
    media message can carry a caption the sender typed, so gating on "is this a text message" would
    drop real evidence.
    """
    text = _CONTROL_RE.sub("", cell(raw))
    if not text or text.startswith(_PARSE_ERROR):
        return ""
    low = text.strip()
    if any(low == a["name"] for a in atts):                    # the attachment's own file name
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{32}", low):                  # a cache key
        return ""
    if _EXTKEY_RE.match(low) or (conv_id and conv_id.lower() in low.lower()):
        return ""                                              # a cache claim's EXTERNAL_KEY
    if _UUID_RE.match(low):                                    # a bare media / message id
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{20,24}(\.\d+)?", low):     # a bare media id, with or without
        return ""                                              # the ".1020" content-type suffix
    return text

# Index-table geometry (one fixed row height + one column track list for the header and every row).
# The conversation id sits next to the name it belongs to rather than at the far right: they are two
# forms of the same answer to "which conversation is this", and an examiner reads them together.
CONV_COLS = ("24px 86px minmax(160px,1.1fr) 250px minmax(170px,1.2fr) 66px 66px 152px 152px 96px")
CONV_ROW_H = 46
# Message-table geometry on a detail page. The content column is the wide one; long text is clipped
# to the row and shown in full when the row is expanded.
MSG_COLS = ("24px 152px 104px minmax(110px,0.8fr) 138px minmax(240px,2fr) 104px 146px")
MSG_ROW_H = 74

# Sort key for a message with no creation timestamp. The rows are written in chronological order
# with these last, and this keeps them there when the table is sorted on the Created column
# (a missing timestamp is "unknown", not "the oldest").
_NO_TIME_SORT = 9e15


# --------------------------------------------------------------------------- helpers

def _esc(value):
    return html.escape(str(value)) if value not in (None, "") else ""


def _fmt_bytes(n):
    if not isinstance(n, (int, float)) or not n:
        return ""
    if n < 1024:
        return f"{int(n)} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _parse_utc(value):
    """The parser's ``'YYYY-MM-DD HH:MM:SS'`` UTC string -> Unix seconds, or None.

    ``getChats`` formats the arroyo millisecond timestamps with SQLite's
    ``datetime(creation_timestamp/1000,'unixepoch')``, i.e. UTC with no zone marker, and rows that
    exist only because of a cache claim carry the literal ``"Unknown"``.
    """
    text = cell(value)
    if not text:
        return None
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _short(text, limit):
    """Shorten a title for a ``<title>`` tag without cutting a ``&#NNNN;`` entity in half."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return re.sub(r"&[^;]*$", "", text[:limit]) + "…"


def _id_str(value):
    """An identifier cell as text, without the ``.0`` pandas leaves on an integer id.

    A column that has any missing value becomes float64, so ``client_message_id`` 1004 arrives as
    ``1004.0``. (This is only ever applied to whole-number ids — **not** to the server message id,
    whose ``.0`` is a real part index.)
    """
    text = cell(value)
    return text[:-2] if re.fullmatch(r"-?\d+\.0", text) else text


def _page_key(conv_id):
    """A filesystem-safe page name for a conversation id (they are UUIDs, but never trust that)."""
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", str(conv_id))
    if safe != str(conv_id) or len(safe) > 60 or not safe:
        safe = (safe[:40] or "conv") + "-" + hashlib.sha1(
            str(conv_id).encode("utf-8", "replace")).hexdigest()[:8]
    return safe


def _hashes(path):
    """(md5, sha256, size) of a file, streamed so any size is safe."""
    md5, sha, total = hashlib.md5(), hashlib.sha256(), 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            md5.update(chunk)
            sha.update(chunk)
            total += len(chunk)
    return md5.hexdigest(), sha.hexdigest(), total


def _detect_ext(path):
    """The file's real extension from its magic bytes (``filetype``, else the built-in sniffer)."""
    if filetype is not None:
        try:
            kind = filetype.guess(path)
            if kind is not None:
                return kind.extension
        except Exception as error:                             # unreadable / zero-byte file
            logger.debug(f"Could not sniff {path}: {error}")
    try:
        with open(path, "rb") as fh:
            return guess_media(fh.read(16))
    except OSError:
        return None


# --------------------------------------------------------------------------- attachments

def publish_attachment(cachefiles_dir, media_dir, basename, cache_key_for=None, cache=None):
    """Make one chat attachment openable from this report, and describe it.

    Returns ``None`` when ``basename`` is not a file in the parser's ``cacheFiles`` folder (i.e. the
    message content is text, not an attachment), else a dict with the published relative path, the
    detected type, the size and the **MD5 / SHA-256 of the bytes as extracted** — the report has to
    let the examiner corroborate the file it shows against the extraction.

    The published copy is a **hard link** (no bytes duplicated) named with the real extension,
    because cache files are named after their ``CACHE_KEY`` with no extension and browsers handle an
    extensionless ``file://`` link inconsistently (Chrome downloads it, ``<video>`` refuses it). Only
    if the filesystem refuses to link is a real copy made.
    """
    if cache is not None and basename in cache:
        return cache[basename]
    source = os.path.join(cachefiles_dir, basename)
    if not (basename and os.path.isfile(source)):
        return None
    ext = _detect_ext(source) or ""
    kind = "video" if ext in _VIDEO_EXT else "image" if ext in _IMAGE_EXT else "file"
    existing = os.path.splitext(basename)[1].lstrip(".").lower()
    name = basename if existing and existing == ext else f"{basename}.{ext}" if ext else basename
    name = re.sub(r"[^0-9A-Za-z_.-]", "_", name)
    os.makedirs(media_dir, exist_ok=True)
    target = os.path.join(media_dir, name)
    published, how = True, "hard link to the file the parser copied out of the extraction"
    if not os.path.exists(target):
        try:
            os.link(source, target)
        except OSError:
            try:
                shutil.copy2(source, target)
                how = "copy (the filesystem does not support hard links here)"
            except OSError as error:
                logger.debug(f"Could not publish attachment {basename}: {error}")
                published, how = False, "could not be published next to this report"
    try:
        md5, sha256, size = _hashes(source)
    except OSError as error:
        logger.debug(f"Could not hash attachment {basename}: {error}")
        md5, sha256, size = "", "", 0
    cache_key, cache_key_how = (cache_key_for(basename) if cache_key_for else (None, ""))
    info = {"name": basename, "ext": ext, "kind": kind, "bytes": size, "md5": md5,
            "sha256": sha256, "rel": ("media/" + name) if published else None, "how": how,
            "cache_key": cache_key, "cache_key_how": cache_key_how}
    if cache is not None:
        cache[basename] = info
    return info


def _attachment_cell(att, prefix="../"):
    """The message row's content cell for an attachment: a real thumbnail / play button."""
    if not att:
        return ""
    if not att["rel"]:
        return (f'<span class="filenone">{_esc(att["name"])} &mdash; '
                f'{_esc(att["ext"] or "unknown type")}, not published</span>')
    if not att["bytes"]:
        return '<span class="filenone">0 bytes on disk</span>'
    url = _esc(prefix + att["rel"])
    if att["kind"] == "image":
        return (f'<a class="filebtn img" href="{url}" target="_blank" '
                f'title="open {_esc(att["name"])}"><img src="{url}" loading="lazy">'
                f'<span class="lbl">{_esc(att["ext"])}</span></a>')
    if att["kind"] == "video":
        return (f'<a class="filebtn play" href="{url}" target="_blank" '
                f'title="open {_esc(att["name"])}">&#9654; <span class="lbl">'
                f'{_esc(att["ext"])}</span></a>')
    return (f'<a class="filebtn" href="{url}" target="_blank" title="open {_esc(att["name"])}">'
            f'{_esc(att["ext"] or "file")}</a>')


# --------------------------------------------------------------------------- message model

def _drop_unrenderable(ctype, att):
    """Whether this row is one of the merge's duplicate rows and should not be listed.

    ``mergeCacheChats`` left-joins every ``CACHE_FILE_CLAIM`` of a message onto that message, so a
    message with three claims becomes three rows. For the two content types that only ever *are*
    their attachment ("Video (Unknown Source)" from ``content_type`` 3 and "Sticker" from
    ``content_type`` 5) a row whose claim has no renderable file on disk is such a duplicate, and the
    legacy report drops it too. Everything else is kept, even when its file is missing — a message
    whose media was not recovered is a finding, not noise.
    """
    want = _MEDIA_ONLY_TYPES.get(ctype)
    return bool(want) and (att is None or att["kind"] != want)


def build_messages(msg_df, cachefiles_dir, media_dir, timefmt, cache_key_for=None,
                   owner_user_id="", owner_names=()):
    """Turn the parser's message frame into ``{conversation id: [message dicts]}``.

    One dict per **message**, not per parsed row: the message/cache join emits one row per cache
    claim, so a message that carries two files (a video and its thumbnail, say) arrives as two
    otherwise identical rows. Listing those separately reads as two messages sent at the same second
    by the same person, which is wrong — so rows sharing a conversation and a server message id are
    merged into one message holding a list of attachments (:func:`_merge_rows`).

    ``timefmt`` is the shared Cocoa formatter, so displayed times honour the examiner's timezone
    while the raw UTC value the database yields is kept alongside it.
    """
    by_conv, att_cache = {}, {}
    owner_lc = {n.lower() for n in owner_names if n}
    owner_id_lc = cell(owner_user_id).lower()
    dropped = skipped_conv = 0
    if msg_df is None or len(msg_df) == 0:
        return by_conv, {"dropped": 0, "skipped_conv": 0}
    columns = list(getattr(msg_df, "columns", []))
    if COL_CONV not in columns:
        logger.warning(f"Conversations: the message frame has no '{COL_CONV}' column "
                       f"({columns}) — no conversation can be built from it")
        return by_conv, {"dropped": 0, "skipped_conv": len(msg_df)}
    for _index, row in msg_df.iterrows():
        conv_id = cell(row.get(COL_CONV))
        if not _UUID_RE.match(conv_id):
            # The legacy report drops these too: without a conversation id of the expected shape
            # the row cannot be attributed to a conversation.
            skipped_conv += 1
            continue
        raw = row.get(COL_CONTENT)
        content = "" if raw is None else str(raw)
        att = publish_attachment(cachefiles_dir, media_dir, content, cache_key_for, att_cache)
        ctype = cell(row.get(COL_TYPE))
        if _drop_unrenderable(ctype, att):
            dropped += 1
            continue
        sender = cell(row.get(COL_SENDER))
        sender_plain = re.sub(r"</?b>", "", sender).strip()
        created_utc = cell(row.get(COL_CREATED))
        read_utc = cell(row.get(COL_READ))
        created_unix = _parse_utc(created_utc)
        read_unix = _parse_utc(read_utc)
        # The parser marks the logged-in account by bolding its name (that is how the legacy HTML
        # report highlights it), which is the most reliable signal that a message was sent from
        # this device; the owner id / name comparison covers the sources that do not bold it.
        outgoing = bool(sender != sender_plain
                        or (sender_plain and (sender_plain.lower() in owner_lc
                                              or sender_plain.lower() == owner_id_lc)))
        # the parsed message content: kept by the parser before the attachment overwrote it, and
        # equal to the content itself for a row that has no attachment
        raw_text = cell(row.get(COL_TEXT)) if COL_TEXT in columns else ("" if att else content)
        raw_type = _id_str(row.get(COL_RAWTYPE)) if cell(row.get(COL_RAWTYPE)) else ""
        by_conv.setdefault(conv_id, []).append({
            "smid": cell(row.get(COL_SMID)),
            "cmid": _id_str(row.get(COL_CMID)),
            "sender": sender_plain,
            "sender_bold": sender != sender_plain,
            "direction": "Sent" if outgoing else ("Received" if sender_plain else ""),
            "types": [ctype] if ctype else [],
            "raw_types": [raw_type] if raw_type else [],
            "text": _own_text(raw_text, [att] if att else [], conv_id, raw_type),
            "raw_text": raw_text,
            "parse_error": bool(raw_text.startswith(_PARSE_ERROR)),
            "atts": [att] if att else [],
            # which reading of arroyo.db this row came from: MAIN_ONLY means the write-ahead log
            # has since deleted it, so the app no longer shows the message (see sqlite_open)
            "wal": cell(row.get(COL_WAL)) if COL_WAL in columns else "",
            "created_utc": created_utc,
            "created": timefmt(created_unix - _COCOA_EPOCH) if created_unix else "",
            "created_unix": created_unix,
            "read_utc": read_utc,
            "read": timefmt(read_unix - _COCOA_EPOCH) if read_unix else "",
        })
    merged = 0
    for conv_id, rows in list(by_conv.items()):
        msgs, n = _merge_rows(rows)
        merged += n
        # chronological, with the timestamp-less cache-only rows last, then by message id
        msgs.sort(key=lambda m: (m["created_unix"] is None, m["created_unix"] or 0,
                                 _smid_sort(m["smid"])))
        seen = {}
        for position, m in enumerate(msgs):                    # anchors must be unique per page
            # Rows the app had not sent yet carry no server message id, so they are anchored on
            # their position in the conversation instead.
            base = "msg-" + (re.sub(r"[^0-9A-Za-z_.-]", "_", m["smid"]) if m["smid"]
                             else f"row{position}")
            seen[base] = seen.get(base, 0) + 1
            m["anchor"] = base if seen[base] == 1 else f"{base}-{seen[base]}"
        by_conv[conv_id] = msgs
    if dropped or skipped_conv:
        logger.info(f"Conversations: skipped {dropped} duplicate media row(s) with no renderable "
                    f"file and {skipped_conv} row(s) without a usable conversation id")
    if merged:
        logger.info(f"Conversations: {merged} parsed row(s) folded into the message they belong to "
                    f"(messages carrying more than one cached file)")
    return by_conv, {"dropped": dropped, "skipped_conv": skipped_conv, "merged": merged}


def _merge_rows(rows):
    """Fold rows that describe the same message into one; return ``(messages, rows_folded)``.

    The key is the server message id, which the parser writes as ``<message>.<part>`` — so two
    *parts* of one message stay separate (they are separate sends), while the several cache claims
    of one part (full media, thumbnail, raw content claim) become one message with several
    attachments. Rows with no server message id are never merged: they are messages the app had not
    finished sending, and nothing distinguishes them from each other.
    """
    out, by_smid, folded = [], {}, 0
    for row in rows:
        first = by_smid.get(row["smid"]) if row["smid"] else None
        if first is None:
            out.append(row)
            if row["smid"]:
                by_smid[row["smid"]] = row
            continue
        folded += 1
        for att in row["atts"]:
            if att and all(att["name"] != have["name"] for have in first["atts"]):
                first["atts"].append(att)
        for ctype in row["types"]:
            if ctype not in first["types"]:
                first["types"].append(ctype)
        for raw in row["raw_types"]:
            if raw not in first["raw_types"]:
                first["raw_types"].append(raw)
        # a row that carried the text keeps it (the row a claim was joined onto has the file
        # instead), and a row that actually has a timestamp beats a cache-only row's "Unknown"
        if not first["text"] and row["text"]:
            first["text"] = row["text"]
        if not first["raw_text"] and row["raw_text"]:
            first["raw_text"] = row["raw_text"]
        first["parse_error"] = first["parse_error"] and row["parse_error"]
        if not first["created_unix"] and row["created_unix"]:
            for key in ("created_utc", "created", "created_unix", "read_utc", "read"):
                first[key] = row[key]
        if not first["sender"] and row["sender"]:
            first["sender"], first["direction"] = row["sender"], row["direction"]
        if not first["cmid"] and row["cmid"]:
            first["cmid"] = row["cmid"]
    return out, folded


def _smid_sort(smid):
    """Sort key for a '<message>.<part>' server message id ('12.0'), tolerating 'None'/''.

    A row with no id sorts last, like a row with no timestamp. (``float('inf')`` is deliberately
    not used: the row data has to survive a round trip through JSON, which has no infinity.)
    """
    try:
        return float(smid)
    except (TypeError, ValueError):
        return _NO_TIME_SORT


# --------------------------------------------------------------------------- conversation model

def _arroyo_blank():
    """A fresh record per conversation.

    A module-level template copied with ``dict()`` is a shallow copy, so every conversation shared
    one ``user_ids`` **list**: each conversation ended up holding every participant in the database,
    every contact therefore matched every conversation, and every contact's row showed the message
    total of the whole extraction. That is a false attribution of people to conversations — the kind
    :func:`contacts_report.contact_conversations` refuses to make from a display name, arrived at by
    accident instead.
    """
    return {"type": None, "user_ids": [], "server_id": "", "created_ms": None,
            "feed_first_ms": None, "feed_last_ms": None, "feed_title": "", "feed_type": None,
            "in_arroyo": False}


def load_arroyo_conversations(arroyo, msg_df=None):
    """Conversation-level facts from ``arroyo.db``, keyed by client conversation id.

    Three tables, all optional, because the schema moves between app versions:

    * ``user_conversation`` — what a conversation *is* (0 = private, 1 = group) and who is in it.
      **Absent on the newest schemas**, which is why the rest of this is worth reading.
    * ``conversation.creation_timestamp`` — when this device created its local row for the
      conversation. Verified against the corpus: it is *not* the date of the first message. On a
      device restored from a backup it is the restore, and messages years older sit under it.
    * ``feed_entry.display_timestamp`` / ``last_updated_timestamp`` — the chat-feed entry's own
      dates, i.e. what the app shows in the chat list.

    The last two exist for a conversation that holds **no message at all**, which is the only
    statement of when such a conversation was active. See :func:`build_conversations` for how they
    are used and how they are labelled — they are not message times and must not be shown as if
    they were.
    """
    out = {}
    if msg_df is not None and COL_SCONV in getattr(msg_df, "columns", []):
        for conv_id, server_id in zip(msg_df[COL_CONV], msg_df[COL_SCONV]):
            key, value = cell(conv_id), _id_str(server_id)
            if key and value:
                out.setdefault(key, _arroyo_blank())
                out[key]["server_id"] = out[key]["server_id"] or value
    if not (arroyo and os.path.isfile(arroyo)):
        return out
    # both readings of arroyo.db — a conversation row the write-ahead log has since dropped still
    # describes a conversation whose messages are in this report
    views = sqlite_open.open_views(arroyo)
    try:
        def both(sql):
            rows, _marks = sqlite_open.query_both(views, sql)
            return rows

        def rec(conv_id):
            return out.setdefault(str(conv_id), _arroyo_blank())

        try:
            for conv_id, ctype, user_ids in both(
                    "select client_conversation_id, conversation_type, group_concat(user_id) "
                    "from user_conversation group by client_conversation_id, conversation_type"):
                if not conv_id:
                    continue
                entry = rec(conv_id)
                if ctype is not None:
                    entry["type"] = ctype
                entry["user_ids"] += [u for u in str(user_ids or "").split(",") if u]
        except sqlite3.DatabaseError as error:
            logger.info(f"Conversations: user_conversation not available ({error}) — conversation "
                        f"type and participants will come from the friends/groups lists only")

        try:
            for conv_id, created in both("select client_conversation_id, creation_timestamp "
                                         "from conversation"):
                if not conv_id:
                    continue
                entry = rec(conv_id)
                entry["in_arroyo"] = True
                if created:
                    entry["created_ms"] = min(entry["created_ms"] or int(created), int(created))
        except sqlite3.DatabaseError as error:
            logger.info(f"Conversations: the conversation table is not available ({error}) — "
                        f"conversations with no message will carry no date")

        try:
            for conv_id, disp, updated, title, ctype in both(
                    "select client_conversation_id, display_timestamp, last_updated_timestamp, "
                    "conversation_title, conversation_type from feed_entry"):
                if not conv_id:
                    continue
                entry = rec(conv_id)
                entry["in_arroyo"] = True
                # earliest of the "shown against the conversation" dates, latest of the updates:
                # both readings of the database can offer one, and the pair is a range
                if disp:
                    entry["feed_first_ms"] = min(entry["feed_first_ms"] or int(disp), int(disp))
                if updated:
                    entry["feed_last_ms"] = max(entry["feed_last_ms"] or 0, int(updated))
                entry["feed_title"] = entry["feed_title"] or cell(title)
                if ctype is not None and entry["feed_type"] is None:
                    entry["feed_type"] = ctype
        except sqlite3.DatabaseError as error:
            logger.info(f"Conversations: feed_entry is not available ({error}) — conversations "
                        f"with no message will carry no date")
    finally:
        views.close()
    return out


# The leading space is deliberate: without whitespace in the text, a double-click on the name next
# to the badge selects into the badge's words too (the same reason the Memories ID labels carry one).
_OWNER_BADGE = (' <span class="ownerdot" title="the account this extraction came from">'
                'device owner</span>')


def _participant(key, contact_links):
    """Resolve one participant (a user id, a username or a display name) to a contact.

    Returns ``{label, display, username, user_id, href, is_owner, raw}``. ``href`` is the contact's
    row in the Contacts report, which is where all of that contact's identifiers are — the display
    name, the username, the previous username and the permanent user id.
    """
    raw = str(key)
    found = (contact_links or {}).get(raw.lower()) or {}
    display, username = found.get("display", ""), found.get("username", "")
    user_id = found.get("user_id", "") or (raw if _UUID_RE.match(raw) else "")
    if display and username and display != username:
        label = f"{display} ({username})"
    else:
        label = display or username or raw
    return {"label": label, "display": display, "username": username, "user_id": user_id,
            "href": found.get("href"), "anchor": found.get("anchor"),
            "is_owner": bool(found.get("is_owner")), "raw": raw}


def _participant_html(part, root, chip=True, closure=None):
    """A participant as a chip: both names, the owner marked, linked to their contact row.

    ``root`` is this page's path back to the reports folder, since the contact link is stored
    relative to it (``../`` from the index, ``../../`` from a conversation page).
    """
    body = text_html(part["label"])
    if part["is_owner"]:
        body += _OWNER_BADGE
    if part["href"]:
        title = f'open the contact record of {part["label"]}'
        body = report_ui.xref(f'<a href="{_esc(root + part["href"])}" target="scauto_contacts" '
                              f'title="{_esc(title)}">{body}</a>',
                              [("ct", part.get("anchor"))], closure=closure, brief=True)
    return f'<span class="party">{body}</span>' if chip else body


_FEED_BASIS = (
    "This conversation holds no message in arroyo.db, so there is no message time to show. The "
    "dates come from the conversation's own row instead: feed_entry.display_timestamp and "
    "feed_entry.last_updated_timestamp — the dates the app itself shows against the conversation "
    "in its chat list — falling back to conversation.creation_timestamp. They say when the "
    "conversation was active on this device; they do NOT say that a message existed at that "
    "moment, and they are not evidence of message content.")

_TIME_SCOPE_HINT = (
    "A conversation has two kinds of time and the window is applied to whichever you pick, because "
    "they are different statements. «First / last activity» is the conversation's own range — and "
    "for a conversation holding no message that comes from the app's chat feed, which says the "
    "conversation was active then and NOT that a message existed then (those cells are marked "
    "«feed»). «Message times» are the times of the messages themselves, and that scope never falls "
    "back to a feed date, so it cannot answer a question about messages with one. «Either kind» is "
    "the union of the two: it can only ever return more conversations than either alone, which is "
    "why it is the default — a filter that leaves something out hides evidence.")

_MSG_TIME_HINT = (
    "Each row here is a message, so the window is applied to its own creation time — the value in "
    "the Created column. A message whose time could not be recovered from arroyo.db is hidden while "
    "a window is set, since it cannot be shown to fall inside one.")

_ACTIVITY_HINT = (
    "First and last activity in this conversation.\n\n"
    "• Normally these are the first and last arroyo.db conversation_message.creation_timestamp — "
    "actual message times, and the row's Msgs count says how many.\n\n"
    "• A conversation with NO message still gets a range, taken from the conversation's own row "
    "(feed_entry.display_timestamp / last_updated_timestamp, or conversation.creation_timestamp). "
    "Those cells are marked «feed» and say so on hover. That is why the columns are labelled "
    "activity rather than message: the two are different statements about different records.\n\n"
    "Do not read conversation.creation_timestamp as the start of the conversation. It is when THIS "
    "device created its local row, and on a device restored from a backup it post-dates the "
    "messages it contains — observed on the test corpus, where a conversation created in one year "
    "holds messages from two years earlier. The conversation's own page states all three values.")


def _ms_to_unix(ms):
    """arroyo stores Unix **milliseconds**; the rest of this report works in Unix seconds."""
    try:
        return int(ms) // 1000 if ms else None
    except (TypeError, ValueError):
        return None


def _activity(times, info, timefmt):
    """First/last activity for one conversation, and where each value came from.

    ``times`` are the message times (Unix seconds). When there are none the conversation's own
    feed/creation timestamps stand in — the only record of when a message-less conversation was
    active — and are flagged so the report never presents them as message times.
    """
    def fmt(unix_s):
        return timefmt(unix_s - _COCOA_EPOCH) if (timefmt and unix_s) else ""

    created = _ms_to_unix((info or {}).get("created_ms"))
    out = {"created": fmt(created), "created_sort": created or 0}
    if times:
        out.update({"first_sort": min(times), "last_sort": max(times),
                    "first": fmt(min(times)), "last": fmt(max(times)), "source": "messages"})
        return out
    feed_first = _ms_to_unix((info or {}).get("feed_first_ms")) or created
    feed_last = _ms_to_unix((info or {}).get("feed_last_ms")) or feed_first
    if not feed_first:
        out.update({"first_sort": 0, "last_sort": 0, "first": "", "last": "", "source": ""})
        return out
    out.update({"first_sort": feed_first, "last_sort": feed_last,
                "first": fmt(feed_first), "last": fmt(feed_last), "source": "feed"})
    return out


def build_conversations(by_conv, contacts, groups, arroyo_info, contact_links=None, timefmt=None):
    """Assemble one record per conversation, from the messages **and** the contact/group lists.

    A conversation is listed even when it has no messages: a friend or group whose conversation id
    the app knows about but for which ``arroyo.db`` holds nothing is a real (and easy to miss)
    finding, so it appears with 0 messages rather than being left out. **Including when only
    arroyo.db knows it** — a ``conversation`` / ``feed_entry`` row with no message, no friend and no
    group behind it was previously dropped from the report altogether, which is the one case where
    "not listed" and "no messages" look identical to the reader. Those rows still carry the dates
    the app shows in its chat list, so the conversation is listed with that range.

    Every derived value records where it came from (``*_src``), which is what the "?" icons show.
    """
    by_contact = {c["conv_id"]: c for c in contacts if c["conv_id"]}
    by_group = {g["conv_id"]: g for g in groups}
    in_arroyo = {k for k, v in (arroyo_info or {}).items() if v.get("in_arroyo") or v.get("user_ids")}

    conversations = []
    for conv_id in sorted(set(by_conv) | set(by_contact) | set(by_group) | in_arroyo):
        msgs = by_conv.get(conv_id, [])
        group = by_group.get(conv_id)
        contact = by_contact.get(conv_id)
        info = arroyo_info.get(conv_id) or {}

        # kind — user_conversation.conversation_type is authoritative when present
        if info.get("type") in (0, 1):
            kind = "Group" if info["type"] == 1 else "Private"
            kind_src = (f"arroyo.db user_conversation.conversation_type = {info['type']} "
                        f"({'1 = group' if info['type'] == 1 else '0 = private'}).")
        elif group:
            kind, kind_src = "Group", ("This conversation id appears in the groups list recovered "
                                       "from the friends artifact (GROUP_ID / GROUP_NAME).")
        elif contact:
            kind, kind_src = "Private", ("This conversation id is the CONVERSATION_ID of a single "
                                         "contact in the friends list.")
        elif info.get("feed_type") in (0, 1):
            kind = "Group" if info["feed_type"] == 1 else "Private"
            kind_src = (f"arroyo.db feed_entry.conversation_type = {info['feed_type']} "
                        f"({'1 = group' if info['feed_type'] == 1 else '0 = private'}) — the "
                        f"conversation's own row in the chat feed. Used because neither the "
                        f"friends/groups lists nor user_conversation covers this conversation.")
        else:
            kind, kind_src = "Unknown", ("Neither the friends list, the groups list nor "
                                         "arroyo.db user_conversation names this conversation id; "
                                         "it is known only from the messages that carry it.")

        # title
        if group and group["name"]:
            title = group["name"]
            title_src = "GROUP_NAME from the groups list in the friends artifact."
        elif group:
            title = "(unnamed group)"
            title_src = ("The groups list has this conversation but no GROUP_NAME — an unnamed "
                         "group chat.")
        elif contact and (contact["display"] or contact["username"]):
            title = contact["display"] or contact["username"]
            title_src = ("The display name / username of the contact whose CONVERSATION_ID this "
                         "is, from the friends list.")
        elif info.get("feed_title"):
            title = info["feed_title"]
            title_src = ("arroyo.db feed_entry.conversation_title — the name the app itself shows "
                         "against this conversation in its chat list. Used because no contact or "
                         "group in the extraction names it.")
        else:
            senders = [m["sender"] for m in msgs if m["sender"] and m["direction"] != "Sent"]
            title = senders[0] if senders else "(unidentified conversation)"
            title_src = ("No contact or group names this conversation, so it is labelled with the "
                         "first non-owner sender_id seen in its messages."
                         if senders else
                         "Nothing in the extraction names this conversation.")

        # participants — resolved to contacts so each one shows both names and can be opened
        if info.get("user_ids"):
            keys = sorted(set(info["user_ids"]))
            participants_src = (f"arroyo.db user_conversation lists {len(keys)} participant user "
                                f"id(s) for this conversation; each is shown with the display name "
                                f"and username of the matching contact, where the friends list has "
                                f"one.")
        elif group and group["participants"]:
            keys = list(group["participants"])
            participants_src = ("GROUP_PARTICIPANTS_USER_NAMES from the groups list in the friends "
                                "artifact — usernames, resolved to the matching contact where "
                                "there is one.")
        elif contact:
            keys = [contact["user_id"] or contact["username"] or contact["display"]]
            participants_src = "The single contact this private conversation belongs to."
        else:
            keys = sorted({m["sender"] for m in msgs if m["sender"]})
            participants_src = ("Derived from the distinct sender_id values of this conversation's "
                                "messages — the conversation's real membership is not recorded in "
                                "the artifacts that were available.")
        participants = [_participant(k, contact_links) for k in keys if k]

        times = [m["created_unix"] for m in msgs if m["created_unix"]]
        activity = _activity(times, info, timefmt)
        senders = {}
        for m in msgs:
            if m["sender"]:
                senders[m["sender"]] = senders.get(m["sender"], 0) + 1
        types = {}
        for m in msgs:
            for ctype in (m["types"] or ["(none)"]):
                types[ctype] = types.get(ctype, 0) + 1
        conversations.append({
            "id": conv_id,
            "server_id": (info.get("server_id") or ""),
            "kind": kind, "kind_src": kind_src,
            "title": title, "title_src": title_src,
            "participants": participants, "participants_src": participants_src,
            "contact": contact, "group": group,
            "messages": msgs,
            "n_messages": len(msgs),
            "n_files": sum(len(m["atts"]) for m in msgs),
            "n_attachments": sum(1 for m in msgs if m["atts"]),
            "n_missing": sum(1 for m in msgs for a in m["atts"] if not a["rel"]),
            "senders": senders, "types": types,
            "n_wal_gone": sum(1 for m in msgs if m.get("wal") == sqlite_open.MAIN_ONLY),
            "first_sort": activity["first_sort"],
            "last_sort": activity["last_sort"],
            "activity": activity,
            "page": f"pages/{_page_key(conv_id)}.html",
        })
    # busiest first: that is the order an examiner wants to triage in
    conversations.sort(key=lambda c: (-c["n_messages"], -c["last_sort"], c["id"]))
    return conversations


# --------------------------------------------------------------------------- shared assets

def write_assets(outdir):
    """Write ``assets/ui.css`` / ``assets/ui.js``, loaded by the index and every detail page.

    The index and the per-conversation pages need the same ~20 KB of virtual-table / selection /
    navigation code. Inlining it in every page (as the single-page reports do) would multiply it by
    the number of conversations, so here it is one subresource both load — which a ``file://`` page
    is allowed to do (unlike ``fetch``).
    """
    assets = os.path.join(outdir, "assets")
    os.makedirs(assets, exist_ok=True)
    with open(os.path.join(assets, "ui.css"), "w", encoding="utf-8") as fh:
        fh.write("/* Snapchat Auto — shared report UI (see scripts/report_ui.py) */\n"
                 + report_ui.PAGE_CSS + report_ui.VTABLE_CSS + report_ui.NAV_CSS
                 + report_ui.SELECT_CSS + report_ui.HINT_CSS + report_ui.TIME_CSS + _REPORT_CSS)
    with open(os.path.join(assets, "ui.js"), "w", encoding="utf-8") as fh:
        # SELECT_JS first: ../selection.js is loaded right after this file and calls SCSel.preload().
        fh.write("/* Snapchat Auto — shared report UI (see scripts/report_ui.py) */\n"
                 + report_ui.SELECT_JS + report_ui.VTABLE_JS + report_ui.HINT_JS
                 + report_ui.NAV_JS + report_ui.SELECT_TOOLBAR_JS + report_ui.TIME_JS + _REPORT_JS)


# Report-specific styling for both tables (kept out of the row data: every byte of a cell is
# multiplied by the row count in the data/*.js files, so per-column styling lives here).
_REPORT_CSS = """
 .vcells>.vc{font-size:12.5px}
 .cid{font-family:ui-monospace,Consolas,monospace;font-size:10px;color:#888}
 .kindbadge{font-weight:700;font-size:11px;white-space:nowrap}
 .kindbadge.group{color:#8a1f5a} .kindbadge.private{color:#25348a} .kindbadge.unknown{color:#999}
 header .kindbadge,header .kindbadge.group,header .kindbadge.private{color:#fff}
 /* conversation index — scoped, so the column rules do not reach the message table below */
 .convs .vcells>.vc.c0{color:#2d2d71;font-weight:700;text-align:center;padding-left:4px;
   padding-right:4px}
 .convs .vr.open .vc.c0{color:#8a1f5a}
 .convs .vcells>.vc.c2{font-weight:600}
 .convs .vcells>.vc.c3{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#33367a}
 .convs .vcells>.vc.c4 a{color:#2d2d71;text-decoration:none}
 .convs .vcells>.vc.c4 a:hover{text-decoration:underline}
 .convs .vcells>.vc.c5,.convs .vcells>.vc.c6{text-align:right;font-weight:600;color:#2d2d71}
 .convs .vcells>.vc.c7,.convs .vcells>.vc.c8{font-size:11.5px;color:#555}
 /* message table (detail pages) */
 .msgs .vcells>.vc.c0{color:#2d2d71;font-weight:700;text-align:center}
 .msgs .vr.open .vc.c0{color:#8a1f5a}
 .msgs .vcells>.vc.c1,.msgs .vcells>.vc.c7{font-size:11px;color:#555;line-height:1.3}
 /* The Content cell holds the message text and its file(s) in a fixed-height row. As a flex column
    the file box keeps its full height and the text is the only thing that gives way, so a long
    message can no longer push the media button half out of the row and show a sliced label. The
    text is clamped to two lines for a tidy cut; the whole message is in the expanded detail. */
 .msgs .vcells>.vc.c5{font-size:12.5px;line-height:1.35;overflow-wrap:anywhere;white-space:pre-wrap;
   display:flex;flex-direction:column;align-items:flex-start;gap:3px}
 .msgs .vcells>.vc.c5 .msgtext{flex:0 1 auto;min-height:0;overflow:hidden;display:-webkit-box;
   -webkit-box-orient:vertical;-webkit-line-clamp:2}
 .msgs .vcells>.vc.c5 .atts{flex:0 0 auto;display:flex;flex-wrap:nowrap;gap:4px;max-width:100%;
   overflow:hidden}
 /* a preview in the row is a marker, not the picture: the expanded row shows it properly */
 .msgs .vcells>.vc.c5 .filebtn img{max-height:30px;max-width:56px}
 .msgs .vcells>.vc.c6{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#33367a}
 .msgs .vr.out{background:#f4f7ff} .msgs .vr.out:hover{background:#e8eeff}
 .msgs .vr.out .vc.c2{color:#25348a;font-weight:700}
 .msgs .vc.c2{font-size:11px;font-weight:600;color:#6a6a80}
 .dirin{color:#2f7d32} .msgtext{display:block}
 .convhead{padding:14px 24px 6px;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
   gap:6px 30px;align-items:start}
 @media(max-width:900px){.convhead{grid-template-columns:1fr}}
 .convtitle{font-size:17px;font-weight:700;margin:12px 24px 0}
 .parts{font-size:12.5px}
 .parts .party{background:#eef0ff;border:1px solid #c9cdf0;color:#2d2d71;border-radius:9px;
   padding:1px 8px;margin:2px 5px 2px 0;display:inline-block;font-size:11.5px}
 .parts .party a{color:#2d2d71;text-decoration:none} .parts .party a:hover{text-decoration:underline}
 .pname{color:#666;font-size:11px;margin-left:6px}
 /* the device owner, wherever a name or a user id of theirs is shown */
 .ownerdot{background:#2d2d71;color:#fff;border-radius:3px;font-size:9px;font-weight:700;
   letter-spacing:.03em;padding:0 4px;margin-left:5px;text-transform:uppercase;white-space:nowrap;
   vertical-align:middle}
 /* a message that carries more than one cached file */
 .multi{background:#eef0ff;border:1px solid #c9cdf0;color:#2d2d71;border-radius:5px;
   padding:5px 9px;font-size:12px;margin-top:6px}
 .cmid{color:#8a8aa0;font-size:10px}
 .parsefail{color:#8a5a00;background:#fff3d6;border:1px solid #e6c983;border-radius:8px;
   padding:0 6px;font-size:10.5px;font-weight:600;white-space:nowrap;margin-left:4px}
 .warn-inline{background:#fff3d6;border-color:#e6c983;color:#8a5a00}
 .walgone{color:#8a3a1c;background:#ffe9e0;border:1px solid #e8bfae;border-radius:8px;
   padding:0 6px;font-size:10.5px;font-weight:600;white-space:nowrap;margin-left:4px}
 .msgs .vcells>.vc.c5 .filebtn{margin-right:0}
 .mdet{font-size:12.5px}
 .body{background:#fff;border:1px solid #e2e2ea;border-radius:6px;padding:8px 10px;
   margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere;max-width:900px}
 /* The preview is capped rather than shown full size: an expanded message has to stay small enough
    to read the conversation around it, and a row whose height changes after the media loads is what
    made scrolling jump (the media tells the table to re-measure — see SCV.remeasure). */
 .shot{max-height:150px;margin-top:6px;display:flex;align-items:flex-start}
 .shot img,.shot video{max-height:150px;max-width:280px;border-radius:6px;
   box-shadow:0 1px 4px rgba(0,0,0,.25);object-fit:contain}
 .shotcap{font-size:11px;margin-top:3px}
 .shotcap a{color:#2d2d71}
 .foot{padding:14px 24px;color:#777;font-size:11.5px}
"""

# Filter glue shared by the index and the message tables (each page defines its own `flt` inputs).
_REPORT_JS = """
var flt_t=0;
function flt(){clearTimeout(flt_t);flt_t=setTimeout(function(){SCV.refilter();},120);}
/* Which of a conversation row's two time lists the window is applied to. They are separate claims:
   `ct` is the conversation's own first/last activity, which for a conversation with no message comes
   from the app's chat feed and says only that it was active then, while `mt` is the times of the
   messages themselves. "Message times only" therefore may not fall back to `ct`, or a feed date
   would answer a question about messages. */
function scConvTimes(m){
 var s=scFv('tscope')||'both';
 if(s==='mt')return m.mt||[];
 if(s==='ct')return m.ct||[];
 return (m.ct||[]).concat(m.mt||[]);}
function xall(btn){
 var op=btn.dataset.o==='1';
 if(!SCV.expandAll(!op,500)){
  alert('Too many rows on this page to expand at once. Narrow the filters or use a smaller '
        +'"rows per page" first.');
  return;}
 btn.dataset.o=op?'0':'1';btn.textContent=op?'Expand all':'Collapse all';}
"""


def _head(title, rel_prefix, run_id, sel_kind, asset_prefix, sel_prefix="", reports_root="",
          partial_css=""):
    """The common ``<head>`` of the index and the detail pages.

    ``sel_prefix`` is set on a conversation page, whose message anchors are page-local: it scopes the
    selection count and the Clear button to this conversation's messages. See ``report_ui.selId``.
    ``reports_root`` is where ``sources.json`` lives, so the examiner's saved selection carries the
    source fingerprints of the run it was made in.

    ``partial_css`` is inlined rather than added to ``assets/ui.css``, because that file is shared with
    every full report and none of them has an element to style with it.
    """
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{_esc(title)}</title>'
            f'<link rel="stylesheet" href="{asset_prefix}assets/ui.css">'
            + (f"<style>{partial_css}</style>" if partial_css else "") +
            f'<script>window.SCAUTO_RUN={json.dumps(run_id)};'
            f'window.SCAUTO_VERSION={json.dumps(app_version.get_version())};'
            f'{report_ui.sources_script(reports_root) if reports_root else ""}'
            f'window.SCAUTO_SELKIND="{sel_kind}";'
            f'window.SCAUTO_SELPREFIX={json.dumps(sel_prefix)};</script>'
            f'<script src="{asset_prefix}assets/ui.js"></script>'
            f'<script src="{rel_prefix}selection.js"></script></head>')


# --------------------------------------------------------------------------- detail page

def _kind_badge(kind):
    icon = {"Group": "&#128101;", "Private": "&#128100;"}.get(kind, "?")
    return f'<span class="kindbadge {kind.lower()}">{icon} {_esc(kind)}</span>'


def _grid(pairs):
    return ('<div class="grid">'
            + "".join(f'<div class="k">{k}</div><div class="v {cls}">{v}</div>'
                      for k, v, cls in pairs if v not in (None, ""))
            + '</div>')


_SMID_HINT = ("The identifier is arroyo.db conversation_message.server_message_id followed by the "
              "part index of the cache claim this row came from (e.g. \"12.0\" is message 12, part "
              "0). It is empty for a message the app had not finished sending — the server had not "
              "assigned an id yet, which is what the \"Sending Message\" content type means.")

_TYPE_HINT = ("\"Text\" is arroyo.db conversation_message.content_type = 1. The media labels are "
              "derived from the CACHE_FILE_CLAIM.EXTERNAL_KEY prefix of the cache claim joined to "
              "the message (\"1:\" = temporarily stored media, \"thumbnail~1:\" = thumbnail, "
              "\"cm-chat-media-video-1\" = media the user saved in the chat), or from content_type "
              "3 (video of unknown source) / 5 (sticker). \"local_message_reference\" means the "
              "attachment was resolved through the row's local_message_references plist. "
              "\"Media (no cached file)\" is a message arroyo.db records as carrying media for "
              "which no cache file survives on this device — the message itself (sender, times, "
              "both ids) is unaffected; only its content was not recovered. It does NOT mean the "
              "media expired: it may equally have been evicted from the cache or never carried by "
              "the extraction. The raw arroyo content_type is in the row detail below. \"System message\" is "
    "content_type 9: an event the app recorded in the conversation rather than anything a user "
    "typed or sent — its protobuf carries no text at all, and the description shown as its content "
    "is written by the parser from the decoded structure.")

_PARSE_FAIL_HINT = (
    "getChats could not read this message's conversation_message.message_content protobuf, so the "
    "parser stored an error marker in place of the text. The row is still listed with everything "
    "else that is known about it (sender, timestamps, ids, any attachment). Verify the message "
    "directly in arroyo.db with the conversation and message ids shown below.")

_TEXT_HINT = (
    "What the sender typed, read out of the field of conversation_message.message_content that "
    "holds it — the message body for a text message, and the caption for a media message that "
    "carries one. It is read from that field rather than from the whole protobuf on purpose: a "
    "message's protobuf also contains its encryption key, its media id and any lens or sticker "
    "name, and those are not the message. A media message with no caption therefore shows no text "
    "at all. Nothing is hidden: the full parsed value is always in the expanded row under "
    "\"message_content (parsed)\", with the content_type it came from beside it.")

_CONTENT_HINT = "The message text and every cached file the message carries. " + _TEXT_HINT

_SENDER_HINT = ("arroyo.db conversation_message.sender_id, replaced with the matching contact's "
                "username by the parser (fixSenders) when the friends list has one — otherwise the "
                "raw user id is shown.")

_DIR_HINT = ("\"Sent\" means the sender matched the logged-in account of this extraction; anything "
             "else is shown as \"Received\". It is blank when the sender could not be identified. "
             "This is derived, not a field of the message.")

_CREATED_HINT = ("arroyo.db conversation_message.creation_timestamp (Unix milliseconds), shown in "
                 "the timezone chosen for this run. Expanding a row shows the stored UTC value "
                 "next to the converted one. \"unknown\" means the row exists only because of a "
                 "cache claim and carries no message timestamp.")


def _attachment_detail(att, prefix, index=None, total=1, closure=None):
    """One attachment's block inside an expanded message: the file, its hashes and its cache link."""
    parts = []
    label = "Attachment" if total == 1 else f"Attachment {index} of {total}"
    parts.append(f'<div class="sect">{label}</div>')
    if att["rel"] and att["bytes"]:
        url = _esc(prefix + att["rel"])
        # The preview sits in a box of a **fixed height**, and tells the table to re-measure once it
        # has loaded. An expanded row is measured as soon as it is in the DOM, so media that resizes
        # the row after loading leaves every offset below it wrong — which is what made scrolling
        # jump — and a full-size preview pushed the rest of the conversation off the screen.
        if att["kind"] == "image":
            parts.append(f'<div class="shot"><a href="{url}" target="_blank">'
                         f'<img src="{url}" loading="lazy" onload="SCV.remeasure()"></a></div>'
                         f'<div class="shotcap"><a href="{url}" target="_blank">'
                         f'open the full-size {_esc(att["ext"])} &#8599;</a></div>')
        elif att["kind"] == "video":
            parts.append(f'<div class="shot"><video controls preload="metadata" src="{url}" '
                         f'onloadedmetadata="SCV.remeasure()"></video></div>'
                         f'<div class="shotcap"><a href="{url}" target="_blank">'
                         f'open the {_esc(att["ext"])} in a new tab &#8599;</a></div>')
        else:
            parts.append(f'<div><a class="filebtn" href="{url}" target="_blank">'
                         f'open {_esc(att["ext"] or "file")}</a>'
                         f'<span class="muted"> &mdash; not a type this report can display '
                         f'inline</span></div>')
    elif att["rel"]:
        parts.append('<div class="muted">The file is 0 bytes on disk — the copy exists but no '
                     'content was stored or captured.</div>')
    parts.append(_grid([
        ("file name", f'<span class="mono">{_esc(att["name"])}</span>', ""),
        ("detected type", _esc(att["ext"] or "unknown (not recognised media)"), ""),
        ("size", _fmt_bytes(att["bytes"]) or "0 B", ""),
        ("MD5", _esc(att["md5"]), "hex"),
        ("SHA-256", _esc(att["sha256"]), "hex"),
        ("published as", _esc(att["rel"] or "") + report_ui.info_icon(att["how"]), "mono"),
    ]))
    if att["cache_key"]:
        parts.append(
            '<div class="chips">'
            + report_ui.xref(
                f'<a class="chip cache" target="scauto_cache" '
                f'href="{prefix}../CacheController/CacheController_report.html'
                f'#ck-{_esc(att["cache_key"])}">&#128451; cache_controller entry '
                f'{_esc(att["cache_key"][:8])}…</a>',
                [("cc", f'ck-{att["cache_key"]}')], closure=closure)
            + report_ui.info_icon(att["cache_key_how"]) + '</div>')
    else:
        parts.append('<div class="muted">No cache_controller entry could be resolved for this '
                     'file name.' + report_ui.info_icon(
                         "Attachments copied out of the SCContent cache are named after their "
                         "cache_controller CACHE_KEY, and SCPersistentMedia copies are matched "
                         "to a claim carrying the same conversation / message / part. Neither "
                         "applied to this file name, so there is no row to link to.") + '</div>')
    return "".join(parts)


def _message_detail(msg, conv, prefix="../", contact_links=None, closure=None):
    """The expandable per-message block: full text / media, hashes, provenance."""
    parts = []
    if msg["text"]:
        parts.append('<div class="sect">Message text</div>'
                     f'<div class="body">{text_html(msg["text"])}</div>')
    if msg["parse_error"]:
        parts.append('<div class="multi warn-inline">The message body could not be parsed'
                     + report_ui.info_icon(_PARSE_FAIL_HINT) + '</div>')
    atts = msg["atts"]
    if len(atts) > 1:
        parts.append(f'<div class="multi">This message carries <b>{len(atts)} cached files</b> — '
                     f'typically the media and its thumbnail, or several parts of one send.'
                     + report_ui.info_icon(
                         "Snapchat's cache index (CACHE_FILE_CLAIM) can hold several claims for one "
                         "message: the full media, a thumbnail, and the raw content claim. The "
                         "parser produces one row per claim; this report folds them back into the "
                         "message they belong to and lists each file below with its own hashes.")
                     + '</div>')
    for n, att in enumerate(atts, 1):
        parts.append(_attachment_detail(att, prefix, n, len(atts), closure))
    if not atts and not msg["text"] and not msg["parse_error"]:
        parts.append('<div class="muted">This row carries neither text nor a recovered '
                     'attachment.</div>')

    # No "?" popovers in here: their text is the same for every message, and this block is written
    # once per message into the detail chunks. The columns they belong to carry them instead, in the
    # table header, where they are written once per page.
    parts.append('<div class="sect">Row values (raw)</div>')
    sender = text_html(msg["sender"])
    link = (contact_links or {}).get(msg["sender"].lower()) if msg["sender"] else None
    if link:                                                   # from pages/<key>.html to Contacts/
        sender = report_ui.xref(f'<a class="detail" target="scauto_contacts" '
                                f'href="{_esc("../../" + link["href"])}">{sender} &#9656;</a>',
                                [("ct", link.get("anchor"))], closure=closure)
    if msg["direction"] == "Sent":
        sender += _OWNER_BADGE
    parts.append(_grid([
        ("client_conversation_id", f'<span class="mono">{_esc(conv["id"])}</span>', ""),
        ("server_conversation_id", f'<span class="mono">{_esc(conv.get("server_id"))}</span>', ""),
        ("server_message_id", _esc(msg["smid"]), "mono"),
        ("client_message_id", _esc(msg["cmid"]), "mono"),
        ("sender_id", sender, ""),
        ("content_type", _esc(" + ".join(msg["types"])), ""),
        ("content_type (arroyo, raw)", _esc(" + ".join(msg.get("raw_types") or [])), "mono"),
        ("message_content (parsed)", text_html(msg["raw_text"]) if msg["raw_text"]
         else '<span class="muted">empty</span>', ""),
        ("creation_timestamp (UTC, as stored)", _esc(msg["created_utc"]), "mono"),
        ("creation_timestamp (report timezone)", _esc(msg["created"]), ""),
        ("read_timestamp (UTC, as stored)", _esc(msg["read_utc"]), "mono"),
        ("read_timestamp (report timezone)", _esc(msg["read"]), ""),
        ("direction", _esc(msg["direction"]), ""),
        ("cached files on this message", str(len(atts)) if len(atts) > 1 else "", ""),
    ]))
    return "".join(parts)


def _message_rows(conv, chunk_of):
    """The compact per-message row payload for the virtual table."""
    rows = []
    for msg in conv["messages"]:
        atts = msg["atts"]
        # Every file of the message, side by side — a message is one row however many it carries.
        # The files live in their own box because the cell is a fixed height: text long enough to
        # fill the row used to push the media button half out of it, showing the examiner a sliced
        # label. The box does not shrink (see .msgs .vc.c5 in _REPORT_CSS), so the text gives way
        # instead and the file is always shown whole.
        files = "".join(_attachment_cell(a) for a in atts)
        content = f'<span class="atts">{files}</span>' if files else ""
        text = msg["text"]
        if text:
            # the row is one fixed height, so only about this much of a message is ever visible:
            # keep the cell small and leave the full text to the expanded detail (every byte here
            # is multiplied by the message count)
            clipped = text[:200] + " …" if len(text) > 200 else text
            content = f'<span class="msgtext">{text_html(clipped)}</span>' + content
        if msg["parse_error"]:
            content += ('<span class="parsefail" title="the message body could not be parsed — '
                        'open the row">&#9888; not parsed</span>')
        if msg.get("wal") == sqlite_open.MAIN_ONLY:
            # only in arroyo.db without its -wal: the app has deleted this message since the last
            # checkpoint, so it is recovered here but is no longer part of the live conversation
            content += ('<span class="walgone" title="This message is only in arroyo.db WITHOUT '
                        'its write-ahead log (-wal). The -wal deleted it, so the app no longer '
                        'shows it — it is recovered prior state, not a current message.">'
                        '&#9888; deleted since checkpoint</span>')
        if not content:
            content = '<span class="muted">—</span>'
        direction = msg["direction"]
        dir_cell = ('Sent' if direction == 'Sent'
                    else f'<span class="dirin">{_esc(direction)}</span>' if direction else '')
        types = " + ".join(msg["types"])
        sender_cell = text_html(msg["sender"]) or '<span class="muted">—</span>'
        if direction == "Sent" and msg["sender"]:
            sender_cell += _OWNER_BADGE
        # both identities of the message: the server's id, and the one the device gave it
        ids = _esc(msg["smid"]) or '<span class="muted">none yet</span>'
        if msg["cmid"]:
            ids += f'<div class="cmid">client {_esc(msg["cmid"])}</div>'
        cells = [
            "▸",
            _esc(msg["created"]) or '<span class="muted">unknown</span>',
            dir_cell,
            sender_cell,
            _esc(types),
            content,
            ids,
            _esc(msg["read"]),
        ]
        searchable = [msg["sender"], types, msg["smid"], msg["cmid"], msg["created_utc"],
                      msg["read_utc"], msg["created"], text, msg["raw_text"]]
        for att in atts:
            searchable += [att["name"], att["ext"], att["md5"], att["sha256"],
                           att["cache_key"] or ""]
        rows.append([
            msg["anchor"], cells,
            " ".join(s for s in searchable if s).lower(),
            {"1": msg["created_unix"] or _NO_TIME_SORT, "2": direction,
             "3": msg["sender"].lower(), "4": types, "6": _smid_sort(msg["smid"])},
            chunk_of.get(msg["anchor"]),
            # the type filter matches any of a message's types, so they travel delimited
            {"dir": direction, "type": "|" + "|".join(msg["types"] or ["(none)"]) + "|",
             "att": "y" if atts else "n",
             "wal": "gone" if msg.get("wal") == sqlite_open.MAIN_ONLY else "live",
             # This message's own time, as the wall clock the row displays (report_ui.ts_key), for
             # the time window. A message whose time could not be recovered gets an empty list and is
             # hidden while a window is set — it cannot be placed inside one.
             "ts": report_ui.ts_keys(msg["created"]),
             # The raw server message id, carried so a saved selection identifies this message by
             # what arroyo.db calls it rather than by our anchor — the anchor is sanitised, may take
             # a duplicate suffix, and for an unsent message is only a *position* in the
             # conversation, which shifts the moment the parser recovers one more row. Omitted when
             # there is none, in which case `selKeys` falls back to the time and sender.
             **({"smid": msg["smid"]} if msg["smid"] else {})},
        ])
    return rows


def _wal_filter_html(n_gone, noun):
    """The "-wal" filter, emitted **only** when there is something for it to match.

    A message that exists only in the reading of ``arroyo.db`` *without* its write-ahead log has
    been deleted since the last checkpoint — recovered prior state, not a live message, and the one
    thing in a chat report an examiner most wants to isolate. It was badged on the row but could not
    be filtered for, in either table. On a database whose -wal deleted nothing the control is left
    out entirely rather than offered as a choice that can only ever empty the table.
    """
    if not n_gone:
        return ""
    if noun == "conversation":
        what, gone, live = ("Conversations holding at least one message that the write-ahead log "
                            "deleted after the last checkpoint",
                            f"with deleted message(s) ({n_gone})",
                            "with no deleted message")
    else:
        what, gone, live = ("Messages recovered by reading arroyo.db without its write-ahead log "
                            "(-wal): the app deleted them after the last checkpoint, so they are "
                            "recovered prior state rather than part of the live conversation",
                            f"deleted since the checkpoint ({n_gone})",
                            "still in the live conversation")
    return (f'<label title="{_esc(what)}">'
            '-wal <select id="wal" onchange="flt()"><option value="">any</option>'
            f'<option value="gone">{_esc(gone)}</option>'
            f'<option value="live">{_esc(live)}</option></select></label>')


_PARTY_HINT = ("Each participant is shown as \"display name (username)\" and opens that contact's "
               "record in the Contacts report, which holds all of their identifiers — display "
               "name, current username, previous username where one was recorded, and the "
               "permanent user id.")


def render_conversation_page(conv, outdir, tz_label, run_id, index_name="Conversations_report.html",
                             contact_links=None, closure=None, prov=None):
    """Write ``pages/<key>.html`` (+ its data files) for one conversation."""
    key = _page_key(conv["id"])
    pages_dir = os.path.join(outdir, "pages")
    data_dir = os.path.join(pages_dir, "data", key)
    os.makedirs(pages_dir, exist_ok=True)

    details = [(m["anchor"], _message_detail(m, conv, contact_links=contact_links, closure=closure))
               for m in conv["messages"]]
    chunk_of = report_ui.write_details(data_dir, details)
    report_ui.write_rows(data_dir, _message_rows(conv, chunk_of))

    parts_html = "".join(_participant_html(p, "../../", closure=closure)
                         for p in conv["participants"])
    ids_html = "<br>".join(
        f'<span class="mono">{_esc(p["user_id"])}</span>'
        + (f' <span class="pname">{text_html(p["username"] or p["display"])}</span>'
           if (p["username"] or p["display"]) else "")
        + (_OWNER_BADGE if p["is_owner"] else "")
        for p in conv["participants"] if p["user_id"])
    top_senders = sorted(conv["senders"].items(), key=lambda kv: -kv[1])
    types = ", ".join(f"{t} ({n})" for t, n in sorted(conv["types"].items(),
                                                      key=lambda kv: -kv[1]))
    files_note = ("" if conv["n_files"] == conv["n_attachments"] else
                  f' ({conv["n_files"]} files in total)')
    left = _grid([
        ("Conversation ID (client)", f'<span class="mono">{_esc(conv["id"])}</span>'
         + report_ui.info_icon("arroyo.db conversation_message.client_conversation_id — the id "
                               "every message of this conversation carries, and the id the friends "
                               "/ groups lists use to point at it. It is the device's own id for "
                               "the conversation."), ""),
        ("Conversation ID (server)", f'<span class="mono">{_esc(conv["server_id"])}</span>'
         + report_ui.info_icon("arroyo.db server_conversation_id — the id Snapchat's servers use "
                               "for the same conversation. Only present on app versions whose "
                               "schema records it."), ""),
        ("Type", _kind_badge(conv["kind"]) + report_ui.info_icon(conv["kind_src"]), ""),
        ("Named from", _esc(conv["title_src"]), ""),
        ("Participants" + report_ui.info_icon(conv["participants_src"] + " " + _PARTY_HINT),
         f'<div class="parts">{parts_html}</div>' if parts_html
         else '<span class="muted">not recorded</span>', ""),
        ("Participant user IDs" + report_ui.info_icon(
            "The permanent identifier of each participant — the only one that does not change when "
            "a username or display name does."), ids_html, ""),
    ])
    right = _grid([
        ("Messages", str(conv["n_messages"]), ""),
        ("With an attachment", str(conv["n_attachments"]) + files_note, ""),
        ("Attachments not published",
         str(conv["n_missing"]) if conv["n_missing"] else "", ""),
        ("First message", _esc(_first_last(conv, "first")), ""),
        ("Last message", _esc(_first_last(conv, "last")), ""),
        ("Content types", _esc(types), ""),
        ("Senders", "<br>".join(f"{text_html(s)} &mdash; {n}" for s, n in top_senders), ""),
    ])

    # `data-keys` inline here rather than through SCV.selKeys: this box is not a virtual row, and
    # there is exactly one of it per page, so the attribute costs nothing.
    conv_keys = _esc(json.dumps({"conv": conv["id"], "server": conv["server_id"] or ""}))
    selbar = ('<span class="selbar">'
              '<label class="selrow"><input type="checkbox" class="selbox" data-kind="conv" '
              f'data-id="conv-{_esc(conv["id"])}" data-keys="{conv_keys}">'
              ' mark this conversation for the case</label>'
              '<button onclick="scSelSaveJson()" title="Download selection.json — the copy to keep '
              'with the case, and the file to hand back to Snapchat_Auto for a partial report">'
              '&#128190; Save selections (.json)</button>'
              '<button onclick="scSelSaveJs()" title="Download the drop-in selection.js to put next '
              'to the reports. Some browsers block a .js download; use the .json then.">'
              'Save as selection.js</button>'
              '<span class="selnote" id="selnote"></span></span>'
              '<div class="sellegacy" id="sellegacy" style="display:none"></div>')

    # A conversation with no messages must not trip the virtual table's "row data missing" banner
    # (it fires on an empty row set), and its empty table needs to say why it is empty.
    has_messages = bool(conv["messages"])
    empty_text = ('No message matches the current filters.' if has_messages else
                  'This conversation has no message in arroyo.db — it is listed because the '
                  'friends / groups list names its conversation id.')
    type_opts = "".join(f'<option value="{html.escape(t, quote=True)}">{_esc(t)}</option>'
                        for t in sorted(conv["types"]))
    partial_css, banner, _figures = partial_report.page_chrome(closure, None, prov)
    # this page's own denominator: how much of *this conversation* the extract holds. The report-wide
    # message figure belongs on the index; here the question is what is missing from this chat.
    of_conv = (f' <span class="pfig">of {conv["n_messages_full"]} in this conversation</span>'
               if closure is not None and conv.get("n_messages_full") is not None else "")
    doc = (
        _head(f'Conversation {_short(conv["title"], 40)}', "../../", run_id, "msg", "../",
              sel_prefix=f'conv-{conv["id"]}|',
              reports_root=os.path.dirname(os.path.abspath(outdir)),
              partial_css=partial_css)
        + '<body>'
        f'<header><h1>{text_html(conv["title"])} &mdash; conversation</h1>'
        f'<div class="sum">{_kind_badge(conv["kind"])} &middot; {conv["n_messages"]} message(s)'
        f'{of_conv} &middot; {conv["n_attachments"]} with an attachment &middot; times in '
        f'<b>{_esc(tz_label)}</b></div></header>'
        f'{banner}'
        f'<a class="back" href="../{index_name}#conv-{_esc(conv["id"])}">'
        f'&larr; Back to the conversations index</a>'
        f'<div class="convhead" id="conv-{_esc(conv["id"])}"><div>{left}</div>'
        f'<div>{right}</div></div>'
        + (report_ui.missing_data_banner("this page") if has_messages else "") +
        '<div class="stickytop"><div class="toolbar">'
        '<input type="search" id="q" placeholder="Search this conversation — text, sender, id, '
        'hash…" oninput="flt()">'
        '<label>Direction <select id="dir" onchange="flt()"><option value="">any</option>'
        '<option value="Sent">sent</option><option value="Received">received</option>'
        '</select></label>'
        f'<label>Type <select id="type" onchange="flt()"><option value="">any</option>'
        f'{type_opts}'
        '</select></label>'
        '<label>Attachment <select id="att" onchange="flt()"><option value="">any</option>'
        '<option value="y">with</option><option value="n">without</option></select></label>'
        + _wal_filter_html(conv["n_wal_gone"], "message")
        # The same control as the index, without the scope question: here every row IS a message, so
        # there is only one kind of time to apply the window to.
        + report_ui.time_filter("t", label="Time", noun="message", hint=_MSG_TIME_HINT) +
        '<button id="xallbtn" data-o="0" onclick="xall(this)">Expand all</button>'
        f'{report_ui.clear_filters_button("message")}'
        f'<span id="count" style="color:#555"></span></div>'
        f'<div class="toolbar">{selbar}</div>'
        '<div class="pager" id="pager"></div>'
        f'<div class="vhdr" id="vhdr" style="grid-template-columns:30px {MSG_COLS}">'
        '<div class="vc sel"><input type="checkbox" class="selall"'
        ' title="Select / unselect every message matching the current filters"'
        ' onclick="SCV.selectShown(this.checked)"></div>'
        '<div class="vc nosort"></div>'
        f'<div class="vc" onclick="SCV.setSort(1)">Created{report_ui.info_icon(_CREATED_HINT)}'
        ' <span class="ar">&#8597;</span></div>'
        f'<div class="vc" onclick="SCV.setSort(2)">Direction{report_ui.info_icon(_DIR_HINT)}'
        ' <span class="ar">&#8597;</span></div>'
        f'<div class="vc" onclick="SCV.setSort(3)">Sender{report_ui.info_icon(_SENDER_HINT)}'
        ' <span class="ar">&#8597;</span></div>'
        f'<div class="vc" onclick="SCV.setSort(4)">Type{report_ui.info_icon(_TYPE_HINT)}'
        ' <span class="ar">&#8597;</span></div>'
        f'<div class="vc nosort">Content{report_ui.info_icon(_CONTENT_HINT)}</div>'
        f'<div class="vc" onclick="SCV.setSort(6)">Msg ID{report_ui.info_icon(_SMID_HINT)}'
        ' <span class="ar">&#8597;</span></div>'
        '<div class="vc nosort">Read</div>'
        '</div></div>'
        '<div class="vwrap msgs" id="vwrap"><div class="vpad" id="vpad"></div>'
        '<div class="vwin" id="vwin"></div></div>'
        f'<div class="vempty" id="vempty" style="display:none">{empty_text}</div>'
        f'<script src="data/{key}/index.js"></script>'
        '<script>'
        'SCV.init({mount:"vwrap",win:"vwin",pad:"vpad",header:"#vhdr",missing:"vmiss",'
        'empty:"vempty",pager:"pager",pageSize:500,selKind:"msg",sort:1,sortDir:1,'
        # A message number restarts in every conversation, so the row anchor ("msg-12.0") is unique
        # only on this page while the *stored* id has to be unique across the run. The anchor stays
        # as it is — every cross-report link and cache_links.json record depends on it.
        f'selPrefix:{json.dumps("conv-" + conv["id"] + "|")},'
        # what a later run matches this message on if our anchor for it has moved
        'selKeys:function(r){var m=r[5]||{},k={conv:' + json.dumps(conv["id"]) + '};'
        'if(m.smid)k.smid=m.smid;'
        'else{k.ts=r[3]["1"];k.sender=r[3]["3"];k.anchor=r[0];}return k;},'
        f'rowHeight:{MSG_ROW_H},estDetail:300,cols:"{MSG_COLS}",'
        f'detailBase:"data/{key}/detail-",'
        'query:function(){return document.getElementById("q").value;},'
        'match:function(m,r){var d=document.getElementById("dir").value,'
        't=document.getElementById("type").value,a=document.getElementById("att").value,'
        'w=scFv("wal");'
        'return (!d||m.dir===d)&&(!t||m.type.indexOf("|"+t+"|")>-1)&&(!a||m.att===a)'
        '&&(!w||m.wal===w)&&scTimeHit(scTimeWin("t"),m.ts);},'
        'rowClass:function(m){return m.dir==="Sent"?"out":"";},'
        'count:function(n,t){document.getElementById("count").textContent='
        'n===t?(n+" messages"):(n+" of "+t+" shown");},'
        'reset:function(){document.getElementById("q").value="";'
        'document.getElementById("dir").value="";document.getElementById("type").value="";'
        'document.getElementById("att").value="";scFvReset("wal");scTimeReset("t");}});'
        'scSyncBoxes();scSelNote();SCSel.onChange(function(){scSyncBoxes();scSelNote();});'
        'scConsumeHash();'
        '</script></body></html>')

    with open(os.path.join(pages_dir, f"{key}.html"), "w", encoding="utf-8") as fh:
        fh.write(doc)
    return f"pages/{key}.html"


def _first_last(conv, which):
    """One activity cell: the time, plus a marker when it is not a message time.

    A conversation with messages shows theirs. A conversation with none shows the dates on its own
    feed row — the only record that it was ever active — badged «feed» so the cell cannot be read
    as "a message was sent then". See :data:`_FEED_BASIS`.
    """
    act = conv.get("activity") or {}
    return report_ui.activity_cell(act.get(which) or "", act.get("source"))


# --------------------------------------------------------------------------- index page

def _index_detail(conv, closure=None):
    """The expanded index row: every participant, with the permanent user id of each.

    The row itself can only name two participants before it overflows, so a group chat's membership
    used to be readable only on the conversation's own page. Here it is one click away, and — since
    the search index carries the same ids — searchable from the index whether or not it is open.
    """
    parts = conv["participants"]
    if parts:
        rows = "".join(
            "<tr>"
            f'<td>{_participant_html(p, "../", chip=False, closure=closure)}</td>'
            f'<td class="mono">{_esc(p["user_id"]) or "<span class=muted>not recorded</span>"}</td>'
            f'<td>{_esc(p["username"])}</td>'
            f'<td>{text_html(p["display"])}</td>'
            "</tr>" for p in parts)
        table = ('<table class="sub"><tr><th>Participant</th><th>User ID</th><th>Username</th>'
                 f'<th>Display name</th></tr>{rows}</table>')
    else:
        table = '<span class="muted">No participant is recorded for this conversation.</span>'
    return (f'<div class="sect">Participants ({len(parts)})'
            + report_ui.info_icon(conv["participants_src"] + " " + _PARTY_HINT) + "</div>"
            + table
            + '<div class="sect">Conversation IDs</div>'
            + _grid([("Client", f'<span class="mono">{_esc(conv["id"])}</span>', ""),
                     ("Server", f'<span class="mono">{_esc(conv["server_id"])}</span>'
                      if conv["server_id"] else '<span class="muted">not recorded</span>', "")])
            + _activity_block(conv))


def _activity_block(conv):
    """The dates section of an expanded row: where each one came from, stated field by field.

    Always rendered, not only for message-less conversations — ``creation_timestamp`` is worth
    seeing next to the message range precisely when both exist, because the two disagreeing is
    itself a finding (a restored device creates its conversation rows on restore day).
    """
    act = conv.get("activity") or {}
    if not act.get("source"):
        return ('<div class="sect">Dates</div><span class="muted">This conversation carries no '
                'date at all: no message, and arroyo.db holds no conversation or feed_entry row '
                'for it.</span>')
    if act["source"] == "messages":
        rows = [("First message", _esc(act["first"]), ""), ("Last message", _esc(act["last"]), "")]
        note = ("arroyo.db conversation_message.creation_timestamp, first and last of the "
                f'{conv["n_messages"]} message(s) listed on this conversation\'s page.')
    else:
        rows = [("First activity (feed)", _esc(act["first"]), ""),
                ("Last activity (feed)", _esc(act["last"]), "")]
        note = _FEED_BASIS
    if act.get("created"):
        rows.append(("Conversation row created", _esc(act["created"]), ""))
    return (f'<div class="sect">Dates{report_ui.info_icon(note + " " + _CREATED_ROW_HINT)}</div>'
            + _grid(rows))


_CREATED_ROW_HINT = (
    "\"Conversation row created\" is arroyo.db conversation.creation_timestamp — when THIS device "
    "created its local row for the conversation, which is not the same as when the conversation "
    "started. On a device restored from a backup it is the restore, and the conversation's own "
    "messages are older than it. Verified on the test corpus.")


def generate_index(conversations, outdir, tz_label, run_id, stats, closure=None, prov=None):
    """Write ``Conversations_report.html`` + ``data/index.js``; return the report path."""
    data_dir = os.path.join(outdir, "data")
    details = [(f'conv-{c["id"]}', _index_detail(c, closure)) for c in conversations]
    chunk_of = report_ui.write_details(data_dir, details)

    rows = []
    for conv in conversations:
        parts = conv["participants"]
        activity = conv.get("activity") or {}
        # the row is one fixed height: name the first two participants and count the rest — the
        # expanded row (and the conversation's own page) lists them all with their user ids
        shown = ", ".join(_participant_html(p, "../", chip=False, closure=closure)
                          for p in parts[:2])
        if len(parts) > 2:
            shown += (f' <span class="more" title="{len(parts) - 2} more participant(s) — expand '
                      f'this row to see them all">+{len(parts) - 2}</span>')
        anchor = f'conv-{conv["id"]}'
        cells = [
            "&#9656;",
            _kind_badge(conv["kind"]),
            text_html(conv["title"]),
            f'<span class="cid">{_esc(conv["id"])}</span>',
            shown,
            str(conv["n_messages"]),
            str(conv["n_attachments"]) if conv["n_attachments"] else "",
            _first_last(conv, "first"),
            _first_last(conv, "last"),
            f'<a class="openbtn" target="scauto_conv_page" title="open this conversation in its '
            f'own tab" href="{_esc(conv["page"])}#conv-{_esc(conv["id"])}">open &#9656;</a>',
        ]
        labels = [p["label"] for p in parts]
        searchable = [conv["id"], conv["server_id"], conv["title"], conv["kind"]] + labels
        # every participant's permanent id and username, so a user id pasted into the search box
        # finds the conversations that account is in — including the ones the row cannot name
        searchable += [p["user_id"] for p in parts] + [p["raw"] for p in parts]
        searchable += [p["username"] for p in parts] + [p["display"] for p in parts]
        searchable += list(conv["senders"])
        rows.append([
            anchor, cells,
            " ".join(s for s in searchable if s).lower(),
            {"1": conv["kind"], "2": conv["title"].lower(), "3": conv["id"],
             "4": " ".join(labels).lower(), "5": conv["n_messages"],
             "6": conv["n_attachments"], "7": conv["first_sort"], "8": conv["last_sort"]},
            chunk_of.get(anchor),
            {"kind": conv["kind"], "msg": "y" if conv["n_messages"] else "n",
             "att": "y" if conv["n_attachments"] else "n",
             "wal": "gone" if conv["n_wal_gone"] else "live",
             # The two kinds of time a conversation has, kept apart because they are different
             # claims. `ct` is the conversation's own first/last activity, which for a conversation
             # holding no message comes from the app's chat feed and says only that it was active
             # then (report_ui.FEED_DATE_TITLE). `mt` is every message's own time. The scope control
             # picks which the window is applied to, and "message times" has to mean exactly that —
             # a feed date presented as a message time would be a claim the evidence does not make.
             "ct": report_ui.ts_keys(*(activity.get(k) or "" for k in ("first", "last"))),
             "mt": report_ui.ts_keys(*(m["created"] for m in conv["messages"])),
             # the conversation's other identity, recorded with a selection as a fallback match
             **({"sid": conv["server_id"]} if conv["server_id"] else {})},
        ])
    report_ui.write_rows(data_dir, rows)

    # What the report did not list, stated rather than left silent: the merge's duplicate media rows
    # and any row whose conversation id was unusable (see `_drop_unrenderable`).
    skipped = stats.get("dropped", 0) + stats.get("skipped_conv", 0)
    skipped_note = (f' &middot; {skipped} parsed row(s) not listed'
                    + report_ui.info_icon(
                        f"{stats.get('dropped', 0)} row(s) were the duplicates the message/cache "
                        f"join produces — a media-only content type (\"Video (Unknown Source)\", "
                        f"\"Sticker\") whose cache claim has no displayable file on disk; the "
                        f"legacy Communications report drops exactly these. "
                        f"{stats.get('skipped_conv', 0)} row(s) had no conversation id of the "
                        f"expected 36-character form and could not be attributed to a "
                        f"conversation.")) if skipped else ""

    empty = sum(1 for c in conversations if not c["n_messages"])
    empty_note = (f'<div class="note">{empty} conversation(s) are listed with <b>0 messages</b>: '
                  f'the friends / groups list names the conversation but arroyo.db held no message '
                  f'for it in this extraction.'
                  + report_ui.info_icon(
                      "These rows come from the CONVERSATION_ID of a contact or the GROUP_ID of a "
                      "group in the friends artifact. They are listed rather than dropped because "
                      "a known conversation with no recoverable messages is itself a finding — the "
                      "messages may have been deleted, or not captured by the extraction.")
                  + '</div>') if empty else ""

    partial_css, banner, figures = partial_report.page_chrome(closure, "conv", prov)
    msg_figures = partial_report.figures_html(closure, "msg") if closure is not None else ""
    doc = (
        _head("Snapchat conversations", "../", run_id, "conv", "",
              reports_root=os.path.dirname(os.path.abspath(outdir)),
              partial_css=partial_css)
        + '<body>'
        f'<header><h1>Snapchat conversations</h1>'
        f'<div class="sum"><b>{len(conversations)}</b> conversation(s) &middot; '
        f'<b>{stats["messages"]}</b> message(s) &middot; '
        f'<b>{stats["attachments"]}</b> with an attachment'
        + (f' ({stats["files"]} files)' if stats["files"] != stats["attachments"] else "")
        + ' &middot; '
        f'{stats["groups"]} group / {stats["private"]} private &middot; '
        f'times in <b>{_esc(tz_label)}</b>{skipped_note}</div>'
        f'{figures}{msg_figures}'
        f'<div class="sum">Source: arroyo.db conversation_message, joined to cache_controller.db '
        f'and the friends / groups artifacts by the iOS parser</div></header>'
        f'{banner}'
        f'{empty_note}'
        # the "row data missing" banner fires on an empty row set, so only emit it when there is
        # something to load in the first place
        + (report_ui.missing_data_banner("Conversations_report.html") if conversations else "") +
        '<div class="stickytop"><div class="toolbar">'
        '<input type="search" id="q" placeholder="Search conversation, participant, id, sender…" '
        'oninput="flt()">'
        '<label>Type <select id="kind" onchange="flt()"><option value="">all</option>'
        '<option value="Private">private</option><option value="Group">group</option>'
        '<option value="Unknown">unknown</option></select></label>'
        '<label>Messages <select id="msg" onchange="flt()"><option value="">any</option>'
        '<option value="y">with messages</option><option value="n">no messages</option>'
        '</select></label>'
        '<label>Attachments <select id="att" onchange="flt()"><option value="">any</option>'
        '<option value="y">with</option><option value="n">without</option></select></label>'
        + _wal_filter_html(sum(1 for c in conversations if c["n_wal_gone"]), "conversation")
        + report_ui.time_filter(
            "t", label="Time", noun="conversation", hint=_TIME_SCOPE_HINT,
            scopes=(("both", "either kind of time"), ("mt", "message times only"),
                    ("ct", "first / last activity only")))
        + report_ui.clear_filters_button("conversation") +
        '<span id="count" style="color:#555"></span></div>'
        f'<div class="toolbar">{report_ui.selection_toolbar("conversation")}</div>'
        '<div class="pager" id="pager"></div>'
        f'<div class="vhdr" id="vhdr" style="grid-template-columns:30px {CONV_COLS}">'
        '<div class="vc sel"><input type="checkbox" class="selall"'
        ' title="Select / unselect every conversation matching the current filters"'
        ' onclick="SCV.selectShown(this.checked)"></div>'
        '<div class="vc nosort" title="expand a row for its full participant list"></div>'
        '<div class="vc" onclick="SCV.setSort(1)">Type <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(2)">Conversation <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(3)">Conversation ID <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(4)">Participants <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(5)">Msgs <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(6)">Att. <span class="ar">&#8597;</span></div>'
        f'<div class="vc" onclick="SCV.setSort(7)">First activity'
        f'{report_ui.info_icon(_ACTIVITY_HINT)} <span class="ar">&#8597;</span></div>'
        f'<div class="vc" onclick="SCV.setSort(8)">Last activity'
        f'{report_ui.info_icon(_ACTIVITY_HINT)} <span class="ar">&#8597;</span></div>'
        '<div class="vc nosort">Detail</div>'
        '</div></div>'
        '<div class="vwrap convs" id="vwrap"><div class="vpad" id="vpad"></div>'
        '<div class="vwin" id="vwin"></div></div>'
        '<div class="vempty" id="vempty" style="display:none">'
        'No conversation matches the current filters.</div>'
        '<script src="data/index.js"></script>'
        '<script>'
        'SCV.init({mount:"vwrap",win:"vwin",pad:"vpad",header:"#vhdr",missing:"vmiss",'
        'empty:"vempty",pager:"pager",pageSize:500,selKind:"conv",sort:5,sortDir:-1,'
        'selKeys:function(r){var m=r[5]||{},k={conv:r[0].slice(5)};'
        'if(m.sid)k.server=m.sid;return k;},'
        f'rowHeight:{CONV_ROW_H},estDetail:200,cols:"{CONV_COLS}",detailBase:"data/detail-",'
        'query:function(){return document.getElementById("q").value;},'
        'match:function(m,r){var k=document.getElementById("kind").value,'
        'g=document.getElementById("msg").value,a=document.getElementById("att").value,'
        'w=scFv("wal");'
        'return (!k||m.kind===k)&&(!g||m.msg===g)&&(!a||m.att===a)&&(!w||m.wal===w)'
        # The scope decides which list the window is applied to. "both" is the union, so it can only
        # ever show more than either alone — the safe default for a filter, since a filter that
        # under-includes hides evidence. The other two are exact about which claim they are matching.
        '&&scTimeHit(scTimeWin("t"),scConvTimes(m))'
        '&&(!document.getElementById("selonly").checked||SCSel.get("conv",SCV.selId(r[0])));},'
        'selectedOnly:function(){return document.getElementById("selonly").checked;},'
        'selCount:function(n){document.getElementById("selcount").textContent=n+" selected";'
        'scSelNote();},'
        'count:function(n,t){document.getElementById("count").textContent='
        'n===t?(n+" conversations"):(n+" of "+t+" shown");},'
        'reset:function(){document.getElementById("q").value="";'
        'document.getElementById("kind").value="";document.getElementById("msg").value="";'
        'document.getElementById("att").value="";scFvReset("wal");scTimeReset("t");'
        'document.getElementById("selonly").checked=false;}});'
        'scSelNote();scConsumeHash();'
        '</script></body></html>')

    report = os.path.join(outdir, "Conversations_report.html")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return report


# --------------------------------------------------------------------------- manifests

def write_cache_links(conversations, outdir):
    """Write ``cache_links.json`` (version 3) — how the cache_controller report links back here.

    Same two indexes as the manifest the legacy Communications report writes, plus an ``href`` that
    points at the message's row on its conversation page (relative to the reports root), because
    with per-conversation pages the target is no longer one document:

    * ``by_key``     : CACHE_KEY -> [{conversation_id, server_message_id, anchor, href, title}]
    * ``by_message`` : "<conversation id>|<server message id>" -> the same records

    ``by_message`` is what lets a cache entry link back even when it is not the exact file this
    report displayed — a chat video is typically a full-media claim, a thumbnail claim and a raw
    content claim, and only one of them is shown.
    """
    manifest = {"version": 3, "report": "Conversations", "by_key": {}, "by_message": {}}
    for conv in conversations:
        # the title travels as plain text: it is a value for another report to escape, not markup
        # (the parser encodes emoji as &#NNNN; character references)
        title = html.unescape(conv["title"])
        for msg in conv["messages"]:
            # Only messages that actually have a recovered attachment are listed — as in the legacy
            # manifest. A message with no cached file can never be what a cache entry points at, and
            # indexing every message would make this file grow with the whole chat history.
            if not msg["atts"]:
                continue
            record = {"conversation_id": conv["id"], "server_message_id": msg["smid"],
                      "anchor": msg["anchor"], "title": title,
                      "href": f'Conversations/{conv["page"]}#{msg["anchor"]}'}
            manifest["by_message"].setdefault(f'{conv["id"]}|{msg["smid"]}', []).append(record)
            # one message can hold several cached files; each is a way into the same message
            for att in msg["atts"]:
                key = att.get("cache_key")
                if key:
                    manifest["by_key"].setdefault(key, []).append(record)
    try:
        with open(os.path.join(outdir, "cache_links.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
    except OSError as error:
        logger.debug(f"Could not write cache_links.json: {error}")
    return manifest


def write_page_manifest(conversations, outdir):
    """Write ``conversation_pages.json`` (conversation id -> detail page), like ``memory_pages``."""
    pages = {c["id"]: c["page"] for c in conversations}
    try:
        with open(os.path.join(outdir, "conversation_pages.json"), "w", encoding="utf-8") as fh:
            json.dump(pages, fh)
    except OSError as error:
        logger.debug(f"Could not write conversation_pages.json: {error}")
    return pages


def conversation_index(conversations):
    """The per-conversation summary the Contacts report needs to link to conversations.

    ``participants`` travels as plain values rather than markup because the Contacts report
    **inverts** it: a contact belongs to every conversation whose participant list names them, not
    only to the one CONVERSATION_ID the friends artifact recorded against them. A contact in three
    group chats is in three conversations, and a report that shows one of them is hiding two.
    """
    # first/last travel as PLAIN TEXT with the source beside them, not as the index's marked-up
    # cell: the Contacts report escapes what it is given, so markup arrives there as visible tag
    # soup. It renders the same "feed" marker itself, from `date_source`.
    return {c["id"]: {"page": c["page"], "title": c["title"], "kind": c["kind"],
                      "messages": c["n_messages"], "attachments": c["n_attachments"],
                      "first": (c["activity"] or {}).get("first", ""),
                      "last": (c["activity"] or {}).get("last", ""),
                      "date_source": (c["activity"] or {}).get("source", ""),
                      "first_sort": c["first_sort"], "last_sort": c["last_sort"],
                      "participants": [{"user_id": p["user_id"], "username": p["username"],
                                        "display": p["display"], "raw": p["raw"],
                                        "label": p["label"], "is_owner": p["is_owner"]}
                                       for p in c["participants"]]}
            for c in conversations}


# --------------------------------------------------------------------------- entry

def index(msg_df, friends_df, group_df, outdir, cachefiles_dir, arroyo=None, tz="local",
          owner_user_id="", owner_username="", cache_key_for=None, report_dir=None, primary=None,
          identifiers=None):
    """Build the conversations report.

    msg_df         : the parser's message frame (see the COL_* names) — **before** its content is
                     turned into the legacy report's HTML.
    friends_df     : whichever getFriends* source answered; group_df: the groups frame.
    outdir         : output directory (…/Reports/Conversations).
    cachefiles_dir : the folder the parser copied the chat attachments into.
    arroyo         : arroyo.db path, for the optional user_conversation enrichment.
    cache_key_for  : callable(attachment file name) -> (CACHE_KEY | None, explanation), used for the
                     link to the cache_controller report (ParseSnapchat_iOS.cacheControllerKey).
    primary        : primary.docobjects path, for the contacts' username / legacy-username pair.
    identifiers    : an already-loaded ``load_identifiers(primary)``, shared with the Contacts
                     report so that file is read once per run; None re-reads it from ``primary``.
    Returns ``(report_path, conversation_index)``; the index feeds the Contacts report's links.
    """
    os.makedirs(outdir, exist_ok=True)
    rdir = report_dir or os.path.dirname(os.path.abspath(outdir))
    run_id = report_ui.run_id(rdir)
    report_ui.write_selection_stub(rdir, run_id)
    timefmt, tz_label = make_time_formatter(tz)

    # the same contact model the Contacts report builds, so a participant here and a row there are
    # the same person with the same identifiers
    contacts = apply_identifiers(
        normalize_contacts(friends_df, owner_user_id, owner_username),
        load_identifiers(primary) if identifiers is None else identifiers)
    contact_links = contact_link_index(contacts)               # hrefs relative to the reports root
    groups = normalize_groups(group_df)
    owner_names = [c["username"] or c["display"] for c in contacts if c["is_owner"]]
    if owner_username:
        owner_names.append(re.sub(r"</?b>", "", str(owner_username)).strip())

    # Attachment publishing happens inside build_messages, so it stays on the index side. Unlike the
    # decryption and hashing the other reports defer, these are hard links out of the folder the
    # parser already filled — near-zero cost. What that does mean is that a partial run's media/ will
    # hold files no included message references, so the partial writer has to prune it to what the
    # rendered rows actually point at.
    by_conv, drop_stats = build_messages(msg_df, cachefiles_dir, os.path.join(outdir, "media"),
                                         timefmt, cache_key_for, owner_user_id, owner_names)
    conversations = build_conversations(by_conv, contacts, groups,
                                        load_arroyo_conversations(arroyo, msg_df), contact_links,
                                        timefmt)

    # The closure's view: two kinds, because a conversation and the messages inside it are separately
    # selectable. A message's *store* id is qualified with its conversation — a message number
    # restarts in every chat, so the page anchor alone names a different message in every one.
    sel_conv = partial_report.Index("conv")
    sel_msg = partial_report.Index("msg")
    for conv in conversations:
        conv_row = f'conv-{conv["id"]}'
        sel_conv.add(conv_row, conv, conv=conv["id"], server=conv.get("server_id"))
        for participant in conv.get("participants") or ():
            if participant.get("user_id"):
                sel_conv.link(partial_report.EDGE_CONV_PARTICIPANT, conv_row, "ct",
                              participant.get("anchor") or contact_anchor(participant))
        for msg in conv.get("messages") or ():
            msg_row = f'{conv_row}|{msg["anchor"]}'
            # Both alternates are qualified with the conversation, for the same reason the row id is:
            # a server message id is a per-conversation ordinal and a timestamp is not unique across
            # chats, so an unqualified key would put one selection on a message in every conversation.
            sel_msg.add(msg_row, msg,
                        smid=f'{conv["id"]}|{msg["smid"]}' if msg.get("smid") else "")
            if msg.get("created_unix") and msg.get("sender"):
                # what finds a message whose anchor was only its position in the conversation
                sel_msg.keys[("ts_sender", f'{conv["id"]}|{msg["created_unix"]}'
                                           f'|{str(msg["sender"]).lower()}')].add(msg_row)
            sel_msg.contains(msg_row, conv_row)
            sel_conv.link(partial_report.EDGE_CONV_MESSAGE, conv_row, "msg", msg_row)
            for att in msg.get("atts") or ():
                if att and att.get("cache_key"):
                    sel_msg.link(partial_report.EDGE_MESSAGE_CACHE, msg_row, "cc",
                                 f'ck-{att["cache_key"]}')

    return partial_report.Stage("conv", conversations, sel_conv,
                                indexes={"conv": sel_conv, "msg": sel_msg},
                                outdir=outdir, tz_label=tz_label, run_id=run_id,
                                contact_links=contact_links, drop_stats=drop_stats)


def _prune_media(outdir, conversations):
    """Delete published attachments no rendered message points at. Returns how many went.

    Attachment publishing happens on the index side — they are hard links out of the folder the parser
    already filled, so deferring them would buy nothing — which means a partial run's ``media/`` starts
    out holding every conversation's files, including those of messages it does not render. A
    disclosure folder carrying media that nothing in it references is a defect: the file is there, an
    examiner can open it, and no page says where it came from.

    Only ever called on a partial run, and only inside that run's own output folder. These are links,
    so removing them cannot touch the extracted copy they point at.
    """
    media_dir = os.path.join(outdir, "media")
    if not os.path.isdir(media_dir):
        return 0
    keep = {os.path.basename(att["rel"])
            for conv in conversations for msg in conv.get("messages") or ()
            for att in msg.get("atts") or () if att and att.get("rel")}
    removed = 0
    for name in os.listdir(media_dir):
        if name in keep:
            continue
        try:
            os.remove(os.path.join(media_dir, name))
            removed += 1
        except OSError as error:
            logger.warning(f"Could not remove {name} from this extract's media folder, which no "
                           f"included message references: {error}")
    return removed


def _narrowed(conv, kept):
    """One conversation with its message list cut to *kept*, and every figure recomputed from it.

    A shallow copy, because the model itself must stay intact: the closure was decided from it and the
    other reports still read it. Recomputing rather than copying the counts is the point — a page that
    lists four messages while its own header says 137 states something untrue about the extract, and
    the header is what a reader takes at face value. ``n_messages_full`` keeps the conversation's real
    size, which is what the "N of M" figure needs.
    """
    msgs = [m for m in conv.get("messages") or () if f'conv-{conv["id"]}|{m["anchor"]}' in kept]
    senders, types = {}, {}
    for msg in msgs:
        if msg["sender"]:
            senders[msg["sender"]] = senders.get(msg["sender"], 0) + 1
        for ctype in (msg["types"] or ["(none)"]):
            types[ctype] = types.get(ctype, 0) + 1
    return {**conv, "messages": msgs,
            "n_messages_full": conv["n_messages"],
            "n_messages": len(msgs),
            "n_files": sum(len(m["atts"]) for m in msgs),
            "n_attachments": sum(1 for m in msgs if m["atts"]),
            "n_missing": sum(1 for m in msgs for a in m["atts"] if not a["rel"]),
            "n_wal_gone": sum(1 for m in msgs if m.get("wal") == sqlite_open.MAIN_ONLY),
            "senders": senders, "types": types}


def render(stage, closure=None, prov=None):
    """Write the index, the per-conversation pages and the manifests.

    ``closure=None`` renders everything. With a closure, a conversation shows only the messages the
    closure includes — which is what "parts of conversations" means.
    """
    outdir, tz_label, run_id = stage["outdir"], stage["tz_label"], stage["run_id"]
    contact_links = stage["contact_links"]
    conversations = [record for _row_id, record in stage.sel.keep(closure)]

    if closure is not None:
        kept = closure.included.get("msg", set())
        conversations = [_narrowed(conv, kept) for conv in conversations]

    write_assets(outdir)
    for conv in conversations:
        conv["page"] = render_conversation_page(conv, outdir, tz_label, run_id,
                                                contact_links=contact_links, closure=closure,
                                                prov=prov)

    stats = {"messages": sum(c["n_messages"] for c in conversations),
             "files": sum(c["n_files"] for c in conversations),
             "attachments": sum(c["n_attachments"] for c in conversations),
             "groups": sum(1 for c in conversations if c["kind"] == "Group"),
             "private": sum(1 for c in conversations if c["kind"] == "Private"),
             **stage["drop_stats"]}
    report = generate_index(conversations, outdir, tz_label, run_id, stats, closure=closure,
                            prov=prov)
    write_page_manifest(conversations, outdir)
    write_cache_links(conversations, outdir)

    logger.info(f"Conversations report: {os.path.abspath(report)}")
    if closure is None:
        logger.info(f"  {len(conversations)} conversation(s), {stats['messages']} message(s), "
                    f"{stats['attachments']} with an attachment ({stats['files']} file(s))")
    else:
        pruned = _prune_media(outdir, conversations)
        logger.info(f"  {len(conversations)} of {len(stage.model)} conversation(s) in this extract, "
                    f"holding {sum(len(c['messages']) for c in conversations)} selected message(s)")
        if pruned:
            logger.info(f"  {pruned} published attachment(s) removed: no message in this extract "
                        f"references them")
    return report, conversation_index(conversations)


def main(msg_df, friends_df, group_df, outdir, cachefiles_dir, arroyo=None, tz="local",
         owner_user_id="", owner_username="", cache_key_for=None, report_dir=None, primary=None,
         identifiers=None):
    """Build the Conversations report: :func:`index` then :func:`render`.

    See :func:`index` for the arguments. A partial run calls the two halves separately, so the closure
    is decided from every report's index before any page is written.
    """
    stage = index(msg_df, friends_df, group_df, outdir, cachefiles_dir, arroyo=arroyo, tz=tz,
                  owner_user_id=owner_user_id, owner_username=owner_username,
                  cache_key_for=cache_key_for, report_dir=report_dir, primary=primary,
                  identifiers=identifiers)
    return render(stage)
