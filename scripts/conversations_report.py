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
from scripts.contacts_report import (normalize_contacts, normalize_groups, apply_identifiers,
                                     load_identifiers, contact_link_index, text_html, cell)
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
COL_CREATED = "Creation Timestamp UTC+0"
COL_READ = "Read Timestamp UTC+0"
COL_SMID = "Server Message ID"
COL_CMID = "Client Message ID"                                 # optional

# Index-table geometry (one fixed row height + one column track list for the header and every row).
CONV_COLS = ("86px minmax(160px,1.1fr) minmax(170px,1.2fr) 66px 66px 152px 152px 250px 96px")
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
    if COL_CONV not in getattr(msg_df, "columns", []):
        logger.warning(f"Conversations: the message frame has no '{COL_CONV}' column "
                       f"({list(getattr(msg_df, 'columns', []))}) — no conversation can be built "
                       f"from it")
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
        by_conv.setdefault(conv_id, []).append({
            "smid": cell(row.get(COL_SMID)),
            "cmid": _id_str(row.get(COL_CMID)),
            "sender": sender_plain,
            "sender_bold": sender != sender_plain,
            "direction": "Sent" if outgoing else ("Received" if sender_plain else ""),
            "types": [ctype] if ctype else [],
            "text": None if att else content,
            "atts": [att] if att else [],
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
        # a row that carried the text keeps it (the row a claim was joined onto has the file
        # instead), and a row that actually has a timestamp beats a cache-only row's "Unknown"
        if first["text"] in (None, "") and row["text"]:
            first["text"] = row["text"]
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

def load_arroyo_conversations(arroyo, msg_df=None):
    """``user_conversation`` → ``{conversation id: {"type", "user_ids", "server_id"}}``.

    The authoritative statement of what a conversation *is* (0 = private, 1 = group) and who is in
    it. The table is absent on newer Snapchat schemas, so this is best-effort: an empty result just
    means the report falls back to the friends/groups lists. The server-side conversation id is
    taken from the message frame when the messages carry one.
    """
    out = {}
    if msg_df is not None and COL_SCONV in getattr(msg_df, "columns", []):
        for conv_id, server_id in zip(msg_df[COL_CONV], msg_df[COL_SCONV]):
            key, value = cell(conv_id), _id_str(server_id)
            if key and value:
                out.setdefault(key, {"type": None, "user_ids": [], "server_id": ""})
                out[key]["server_id"] = out[key]["server_id"] or value
    if not (arroyo and os.path.isfile(arroyo)):
        return out
    try:
        conn = sqlite3.connect(f"file:{arroyo}?mode=ro", uri=True)
        try:
            cur = conn.execute("select client_conversation_id, conversation_type, "
                               "group_concat(user_id) from user_conversation "
                               "group by client_conversation_id, conversation_type")
            for conv_id, ctype, user_ids in cur.fetchall():
                if not conv_id:
                    continue
                rec = out.setdefault(str(conv_id),
                                     {"type": None, "user_ids": [], "server_id": ""})
                if ctype is not None:
                    rec["type"] = ctype
                rec["user_ids"] += [u for u in str(user_ids or "").split(",") if u]
        finally:
            conn.close()
    except sqlite3.DatabaseError as error:
        logger.info(f"Conversations: user_conversation not available ({error}) — conversation type "
                    f"and participants will come from the friends/groups lists only")
    return out


_OWNER_BADGE = ('<span class="ownerdot" title="the account this extraction came from">'
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
            "href": found.get("href"), "is_owner": bool(found.get("is_owner")), "raw": raw}


def _participant_html(part, root, chip=True):
    """A participant as a chip: both names, the owner marked, linked to their contact row.

    ``root`` is this page's path back to the reports folder, since the contact link is stored
    relative to it (``../`` from the index, ``../../`` from a conversation page).
    """
    body = text_html(part["label"])
    if part["is_owner"]:
        body += _OWNER_BADGE
    if part["href"]:
        title = f'open the contact record of {part["label"]}'
        body = (f'<a href="{_esc(root + part["href"])}" target="scauto_contacts" '
                f'title="{_esc(title)}">{body}</a>')
    return f'<span class="party">{body}</span>' if chip else body


def build_conversations(by_conv, contacts, groups, arroyo_info, contact_links=None):
    """Assemble one record per conversation, from the messages **and** the contact/group lists.

    A conversation is listed even when it has no messages: a friend or group whose conversation id
    the app knows about but for which ``arroyo.db`` holds nothing is a real (and easy to miss)
    finding, so it appears with 0 messages rather than being left out.

    Every derived value records where it came from (``*_src``), which is what the "?" icons show.
    """
    by_contact = {c["conv_id"]: c for c in contacts if c["conv_id"]}
    by_group = {g["conv_id"]: g for g in groups}

    conversations = []
    for conv_id in sorted(set(by_conv) | set(by_contact) | set(by_group)):
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
            "first_sort": min(times) if times else 0,
            "last_sort": max(times) if times else 0,
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
                 + report_ui.SELECT_CSS + report_ui.HINT_CSS + _REPORT_CSS)
    with open(os.path.join(assets, "ui.js"), "w", encoding="utf-8") as fh:
        # SELECT_JS first: ../selection.js is loaded right after this file and calls SCSel.preload().
        fh.write("/* Snapchat Auto — shared report UI (see scripts/report_ui.py) */\n"
                 + report_ui.SELECT_JS + report_ui.VTABLE_JS + report_ui.HINT_JS
                 + report_ui.NAV_JS + report_ui.SELECT_TOOLBAR_JS + _REPORT_JS)


# Report-specific styling for both tables (kept out of the row data: every byte of a cell is
# multiplied by the row count in the data/*.js files, so per-column styling lives here).
_REPORT_CSS = """
 .vcells>.vc{font-size:12.5px}
 .cid{font-family:ui-monospace,Consolas,monospace;font-size:10px;color:#888}
 .kindbadge{font-weight:700;font-size:11px;white-space:nowrap}
 .kindbadge.group{color:#8a1f5a} .kindbadge.private{color:#25348a} .kindbadge.unknown{color:#999}
 header .kindbadge,header .kindbadge.group,header .kindbadge.private{color:#fff}
 /* conversation index — scoped, so the column rules do not reach the message table below */
 .convs .vcells>.vc.c1{font-weight:600}
 .convs .vcells>.vc.c2 a{color:#2d2d71;text-decoration:none}
 .convs .vcells>.vc.c2 a:hover{text-decoration:underline}
 .convs .vcells>.vc.c3,.convs .vcells>.vc.c4{text-align:right;font-weight:600;color:#2d2d71}
 .convs .vcells>.vc.c5,.convs .vcells>.vc.c6{font-size:11.5px;color:#555}
 .convs .vcells>.vc.c7{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#33367a}
 /* message table (detail pages) */
 .msgs .vcells>.vc.c0{color:#2d2d71;font-weight:700;text-align:center}
 .msgs .vr.open .vc.c0{color:#8a1f5a}
 .msgs .vcells>.vc.c1,.msgs .vcells>.vc.c7{font-size:11px;color:#555;line-height:1.3}
 .msgs .vcells>.vc.c5{font-size:12.5px;line-height:1.35;overflow-wrap:anywhere;white-space:pre-wrap}
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
 .vcells>.vc.c5 .filebtn{margin-right:4px}
 .mdet{font-size:12.5px}
 .mdet .body{background:#fff;border:1px solid #e2e2ea;border-radius:6px;padding:8px 10px;
   margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere;max-width:900px}
 .mdet video{max-width:420px;max-height:320px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.25)}
 .mdet img.full{max-width:420px;max-height:420px;border-radius:6px;
   box-shadow:0 1px 4px rgba(0,0,0,.25)}
 .foot{padding:14px 24px;color:#777;font-size:11.5px}
"""

# Filter glue shared by the index and the message tables (each page defines its own `flt` inputs).
_REPORT_JS = """
var flt_t=0;
function flt(){clearTimeout(flt_t);flt_t=setTimeout(function(){SCV.refilter();},120);}
function xall(btn){
 var op=btn.dataset.o==='1';
 if(!SCV.expandAll(!op,500)){
  alert('Too many rows on this page to expand at once. Narrow the filters or use a smaller '
        +'"rows per page" first.');
  return;}
 btn.dataset.o=op?'0':'1';btn.textContent=op?'Expand all':'Collapse all';}
"""


def _head(title, rel_prefix, run_id, sel_kind, asset_prefix):
    """The common ``<head>`` of the index and the detail pages."""
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{_esc(title)}</title>'
            f'<link rel="stylesheet" href="{asset_prefix}assets/ui.css">'
            f'<script>window.SCAUTO_RUN={json.dumps(run_id)};'
            f'window.SCAUTO_SELKIND="{sel_kind}";</script>'
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
              "attachment was resolved through the row's local_message_references plist.")

_CONTENT_HINT = ("Text comes from the parsed conversation_message.message_content protobuf. When a "
                 "message has a cached attachment, the parser replaces the content with that "
                 "attachment, so the row shows the file rather than any text the message also "
                 "carried — open the row for the file's name, type, size and hashes.")

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


def _attachment_detail(att, prefix, index=None, total=1):
    """One attachment's block inside an expanded message: the file, its hashes and its cache link."""
    parts = []
    label = "Attachment" if total == 1 else f"Attachment {index} of {total}"
    parts.append(f'<div class="sect">{label}</div>')
    if att["rel"] and att["bytes"]:
        url = _esc(prefix + att["rel"])
        if att["kind"] == "image":
            parts.append(f'<div><a href="{url}" target="_blank">'
                         f'<img class="full" src="{url}" loading="lazy"></a></div>')
        elif att["kind"] == "video":
            parts.append(f'<div><video controls preload="metadata" src="{url}"></video>'
                         f'<div><a href="{url}" target="_blank">open {_esc(att["ext"])} in a '
                         f'new tab</a></div></div>')
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
            f'<a class="chip cache" target="scauto_cache" '
            f'href="{prefix}../CacheController/CacheController_report.html'
            f'#ck-{_esc(att["cache_key"])}">&#128451; cache_controller entry '
            f'{_esc(att["cache_key"][:8])}…</a>'
            + report_ui.info_icon(att["cache_key_how"]) + '</div>')
    else:
        parts.append('<div class="muted">No cache_controller entry could be resolved for this '
                     'file name.' + report_ui.info_icon(
                         "Attachments copied out of the SCContent cache are named after their "
                         "cache_controller CACHE_KEY, and SCPersistentMedia copies are matched "
                         "to a claim carrying the same conversation / message / part. Neither "
                         "applied to this file name, so there is no row to link to.") + '</div>')
    return "".join(parts)


def _message_detail(msg, conv, prefix="../", contact_links=None):
    """The expandable per-message block: full text / media, hashes, provenance."""
    parts = []
    if msg["text"] is not None and cell(msg["text"]):
        parts.append('<div class="sect">Message text</div>'
                     f'<div class="body">{text_html(msg["text"])}</div>')
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
        parts.append(_attachment_detail(att, prefix, n, len(atts)))
    if not atts and (msg["text"] is None or not cell(msg["text"])):
        parts.append('<div class="muted">This row carries neither text nor a recovered '
                     'attachment.</div>')

    # No "?" popovers in here: their text is the same for every message, and this block is written
    # once per message into the detail chunks. The columns they belong to carry them instead, in the
    # table header, where they are written once per page.
    parts.append('<div class="sect">Row values (raw)</div>')
    sender = text_html(msg["sender"])
    link = (contact_links or {}).get(msg["sender"].lower()) if msg["sender"] else None
    if link:                                                   # from pages/<key>.html to Contacts/
        sender = (f'<a class="detail" target="scauto_contacts" '
                  f'href="{_esc("../../" + link["href"])}">{sender} &#9656;</a>')
    if msg["direction"] == "Sent":
        sender += _OWNER_BADGE
    parts.append(_grid([
        ("client_conversation_id", f'<span class="mono">{_esc(conv["id"])}</span>', ""),
        ("server_conversation_id", f'<span class="mono">{_esc(conv.get("server_id"))}</span>', ""),
        ("server_message_id", _esc(msg["smid"]), "mono"),
        ("client_message_id", _esc(msg["cmid"]), "mono"),
        ("sender_id", sender, ""),
        ("content_type", _esc(" + ".join(msg["types"])), ""),
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
        # every file of the message, side by side — a message is one row however many it carries
        content = "".join(_attachment_cell(a) for a in atts)
        text = cell(msg["text"])
        if text:
            # the row is one fixed height, so only about this much of a message is ever visible:
            # keep the cell small and leave the full text to the expanded detail (every byte here
            # is multiplied by the message count)
            clipped = text[:200] + " …" if len(text) > 200 else text
            content = f'<span class="msgtext">{text_html(clipped)}</span>' + content
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
                      msg["read_utc"], msg["created"], text]
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
             "att": "y" if atts else "n"},
        ])
    return rows


_PARTY_HINT = ("Each participant is shown as \"display name (username)\" and opens that contact's "
               "record in the Contacts report, which holds all of their identifiers — display "
               "name, current username, previous username where one was recorded, and the "
               "permanent user id.")


def render_conversation_page(conv, outdir, tz_label, run_id, index_name="Conversations_report.html",
                             contact_links=None):
    """Write ``pages/<key>.html`` (+ its data files) for one conversation."""
    key = _page_key(conv["id"])
    pages_dir = os.path.join(outdir, "pages")
    data_dir = os.path.join(pages_dir, "data", key)
    os.makedirs(pages_dir, exist_ok=True)

    details = [(m["anchor"], _message_detail(m, conv, contact_links=contact_links))
               for m in conv["messages"]]
    chunk_of = report_ui.write_details(data_dir, details)
    report_ui.write_rows(data_dir, _message_rows(conv, chunk_of))

    parts_html = "".join(_participant_html(p, "../../") for p in conv["participants"])
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

    selbar = ('<span class="selbar">'
              '<label class="selrow"><input type="checkbox" class="selbox" data-kind="conv" '
              f'data-id="conv-{_esc(conv["id"])}"> mark this conversation for the case</label>'
              '<button onclick="scSelSave()" title="Download selection.js — put it next to the '
              'reports so every report of this run loads it">&#128190; Save selections</button>'
              '<span class="selnote" id="selnote"></span></span>')

    # A conversation with no messages must not trip the virtual table's "row data missing" banner
    # (it fires on an empty row set), and its empty table needs to say why it is empty.
    has_messages = bool(conv["messages"])
    empty_text = ('No message matches the current filters.' if has_messages else
                  'This conversation has no message in arroyo.db — it is listed because the '
                  'friends / groups list names its conversation id.')
    type_opts = "".join(f'<option value="{html.escape(t, quote=True)}">{_esc(t)}</option>'
                        for t in sorted(conv["types"]))
    doc = (
        _head(f'Conversation {_short(conv["title"], 40)}', "../../", run_id, "msg", "../")
        + '<body>'
        f'<header><h1>{text_html(conv["title"])} &mdash; conversation</h1>'
        f'<div class="sum">{_kind_badge(conv["kind"])} &middot; {conv["n_messages"]} message(s) '
        f'&middot; {conv["n_attachments"]} with an attachment &middot; times in '
        f'<b>{_esc(tz_label)}</b></div></header>'
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
        '<button id="xallbtn" data-o="0" onclick="xall(this)">Expand all</button>'
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
        f'rowHeight:{MSG_ROW_H},estDetail:300,cols:"{MSG_COLS}",'
        f'detailBase:"data/{key}/detail-",'
        'query:function(){return document.getElementById("q").value;},'
        'match:function(m,r){var d=document.getElementById("dir").value,'
        't=document.getElementById("type").value,a=document.getElementById("att").value;'
        'return (!d||m.dir===d)&&(!t||m.type.indexOf("|"+t+"|")>-1)&&(!a||m.att===a);},'
        'rowClass:function(m){return m.dir==="Sent"?"out":"";},'
        'count:function(n,t){document.getElementById("count").textContent='
        'n===t?(n+" messages"):(n+" of "+t+" shown");},'
        'reset:function(){document.getElementById("q").value="";'
        'document.getElementById("dir").value="";document.getElementById("type").value="";'
        'document.getElementById("att").value="";}});'
        'scSyncBoxes();scSelNote();SCSel.onChange(function(){scSyncBoxes();scSelNote();});'
        'scConsumeHash();'
        '</script></body></html>')

    with open(os.path.join(pages_dir, f"{key}.html"), "w", encoding="utf-8") as fh:
        fh.write(doc)
    return f"pages/{key}.html"


def _first_last(conv, which):
    """The conversation's first/last message time, already formatted by the message builder.

    ``build_messages`` sorted each conversation chronologically with the timestamp-less rows last,
    so the dated messages are already in order here.
    """
    dated = [m for m in conv["messages"] if m["created_unix"]]
    if not dated:
        return ""
    return dated[0]["created"] if which == "first" else dated[-1]["created"]


# --------------------------------------------------------------------------- index page

def generate_index(conversations, outdir, tz_label, run_id, stats):
    """Write ``Conversations_report.html`` + ``data/index.js``; return the report path."""
    rows = []
    for conv in conversations:
        parts = conv["participants"]
        # the row is one fixed height: name the first two participants and count the rest (the
        # detail page lists them all, linked to their contact records)
        shown = ", ".join(_participant_html(p, "../", chip=False) for p in parts[:2])
        if len(parts) > 2:
            shown += f' <span class="more">+{len(parts) - 2}</span>'
        cells = [
            _kind_badge(conv["kind"]),
            text_html(conv["title"]),
            shown,
            str(conv["n_messages"]),
            str(conv["n_attachments"]) if conv["n_attachments"] else "",
            _esc(_first_last(conv, "first")),
            _esc(_first_last(conv, "last")),
            f'<span class="cid">{_esc(conv["id"])}</span>',
            f'<a class="detail" href="{_esc(conv["page"])}#conv-{_esc(conv["id"])}">open &#9656;</a>',
        ]
        labels = [p["label"] for p in parts]
        searchable = [conv["id"], conv["server_id"], conv["title"], conv["kind"]] + labels
        searchable += [p["user_id"] for p in parts] + [p["raw"] for p in parts]
        searchable += list(conv["senders"])
        rows.append([
            f'conv-{conv["id"]}', cells,
            " ".join(s for s in searchable if s).lower(),
            {"0": conv["kind"], "1": conv["title"].lower(),
             "2": " ".join(labels).lower(), "3": conv["n_messages"],
             "4": conv["n_attachments"], "5": conv["first_sort"], "6": conv["last_sort"],
             "7": conv["id"]},
            None,
            {"kind": conv["kind"], "msg": "y" if conv["n_messages"] else "n",
             "att": "y" if conv["n_attachments"] else "n"},
        ])
    report_ui.write_rows(os.path.join(outdir, "data"), rows)

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

    doc = (
        _head("Snapchat conversations", "../", run_id, "conv", "")
        + '<body>'
        f'<header><h1>Snapchat conversations</h1>'
        f'<div class="sum"><b>{len(conversations)}</b> conversation(s) &middot; '
        f'<b>{stats["messages"]}</b> message(s) &middot; '
        f'<b>{stats["attachments"]}</b> with an attachment'
        + (f' ({stats["files"]} files)' if stats["files"] != stats["attachments"] else "")
        + ' &middot; '
        f'{stats["groups"]} group / {stats["private"]} private &middot; '
        f'times in <b>{_esc(tz_label)}</b>{skipped_note}</div>'
        f'<div class="sum">Source: arroyo.db conversation_message, joined to cache_controller.db '
        f'and the friends / groups artifacts by the iOS parser</div></header>'
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
        '<span id="count" style="color:#555"></span></div>'
        f'<div class="toolbar">{report_ui.selection_toolbar("conversation")}</div>'
        '<div class="pager" id="pager"></div>'
        f'<div class="vhdr" id="vhdr" style="grid-template-columns:30px {CONV_COLS}">'
        '<div class="vc sel"><input type="checkbox" class="selall"'
        ' title="Select / unselect every conversation matching the current filters"'
        ' onclick="SCV.selectShown(this.checked)"></div>'
        '<div class="vc" onclick="SCV.setSort(0)">Type <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(1)">Conversation <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(2)">Participants <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(3)">Msgs <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(4)">Att. <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(5)">First message <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(6)">Last message <span class="ar">&#8597;</span></div>'
        '<div class="vc" onclick="SCV.setSort(7)">Conversation ID <span class="ar">&#8597;</span></div>'
        '<div class="vc nosort">Detail</div>'
        '</div></div>'
        '<div class="vwrap convs" id="vwrap"><div class="vpad" id="vpad"></div>'
        '<div class="vwin" id="vwin"></div></div>'
        '<div class="vempty" id="vempty" style="display:none">'
        'No conversation matches the current filters.</div>'
        '<script src="data/index.js"></script>'
        '<script>'
        'SCV.init({mount:"vwrap",win:"vwin",pad:"vpad",header:"#vhdr",missing:"vmiss",'
        'empty:"vempty",pager:"pager",pageSize:500,selKind:"conv",sort:3,sortDir:-1,'
        f'rowHeight:{CONV_ROW_H},cols:"{CONV_COLS}",detailBase:null,'
        'query:function(){return document.getElementById("q").value;},'
        'match:function(m,r){var k=document.getElementById("kind").value,'
        'g=document.getElementById("msg").value,a=document.getElementById("att").value;'
        'return (!k||m.kind===k)&&(!g||m.msg===g)&&(!a||m.att===a)'
        '&&(!document.getElementById("selonly").checked||SCSel.get("conv",r[0]));},'
        'selectedOnly:function(){return document.getElementById("selonly").checked;},'
        'selCount:function(n){document.getElementById("selcount").textContent=n+" selected";'
        'scSelNote();},'
        'count:function(n,t){document.getElementById("count").textContent='
        'n===t?(n+" conversations"):(n+" of "+t+" shown");},'
        'reset:function(){document.getElementById("q").value="";'
        'document.getElementById("kind").value="";document.getElementById("msg").value="";'
        'document.getElementById("att").value="";'
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
    """The per-conversation summary the Contacts report needs to link to conversations."""
    return {c["id"]: {"page": c["page"], "title": c["title"], "kind": c["kind"],
                      "messages": c["n_messages"], "attachments": c["n_attachments"],
                      "first": _first_last(c, "first"), "last": _first_last(c, "last"),
                      "first_sort": c["first_sort"], "last_sort": c["last_sort"]}
            for c in conversations}


# --------------------------------------------------------------------------- entry

def main(msg_df, friends_df, group_df, outdir, cachefiles_dir, arroyo=None, tz="local",
         owner_user_id="", owner_username="", cache_key_for=None, report_dir=None, primary=None):
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
    Returns ``(report_path, conversation_index)``; the index feeds the Contacts report's links.
    """
    os.makedirs(outdir, exist_ok=True)
    rdir = report_dir or os.path.dirname(os.path.abspath(outdir))
    run_id = report_ui.run_id(rdir)
    report_ui.write_selection_stub(rdir, run_id)
    timefmt, tz_label = make_time_formatter(tz)

    # the same contact model the Contacts report builds, so a participant here and a row there are
    # the same person with the same identifiers
    contacts = apply_identifiers(normalize_contacts(friends_df, owner_user_id, owner_username),
                                 load_identifiers(primary))
    contact_links = contact_link_index(contacts)               # hrefs relative to the reports root
    groups = normalize_groups(group_df)
    owner_names = [c["username"] or c["display"] for c in contacts if c["is_owner"]]
    if owner_username:
        owner_names.append(re.sub(r"</?b>", "", str(owner_username)).strip())

    by_conv, drop_stats = build_messages(msg_df, cachefiles_dir, os.path.join(outdir, "media"),
                                         timefmt, cache_key_for, owner_user_id, owner_names)
    conversations = build_conversations(by_conv, contacts, groups,
                                        load_arroyo_conversations(arroyo, msg_df), contact_links)

    write_assets(outdir)
    for conv in conversations:
        conv["page"] = render_conversation_page(conv, outdir, tz_label, run_id,
                                                contact_links=contact_links)

    stats = {"messages": sum(c["n_messages"] for c in conversations),
             "files": sum(c["n_files"] for c in conversations),
             "attachments": sum(c["n_attachments"] for c in conversations),
             "groups": sum(1 for c in conversations if c["kind"] == "Group"),
             "private": sum(1 for c in conversations if c["kind"] == "Private"),
             **drop_stats}
    report = generate_index(conversations, outdir, tz_label, run_id, stats)
    write_page_manifest(conversations, outdir)
    write_cache_links(conversations, outdir)

    logger.info(f"Conversations report: {os.path.abspath(report)}")
    logger.info(f"  {len(conversations)} conversation(s), {stats['messages']} message(s), "
                f"{stats['attachments']} with an attachment ({stats['files']} file(s))")
    return report, conversation_index(conversations)
