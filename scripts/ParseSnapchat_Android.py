"""
Snapchat for Android: read an extracted app folder and build the same reports the iOS side builds.

The two apps share their messaging and content-caching core, so two of the artifacts read here are
the **same** databases iOS has, with the same schema:

* ``arroyo.db`` — conversations and messages. Parsed by the functions in ``ParseSnapchat_iOS``
  unchanged (``getChats``, ``fixSenders``, the cache join in ``mergeCacheChats``);
* ``cache_controller.db`` and the ``com.snap.file_manager_*_SCContent_*`` folders — the index of the
  cached files and the files themselves, read by the cache_controller report on both platforms and
  used here to attach chat media to its message.

What differs is everything around them, and that is what this module supplies:

* the **contacts** come from ``main.db``'s ``Friend`` table (plus ``CombinedUsername`` for the
  username pair), not from plists or ``primary.docobjects``;
* the **account** is named by ``arroyo.db`` (``required_values``) and by ``shared_prefs``;
* the **Memories** are in ``memories.db`` — see ``scripts/memories_android_report.py``.

Where each file is found is ``scripts/android_layout.py``'s job; a missing artifact leaves its report
out with one logged line, and never costs the others.
"""

import os
import re
import shutil
import logging
from datetime import datetime

import pandas as pd

from scripts import android_layout
from scripts import source_fingerprint
from scripts import ParseSnapchat_iOS as shared
from scripts.data import sqlite_open

logger = logging.getLogger(__name__)

#: The name the Contacts report gives this source (see contacts_report.SOURCE_NOTES).
FRIENDS_SOURCE = "main.db Friend"

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# --------------------------------------------------------------------------- helpers

def _cell(value):
    """A database value as display text: None / NaN become ""."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _int(value):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def schema_comments(db_path, table):
    """``{column: comment}`` from the ``-- …`` comments in a table's own ``CREATE TABLE`` text.

    SQLite keeps the statement a table was created with verbatim in ``sqlite_master``, comments
    included, so what the app wrote next to a column travels with the database. Quoting it lets a
    report say what a column holds on the strength of the evidence itself — anyone can read the same
    text out of the same file.
    """
    rows, _marks = [], []
    try:
        views = sqlite_open.open_views(db_path)
    except Exception as error:                                 # noqa: BLE001
        logger.debug(f"Could not open {db_path}: {error}")
        return {}
    try:
        rows, _marks = sqlite_open.query_both(
            views, "select sql from sqlite_master where type = 'table' and name = ?", [table])
    finally:
        views.close()
    out = {}
    for (sql,) in rows:
        pending = []                                   # comment lines waiting for their column
        for line in str(sql or "").splitlines():
            only = re.match(r"\s*--\s*(.*?)\s*$", line)
            if only:
                if only.group(1) and not only.group(1).lower().startswith(("http", "see http")):
                    pending.append(only.group(1))
                continue
            mo = re.match(r"\s*[`\"]?(\w+)[`\"]?\s+[A-Za-z]+((?:[^-]|-(?!-))*)(?:--\s*(.+?))?\s*$",
                          line)
            if not mo or mo.group(1).lower() in ("primary", "unique", "foreign", "constraint",
                                                  "check", "create"):
                pending = []
                continue
            text = " ".join(pending + ([mo.group(3)] if mo.group(3) else []))
            pending = []
            if text:
                out.setdefault(mo.group(1), text)
    return out


def _ms_text(value, timefmt):
    """A Unix-milliseconds value through the run's formatter, or "" for 0 / None."""
    ms = _int(value)
    if not ms:
        return ""
    try:
        return timefmt(ms / 1000.0 - 978307200)
    except Exception:                                          # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- the account

# shared_prefs keys that name the signed-in account. Matched on the key's own name, case-insensitively,
# and reported with the file and key they came from, so nothing here is an interpretation: the
# examiner sees the stored name next to the stored value.
_ACCOUNT_KEY_RE = re.compile(
    r"(^|[_.])(user_?id|username|user_name|display_?name|phone(_number)?|email|birthday|"
    r"mutable_?username|first_?name|last_?name)$", re.I)

#: Preference files shown whole on the owner's row — the two ALEAPP's Android Snapchat module reads as
#: its "Identity Persistent Store" and "Login Signup Store" artifacts (Alexis Brignoni, MIT) — and the
#: keys in them it reads as Unix milliseconds, which are shown formatted beside the stored value.
_ACCOUNT_FILES = ("identity_persistent_store.xml", "LoginSignupStore.xml")
_ACCOUNT_MS_KEYS = {"INSTALL_ON_DEVICE_TIMESTAMP", "LONG_CLIENT_ID_DEVICE_TIMESTAMP",
                    "FIRST_LOGGED_IN_ON_DEVICE_TIMESTAMP"}


ACCOUNT_NOTE = (
    "Every value here is shown as stored, labelled with the file and key (or table and key) it was "
    "read from. arroyo.db required_values is the messaging database's own record of the signed-in "
    "account (its USERID row is how this run identified the device owner). From shared_prefs, two "
    "files are shown whole — identity_persistent_store.xml and LoginSignupStore.xml, the two ALEAPP's "
    "Android Snapchat module (Alexis Brignoni, MIT) reports, with INSTALL_ON_DEVICE_TIMESTAMP, "
    "LONG_CLIENT_ID_DEVICE_TIMESTAMP and FIRST_LOGGED_IN_ON_DEVICE_TIMESTAMP read as Unix "
    "milliseconds as it reads them — and from every other preference file only the keys whose name "
    "says user id, username, display name, phone, e-mail or birthday. No value is interpreted beyond "
    "its key's name.")


def owner_identity(layout, timefmt=None):
    """The signed-in account: ``{user_id, username, display_name, rows, sources}``.

    ``user_id`` comes from ``arroyo.db`` → ``required_values`` (key ``USERID``) — the same record the
    iOS parser falls back to, written by the messaging core both apps share — and otherwise from the
    first shared_prefs value that is a user id. ``rows`` are ``(label, value)`` for the account block
    on the owner's contact row, each label naming the file and key it was read from.
    """
    owner = {"user_id": "", "username": "", "display_name": "", "rows": [], "sources": []}
    arroyo = layout.db("arroyo")
    if arroyo:
        try:
            df, _info = sqlite_open.read_sql(
                arroyo, "select key, value from required_values")
            for key, value in zip(df.get("key", []), df.get("value", [])):
                text = _cell(value)
                if not text:
                    continue
                owner["rows"].append((f"arroyo.db required_values — {key}", text))
                if str(key).upper() == "USERID" and not owner["user_id"]:
                    owner["user_id"] = text
                    owner["sources"].append("arroyo.db required_values USERID")
        except Exception as error:                             # noqa: BLE001
            logger.debug(f"Could not read required_values: {error}")

    for path in layout.shared_prefs:
        name = os.path.basename(path)
        whole = name in _ACCOUNT_FILES
        for key, kind, value in android_layout.read_prefs(path):
            if isinstance(value, list) or not (whole or _ACCOUNT_KEY_RE.search(key)):
                continue
            text = _cell(value)
            if not text or len(text) > 200:
                continue
            if key in _ACCOUNT_MS_KEYS and _int(text) and timefmt:
                text = f"{_ms_text(text, timefmt)} (raw {text})"
            owner["rows"].append((f"shared_prefs/{name} — {key}", text))
            low = key.lower()
            if not owner["user_id"] and re.search(r"user_?id$", low) and _UUID_RE.match(text):
                owner["user_id"] = text
                owner["sources"].append(f"shared_prefs/{name} {key}")
            elif (not owner["username"] and re.search(r"(^|_)username$", low)
                  and "mutable" not in low):
                owner["username"] = text
                owner["sources"].append(f"shared_prefs/{name} {key}")
            elif not owner["display_name"] and re.search(r"display_?name$", low):
                owner["display_name"] = text
    if owner["user_id"]:
        logger.info(f"Account: user ID {owner['user_id']} ({', '.join(owner['sources'][:2])})")
    else:
        logger.warning("Account: no user ID found in arroyo.db or shared_prefs — messages cannot be "
                       "marked as sent or received")
    return owner


# --------------------------------------------------------------------------- contacts

#: Friend columns carried into the contact's detail, with what the report calls them. The table's own
#: schema comment, where it has one, is shown beside the value (see :func:`schema_comments`).
_FRIEND_DETAIL = (
    ("addedTimestamp", "Added (Friend.addedTimestamp)", "ms"),
    ("reverseAddedTimestamp", "Reverse added (Friend.reverseAddedTimestamp)", "ms"),
    ("friendLinkType", "Friend.friendLinkType (as stored)", "raw"),
    ("phone", "Phone (Friend.phone)", "text"),
    ("birthday", "Birthday (Friend.birthday, as stored)", "raw"),
    ("streakLength", "Streak length (Friend.streakLength)", "raw"),
    ("streakExpiration", "Streak expiration (Friend.streakExpiration)", "ms"),
    ("isOfficial", "Official account (Friend.isOfficial)", "bool"),
    ("isPopular", "Popular account (Friend.isPopular)", "bool"),
    ("isBrand", "Brand account (Friend.isBrand)", "bool"),
    ("syncSource", "Friend.syncSource (as stored)", "raw"),
    ("_lastModifiedTimestamp", "Row last modified (Friend._lastModifiedTimestamp)", "ms"),
    ("serverDisplayName", "Server display name (Friend.serverDisplayName)", "text"),
)

#: Tables of main.db that list a Friend row for a reason of their own, keyed on Friend._id.
_MEMBERSHIP = (
    ("FriendWhoAddedMe", "friendRowId", "listed in FriendWhoAddedMe"),
    ("SuggestedFriend", "friendRowId", "listed in SuggestedFriend"),
    ("BestFriend", "friendRowId", "listed in BestFriend"),
    ("ContactFriend", "friendRowId", "listed in ContactFriend"),
)


def read_contacts(layout, owner, timefmt):
    """``main.db`` → ``Friend`` as the friends frame the Contacts and Conversations reports take.

    Returns ``(friends_df, identifiers, stats)``. ``friends_df`` has the standard columns (``User ID``,
    ``Username``, ``Display name``) plus ``_extra``: the row's own Android fields as
    ``[(label, value, note)]`` for the contact's detail. ``identifiers`` is the username pair per user
    id in the shape ``contacts_report.load_identifiers`` returns, from ``CombinedUsername``.

    Every row of the table is returned. The table is the app's record of every user it has to show —
    the friends, but also people who added the account, people it added, group members, suggestions —
    so the report carries a warning and each row states what the table itself says about the link
    (the two "added" timestamps, the raw link type, and which of the other friend tables lists it).
    Reading the table as "the friends list" is the trap the report exists to avoid.
    """
    empty = pd.DataFrame({"User ID": pd.Series(dtype=object), "Username": pd.Series(dtype=object),
                          "Display name": pd.Series(dtype=object)})
    main_db = layout.db("main") or layout.db("tcspahn")
    if not main_db:
        logger.info("Contacts: main.db not present — contacts will be the message senders only")
        return empty, {}, {}
    cols = sqlite_open.table_columns(main_db, "Friend")
    if not cols or "userId" not in cols:
        logger.info("Contacts: main.db has no Friend table with a userId column")
        return empty, {}, {}
    comments = schema_comments(main_db, "Friend")

    select = [f"f.{c} as {c}" for c in ("_id", "userId", "username", "displayName")
              if c in cols]
    select += [f"f.{c} as {c}" for c, _label, _kind in _FRIEND_DETAIL if c in cols]
    joins = ""
    combined_cols = sqlite_open.table_columns(main_db, "CombinedUsername")
    if "combinedUsernameRowId" in cols and {"_id", "originalUsername"} <= combined_cols:
        select.append("c.originalUsername as originalUsername")
        if "mutableUsername" in combined_cols:
            select.append("c.mutableUsername as mutableUsername")
        joins = " left join CombinedUsername c on c._id = f.combinedUsernameRowId"
    df, info = sqlite_open.read_sql(main_db, f"select {', '.join(select)} from Friend f{joins}")
    if df is None or not len(df):
        logger.info("Contacts: main.db Friend is empty")
        return empty, {}, {}

    listed = {}
    for table, key, label in _MEMBERSHIP:
        tcols = sqlite_open.table_columns(main_db, table)
        if key not in tcols:
            continue
        ids, _info = sqlite_open.read_sql(main_db, f"select {key} as rid from {table}")
        for rid in ids.get("rid", []):
            if _int(rid) is not None:
                listed.setdefault(_int(rid), []).append(label)

    rows, identifiers, seen = [], {}, {}
    for _index, rec in df.iterrows():
        user_id = _cell(rec.get("userId"))
        if not user_id:
            continue
        wal = rec.get("_wal")
        if user_id.lower() in seen and wal == sqlite_open.MAIN_ONLY:
            continue                                  # the live row wins; the older one is not a person
        username = _cell(rec.get("username"))
        original = _cell(rec.get("originalUsername"))
        mutable = _cell(rec.get("mutableUsername"))
        extra = []
        for col, label, kind in _FRIEND_DETAIL:
            if col not in df.columns:
                continue
            raw = rec.get(col)
            if kind == "ms":
                value = _ms_text(raw, timefmt)
                if value:
                    value = f"{value} (raw {_int(raw)})"
            elif kind == "bool":
                value = {1: "yes", 0: ""}.get(_int(raw), _cell(raw))
            else:
                value = _cell(raw)
            if value:
                # the column's comment in the table's own CREATE TABLE text, quoted as such
                extra.append((label, value, f"schema:{comments[col]}" if comments.get(col) else ""))
        for label in listed.get(_int(rec.get("_id")), []):
            extra.append(("main.db", label, ""))
        if wal == sqlite_open.MAIN_ONLY:
            extra.append(("Recovered row", "present only in main.db WITHOUT its -wal: changed or "
                          "deleted since the last checkpoint", sqlite_open.MARKER_HELP[wal]))
        seen[user_id.lower()] = len(rows)
        rows.append({"User ID": user_id, "Username": mutable or username,
                     "Display name": _cell(rec.get("displayName")), "_extra": extra})
        if original or mutable:
            current = mutable or username
            identifiers[user_id.lower()] = {
                "username": current,
                "mutable_username": mutable,
                "legacy_username": original if original and original != current else "",
                "superseded": []}

    friends_df = pd.DataFrame(rows) if rows else empty
    added = sum(1 for r in rows if any(e[0].startswith("Added (") for e in r["_extra"]))
    stats = {"rows": len(rows), "added": added}
    logger.info(f"Contacts: {len(rows)} row(s) in main.db Friend ({added} with an addedTimestamp); "
                f"{sqlite_open.describe(info)}")
    if identifiers:
        changed = sum(1 for v in identifiers.values() if v["legacy_username"])
        logger.info(f"Contacts: username pairs from main.db CombinedUsername for {len(identifiers)} "
                    f"user(s), {changed} whose username changed")
    return friends_df, identifiers, stats


# --------------------------------------------------------------------------- messages

def _empty_persistent():
    return pd.DataFrame({c: pd.Series(dtype=object) for c in
                         ("CACHE_KEY", "TYPE", "client_conversation_id", "server_message_id",
                          "SERVER_MESSAGE_ID_PART")})


#: How a chat attachment that was NOT one whole cache file was put together, by CACHE_KEY — the
#: explanation the Conversations report shows beside its cache_controller link (see
#: :func:`chat_cache_key`). Filled by :func:`materialize_chat_media` for the run in progress.
_MATERIALIZED = {}
_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")


_CHAT_EK_RE = re.compile(r"^[^:]*:([0-9A-Fa-f-]{36}):(\d+)")


def message_key_pairs(blob, limit=16):
    """Every (32-byte, 16-byte) pair of length-delimited values in a message's protobuf, at any
    depth — the candidates a cached chat file is tried with when it is not plaintext. A media message
    carries its key and IV in its own content; which fields hold them is not assumed, because a pair
    that is not the file's key cannot turn it into media, so every candidate is safe to try."""
    thirty_two, sixteen = [], []

    def walk(data, depth):
        if depth > 10 or len(thirty_two) > limit and len(sixteen) > limit:
            return
        pos = 0
        try:
            while pos < len(data):
                key, shift = 0, 0
                while True:
                    byte = data[pos]
                    pos += 1
                    key |= (byte & 0x7F) << shift
                    shift += 7
                    if not byte & 0x80:
                        break
                wire = key & 7
                if key >> 3 == 0:
                    return
                if wire == 0:
                    while data[pos] & 0x80:
                        pos += 1
                    pos += 1
                elif wire == 1:
                    pos += 8
                elif wire == 5:
                    pos += 4
                elif wire == 2:
                    length, shift = 0, 0
                    while True:
                        byte = data[pos]
                        pos += 1
                        length |= (byte & 0x7F) << shift
                        shift += 7
                        if not byte & 0x80:
                            break
                    value = data[pos:pos + length]
                    pos += length
                    if len(value) == 32 and value not in thirty_two:
                        thirty_two.append(value)
                    elif len(value) == 16 and value not in sixteen:
                        sixteen.append(value)
                    if len(value) > 2:
                        walk(value, depth + 1)
                else:
                    return
        except IndexError:
            return

    if blob:
        walk(bytes(blob), 0)
    return [(k, iv) for k in thirty_two[:limit] for iv in sixteen[:limit]]


def _decrypt_with(raw, pairs):
    """``(plaintext, (key, iv))`` for the first pair that turns ``raw`` into media, else ``(None,
    None)``. Two blocks are tried first; only a pair that passes is used on the whole file."""
    from Crypto.Cipher import AES
    from scripts.memories_media_report import guess_media, _strip_pkcs7
    if len(raw) < 32:
        return None, None
    for key, iv in pairs:
        if not guess_media(AES.new(key, AES.MODE_CBC, iv).decrypt(raw[:32])[:16]):
            continue
        body = raw[:len(raw) - len(raw) % 16]
        plain = _strip_pkcs7(AES.new(key, AES.MODE_CBC, iv).decrypt(body))
        if guess_media(plain[:16]):
            return plain, (key, iv)
    return None, None


def materialize_chat_media(cache_df, app, dest, message_blobs=None):
    """Rebuild the chat media the cache holds only in pieces, so the Conversations report can show it.

    The shared join (``ParseSnapchat_iOS.mergeCache``) copies a claim's file only when a **whole** file
    named after its CACHE_KEY is media. The same cache stores media two other ways
    (docs/report_cache_controller.md): as byte-range shards (``<key>_<start>-<end>``,
    ``<key>_PREFETCH``), rebuilt here by concatenating them in offset order, and as a bundle — the file
    named after the key is a small descriptor and the content sits in ``<key>_<child>`` files — whose
    largest media child is taken. On iOS a chat video that is a bundle reaches the report through
    ``SCPersistentMedia``; Android has no such folder, so without this every chat video stored that way
    would be listed as having no cached file.

    A file that is whole (or rebuilt) but not plaintext is then tried with the key / IV pairs its
    message carries (``message_blobs``: ``{(conversation, server message id): message_content}``;
    see :func:`message_key_pairs`), and kept only when that decrypts it to media.

    Each rebuilt file is written to ``dest`` under its CACHE_KEY, where the shared join finds it like
    any other cache file. Only bytes that ARE media (by their magic bytes) are written. Returns how
    many were.
    """
    from scripts.memories_media_report import index_sccontent, _resolve_sccontent, guess_media

    _MATERIALIZED.clear()
    keys = sorted({str(k) for k in cache_df.get("CACHE_KEY", []) if _KEY_RE.match(str(k or ""))})
    if not keys:
        return 0
    # which message each claim names, for the keys its content carries
    claims_of = {}
    for ck, ek in zip(cache_df.get("CACHE_KEY", []), cache_df.get("EXTERNAL_KEY", [])):
        mo = _CHAT_EK_RE.match(str(ek or ""))
        if mo:
            claims_of.setdefault(str(ck).lower(), []).append((mo.group(1), mo.group(2)))
    full, parts = index_sccontent(app)
    children = {}
    for name, paths in full.items():
        head, sep, child = name.partition("_")
        if sep and _KEY_RE.match(head):
            children.setdefault(head.lower(), []).append((child, paths[0]))
    os.makedirs(dest, exist_ok=True)
    made = 0
    for key in keys:
        whole = full.get(key) or []
        raw_whole = None
        if whole:
            try:
                with open(whole[0], "rb") as fh:
                    raw_whole = fh.read()
            except OSError:
                continue
            if guess_media(raw_whole[:16]):
                continue                                 # the shared join takes it as it is
        data, note = None, ""
        if parts.get(key.lower()):
            raw, _fulls, part_paths, coverage = _resolve_sccontent(key, {}, parts)
            if raw and guess_media(raw[:16]):
                gaps = (coverage or {}).get("gaps") or []
                data = raw
                names = ", ".join(os.path.basename(p) for p in part_paths[:4])
                more = " …" if len(part_paths) > 4 else ""
                note = (f"This attachment was rebuilt from the {len(part_paths)} byte-range shard(s) "
                        f"the cache stores for CACHE_KEY {key} ({names}{more}), concatenated in "
                        f"offset order"
                        + (f"; {len(gaps)} gap(s) between them mean it is incomplete" if gaps else "")
                        + ". The cache_controller entry lists each shard with its own hashes.")
        if data is None:
            best = None
            for child, path in children.get(key.lower(), []):
                try:
                    with open(path, "rb") as fh:
                        head = fh.read(16)
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if guess_media(head) and (best is None or size > best[2]):
                    best = (child, path, size)
            if best:
                with open(best[1], "rb") as fh:
                    data = fh.read()
                note = (f"CACHE_KEY {key} is a bundle: the file named after the key is its descriptor "
                        f"and the content is in child files. This attachment is its largest media "
                        f"child, {key}_{best[0]}; the cache_controller entry lists every child with "
                        f"its own hashes.")
        if data is None and message_blobs:
            # not plaintext in any shape: try the key / IV pairs the message itself carries, on the
            # whole file, the rebuilt shards and each bundle child (largest first)
            pairs = []
            for conv, smid in claims_of.get(key.lower(), []):
                pairs += message_key_pairs(message_blobs.get((conv, smid)))
            candidates = []
            if raw_whole:
                candidates.append((key, raw_whole))
            if parts.get(key.lower()):
                shards, _fulls, _paths, _coverage = _resolve_sccontent(key, {}, parts)
                if shards:
                    candidates.append((f"{key} (its byte-range shards, concatenated)", shards))
            for child, path in sorted(children.get(key.lower(), []),
                                      key=lambda cp: -os.path.getsize(cp[1])):
                try:
                    with open(path, "rb") as fh:
                        candidates.append((f"{key}_{child}", fh.read()))
                except OSError:
                    continue
            for label, raw in candidates if pairs else ():
                plain, _pair = _decrypt_with(raw, pairs)
                if plain is not None:
                    data = plain
                    note = (f"The cached file {label} is encrypted. It was decrypted (AES-256-CBC) "
                            f"with a key / IV pair carried in the content of the message its claim "
                            f"names (arroyo.db conversation_message.message_content), and kept "
                            f"because the result is media. The cache_controller entry shows the "
                            f"file as stored.")
                    break
        if data is None:
            continue
        with open(os.path.join(dest, key), "wb") as fh:
            fh.write(data)
        _MATERIALIZED[key.lower()] = note
        made += 1
    if made:
        logger.info(f"Chat media: {made} attachment(s) rebuilt from byte-range shards, taken from a "
                    f"bundle's child file or decrypted with the key their message carries — which a "
                    f"whole-file lookup would have missed")
    return made


def chat_cache_key(basename):
    """``cache_key_for`` for the Conversations report: the iOS answer, unless the attachment was
    rebuilt by :func:`materialize_chat_media`, in which case how it was rebuilt is the explanation."""
    key = str(basename).split(".")[0]
    note = _MATERIALIZED.get(key.lower())
    if note and _KEY_RE.match(key):
        return key, note
    return shared.cacheControllerKey(basename)


def read_messages(layout, owner, friends_df, staging_dir):
    """``arroyo.db`` joined to the cache, as the message frame the Conversations report takes.

    The same steps and the same functions as the iOS parser — the database is the same one. Two
    things are set up first because those functions read them from their module: the account the run
    identified (claims of another account are kept out of this one's conversations) and the cache
    folders chat media is copied out of. There is no ``SCPersistentMedia`` on Android, so that half of
    the join is empty.
    """
    arroyo = layout.db("arroyo")
    if not arroyo:
        logger.warning("arroyo.db not present — no conversations can be reported")
        return None
    shared.uuid = owner.get("user_id") or ""
    shared.outputDir = staging_dir
    shared.SCContentFolder = [d.rstrip("/") + "/" for d in layout.sccontent_dirs]
    shared.persistent_cache_keys = {}
    os.makedirs(os.path.join(staging_dir, "cacheFiles"), exist_ok=True)

    chats_df = shared.getChats(arroyo)
    chats_df = shared.fixSenders(chats_df, friends_df, pd.DataFrame({}))
    cache_path = layout.db("cache_controller")
    if cache_path:
        cache_df = shared.getCache(cache_path)
        if cache_df is None:
            cache_df = shared.empty_cache_frame()
    else:
        logger.info("No cache_controller.db — chat media cannot be matched to messages")
        cache_df = shared.empty_cache_frame()
    # Sharded and bundled chat media, rebuilt into a folder the shared join searches FIRST: it keeps
    # the first file it finds under a name, and for a bundle the real folder's file of that name is
    # the descriptor. Only keys with no whole media file are rebuilt, so nothing real is shadowed.
    rebuilt = os.path.join(staging_dir, "rebuilt")
    blobs = {}
    try:
        rows, _info = sqlite_open.read_sql(
            arroyo, "select client_conversation_id as c, server_message_id as s, message_content as b "
                    "from conversation_message where server_message_id is not null")
        for conv, smid, blob in zip(rows.get("c", []), rows.get("s", []), rows.get("b", [])):
            if blob is not None:
                blobs.setdefault((str(conv), str(int(smid))), blob)
    except Exception as error:                                 # noqa: BLE001 — keys are optional
        logger.debug(f"Could not read message contents for their media keys: {error}")
    if materialize_chat_media(cache_df, layout.app, rebuilt, blobs):
        shared.SCContentFolder = [rebuilt.replace("\\", "/") + "/"] + shared.SCContentFolder
    cache_df = shared.mergeCache(cache_df, shared.empty_cache_frame())
    cache_arroyo_df = shared.getCacheArroyo(arroyo, cache_df)
    final_df = shared.mergeCacheChats(cache_df, chats_df, _empty_persistent(), cache_arroyo_df)
    final_df = final_df.drop_duplicates()
    final_df = final_df.sort_values(by=["Client Conversation ID", "Server Message ID"])
    final_df = final_df.rename(columns={"Creation Timestamp": "Creation Timestamp UTC+0",
                                        "Read Timestamp": "Read Timestamp UTC+0"})
    wanted = ["Client Conversation ID", "Server Conversation ID", "Sender ID", "Sender User ID",
              "Message Content", "Message Text", "Content Type", "Content Type (arroyo)",
              "Creation Timestamp UTC+0", "Read Timestamp UTC+0", "Server Message ID",
              "Client Message ID", "WAL View"]
    final_df = final_df[[c for c in wanted if c in final_df.columns]]

    # the attachments no message ended up pointing at are not the report's to publish
    referenced = set(final_df["Message Content"].astype(str)) if len(final_df) else set()
    cache_dir = os.path.join(staging_dir, "cacheFiles")
    for name in os.listdir(cache_dir):
        if name not in referenced:
            try:
                os.remove(os.path.join(cache_dir, name))
            except OSError:
                pass
    return final_df


# --------------------------------------------------------------------------- the run

def _source_artifacts(layout, keychain):
    """``{role: path}`` for the fingerprint manifest — what decides what the reports contain."""
    prefs = {}
    for path in layout.shared_prefs:
        entries = android_layout.read_prefs(path)
        # the preference files the account block quotes from, and nothing else
        if os.path.basename(path) in _ACCOUNT_FILES or any(
                _ACCOUNT_KEY_RE.search(k) for k, _t, _v in entries):
            prefs[f"shared_prefs:{os.path.basename(path)}"] = path
    out = {"arroyo": layout.db("arroyo"), "cache_controller": layout.db("cache_controller"),
           "main_db": layout.db("main") or layout.db("tcspahn"), "memories_db": layout.db("memories"),
           "core_db": layout.db("core"), "keychain": keychain or ""}
    out.update(prefs)
    return out


def main(extracted_root, keychain="", padding="both", tz="local", report_dir=None, tile_server="",
         zip_path="", hash_zip=False, legacy_reports=False):
    """Build every Android report from an extraction folder (what ``extract_zip`` wrote).

    ``keychain`` is accepted for symmetry with the iOS run and recorded in the fingerprint manifest;
    nothing on the Android side needs one yet (see docs/snapchat_android.md).
    """
    from scripts.memories_media_report import make_time_formatter

    report_dir = report_dir or ("./Report_" + datetime.today().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(report_dir, exist_ok=True)
    layouts = android_layout.discover(extracted_root)
    if not layouts:
        from scripts.data.extract_zip import SnapchatNotFound
        raise SnapchatNotFound(f"No Snapchat for Android data folder under {extracted_root}")
    if len(layouts) > 1:
        logger.warning(f"The app is installed for {len(layouts)} Android users / profiles; the "
                       f"reports cover {layouts[0].device_path(layouts[0].app)} only — the others "
                       f"are: " + ", ".join(l.device_path(l.app) for l in layouts[1:]))
    layout = layouts[0]
    layout.log_inventory()
    # Structure only, beside the log: table and column names, row counts, preference key names and
    # file counts — no message, no value, no file name. What the app version on the device stores,
    # including what the parser does not read, without any of the evidence.
    survey = android_layout.write_survey(
        layout, os.path.join(os.path.dirname(os.path.abspath(report_dir)), "android_survey.json"))
    if survey:
        logger.info(f"Layout survey (structure only, no content): {survey}")

    sources = source_fingerprint.collect(_source_artifacts(layout, keychain), zip_path=zip_path,
                                         hash_zip=hash_zip)
    source_fingerprint.write_sources(report_dir, sources)
    n_found = sum(1 for r in sources["artifacts"].values() if r.get("present"))
    logger.info(f"Sources: {n_found} of {len(sources['artifacts'])} artifact(s) present, digest "
                f"{sources['digest'][:16]}…")

    timefmt, _tz_label = make_time_formatter(tz)
    owner = owner_identity(layout, timefmt)
    friends_df, identifiers, _stats = read_contacts(layout, owner, timefmt)
    if owner["user_id"] and len(friends_df):
        mine = friends_df[friends_df["User ID"].str.lower() == owner["user_id"].lower()]
        if len(mine):
            owner["username"] = owner["username"] or _cell(mine.iloc[0]["Username"])
            owner["display_name"] = owner["display_name"] or _cell(mine.iloc[0]["Display name"])
    account = {"_title": "Account — as the app stores it on this device",
               "_note": ACCOUNT_NOTE, "_rows": owner["rows"]} if owner["rows"] else None

    staging = os.path.join(report_dir, "_chat_attachments")
    conv_index = {}
    try:
        msg_df = read_messages(layout, owner, friends_df, staging)
        if msg_df is not None:
            from scripts import conversations_report
            _report, conv_index = conversations_report.main(
                msg_df=msg_df, friends_df=friends_df, group_df=pd.DataFrame({}),
                outdir=os.path.join(report_dir, "Conversations"),
                cachefiles_dir=os.path.join(staging, "cacheFiles") + "/",
                arroyo=layout.db("arroyo"), tz=tz, owner_user_id=owner["user_id"],
                owner_username=owner["username"], cache_key_for=chat_cache_key,
                report_dir=report_dir, primary=None, identifiers=identifiers)
    except Exception as error:                                 # noqa: BLE001
        logger.error(f"Conversations report failed: {error}", exc_info=True)

    try:
        from scripts import contacts_report
        contacts_report.main(friends_df, os.path.join(report_dir, "Contacts"), conv_index=conv_index,
                             owner_user_id=owner["user_id"], owner_username=owner["username"],
                             friends_source=FRIENDS_SOURCE, tz=tz, report_dir=report_dir,
                             primary=None, identifiers=identifiers, account=account,
                             snapchatters=[])
    except Exception as error:                                 # noqa: BLE001
        logger.error(f"Contacts report failed: {error}", exc_info=True)

    if legacy_reports:
        try:
            from scripts import getCacheAndroid
            getCacheAndroid.main(layout.app, os.path.join(report_dir, "Communications_legacy"))
        except Exception as error:                             # noqa: BLE001
            logger.error(f"Legacy Communications report failed: {error}", exc_info=True)
    else:
        logger.info("Legacy reports: not produced (superseded by the Conversations and Contacts "
                    "reports). Tick «Include the legacy reports» or pass --legacy-reports yes to "
                    "have them.")

    try:
        from scripts import memories_android_report
        memories_android_report.main(layout, outdir=os.path.join(report_dir, "Memories"), tz=tz,
                                     padding=padding, tile_server=tile_server,
                                     report_dir=report_dir)
    except Exception as error:                                 # noqa: BLE001
        logger.error(f"Memories report failed: {error}", exc_info=True)

    try:
        from scripts import cache_controller_report
        cache_controller_report.main(layout.app, outdir=os.path.join(report_dir, "CacheController"),
                                     tz=tz, src_root=layout.root, report_dir=report_dir)
    except Exception as error:                                 # noqa: BLE001
        logger.error(f"cache_controller report failed: {error}", exc_info=True)

    # the parser's staging folder: the Conversations report has hard-linked what it shows into its
    # own media/ folder, so the copies here are not part of the report
    shutil.rmtree(staging, ignore_errors=True)
    return report_dir
