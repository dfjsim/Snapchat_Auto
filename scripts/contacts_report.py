"""
Snapchat contacts report — one row per contact, with a link to that contact's conversation.

``Reports/Contacts/Contacts_report.html``: a single virtualized table (search / sort / paging /
selection, see :mod:`scripts.report_ui`) listing every contact the parser could recover, the
identifiers that tie them to a conversation, and how many messages that conversation holds.

Where contacts come from
------------------------
Snapchat stores the friends list in different places depending on the app version, and
``ParseSnapchat_iOS`` tries them in order (``getFriendsPlist`` →
``getFriendsAppGroupPlistStorage`` → ``getFriendsPrimary_DisplayMetadata`` →
``getFriendsPrimary``). Whichever one answered is passed in as ``friends_source`` and named in the
report, because it changes what the table means: the two plist sources are the **friends list**,
while the ``primary.docobjects`` fallbacks are "every Snapchatter this device knows about", which
includes people who are not friends. :data:`SOURCE_NOTES` holds that caveat per source and the
report shows it — an examiner must not read "contact" as "friend" when it isn't.

This module also owns the two normalizers (:func:`normalize_contacts`, :func:`normalize_groups`)
that turn those source-dependent DataFrames into one stable shape; the Conversations report imports
them to title its conversations.
"""

import os
import re
import json
import html
import sqlite3
import logging

from scripts import report_ui
from scripts import app_version
from scripts import partial_report
from scripts.data import sqlite_open

logger = logging.getLogger(__name__)

# The friends DataFrame's columns depend on which source answered, so every lookup goes through
# _pick_col rather than assuming a spelling ('Display name' vs 'Display Name', ...).
_DISPLAY_COLS = ("Display name", "Display Name", "display_name")
_USERNAME_COLS = ("Username", "username")
_USERID_COLS = ("User ID", "userId", "user_id")
_CONVID_COLS = ("Conversation ID", "conversation_id")

# Values the parser writes when a field could not be recovered; they are not real values.
_PLACEHOLDERS = {"", "unknown", "nan", "none", "$null"}

SOURCE_NOTES = {
    "group.snapchat.picaboo.plist": (
        "Read from the app-group plist group.snapchat.picaboo.plist, key 'share_user' (an "
        "NSKeyedArchiver blob), SECTIONS -> DESTINATIONS. These entries are the account's own "
        "friends list, so a row here is a friend (or a group) the account had."),
    "app_group_plist_storage": (
        "Read from app_group_plist_storage, key 'snapchatter_repository' (an NSKeyedArchiver blob), "
        "FRIENDS / GROUPS. These entries are the account's own friends list."),
    "primary.docobjects (DisplayMetadata)": (
        "Fallback source: primary.docobjects, table snapchatters__displaymetadata (display names "
        "carved out of the 'p' blob) joined to arroyo.db user_conversation for the conversation id. "
        "WARNING: this table is not the friends list -- it MIGHT contain users who are not friends "
        "(anyone the app has display metadata for)."),
    "primary.docobjects (Snapchatters)": (
        "Last-resort source: primary.docobjects, table 'snapchatter' joined to "
        "'index_snapchatterusername' for usernames and to arroyo.db user_conversation for the "
        "conversation id. WARNING: this is every Snapchatter the device knows about -- it WILL "
        "contain users who are not friends."),
    # Android: one table, read whole. See ParseSnapchat_Android.read_contacts.
    "main.db Friend": (
        "Read from the Android app's databases/main.db, table 'Friend' (userId, username, "
        "displayName), joined to 'CombinedUsername' on combinedUsernameRowId for the username pair. "
        "Every row of the table is listed. Expand a row for what the table itself records about the "
        "link: the two 'added' timestamps (with the column comments the table's own schema carries), "
        "the stored friendLinkType, and which other friend table of main.db lists the user "
        "(FriendWhoAddedMe, SuggestedFriend, BestFriend). WARNING: this table is the app's record of "
        "every user it has to show, not the friends list -- it MIGHT contain users who are not "
        "friends (people who added the account, people it added, group members, suggestions)."),
}

#: Where each of the three username fields was read, per source; iOS's tables unless named here.
_USERNAME_TABLES = {
    "main.db Friend": ("main.db Friend.username / CombinedUsername.mutableUsername",
                       "main.db CombinedUsername.mutableUsername",
                       "main.db CombinedUsername.originalUsername"),
}
_IOS_USERNAME_TABLES = ("index_snapchatterusername", "index_snapchattermutableUsername",
                        "index_snapchatterlegacyUsername")

#: The header's "username history from …" line, per source (iOS's primary.docobjects otherwise).
ANDROID_IDENTIFIERS_NOTE = (
    "Read from the Android app's databases/main.db: table 'CombinedUsername' (originalUsername, "
    "mutableUsername), which each Friend row points at through combinedUsernameRowId. When the two "
    "differ, the mutable username is shown as the username and the original one as the legacy "
    "username — the one the account had before it was changed. Both are shown as stored.")
_IDENTIFIER_SOURCES = {
    "main.db Friend": ("main.db CombinedUsername", ANDROID_IDENTIFIERS_NOTE),
}

# Index-table geometry: one fixed row height and one column track list for the header and the rows.
CT_COLS = ("24px minmax(140px,1fr) minmax(130px,1fr) minmax(120px,0.9fr) 250px minmax(180px,1.1fr) "
           "74px 148px 148px")
CT_ROW_H = 46

# What each identifier is worth, shown on its column. See IDENTIFIER_NOTE for the whole picture.
DISPLAY_NAME_NOTE = (
    "The name this device's user gave the contact (or the name Snapchat displayed for them), from "
    "the friends artifact named at the top of this report. It is local to this device and can be "
    "changed at any time, so it is the weakest identifier: two devices can call the same account "
    "different things.")
USERNAME_NOTE = (
    "The @username the contact chose. Snapchat lets an account change it, so a username identifies "
    "the account only at a point in time — check the Legacy username column.")
LEGACY_NOTE = (
    "The username this contact used before changing it, when the device still has a record of the "
    "change. Blank means either no change was recorded or the previous name is the same as the "
    "current one. This is why an older report or chat log can name the same account differently.")
USER_ID_NOTE = (
    "The account's permanent identifier (a UUID). It does not change when the username or display "
    "name does, so it is the only identifier safe to correlate accounts on, across reports, "
    "devices and time.")


# --------------------------------------------------------------------------- small helpers

def _pick_col(df, names):
    """The first of ``names`` that exists in ``df``, or None."""
    if df is None:
        return None
    try:
        cols = list(df.columns)
    except Exception:
        return None
    return next((n for n in names if n in cols), None)


def cell(value):
    """A DataFrame cell as a clean string: NaN / None / the parser's placeholders become ""."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in _PLACEHOLDERS:
        return ""
    return text


def _unbold(value):
    """Strip the ``<b>...</b>`` the parser wraps the logged-in user's name in.

    ``getFriendsPlist`` / ``getFriendsAppGroupPlistStorage`` mark the device owner by bolding the
    username (that is how the legacy HTML report highlights it). Returns ``(text, was_bold)`` so the
    marking survives as data instead of as markup.
    """
    text = cell(value)
    match = re.fullmatch(r"\s*<b>(.*)</b>\s*", text, re.DOTALL)
    return (match.group(1).strip(), True) if match else (text, False)


def _as_list(value):
    """A participants cell as a list of names — it may be a real list, or a stringified one."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if cell(v)]
    try:                                                       # numpy array / pandas Series
        if hasattr(value, "tolist"):
            return _as_list(value.tolist())
    except Exception:
        pass
    text = cell(value)
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):            # "['a', 'b']"
        try:
            return _as_list(json.loads(text.replace("'", '"')))
        except Exception:
            text = text[1:-1]
    return [p.strip().strip("'\"") for p in text.split(",") if p.strip().strip("'\"")]


def _first_scalar(value):
    """First element of a cell that may hold a one-item list.

    ``getFriendsPrimary`` builds its groups frame with ``conv_id = []; conv_id.append(id)``, so the
    Conversation ID column holds *lists* there and plain strings elsewhere.
    """
    if isinstance(value, (list, tuple)):
        return cell(value[0]) if value else ""
    return cell(value)


_ENTITY_RE = re.compile(r"&(#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")


def text_html(value):
    """HTML-escape parsed Snapchat text **without** breaking the entities the parser produced.

    Display names and message bodies are re-encoded by the parser with
    ``encode('cp1252', 'xmlcharrefreplace')``, so an emoji arrives here as the literal text
    ``&#128512;``. Plain :func:`html.escape` would turn that into ``&amp;#128512;`` and the examiner
    would read the entity instead of the emoji — so ``&`` is escaped everywhere *except* where it
    already starts a character reference, while ``<`` and ``>`` are always escaped (report content
    must never become markup).
    """
    text = "" if value is None else str(value)
    out, pos = [], 0
    for match in _ENTITY_RE.finditer(text):
        out.append(html.escape(text[pos:match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(html.escape(text[pos:]))
    # the parser stores hard line breaks as the two characters \n (see getHtml in ParseSnapchat_iOS)
    return "".join(out).replace("\\n", "<br>").replace("\n", "<br>")


# --------------------------------------------------------------------------- normalizers

def normalize_contacts(friends_df, owner_user_id="", owner_username=""):
    """Turn the source-dependent friends DataFrame into a list of contact dicts.

    Returns ``[{display, username, user_id, conv_id, is_owner}]``, deduplicated on
    (user_id, username, conv_id). ``is_owner`` marks the account the extraction came from — either
    because the parser bolded its name, or because the id/name matches the logged-in user.
    """
    contacts, seen = [], set()
    if friends_df is None or len(friends_df) == 0:
        return contacts
    col_display = _pick_col(friends_df, _DISPLAY_COLS)
    col_username = _pick_col(friends_df, _USERNAME_COLS)
    col_userid = _pick_col(friends_df, _USERID_COLS)
    col_convid = _pick_col(friends_df, _CONVID_COLS)
    # the source's own per-row fields, as (label, value, note) — only the Android parser supplies them
    col_extra = "_extra" if "_extra" in friends_df.columns else None
    owner_id = cell(owner_user_id).lower()
    owner_name = _unbold(owner_username)[0].lower()
    for _index, row in friends_df.iterrows():
        display, display_bold = _unbold(row.get(col_display) if col_display else "")
        username, username_bold = _unbold(row.get(col_username) if col_username else "")
        user_id = cell(row.get(col_userid)) if col_userid else ""
        conv_id = _first_scalar(row.get(col_convid)) if col_convid else ""
        if not (display or username or user_id):
            continue
        is_owner = bool(display_bold or username_bold
                        or (owner_id and user_id.lower() == owner_id)
                        or (owner_name and username.lower() == owner_name))
        key = (user_id.lower(), username.lower(), conv_id.lower())
        if key in seen:
            continue
        seen.add(key)
        contact = {"display": display, "username": username, "user_id": user_id,
                   "legacy_username": "", "conv_id": conv_id, "is_owner": is_owner}
        if col_extra:
            extra = row.get(col_extra)
            contact["extra"] = list(extra) if isinstance(extra, (list, tuple)) else []
        contacts.append(contact)
    return contacts


def contact_anchor(contact):
    """The stable row anchor other reports link a contact by — its permanent user id when known."""
    for value in (contact.get("user_id"), contact.get("username"), contact.get("conv_id")):
        if value:
            return "ct-" + re.sub(r"[^0-9A-Za-z_.:-]", "_", str(value))
    return "ct-unknown"


def contact_link_index(contacts):
    """``{key: {href, display, username, user_id, is_owner}}`` for every way another report may name
    a contact (user id, username, display name — all lower-cased), so it can link to the row that
    holds all of that contact's identifiers.

    ``href`` is relative to the **reports root**; the caller prefixes it with its own depth.
    ``anchor`` is the same row's id on its own, so a caller that has to decide whether that row is in
    a partial report does not re-derive it through :func:`contact_anchor` and risk disagreeing.
    """
    index = {}
    for contact in contacts:
        entry = {"href": f"Contacts/Contacts_report.html#{contact_anchor(contact)}",
                 "anchor": contact_anchor(contact),
                 "display": contact["display"], "username": contact["username"],
                 "user_id": contact["user_id"], "is_owner": contact["is_owner"]}
        for key in (contact["user_id"], contact["username"], contact["display"]):
            if key:
                index.setdefault(key.lower(), entry)
    return index


# --------------------------------------------------------------------------- identifiers

# A Snapchat contact has three or four identifiers, and they are not equally stable:
#
#   display name     set by *this* device's user, purely local, changeable at will
#   username         chosen by the contact, changeable (rarely) by them
#   legacy username  the username they had before such a change — the reason this matters
#   user ID          a UUID, permanent, the only identifier that never changes
#
# primary.docobjects keeps the username pair in two side tables that share `snapchatter`'s rowid.
IDENTIFIER_NOTE = (
    "A Snapchat contact has up to four identifiers. The USER ID (a UUID) is permanent and is the "
    "only one safe to correlate on. The USERNAME is chosen by the contact and can be changed; when "
    "it has been, the previous one is kept as the LEGACY USERNAME, so an older report, chat log or "
    "witness statement may name the same person differently. The DISPLAY NAME is set locally by "
    "this device's user and means nothing outside this device.")

PRIMARY_SOURCE_NOTE = (
    "Read from primary.docobjects: table 'snapchatter' (userId, and the 'p' blob that also carries "
    "the names) joined on rowid to 'index_snapchatterusername' (current username), "
    "'index_snapchattermutableUsername' and 'index_snapchatterlegacyUsername' (the username used "
    "before it was changed). The four tables share one rowid per Snapchatter.")

MUTABLE_NOTE = (
    "As stored in primary.docobjects table 'index_snapchattermutableUsername'. What distinguishes "
    "it from 'username' is not established: in every tested extraction the two were equal on every "
    "row, while the LEGACY username is the one that records a rename. It is shown because the app "
    "stores it, and flagged on the row when it differs from the username.")

SNAPCHATTERS_NOTE = (
    "primary.docobjects table 'snapchatter' holds every Snapchatter record the app has cached — not "
    "only friends. On the tested extractions the friends list accounted for a handful of rows and "
    "every other row was named in a 'snapchatters__displaysuggestion' page, i.e. a Quick Add / "
    "'people you may know' suggestion the app had shown. Another tool that lists the 'snapchatter' "
    "table as the friends list reports all of these as friends; they are NOT in the account's own "
    "friends list, which is what the table above was read from. 'Why cached' names the docobjects "
    "table whose document mentions the user id — 'Quick Add suggestion' is the one meaning that was "
    "verified; any other table is named as stored, with no interpretation. Rows here are not "
    "contacts: they carry no row selection, no anchors, and are left out of a partial report. "
    "Method after iLEAPP (Alexis Brignoni), scripts/artifacts/snapchat.py, whose 'Snapchat - "
    "Friends' artifact is this table.")

WHY_QUICK_ADD = "Quick Add suggestion"
WHY_UNNAMED = "cached by the app; no docobjects table names it"


def _text_column(conn, table, prefer="username"):
    """The column of ``table`` holding its text value, or None if the table is absent.

    The index tables' column names vary between app versions, so the column is looked up rather
    than assumed: one whose name mentions the wanted word, else the first non-rowid column.
    """
    try:
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.DatabaseError:
        return None
    if not cols:
        return None
    for col in cols:
        if prefer in col.lower():
            return col
    for col in cols:
        if col.lower() not in ("rowid", "docid", "id"):
            return col
    return None


def load_identifiers(primary):
    """``primary.docobjects`` → ``{user_id: {"username", "legacy_username"}}``.

    Best effort by design: the tables are absent on some app versions, and a missing one only means
    that identifier is not shown. Never raises — the report is still worth producing without it.
    """
    out = {}
    if not (primary and os.path.isfile(str(primary))):
        return out
    try:
        # both readings of primary.docobjects: a username row the write-ahead log has since
        # replaced is a *previous* username for that account, which is exactly what this table
        # exists to show
        views = sqlite_open.open_views(primary)
        conn = views.merged
    except sqlite3.DatabaseError as error:
        logger.debug(f"Could not open {primary}: {error}")
        return out
    try:
        user_col = _text_column(conn, "index_snapchatterusername")
        mutable_col = _text_column(conn, "index_snapchattermutableUsername", prefer="username")
        legacy_col = _text_column(conn, "index_snapchatterlegacyUsername", prefer="username")
        if not (user_col or legacy_col or mutable_col):
            logger.info("Contacts: primary.docobjects has no username index tables — the username "
                        "history cannot be shown")
            return out
        select = ["s.userId as user_id"]
        joins = ""
        if user_col:
            select.append(f"u.{user_col} as username")
            joins += " left join index_snapchatterusername u on u.rowid = s.rowid"
        if mutable_col:
            select.append(f"m.{mutable_col} as mutable_username")
            joins += " left join index_snapchattermutableUsername m on m.rowid = s.rowid"
        if legacy_col:
            select.append(f"l.{legacy_col} as legacy_username")
            joins += " left join index_snapchatterlegacyUsername l on l.rowid = s.rowid"
        query = f"select {', '.join(select)} from snapchatter s{joins}"
        names = [d[0] for d in conn.execute(f"{query} limit 0").description]
        rows, marks = sqlite_open.query_both(views, query)
        for row, mark in zip(rows, marks):
            record = dict(zip(names, row))
            user_id = cell(record.get("user_id"))
            if not user_id:
                continue
            entry = out.setdefault(user_id.lower(), {"username": "", "mutable_username": "",
                                                     "legacy_username": "", "superseded": []})
            for key in ("username", "mutable_username", "legacy_username"):
                value = cell(record.get(key))
                if value and not entry[key]:
                    entry[key] = value
                elif value and mark == sqlite_open.MAIN_ONLY and value != entry[key]:
                    # a name this account had before the last checkpoint, replaced since
                    entry.setdefault("superseded", []).append(value)
    except sqlite3.DatabaseError as error:
        logger.info(f"Contacts: could not read the username tables from primary.docobjects "
                    f"({error}) — usernames will be whatever the friends list held")
    finally:
        views.close()
    if out:
        legacy = sum(1 for v in out.values()
                     if v["legacy_username"] and v["legacy_username"] != v["username"])
        logger.info(f"Contacts: {len(out)} identifier record(s) from primary.docobjects, "
                    f"{legacy} with a different legacy username")
    return out


def apply_identifiers(contacts, identifiers):
    """Add ``legacy_username`` and ``mutable_username`` to each contact and fill a missing
    username from the same source.

    A legacy username equal to the current one is not a rename and is not shown as one. The
    mutable username is carried as stored (see :data:`MUTABLE_NOTE`); ``mutable_differs`` says
    whether it disagrees with the username, which is what the row flags.
    """
    for contact in contacts:
        record = identifiers.get(contact["user_id"].lower()) if contact["user_id"] else None
        legacy = (record or {}).get("legacy_username", "")
        username = (record or {}).get("username", "")
        mutable = (record or {}).get("mutable_username", "")
        if username and not contact["username"]:
            contact["username"] = username
        current = contact["username"] or username
        contact["legacy_username"] = legacy if legacy and legacy != current else ""
        contact["mutable_username"] = mutable
        contact["mutable_differs"] = bool(mutable and current and mutable != current)
    return contacts


_UUID_TEXT_RE = re.compile(rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                           rb"[0-9a-fA-F]{12}")
# The snapchatter table itself is not "somewhere the user id is mentioned", and the index tables
# carry no documents. Every other table in the store is searched — including
# snapchatters__displaymetadata, whose membership (friends plus the official accounts the app
# converses with, on the tested devices) is worth naming.
_SNAPCHATTER_OWN_TABLES = ("snapchatter", "sqlite_sequence")


def load_snapchatters(primary, contact_ids=(), owner_user_id=""):
    """Every ``snapchatter`` row of ``primary.docobjects`` that is **not** a contact, with why the
    app cached it — see :data:`SNAPCHATTERS_NOTE`.

    Returns ``[{user_id, username, mutable_username, legacy_username, display_name, why, wal}]``
    sorted by username. ``why`` is :data:`WHY_QUICK_ADD`, ``"named in <table>"`` for another
    docobjects table whose document embeds the user id as text, or :data:`WHY_UNNAMED`. Both
    readings of the store are used, and ``wal`` is the row's marker. Never raises.
    """
    out = []
    if not (primary and os.path.isfile(str(primary))):
        return out
    excluded = {str(c).lower() for c in contact_ids if c}
    if owner_user_id:
        excluded.add(str(owner_user_id).lower())
    try:
        views = sqlite_open.open_views(primary)
    except sqlite3.DatabaseError as error:
        logger.debug(f"Could not open {primary}: {error}")
        return out
    total = 0
    try:
        from scripts.data import flatbuffers_doc
        conn = views.merged
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type = 'table'")}
        if "snapchatter" not in tables:
            return out
        # which other tables' documents mention each user id: the documents are FlatBuffers and
        # embed the UUID as text, so a plain scan of every blob finds every mention
        mentions = {}
        for table in sorted(tables):
            if table in _SNAPCHATTER_OWN_TABLES or table.startswith("index_"):
                continue
            blob_rows, _marks = sqlite_open.query_both(views, f'select p from "{table}"')
            for (blob,) in blob_rows:
                if isinstance(blob, (bytes, bytearray, memoryview)):
                    for match in _UUID_TEXT_RE.findall(bytes(blob)):
                        mentions.setdefault(match.decode("ascii").lower(), set()).add(table)
        select = ["s.userId as user_id", "s.p as p"]
        joins = ""
        for table, alias, key in (("index_snapchatterusername", "u", "username"),
                                  ("index_snapchattermutableUsername", "m", "mutable_username"),
                                  ("index_snapchatterlegacyUsername", "l", "legacy_username")):
            col = _text_column(conn, table, prefer="username")
            if col:
                select.append(f"{alias}.{col} as {key}")
                joins += f" left join {table} {alias} on {alias}.rowid = s.rowid"
        query = f"select {', '.join(select)} from snapchatter s{joins}"
        names = [d[0] for d in conn.execute(f"{query} limit 0").description]
        rows, marks = sqlite_open.query_both(views, query)
        seen = set()
        for row, mark in zip(rows, marks):
            record = dict(zip(names, row))
            user_id = cell(record.get("user_id"))
            if not user_id or user_id.lower() in seen:
                continue
            seen.add(user_id.lower())
            total += 1
            if user_id.lower() in excluded:
                continue
            doc = flatbuffers_doc.snapchatter_names(record.get("p"), user_id)
            named_in = sorted(mentions.get(user_id.lower(), ()))
            if "snapchatters__displaysuggestion" in named_in:
                why = WHY_QUICK_ADD
            elif named_in:
                why = "named in " + ", ".join(named_in)
            else:
                why = WHY_UNNAMED
            out.append({"user_id": user_id,
                        "username": cell(record.get("username")) or doc.get("username", ""),
                        "mutable_username": cell(record.get("mutable_username")),
                        "legacy_username": cell(record.get("legacy_username")),
                        "display_name": doc.get("display_name", ""),
                        "why": why, "wal": mark})
    except sqlite3.DatabaseError as error:
        logger.info(f"Contacts: could not read the snapchatter table from primary.docobjects "
                    f"({error})")
    finally:
        views.close()
    out.sort(key=lambda r: (r["username"].lower(), r["user_id"].lower()))
    if total:
        quick = sum(1 for r in out if r["why"] == WHY_QUICK_ADD)
        logger.info(f"Contacts: {total} Snapchatter record(s) in primary.docobjects — "
                    f"{total - len(out)} in the contacts list, {quick} Quick Add suggestion(s), "
                    f"{len(out) - quick} other")
    return out


def normalize_groups(group_df):
    """Turn the groups DataFrame into ``[{conv_id, name, participants}]`` (see :func:`_as_list`)."""
    groups, seen = [], set()
    if group_df is None or len(group_df) == 0:
        return groups
    col_name = _pick_col(group_df, ("Group Name", "group_name"))
    col_convid = _pick_col(group_df, _CONVID_COLS)
    col_parts = _pick_col(group_df, ("Participants", "participants"))
    for _index, row in group_df.iterrows():
        conv_id = _first_scalar(row.get(col_convid)) if col_convid else ""
        name = cell(row.get(col_name)) if col_name else ""
        parts = _as_list(row.get(col_parts)) if col_parts else []
        if not conv_id:
            continue
        if conv_id.lower() in seen:
            continue
        seen.add(conv_id.lower())
        groups.append({"conv_id": conv_id, "name": name, "participants": parts})
    return groups


# --------------------------------------------------------------------------- HTML

def _esc(value):
    return html.escape(str(value)) if value not in (None, "") else ""


def _source_block(friends_source):
    """The provenance banner: which artifact the contacts were read from, and its caveat."""
    if not friends_source:
        return ('<div class="note">The source of the contact list was not recorded for this run.'
                '</div>')
    note = SOURCE_NOTES.get(friends_source, "")
    warn = "WARNING" in note
    css = "warn" if warn else "note"
    return (f'<div class="{css}"><b>Source:</b> {_esc(friends_source)}'
            f'{report_ui.info_icon(note) if note else ""}'
            + (f' &mdash; {_esc(note.split("WARNING: ")[-1])}' if warn else "") + '</div>')


MULTI_CONV_NOTE = (
    "The friends artifact records ONE conversation id against a contact — their private "
    "conversation with this device. It is not the only conversation they take part in: every group "
    "chat they are a member of is another one. This column therefore lists every conversation whose "
    "participant list (arroyo.db user_conversation, or the groups list) carries this contact's "
    "USER ID, with the friends artifact's own id marked 'from the friends list'. Expand the row to "
    "see them all with their conversation ids. Matching is on the user id alone — never on a "
    "username or display name, which can be changed, reused or shared between accounts, so a name "
    "match would attribute a conversation to a person on evidence that does not identify them. A "
    "contact whose user id was not recovered therefore shows only the friends list's conversation.")


def contact_conversations(contact, conv_index):
    """Every conversation this contact takes part in — not just the one the friends list names.

    Returns ``[{id, why, …the conversation summary}]``, the friends artifact's own conversation
    first and the rest by message count. ``why`` records which of the two made the association, so
    the report can say it rather than presenting both as the same kind of fact.

    Membership is matched on the **permanent user id only**. A display name is set locally by this
    device's user and two accounts can share one; a username can be changed and the old one reused
    by somebody else. Matching on either would put a conversation on a person's row on the strength
    of a name — a false attribution, and the worst kind, because it looks exactly like a true one.
    A contact whose user id was never recovered is therefore listed only with the conversation the
    friends artifact names, which is the honest answer rather than a guessed one.
    """
    key = str(contact.get("user_id") or "").lower()
    out, seen = [], set()

    def add(conv_id, why):
        conv = conv_index.get(conv_id)
        if not conv_id or conv_id in seen or conv is None:
            return
        seen.add(conv_id)
        out.append(dict(conv, id=conv_id, why=why))

    add(contact.get("conv_id"), "friends")
    if key:
        for conv_id, conv in conv_index.items():
            for part in conv.get("participants") or []:
                if str(part.get("user_id") or "").lower() == key:
                    add(conv_id, "participant")
                    break
    first = out[:1] if out and out[0]["why"] == "friends" else []
    rest = sorted(out[len(first):], key=lambda c: -(c.get("messages") or 0))
    return first + rest


_ACTIVITY_HINT = (
    "The earliest and latest activity across EVERY conversation this contact takes part in, not "
    "only the one the friends list names — so for someone in several group chats these are not one "
    "conversation's dates. The Msgs count is the same sum, which is why two contacts in the same "
    "group chat can show the same figure.\n\n"
    "Normally these are message times (arroyo.db conversation_message.creation_timestamp). A value "
    "marked «feed» comes from a conversation that holds no message, where the only record of "
    "activity is the conversation's own row in the app's chat feed — that is not a message time. "
    "Expand the row to see which conversation each date came from.")

_WHY_LABEL = {
    "friends": ("from the friends list",
                "This is the CONVERSATION_ID the friends artifact records against this contact — "
                "the app's own association between the person and a conversation."),
    "participant": ("participant list carries this user ID",
                    "This conversation's participant list (arroyo.db user_conversation, or the "
                    "groups list in the friends artifact) carries this contact's permanent user id. "
                    "That is what makes a contact a member of a group chat as well as of their "
                    "private conversation. The match is on the user id alone, so it does not depend "
                    "on a display name or a username, either of which can change."),
}


ACCOUNT_NOTE = (
    "Read by key from Documents/user.plist, a Snap TSAF container (not a plist): 'username', "
    "'user_id' and 'laguna_id' under the User object, and 'identifier', 'encryption_key' and "
    "'initialization_vector' under its client_encryption object. All are shown as stored. What the "
    "laguna id names is not established.\n\n"
    "The client-encryption values are a SECOND such record: the key that opens this device's "
    "encrypted caches is the one in Documents/ClientEncryptionService.plist, which carries a "
    "different identifier and a different key. Nothing in any tested extraction is encrypted with "
    "the key shown here — every block-aligned file on four devices was tested against it, by "
    "decrypting each file's last blocks and checking the padding, which identifies a CBC key "
    "whatever the IV or framing. So treat these as an identifier and key material belonging to the "
    "account record, not as a key to try against the caches. See "
    "docs/snapchat_ios_cache_media.md.\n\n"
    "A signed-out account leaves username / user_id / laguna_id empty in this file. Keyed read "
    "after iLEAPP (Alexis Brignoni), scripts/artifacts/snapchat.py, 'Snapchat - Account'.")

_ACCOUNT_LABELS = (("username", "Username (user.plist)"), ("user_id", "User ID (user.plist)"),
                   ("laguna_id", "Laguna ID"), ("identifier", "Client-encryption identifier"),
                   ("encryption_key", "Client-encryption key (base64, as stored)"),
                   ("initialization_vector", "Client-encryption IV (base64, as stored)"))


def _username_rows(contact):
    """The three stored username fields as ``(label, html)`` grid rows, each naming its table."""
    def src(table):
        return f' <span class="muted">{_esc(table)}</span>'
    user_table, mutable_table, legacy_table = contact.get("username_tables") or _IOS_USERNAME_TABLES
    mutable = contact.get("mutable_username") or ""
    legacy = contact.get("legacy_username") or ""
    # the note on the mutable username is about primary.docobjects; another source is named as stored
    mutable_note = report_ui.info_icon(MUTABLE_NOTE) if not contact.get("username_tables") else ""
    return [("Username", text_html(contact["username"]) + src(user_table)
             if contact["username"] else ""),
            ("Mutable username",
             (text_html(mutable) + (' <span class="legacy">differs from the username</span>'
                                    if contact.get("mutable_differs") else "")
              if mutable else '<span class="muted">not stored</span>')
             + src(mutable_table) + mutable_note),
            ("Legacy username",
             (text_html(legacy) if legacy else '<span class="muted">not stored</span>')
             + src(legacy_table))]


_SCHEMA_NOTE = "The table's own schema text (sqlite_master) describes this column as: "


def _extra_html(contact):
    """The source's own fields for this contact (the Android Friend row), each as stored.

    A note that begins with :data:`_SCHEMA_NOTE`'s subject is the comment the app wrote beside the
    column in its CREATE TABLE statement, which SQLite keeps verbatim — quoted, not interpreted.
    """
    extra = contact.get("extra") or []
    if not extra:
        return ""
    rows = []
    for label, value, note in extra:
        hint = ""
        if note and note.startswith("schema:"):
            hint = report_ui.info_icon(_SCHEMA_NOTE + note[len("schema:"):].strip())
        elif note:
            hint = report_ui.info_icon(note)
        rows.append(f'<div class="k">{_esc(label)}</div><div class="v">{text_html(value)}'
                    f'{hint}</div>')
    return ('<div class="sect">What the contact table records'
            + report_ui.info_icon(SOURCE_NOTES.get("main.db Friend", "")) + "</div>"
            + f'<div class="grid">{"".join(rows)}</div>')


def _contact_detail(contact, convs, rel_prefix, closure=None, account=None):
    """The expanded contact row: every conversation they are in, with its conversation id.

    ``account`` (the device owner's ``user.plist`` values) is rendered on the owner's row only.
    """
    if convs:
        rows = "".join(
            "<tr>"
            + (f'<td>' + report_ui.xref(
                f'<a class="openbtn" target="scauto_conv_page" '
                f'href="{rel_prefix}Conversations/{_esc(c["page"])}#conv-{_esc(c["id"])}" '
                f'title="open this conversation in its own tab">'
                f'{text_html(c.get("title") or c["id"])} &#9656;</a>',
                [("conv", f'conv-{c["id"]}')], closure=closure) + "</td>"
               if c.get("page") else
               f'<td>{text_html(c.get("title") or c["id"])}</td>')
            + f'<td class="mono">{_esc(c["id"])}</td>'
            + f'<td>{_esc(c.get("kind") or "")}</td>'
            + f'<td class="num">{c.get("messages") or 0}</td>'
            + f'<td>{report_ui.activity_cell(c.get("first"), c.get("date_source"))}</td>'
            + f'<td>{report_ui.activity_cell(c.get("last"), c.get("date_source"))}</td>'
            + f'<td>{_esc(_WHY_LABEL.get(c["why"], (c["why"], ""))[0])}'
            + report_ui.info_icon(_WHY_LABEL.get(c["why"], ("", ""))[1]) + "</td>"
            "</tr>" for c in convs)
        table = ('<table class="sub"><tr><th>Conversation</th><th>Conversation ID</th><th>Type</th>'
                 '<th>Msgs</th><th>First activity</th><th>Last activity</th>'
                 '<th>Listed because</th>'
                 f'</tr>{rows}</table>')
    elif contact.get("conv_id"):
        table = (f'<div class="mono">{_esc(contact["conv_id"])}</div>'
                 '<span class="muted">The friends list gives this contact a conversation id, but '
                 'arroyo.db holds no conversation for it in this extraction.</span>')
    else:
        table = ('<span class="muted">No conversation in this extraction names this contact.'
                 '</span>')
    ids = [("Display name", text_html(contact["display"]))] + _username_rows(contact) + [
           ("User ID", f'<span class="mono">{_esc(contact["user_id"])}</span>')]
    grid = "".join(f'<div class="k">{k}</div><div class="v">{v}</div>' for k, v in ids if v)
    account_html = ""
    if contact["is_owner"] and account and account.get("_rows"):
        # rows that already name the file and key they were read from (the Android parser)
        account_grid = "".join(f'<div class="k">{_esc(k)}</div><div class="v mono">{_esc(v)}</div>'
                               for k, v in account["_rows"])
        note = report_ui.info_icon(account["_note"]) if account.get("_note") else ""
        account_html = (f'<div class="sect">{_esc(account.get("_title") or "Account")}{note}</div>'
                        f'<div class="grid">{account_grid}</div>')
    elif contact["is_owner"] and account:
        pairs = [(label, account[key]) for key, label in _ACCOUNT_LABELS if account.get(key)]
        account_grid = "".join(f'<div class="k">{_esc(k)}</div><div class="v mono">{_esc(v)}</div>'
                               for k, v in pairs)
        account_html = ('<div class="sect">Account — Documents/user.plist'
                        + report_ui.info_icon(ACCOUNT_NOTE) + "</div>"
                        + f'<div class="grid">{account_grid}</div>')
    return (f'<div class="sect">Conversations ({len(convs)})'
            + report_ui.info_icon(MULTI_CONV_NOTE) + "</div>" + table
            + '<div class="sect">Identifiers' + report_ui.info_icon(IDENTIFIER_NOTE) + "</div>"
            + f'<div class="grid">{grid}</div>' + _extra_html(contact) + account_html)


def _snapchatters_section(snapchatters, closure=None):
    """The Snapchatters the app cached that are not contacts — a plain table, collapsed, with no
    selection and no anchors, so nothing can mistake a row here for a contact."""
    if not snapchatters:
        return ""
    if closure is not None:
        return ('<div class="foot">Snapchatters the app cached that are not contacts: not part of '
                'this extract (they carry no row selection).</div>')
    quick = sum(1 for r in snapchatters if r["why"] == WHY_QUICK_ADD)
    rows = []
    for r in snapchatters:
        why = _esc(r["why"])
        if r["why"] == WHY_QUICK_ADD:
            why = f'<span class="why">{why}</span>'
        wal = ""
        if r.get("wal") == sqlite_open.MAIN_ONLY:
            wal = (' <span class="legacy" title="only in primary.docobjects WITHOUT its -wal: '
                   'replaced or deleted since the last checkpoint">superseded</span>')
        rows.append("<tr>"
                    f'<td>{text_html(r["display_name"]) or "<span class=muted>&mdash;</span>"}</td>'
                    f'<td>{text_html(r["username"]) or "<span class=muted>&mdash;</span>"}{wal}</td>'
                    f'<td>{text_html(r["mutable_username"]) or "<span class=muted>&mdash;</span>"}</td>'
                    f'<td>{text_html(r["legacy_username"]) or "<span class=muted>&mdash;</span>"}</td>'
                    f'<td class="mono">{_esc(r["user_id"])}</td>'
                    f'<td>{why}</td></tr>')
    return (f'<details class="others"><summary><b>{len(snapchatters)}</b> Snapchatter(s) the app '
            f'cached that are <b>not</b> contacts &middot; <b>{quick}</b> Quick Add suggestion(s)'
            f'{report_ui.info_icon(SNAPCHATTERS_NOTE)}</summary>'
            '<div class="note">These rows are NOT the account\'s friends. They are every other record '
            'in primary.docobjects\' <code>snapchatter</code> table — people the app has shown, '
            'mostly as Quick Add suggestions. Another tool reading that table as a friends list '
            'reports them as friends.</div>'
            '<table class="sub others"><tr><th>Display name</th><th>Username</th>'
            f'<th>Mutable username{report_ui.info_icon(MUTABLE_NOTE)}</th><th>Legacy username</th>'
            '<th>User ID</th><th>Why cached</th></tr>' + "".join(rows) + '</table></details>')


def generate_report(contacts, outdir, conv_index=None, friends_source="", tz_label="",
                    run_id="default", rel_prefix="../", identifiers_read=False,
                    closure=None, prov=None, account=None, snapchatters=None):
    """Write ``Contacts_report.html`` (+ ``data/index.js``) and return its path.

    ``conv_index`` maps a conversation id to what the Conversations report knows about it
    (``{page, title, kind, messages, attachments, first, last, first_sort, last_sort,
    participants}``), which is what lets each contact row link to **every** conversation that
    contact takes part in — see :func:`contact_conversations`.
    """
    # The source fingerprints this run recorded, so the examiner's saved selection carries
    # them and a later partial run can check the extraction it is handed against this one.
    sources_js = report_ui.sources_script(os.path.dirname(os.path.abspath(outdir)))
    conv_index = conv_index or {}
    os.makedirs(outdir, exist_ok=True)

    data_dir = os.path.join(outdir, "data")
    all_convs = {contact_anchor(c): contact_conversations(c, conv_index) for c in contacts}
    details = [(contact_anchor(c),
                _contact_detail(c, all_convs[contact_anchor(c)], rel_prefix, closure, account))
               for c in contacts]
    chunk_of = report_ui.write_details(data_dir, details)

    rows = []
    with_conv = with_msgs = with_legacy = multi_conv = 0
    for contact in contacts:
        if contact.get("legacy_username"):
            with_legacy += 1
        anchor = contact_anchor(contact)
        convs = all_convs[anchor]
        conv_id = contact["conv_id"]
        # The counts summarise every conversation the contact is in, not only the friends list's
        # one: for a contact in three group chats, one conversation's figures are not their activity.
        n_msgs = sum(c.get("messages") or 0 for c in convs)
        firsts = [c["first_sort"] for c in convs if c.get("first_sort")]
        lasts = [c["last_sort"] for c in convs if c.get("last_sort")]
        first_sort, last_sort = (min(firsts) if firsts else 0), (max(lasts) if lasts else 0)
        # the date AND where it came from: a conversation with no message contributes its feed
        # dates, which must stay marked as such here too rather than becoming a bare timestamp
        first_c = next((c for c in convs if c.get("first_sort") == first_sort), None)
        last_c = next((c for c in convs if c.get("last_sort") == last_sort), None)
        first_txt = (first_c or {}).get("first") or ""
        last_txt = (last_c or {}).get("last") or ""
        first_src = (first_c or {}).get("date_source") or ""
        last_src = (last_c or {}).get("date_source") or ""
        if conv_id or convs:
            with_conv += 1
        if n_msgs:
            with_msgs += 1
        if len(convs) > 1:
            multi_conv += 1
        owner = (' <span class="ownerbadge" title="the account this extraction came from">'
                 'device owner</span>') if contact["is_owner"] else ""
        if contact.get("mutable_differs"):
            owner += (' <span class="legacy" title="the stored mutable username differs from the '
                      'username — expand the row">mutable &ne;</span>')
        if convs:
            lead = convs[0]
            more = (f' <span class="more" title="in {len(convs) - 1} more conversation(s) — expand '
                    f'this row to see them all">+{len(convs) - 1}</span>') if len(convs) > 1 else ""
            if lead.get("page"):
                conv_cell = (report_ui.xref(
                                 f'<a class="openbtn" target="scauto_conv_page" '
                                 f'href="{rel_prefix}Conversations/{_esc(lead["page"])}'
                                 f'#conv-{_esc(lead["id"])}" title="open this conversation in its '
                                 f'own tab">{text_html(lead.get("title") or lead["id"])} '
                                 f'&#9656;</a>',
                                 [("conv", f'conv-{lead["id"]}')], closure=closure, brief=True)
                             + f'{more}<div class="cid">{_esc(lead["id"])}</div>')
            else:
                conv_cell = (f'{text_html(lead.get("title") or lead["id"])}{more}'
                             f'<div class="cid">{_esc(lead["id"])}</div>')
        elif conv_id:
            # A conversation id from the friends list that the chat database has no messages for:
            # the contact exists, the conversation does not (yet) in this extraction.
            conv_cell = (f'<div class="cid">{_esc(conv_id)}</div>'
                         '<span class="muted">no conversation in arroyo.db</span>')
        else:
            conv_cell = '<span class="muted">&mdash;</span>'
        legacy = contact.get("legacy_username") or ""
        if legacy:
            legacy_cell = (f'<span class="legacy" title="the username this contact used before '
                           f'changing it">{text_html(legacy)}</span>')
        else:
            legacy_cell = '<span class="muted">&mdash;</span>'
        cells = [
            "&#9656;",
            text_html(contact["display"]) or '<span class="muted">&mdash;</span>',
            (text_html(contact["username"]) or '<span class="muted">&mdash;</span>') + owner,
            legacy_cell,
            _esc(contact["user_id"]) + (' <span class="ownerdot" title="the account this '
                                        'extraction came from">owner</span>'
                                        if contact["is_owner"] else ""),
            conv_cell,
            str(n_msgs) if (conv_id or convs) else "",
            report_ui.activity_cell(first_txt, first_src),
            report_ui.activity_cell(last_txt, last_src),
        ]
        searchable = [contact["display"], contact["username"], legacy, contact["user_id"], conv_id,
                      contact.get("mutable_username") or ""]
        # every conversation id and title the contact is in, so searching an id finds the people in
        # it — and so a group chat's members are findable from the group's own id
        searchable += [c["id"] for c in convs] + [c.get("title") or "" for c in convs]
        searchable += [str(value) for _label, value, _note in contact.get("extra") or ()]
        if contact["is_owner"]:
            searchable.append("device owner")
        rows.append([
            anchor, cells,
            " ".join(s for s in searchable if s).lower(),
            {"1": contact["display"].lower(), "2": contact["username"].lower(),
             "3": legacy.lower(), "4": contact["user_id"],
             "5": ((convs[0].get("title") if convs else "") or conv_id).lower(),
             "6": n_msgs, "7": first_sort, "8": last_sort},
            chunk_of.get(anchor),
            {"conv": "y" if (conv_id or convs) else "n", "msg": "y" if n_msgs else "n",
             "owner": "y" if contact["is_owner"] else "n",
             "legacy": "y" if legacy else "n",
             "multi": "y" if len(convs) > 1 else "n",
             # Every identifier this contact has, so a saved selection is not left depending on
             # `contact_anchor`'s fallback chain — which can name a contact by username, or by a
             # conversation id, or "unknown", none of which is guaranteed to survive a build that
             # resolves one more identifier.
             **({"uid": contact["user_id"]} if contact["user_id"] else {}),
             **({"user": contact["username"]} if contact["username"] else {}),
             **({"conv_id": conv_id} if conv_id else {})},
        ])
    report_ui.write_rows(data_dir, rows)

    index_css = """
 .vcells>.vc{font-size:12.5px}
 .vcells>.vc.c0{color:#2d2d71;font-weight:700;text-align:center;padding-left:4px;padding-right:4px}
 .vr.open .vc.c0{color:#8a1f5a}
 .vcells>.vc.c4{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#33367a}
 .vcells>.vc.c6{text-align:right;font-weight:600;color:#2d2d71}
 .vcells>.vc.c7,.vcells>.vc.c8{font-size:11.5px;color:#555}
 table.sub td.num{text-align:right;font-weight:600;color:#2d2d71}
 .cid{font-family:ui-monospace,Consolas,monospace;font-size:10px;color:#888}
 .legacy{color:#8a1f5a}
 .ownerbadge{background:#2d2d71;color:#fff;border-radius:3px;font-size:9px;font-weight:700;
   letter-spacing:.04em;padding:1px 4px;margin-left:5px;text-transform:uppercase}
 .ownerdot{background:#e7ecff;color:#25348a;border:1px solid #b9c3f0;border-radius:3px;
   font-size:9px;font-weight:700;padding:0 4px;margin-left:5px;text-transform:uppercase;
   font-family:-apple-system,Segoe UI,Roboto,sans-serif}
 .foot{padding:14px 24px;color:#777;font-size:11.5px}
 details.others{margin:10px 24px 18px;border:1px solid #d9d9e6;border-radius:6px;background:#fafaff}
 details.others>summary{cursor:pointer;padding:8px 12px;font-size:12.5px;color:#2d2d71}
 details.others .note{margin:0 12px}
 table.sub.others{margin:8px 12px 12px;font-size:11.5px}
 .why{background:#f3e8f2;color:#8a1f5a;border:1px solid #e0c2d8;border-radius:8px;padding:0 6px}
"""

    partial_css, banner, figures = partial_report.page_chrome(closure, "ct", prov)
    ident_label, ident_note = _IDENTIFIER_SOURCES.get(friends_source,
                                                      ("primary.docobjects", PRIMARY_SOURCE_NOTE))

    counts_hint = ("Message and time counts are the total across EVERY conversation this contact "
                   "takes part in (see the Conversations column), taken from the Conversations "
                   "report — not just the conversation the friends list names. First / last are the "
                   "earliest and latest message across those conversations. A contact with a "
                   "conversation id but 0 messages means arroyo.db held no message for it in this "
                   "extraction.")

    doc = (f'<!doctype html><html><head><meta charset="utf-8">'
           f'<title>Snapchat contacts</title>'
           f'<style>{report_ui.PAGE_CSS}{index_css}{report_ui.VTABLE_CSS}{report_ui.NAV_CSS}'
           f'{report_ui.SELECT_CSS}{report_ui.HINT_CSS}{partial_css}</style>'
           f'<script>window.SCAUTO_RUN={json.dumps(run_id)};window.SCAUTO_VERSION={json.dumps(app_version.get_version())};{sources_js}window.SCAUTO_SELKIND="ct";</script>'
           f'<script>{report_ui.SELECT_JS}</script>'
           f'<script src="{rel_prefix}selection.js"></script>'
           f'<script>{report_ui.VTABLE_JS}</script></head><body>'
           f'<header><h1>Snapchat contacts</h1>'
           f'<div class="sum"><b>{len(contacts)}</b> contact(s) &middot; '
           f'<b>{with_conv}</b> in a conversation &middot; '
           f'<b>{multi_conv}</b> in more than one'
           f'{report_ui.info_icon(MULTI_CONV_NOTE)} &middot; '
           f'<b>{with_msgs}</b> with messages &middot; '
           f'<b>{with_legacy}</b> whose username changed'
           + (f' &middot; <b>{len(snapchatters)}</b> cached Snapchatter(s) that are not contacts'
              f'{report_ui.info_icon(SNAPCHATTERS_NOTE)}' if snapchatters and closure is None
              else '')
           + (f' &middot; times in <b>{_esc(tz_label)}</b>' if tz_label else '') +
           f'</div>'
           f'<div class="sum">Up to four identifiers per contact'
           f'{report_ui.info_icon(IDENTIFIER_NOTE)}'
           + (f' &middot; username history from {_esc(ident_label)}'
              f'{report_ui.info_icon(ident_note)}' if identifiers_read else
              f' &middot; <span title="the username index tables were not available">no username '
              f'history available</span>'
              f'{report_ui.info_icon(ident_note)}') +
           f'{figures}</div></header>'
           f'{banner}'
           f'{_source_block(friends_source)}'
           # the "row data missing" banner fires on an empty row set, so only emit it when there
           # are contacts to load in the first place
           + (report_ui.missing_data_banner("Contacts_report.html") if contacts else "") +
           f'<div class="stickytop"><div class="toolbar">'
           f'<input type="search" id="q" placeholder="Search name, username, user id, '
           f'conversation…" oninput="flt()">'
           f'<label>Conversation <select id="conv" onchange="flt()"><option value="">any</option>'
           f'<option value="y">has a conversation id</option>'
           f'<option value="n">no conversation id</option></select></label>'
           f'<label>Messages <select id="msg" onchange="flt()"><option value="">any</option>'
           f'<option value="y">with messages</option><option value="n">no messages</option>'
           f'</select></label>'
           f'<label title="Contacts whose username has changed — the report kept the previous one">'
           f'Username changed <select id="legacy" onchange="flt()">'
           f'<option value="">any</option><option value="y">yes</option>'
           f'<option value="n">no</option></select></label>'
           f'<label title="Contacts who take part in more than one conversation — a private '
           f'conversation and one or more group chats">In several <select id="multi" '
           f'onchange="flt()"><option value="">any</option>'
           f'<option value="y">several conversations</option>'
           f'<option value="n">one or none</option></select></label>'
           f'{report_ui.clear_filters_button("contact")}'
           f'<span id="count" style="color:#555"></span></div>'
           f'<div class="toolbar">{report_ui.selection_toolbar("contact")}</div>'
           f'<div class="pager" id="pager"></div>'
           f'<div class="vhdr" id="vhdr" style="grid-template-columns:30px {CT_COLS}">'
           f'<div class="vc sel"><input type="checkbox" class="selall"'
           f' title="Select / unselect every contact matching the current filters"'
           f' onclick="SCV.selectShown(this.checked)"></div>'
           f'<div class="vc nosort" title="expand a row for every conversation this contact is in">'
           f'</div>'
           f'<div class="vc" onclick="SCV.setSort(1)">Display name'
           f'{report_ui.info_icon(DISPLAY_NAME_NOTE)} <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(2)">Username'
           f'{report_ui.info_icon(USERNAME_NOTE)} <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(3)">Legacy username'
           f'{report_ui.info_icon(LEGACY_NOTE)} <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(4)">User ID'
           f'{report_ui.info_icon(USER_ID_NOTE)} <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(5)">Conversations'
           f'{report_ui.info_icon(MULTI_CONV_NOTE)} <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(6)">Msgs <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(7)">First activity{report_ui.info_icon(_ACTIVITY_HINT)} <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(8)">Last activity{report_ui.info_icon(_ACTIVITY_HINT)} <span class="ar">&#8597;</span></div>'
           f'</div></div>'
           f'<div class="vwrap" id="vwrap"><div class="vpad" id="vpad"></div>'
           f'<div class="vwin" id="vwin"></div></div>'
           f'<div class="vempty" id="vempty" style="display:none">'
           f'No contact matches the current filters.</div>'
           f'<div class="foot">Message counts{report_ui.info_icon(counts_hint)}</div>'
           f'{_snapchatters_section(snapchatters or [], closure)}'
           f'<script src="data/index.js"></script>'
           f'<script>{report_ui.HINT_JS}{report_ui.NAV_JS}{report_ui.SELECT_TOOLBAR_JS}'
           'var flt_t=0;'
           'function flt(){clearTimeout(flt_t);flt_t=setTimeout(function(){SCV.refilter();},120);}'
           'SCV.init({mount:"vwrap",win:"vwin",pad:"vpad",header:"#vhdr",missing:"vmiss",'
           f'empty:"vempty",pager:"pager",pageSize:500,selKind:"ct",sort:6,sortDir:-1,'
           'emptyAll:"This extract contains no contact.",'
           # contact_anchor falls back username -> conversation id -> "ct-unknown", so the anchor is
           # not always the user id and is not always unique; every identifier this row has travels
           # with the selection so a later run can still find the contact.
           'selKeys:function(r){var m=r[5]||{},k={};'
           'if(m.uid)k.uid=m.uid;if(m.user)k.user=m.user;if(m.conv_id)k.conv=m.conv_id;'
           'return k;},'
           f'rowHeight:{CT_ROW_H},estDetail:200,cols:"{CT_COLS}",detailBase:"data/detail-",'
           + partial_report.pulled_config(closure, "ct") +
           'query:function(){return document.getElementById("q").value;},'
           'match:function(m,r){var c=document.getElementById("conv").value,'
           'g=document.getElementById("msg").value,l=document.getElementById("legacy").value,'
           'x=document.getElementById("multi").value;'
           'return (!c||m.conv===c)&&(!g||m.msg===g)&&(!l||m.legacy===l)&&(!x||m.multi===x)'
           '&&scSelPass("ct",SCV.selId(r[0]));},'
           'selectedOnly:scSelOnly,'
           'selCount:scSelCount,'
           'count:function(n,t){document.getElementById("count").textContent='
           'n===t?(n+" contacts"):(n+" of "+t+" shown");},'
           'reset:function(){document.getElementById("q").value="";'
           'document.getElementById("conv").value="";document.getElementById("msg").value="";'
           'document.getElementById("legacy").value="";document.getElementById("multi").value="";'
           'document.getElementById("selonly").value="";}});'
           'scSelNote();scConsumeHash();'
           '</script></body></html>')

    report = os.path.join(outdir, "Contacts_report.html")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return report


def index(friends_df, outdir, owner_user_id="", owner_username="", friends_source="", tz="local",
          report_dir=None, primary=None, identifiers=None, account=None, snapchatters=None):
    """Work out which contacts exist, without writing anything. See :func:`main` for the arguments.

    This is the half a partial run needs before it can decide what to render: reading the friends
    artifact is cheap, and the closure has to know every contact (and every identifier each can be
    found by) before any report is written. :func:`render` does the writing.
    """
    try:
        from scripts.memories_media_report import make_time_formatter
        tz_label = make_time_formatter(tz)[1]
    except Exception as error:                                 # label only — never fail on it
        logger.debug(f"Could not resolve the timezone label for {tz!r}: {error}")
        tz_label = ""
    rdir = report_dir or os.path.dirname(os.path.abspath(outdir))
    run_id = report_ui.run_id(rdir)
    report_ui.write_selection_stub(rdir, run_id)
    identifiers = load_identifiers(primary) if identifiers is None else identifiers
    contacts = apply_identifiers(
        normalize_contacts(friends_df, owner_user_id, owner_username), identifiers)
    if friends_source in _USERNAME_TABLES:
        for contact in contacts:
            contact["username_tables"] = _USERNAME_TABLES[friends_source]
    if snapchatters is None:
        # everyone else the store knows: the contacts (whichever artifact they came from) and the
        # owner are what make the rest "not contacts", so they are decided here, after the contacts
        snapchatters = load_snapchatters(primary, [c["user_id"] for c in contacts], owner_user_id)

    sel = partial_report.Index("ct")
    for contact in contacts:
        # every identifier this contact has, because `contact_anchor`'s fallback chain (username ->
        # conversation id -> "ct-unknown") is not guaranteed to survive a build that resolves one more
        sel.add(contact_anchor(contact), contact,
                uid=contact.get("user_id"), user=contact.get("username"),
                conv=contact.get("conv_id"))

    return partial_report.Stage("ct", contacts, sel, tz_label=tz_label, run_id=run_id,
                                friends_source=friends_source,
                                identifiers_read=bool(identifiers), account=account or {},
                                snapchatters=snapchatters)


def render(stage, outdir, conv_index=None, closure=None, prov=None):
    """Write the report from what :func:`index` worked out. ``closure=None`` renders every contact."""
    contacts = [record for _row_id, record in stage.sel.keep(closure)]
    report = generate_report(contacts, outdir, conv_index=conv_index,
                             friends_source=stage["friends_source"], tz_label=stage["tz_label"],
                             run_id=stage["run_id"], identifiers_read=stage["identifiers_read"],
                             closure=closure, prov=prov, account=stage["account"],
                             snapchatters=stage["snapchatters"])
    logger.info(f"Contacts report: {os.path.abspath(report)}")
    if closure is None:
        logger.info(f"  {len(contacts)} contact(s) from "
                    f"{stage['friends_source'] or 'an unrecorded source'}")
    else:
        logger.info(f"  {len(contacts)} of {len(stage.model)} contact(s) in this extract")
    return report


def main(friends_df, outdir, conv_index=None, owner_user_id="", owner_username="",
         friends_source="", tz="local", report_dir=None, primary=None, identifiers=None,
         account=None, snapchatters=None):
    """Build the contacts report from the friends DataFrame ``ParseSnapchat_iOS`` recovered.

    friends_df   : whichever getFriends* source answered (columns vary — see the normalizers).
    outdir       : output directory (…/Reports/Contacts).
    conv_index   : conversation id -> the Conversations report's summary for it (for the links).
    friends_source : which artifact the contacts came from — named in the report (SOURCE_NOTES).
    tz           : only used to label the times this report shows, which the Conversations report
                   has already formatted; imported lazily so this module stays dependency-free.
    primary      : primary.docobjects path, for the username / legacy-username identifiers.
    identifiers  : an already-loaded ``load_identifiers(primary)``. The caller reads that file once
                   and gives the same result to both chat reports; passing None re-reads it from
                   ``primary``, which keeps this report usable on its own.
    account      : the owner's ``Documents/user.plist`` values (``ParseSnapchat_iOS.getAccount``),
                   shown on the owner's row.
    snapchatters : an already-loaded ``load_snapchatters(...)``; None reads it from ``primary``.

    A full run in one call: :func:`index` then :func:`render`. A partial run calls the two halves
    separately, because the closure has to be decided from every report's index at once.
    """
    stage = index(friends_df, outdir, owner_user_id=owner_user_id, owner_username=owner_username,
                  friends_source=friends_source, tz=tz, report_dir=report_dir, primary=primary,
                  identifiers=identifiers, account=account, snapchatters=snapchatters)
    return render(stage, outdir, conv_index=conv_index)
