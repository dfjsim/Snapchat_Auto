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
}

# Index-table geometry: one fixed row height and one column track list for the header and the rows.
CT_COLS = ("minmax(140px,1fr) minmax(130px,1fr) minmax(120px,0.9fr) 250px minmax(180px,1.1fr) "
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
        contacts.append({"display": display, "username": username, "user_id": user_id,
                         "legacy_username": "", "conv_id": conv_id, "is_owner": is_owner})
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
    """
    index = {}
    for contact in contacts:
        entry = {"href": f"Contacts/Contacts_report.html#{contact_anchor(contact)}",
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
    "the names) joined on rowid to 'index_snapchatterusername' (current username) and "
    "'index_snapchatterlegacyUsername' (the username used before it was changed). The three tables "
    "share one rowid per contact.")


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
        conn = sqlite3.connect(f"file:{primary}?mode=ro", uri=True)
    except sqlite3.DatabaseError as error:
        logger.debug(f"Could not open {primary}: {error}")
        return out
    try:
        user_col = _text_column(conn, "index_snapchatterusername")
        legacy_col = _text_column(conn, "index_snapchatterlegacyUsername", prefer="username")
        if not (user_col or legacy_col):
            logger.info("Contacts: primary.docobjects has no username index tables — the username "
                        "history cannot be shown")
            return out
        select = ["s.userId as user_id"]
        joins = ""
        if user_col:
            select.append(f"u.{user_col} as username")
            joins += " left join index_snapchatterusername u on u.rowid = s.rowid"
        if legacy_col:
            select.append(f"l.{legacy_col} as legacy_username")
            joins += " left join index_snapchatterlegacyUsername l on l.rowid = s.rowid"
        query = f"select {', '.join(select)} from snapchatter s{joins}"
        cur = conn.execute(query)
        names = [d[0] for d in cur.description]
        for row in cur.fetchall():
            record = dict(zip(names, row))
            user_id = cell(record.get("user_id"))
            if not user_id:
                continue
            entry = out.setdefault(user_id.lower(), {"username": "", "legacy_username": ""})
            for key in ("username", "legacy_username"):
                value = cell(record.get(key))
                if value and not entry[key]:
                    entry[key] = value
    except sqlite3.DatabaseError as error:
        logger.info(f"Contacts: could not read the username tables from primary.docobjects "
                    f"({error}) — usernames will be whatever the friends list held")
    finally:
        conn.close()
    if out:
        legacy = sum(1 for v in out.values()
                     if v["legacy_username"] and v["legacy_username"] != v["username"])
        logger.info(f"Contacts: {len(out)} identifier record(s) from primary.docobjects, "
                    f"{legacy} with a different legacy username")
    return out


def apply_identifiers(contacts, identifiers):
    """Add ``legacy_username`` to each contact and fill a missing username from the same source.

    A legacy username equal to the current one is not a rename and is not shown as one.
    """
    for contact in contacts:
        record = identifiers.get(contact["user_id"].lower()) if contact["user_id"] else None
        legacy = (record or {}).get("legacy_username", "")
        username = (record or {}).get("username", "")
        if username and not contact["username"]:
            contact["username"] = username
        current = contact["username"] or username
        contact["legacy_username"] = legacy if legacy and legacy != current else ""
    return contacts


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


def generate_report(contacts, outdir, conv_index=None, friends_source="", tz_label="",
                    run_id="default", rel_prefix="../", identifiers_read=False):
    """Write ``Contacts_report.html`` (+ ``data/index.js``) and return its path.

    ``conv_index`` maps a conversation id to what the Conversations report knows about it
    (``{page, title, messages, attachments, first, last, first_sort, last_sort}``), which is what
    lets each contact row link straight to that conversation's detail page.
    """
    conv_index = conv_index or {}
    os.makedirs(outdir, exist_ok=True)

    rows = []
    with_conv = with_msgs = with_legacy = 0
    for contact in contacts:
        if contact.get("legacy_username"):
            with_legacy += 1
        conv_id = contact["conv_id"]
        conv = conv_index.get(conv_id) or {}
        n_msgs = conv.get("messages", 0)
        if conv_id:
            with_conv += 1
        if n_msgs:
            with_msgs += 1
        anchor = contact_anchor(contact)
        owner = (' <span class="ownerbadge" title="the account this extraction came from">'
                 'device owner</span>') if contact["is_owner"] else ""
        if conv_id and conv.get("page"):
            conv_cell = (f'<a class="openbtn" target="scauto_conv_page" '
                         f'href="{rel_prefix}Conversations/{_esc(conv["page"])}'
                         f'#conv-{_esc(conv_id)}" title="open this conversation in its own tab">'
                         f'{text_html(conv.get("title") or conv_id)} &#9656;</a>'
                         f'<div class="cid">{_esc(conv_id)}</div>')
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
            text_html(contact["display"]) or '<span class="muted">&mdash;</span>',
            (text_html(contact["username"]) or '<span class="muted">&mdash;</span>') + owner,
            legacy_cell,
            _esc(contact["user_id"]) + (' <span class="ownerdot" title="the account this '
                                        'extraction came from">owner</span>'
                                        if contact["is_owner"] else ""),
            conv_cell,
            str(n_msgs) if conv_id else "",
            _esc(conv.get("first", "")),
            _esc(conv.get("last", "")),
        ]
        searchable = [contact["display"], contact["username"], legacy, contact["user_id"], conv_id,
                      conv.get("title", "")]
        if contact["is_owner"]:
            searchable.append("device owner")
        rows.append([
            anchor, cells,
            " ".join(s for s in searchable if s).lower(),
            {"0": contact["display"].lower(), "1": contact["username"].lower(),
             "2": legacy.lower(), "3": contact["user_id"],
             "4": (conv.get("title") or conv_id).lower(),
             "5": n_msgs, "6": conv.get("first_sort") or 0, "7": conv.get("last_sort") or 0},
            None,
            {"conv": "y" if conv_id else "n", "msg": "y" if n_msgs else "n",
             "owner": "y" if contact["is_owner"] else "n",
             "legacy": "y" if legacy else "n"},
        ])
    report_ui.write_rows(os.path.join(outdir, "data"), rows)

    index_css = """
 .vcells>.vc{font-size:12.5px}
 .vcells>.vc.c3{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#33367a}
 .vcells>.vc.c5{text-align:right;font-weight:600;color:#2d2d71}
 .vcells>.vc.c6,.vcells>.vc.c7{font-size:11.5px;color:#555}
 .cid{font-family:ui-monospace,Consolas,monospace;font-size:10px;color:#888}
 .legacy{color:#8a1f5a}
 .ownerbadge{background:#2d2d71;color:#fff;border-radius:3px;font-size:9px;font-weight:700;
   letter-spacing:.04em;padding:1px 4px;margin-left:5px;text-transform:uppercase}
 .ownerdot{background:#e7ecff;color:#25348a;border:1px solid #b9c3f0;border-radius:3px;
   font-size:9px;font-weight:700;padding:0 4px;margin-left:5px;text-transform:uppercase;
   font-family:-apple-system,Segoe UI,Roboto,sans-serif}
 .foot{padding:14px 24px;color:#777;font-size:11.5px}
"""

    counts_hint = ("Message and time counts come from the Conversations report for the "
                   "conversation id on this row; a contact with a conversation id but 0 messages "
                   "means arroyo.db held no message for that conversation in this extraction.")

    doc = (f'<!doctype html><html><head><meta charset="utf-8">'
           f'<title>Snapchat contacts</title>'
           f'<style>{report_ui.PAGE_CSS}{index_css}{report_ui.VTABLE_CSS}{report_ui.NAV_CSS}'
           f'{report_ui.SELECT_CSS}{report_ui.HINT_CSS}</style>'
           f'<script>window.SCAUTO_RUN={json.dumps(run_id)};window.SCAUTO_SELKIND="ct";</script>'
           f'<script>{report_ui.SELECT_JS}</script>'
           f'<script src="{rel_prefix}selection.js"></script>'
           f'<script>{report_ui.VTABLE_JS}</script></head><body>'
           f'<header><h1>Snapchat contacts</h1>'
           f'<div class="sum"><b>{len(contacts)}</b> contact(s) &middot; '
           f'<b>{with_conv}</b> with a conversation id &middot; '
           f'<b>{with_msgs}</b> with messages &middot; '
           f'<b>{with_legacy}</b> whose username changed'
           + (f' &middot; times in <b>{_esc(tz_label)}</b>' if tz_label else '') +
           f'</div>'
           f'<div class="sum">Up to four identifiers per contact'
           f'{report_ui.info_icon(IDENTIFIER_NOTE)}'
           + (f' &middot; username history from primary.docobjects'
              f'{report_ui.info_icon(PRIMARY_SOURCE_NOTE)}' if identifiers_read else
              f' &middot; <span title="the username index tables were not available">no username '
              f'history available</span>'
              f'{report_ui.info_icon(PRIMARY_SOURCE_NOTE)}') +
           f'</div></header>'
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
           f'<span id="count" style="color:#555"></span></div>'
           f'<div class="toolbar">{report_ui.selection_toolbar("contact")}</div>'
           f'<div class="pager" id="pager"></div>'
           f'<div class="vhdr" id="vhdr" style="grid-template-columns:30px {CT_COLS}">'
           f'<div class="vc sel"><input type="checkbox" class="selall"'
           f' title="Select / unselect every contact matching the current filters"'
           f' onclick="SCV.selectShown(this.checked)"></div>'
           f'<div class="vc" onclick="SCV.setSort(0)">Display name'
           f'{report_ui.info_icon(DISPLAY_NAME_NOTE)} <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(1)">Username'
           f'{report_ui.info_icon(USERNAME_NOTE)} <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(2)">Legacy username'
           f'{report_ui.info_icon(LEGACY_NOTE)} <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(3)">User ID'
           f'{report_ui.info_icon(USER_ID_NOTE)} <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(4)">Conversation <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(5)">Msgs <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(6)">First message <span class="ar">&#8597;</span></div>'
           f'<div class="vc" onclick="SCV.setSort(7)">Last message <span class="ar">&#8597;</span></div>'
           f'</div></div>'
           f'<div class="vwrap" id="vwrap"><div class="vpad" id="vpad"></div>'
           f'<div class="vwin" id="vwin"></div></div>'
           f'<div class="vempty" id="vempty" style="display:none">'
           f'No contact matches the current filters.</div>'
           f'<div class="foot">Message counts{report_ui.info_icon(counts_hint)}</div>'
           f'<script src="data/index.js"></script>'
           f'<script>{report_ui.HINT_JS}{report_ui.NAV_JS}{report_ui.SELECT_TOOLBAR_JS}'
           'var flt_t=0;'
           'function flt(){clearTimeout(flt_t);flt_t=setTimeout(function(){SCV.refilter();},120);}'
           'SCV.init({mount:"vwrap",win:"vwin",pad:"vpad",header:"#vhdr",missing:"vmiss",'
           f'empty:"vempty",pager:"pager",pageSize:500,selKind:"ct",sort:5,sortDir:-1,'
           f'rowHeight:{CT_ROW_H},cols:"{CT_COLS}",detailBase:null,'
           'query:function(){return document.getElementById("q").value;},'
           'match:function(m,r){var c=document.getElementById("conv").value,'
           'g=document.getElementById("msg").value,l=document.getElementById("legacy").value;'
           'return (!c||m.conv===c)&&(!g||m.msg===g)&&(!l||m.legacy===l)'
           '&&(!document.getElementById("selonly").checked||SCSel.get("ct",r[0]));},'
           'selectedOnly:function(){return document.getElementById("selonly").checked;},'
           'selCount:function(n){document.getElementById("selcount").textContent=n+" selected";'
           'scSelNote();},'
           'count:function(n,t){document.getElementById("count").textContent='
           'n===t?(n+" contacts"):(n+" of "+t+" shown");},'
           'reset:function(){document.getElementById("q").value="";'
           'document.getElementById("conv").value="";document.getElementById("msg").value="";'
           'document.getElementById("legacy").value="";'
           'document.getElementById("selonly").checked=false;}});'
           'scSelNote();scConsumeHash();'
           '</script></body></html>')

    report = os.path.join(outdir, "Contacts_report.html")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return report


def main(friends_df, outdir, conv_index=None, owner_user_id="", owner_username="",
         friends_source="", tz="local", report_dir=None, primary=None):
    """Build the contacts report from the friends DataFrame ``ParseSnapchat_iOS`` recovered.

    friends_df   : whichever getFriends* source answered (columns vary — see the normalizers).
    outdir       : output directory (…/Reports/Contacts).
    conv_index   : conversation id -> the Conversations report's summary for it (for the links).
    friends_source : which artifact the contacts came from — named in the report (SOURCE_NOTES).
    tz           : only used to label the times this report shows, which the Conversations report
                   has already formatted; imported lazily so this module stays dependency-free.
    primary      : primary.docobjects path, for the username / legacy-username identifiers.
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
    identifiers = load_identifiers(primary)
    contacts = apply_identifiers(
        normalize_contacts(friends_df, owner_user_id, owner_username), identifiers)
    report = generate_report(contacts, outdir, conv_index=conv_index,
                             friends_source=friends_source, tz_label=tz_label, run_id=run_id,
                             identifiers_read=bool(identifiers))
    logger.info(f"Contacts report: {os.path.abspath(report)}")
    logger.info(f"  {len(contacts)} contact(s) from {friends_source or 'an unrecorded source'}")
    return report
