"""
Snapchat iOS ``cache_controller.db`` report.

``Documents/global_scoped/cachecontroller/cache_controller.db`` is Snapchat's index of every
file it has cached on the device. This report surfaces that index, one row per **physical cache
file** (``CACHE_KEY``), and links each entry to:

* the on-disk cache file(s) under ``Documents/com.snap.file_manager_*_SCContent_*`` (whole file,
  byte-range parts, or the child files of a bundle), and
* the other Snapchat Auto reports — a Memory (``Memories_report.html``) or a chat message
  (``Communications_report.html``) — with two-way anchors so you can jump between them.

Tables used (columns are read dynamically, since they vary between app versions):

* ``CACHE_FILE_CLAIM``     — the semantic claim(s) on a file: ``EXTERNAL_KEY`` (what it is),
  ``MEDIA_CONTEXT_TYPE``, ``USER_ID`` and the create/expire/delete timestamps. One physical file
  can carry several claims (e.g. ``W7_…`` and ``video~W7_…``).
* ``CACHE_FILE_METADATA``  — the physical file: ``FILE_SIZE_BYTES``, ``TYPE`` (1 file / 2 sharded
  / 3 bundle), ``SHARD_INDEX``, the ``CHILDREN`` protobuf (parts / child keys) and
  ``CONTENT_RETRIEVAL_METADATA`` (the CDN URL + content SHA-256).
* ``CACHE_FILE_SAMPLED_TOMBSTONE`` — a sample of files Snapchat has already deleted.
* ``CACHE_KEY_VIRTUALIZATION`` — a ``VIRTUAL_CACHE_KEY`` ↔ ``CACHE_KEY`` mapping. Empty in every
  extraction seen so far, so its exact meaning is **unconfirmed** — the report just lists it.

See ``docs/snapchat_ios_memories_decryption.md`` for how ``CACHE_KEY`` addresses the SCContent
cache and how ``EXTERNAL_KEY`` encodes Memory snaps.
"""

import os
import re
import sys
import json
import html
import glob
import sqlite3
import hashlib
import logging
from datetime import datetime
from urllib.parse import urlparse

# Pure helpers reused from the Memories media report (path rendering, SCContent indexing).
from scripts.memories_media_report import (
    find_app_container, find_profiles, index_sccontent, device_path,
    load_path_manifest, make_time_formatter, _collapse_part_paths,
    _scope_user, _UUID_RE, _SC_SPLIT_RE,
)

try:
    import blackboxprotobuf                                    # already a project dependency
except Exception:                                              # pragma: no cover
    blackboxprotobuf = None

logger = logging.getLogger(__name__)

# Cocoa epoch (2001-01-01) as Unix seconds — used to reuse the Memories tz/DST formatter, which
# expects a Cocoa timestamp, for the Unix-epoch-millis columns in cache_controller.db.
_COCOA_EPOCH = 978307200


def make_ms_formatter(tz):
    """Return (fmt, label) where fmt(unix_ms) -> localized time string, honouring `tz` (DST-aware).

    cache_controller.db stores Unix epoch *milliseconds*; the shared Memories formatter expects a
    Cocoa timestamp, so we convert ms -> Cocoa seconds and reuse all of its timezone handling.
    """
    cocoa_fmt, label = make_time_formatter(tz)
    def fmt(ms):
        if ms in (None, "", 0):
            return ""
        try:
            return cocoa_fmt(float(ms) / 1000.0 - _COCOA_EPOCH)
        except Exception:
            return ""
    return fmt, label


# --------------------------------------------------------------------------- classification

# MEDIA_CONTEXT_TYPE values we are confident about (from the parser and observed data); others are
# shown as their raw number. Snapchat reuses these numbers across contexts, so keep this short.
MCT_LABELS = {
    2: "Chat media", 3: "Chat media", 19: "Full media", 26: "Rendered low-res",
}

# Snap-scoped EXTERNAL_KEY prefixes -> (category, role). The trailing value is a snap UUID that
# joins to ZGALLERYSNAP.ZSNAPID (the Memory), which is how these link back to the Memories report.
_SNAP_PREFIXES = [
    ("snap-media-", "Memory media", "media"),
    ("snap-overlay-", "Memory overlay", "overlay"),
    ("snap-rendered-lowres-", "Memory thumbnail", "rendered"),
    ("snap-thumbnail-", "Memory thumbnail", "thumbnail"),
    ("g-media-", "Memory media", "media"),
]


def classify_external_key(ek, mct):
    """Return (category, snap_uuid_or_None) for one EXTERNAL_KEY.

    snap_uuid is set only for Memory-scoped keys (``snap-*-<UUID>``), so the caller can link the
    entry to a Memory. Everything else is bucketed for filtering/sorting in the report.
    """
    if not ek:
        return ("Unknown", None)
    low = ek.lower()
    for prefix, category, _role in _SNAP_PREFIXES:
        if low.startswith(prefix):
            mo = _UUID_RE.search(ek)
            return (category, mo.group(0) if mo else None)
    if "lens.data" in low or "/lens/" in low or low.startswith("lens"):
        return ("Lens", None)
    if "previewmedia" in low or "preview_thumbnail" in low:
        return ("Preview", None)
    if low.startswith("app_install"):
        return ("App install", None)
    if low.startswith("topvideo") or low.startswith("video~") or "firstframe" in low:
        return ("Video / Discover", None)
    if ek.startswith("http://") or ek.startswith("https://"):
        return ("CDN media", None)
    if mct in (2, 3):
        return ("Chat media", None)
    return ("Other", None)


def _category_of(claims):
    """Pick the most meaningful category across a physical file's claims (Memory beats Other)."""
    order = ["Memory media", "Memory overlay", "Memory thumbnail", "Chat media", "Video / Discover",
             "Lens", "Preview", "App install", "CDN media", "Other", "Unknown"]
    cats = {c["category"] for c in claims}
    for name in order:
        if name in cats:
            return name
    return next(iter(cats)) if cats else "Unknown"


# --------------------------------------------------------------------------- protobuf helpers

def _as_text(v):
    """Best-effort text for a protobuf bytes/scalar field."""
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8")
        except Exception:
            return v.hex()
    return v


def parse_children(blob):
    """Decode a CACHE_FILE_METADATA.CHILDREN protobuf into a list of {name, size, offset} dicts.

    Field 1 holds one child or a list of children; each child is {1: name, 2: {1: size, 2: {1:
    offset}}}. Names are either byte-range parts (``94208-693856`` / ``PREFETCH``) for a sharded
    file, or a child cache key for a bundle. Returns [] on anything unexpected.
    """
    if not blob or blackboxprotobuf is None:
        return []
    try:
        data, _ = blackboxprotobuf.decode_message(bytes(blob))
    except Exception:
        return []
    node = data.get("1")
    if node is None:
        return []
    items = node if isinstance(node, list) else [node]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = _as_text(it.get("1"))
        size = offset = None
        meta = it.get("2")
        if isinstance(meta, dict):
            size = meta.get("1")
            inner = meta.get("2")
            if isinstance(inner, dict):
                offset = inner.get("1")
        out.append({"name": name, "size": size, "offset": offset})
    return out


def parse_retrieval(blob):
    """Pull the CDN URL and content reference out of CONTENT_RETRIEVAL_METADATA. Returns
    {url, content_ref}.

    ``content_ref`` is protobuf field 8, whose form varies by app version / media kind: a CDN media
    token (most common — the same token found after ``/d/`` in the URL, sometimes with a ``.NNN``
    suffix), a 64-hex content SHA-256 (newer app versions), or the 32-hex CACHE_KEY (older). The
    caller labels it by inspecting the value, so we never claim a token is a hash.
    """
    if not blob or blackboxprotobuf is None:
        return {}
    try:
        data, _ = blackboxprotobuf.decode_message(bytes(blob))
    except Exception:
        return {}
    out = {}
    src = data.get("5") if isinstance(data.get("5"), dict) else data.get("6")
    if isinstance(src, dict):
        url = _as_text(src.get("1"))
        if url:
            out["url"] = url
    h = data.get("8")
    if isinstance(h, (bytes, bytearray, str)):                 # skip the rare nested-structure case
        out["content_ref"] = _as_text(h)
    return out


# --------------------------------------------------------------------------- data model

def _read_all(conn, table):
    """Read a whole table as a list of dicts, or [] if it's absent/unreadable."""
    try:
        cur = conn.execute(f"SELECT * FROM {table}")
    except sqlite3.DatabaseError as error:
        logger.debug(f"{table} not readable: {error}")
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def find_cache_controllers(app):
    """Locate every cache_controller.db under the app container."""
    return glob.glob(os.path.join(app, "Documents", "global_scoped", "cachecontroller",
                                  "cache_controller.db"))


# scdb URL columns whose CDN token addresses an SCContent cache file (CACHE_KEY = SHA256(token)[:16]).
_MEM_URL_COLS = {
    "ZMEDIADOWNLOADURL": "ZMEDIADOWNLOADURL (media)",
    "ZOVERLAYDOWNLOADURL": "ZOVERLAYDOWNLOADURL (overlay)",
    "ZTHUMBNAILDOWNLOADURL": "ZTHUMBNAILDOWNLOADURL (thumbnail)",
}


def _url_token(url):
    """Last path segment of a CDN URL (the cache token), or None."""
    if not url:
        return None
    seg = urlparse(url).path.rstrip("/").split("/")[-1]
    return seg or None


def load_memory_index(app):
    """Return three maps used to link cache entries to Memories, in priority order:

    * ``snap_ids``  : {UPPER(ZSNAPID): (ZSNAPID, user_hash)} — the primary link (a snap UUID
      embedded in a ``snap-*``/``g-media-`` EXTERNAL_KEY).
    * ``url_keys``  : {cache_key_lower: (ZSNAPID, user_hash, url_field)} — the fallback for
      CDN-downloaded media: SHA-256 of a Memory URL's token (first 16 bytes) IS the CACHE_KEY.
    * ``media_ids`` : {UPPER(ZMEDIAID): (ZSNAPID, user_hash)} — last-resort fallback for an
      EXTERNAL_KEY carrying the Memory's ZMEDIAID instead of its ZSNAPID.
    """
    snap_ids, url_keys, media_ids = {}, {}, {}
    for p in find_profiles(app):
        try:
            conn = sqlite3.connect(f"file:{p['scdb']}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cols = {r[1] for r in conn.execute("PRAGMA table_info(ZGALLERYSNAP)")}
            url_cols = [c for c in _MEM_URL_COLS if c in cols]
            has_mediaid = "ZMEDIAID" in cols
            for row in conn.execute("SELECT * FROM ZGALLERYSNAP WHERE ZSNAPID IS NOT NULL"):
                sid = str(row["ZSNAPID"])
                snap_ids[sid.upper()] = (sid, p["userHash"])
                if has_mediaid and row["ZMEDIAID"]:
                    media_ids.setdefault(str(row["ZMEDIAID"]).upper(), (sid, p["userHash"]))
                for c in url_cols:
                    tok = _url_token(row[c])
                    if tok:
                        ck = hashlib.sha256(tok.encode()).hexdigest()[:32]
                        url_keys.setdefault(ck.lower(), (sid, p["userHash"], _MEM_URL_COLS[c]))
            conn.close()
        except sqlite3.DatabaseError as error:
            logger.debug(f"Could not read memory index from {p['scdb']}: {error}")
    return {"snap_ids": snap_ids, "url_keys": url_keys, "media_ids": media_ids}


def load_chat_links(report_dir):
    """Load the chat attachment manifest written by the Communications report, if present.

    Maps CACHE_KEY -> [{conversation_id, server_message_id}]. Empty when the Communications report
    didn't run or produced no cached attachments.
    """
    for cand in (os.path.join(report_dir or "", "Communications", "cache_links.json"),):
        if cand and os.path.isfile(cand):
            try:
                with open(cand, encoding="utf-8") as f:
                    return json.load(f) or {}
            except Exception as error:
                logger.debug(f"Could not read chat link manifest {cand}: {error}")
    return {}


def _resolve_on_disk(cache_key, children, scfull, scparts):
    """Resolve a cache key to on-disk source paths + total bytes present.

    Looks for a whole ``<cache_key>`` file, its byte-range parts, and — for bundles — the files of
    each named child key. Returns (paths, bytes_on_disk, found_bool, scope_by_path), where
    scope_by_path maps each path to the SCContent account UUID it physically lives under.
    """
    paths, total = [], 0
    seen = set()
    scope_by_path = {}

    def add(p):
        nonlocal total
        rp = p.replace("\\", "/")
        if rp in seen:
            return
        seen.add(rp)
        paths.append(p)
        scope_by_path[p] = _scope_user(p)
        try:
            total += os.path.getsize(p)
        except OSError:
            pass

    for p in scfull.get(cache_key, []):
        add(p)
    for _off, p in sorted(scparts.get(cache_key.lower(), [])):
        add(p)
    # bundle child keys (a 32-hex name, optionally with a leading marker byte like 'z')
    for ch in children:
        name = ch.get("name") or ""
        if _SC_SPLIT_RE.match(name):                           # byte-range part, handled via scparts
            continue
        key = name[1:] if name[:1].isalpha() and len(name) == 33 else name
        if re.fullmatch(r"[0-9a-fA-F]{32}", key or ""):
            for p in scfull.get(key, []):
                add(p)
            for _off, p in sorted(scparts.get(key.lower(), [])):
                add(p)
    return paths, total, bool(paths), scope_by_path


def build_entries(db, app, scfull, scparts, mem_index, chat_links, ms_fmt):
    """Build one entry dict per physical cache file (CACHE_KEY) from a cache_controller.db.

    Returns (entries, virtualization_rows). Each entry aggregates its claims, metadata, on-disk
    resolution and cross-report links.
    """
    snap_ids = mem_index["snap_ids"]
    url_keys = mem_index["url_keys"]
    media_ids = mem_index["media_ids"]
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    claims = _read_all(conn, "CACHE_FILE_CLAIM")
    metas = _read_all(conn, "CACHE_FILE_METADATA")
    tombstones = _read_all(conn, "CACHE_FILE_SAMPLED_TOMBSTONE")
    virtual = _read_all(conn, "CACHE_KEY_VIRTUALIZATION")
    conn.close()

    meta_by_key = {}
    for m in metas:
        meta_by_key.setdefault(m.get("CACHE_KEY"), m)          # first row wins per physical file

    # group claims by physical file
    by_key = {}
    for c in claims:
        key = c.get("CACHE_KEY")
        if not key:
            continue
        ek = c.get("EXTERNAL_KEY") or ""
        mct = c.get("MEDIA_CONTEXT_TYPE")
        category, snap_uuid = classify_external_key(ek, mct)
        by_key.setdefault(key, []).append({
            "external_key": ek,
            "mct": mct,
            "user_id": c.get("USER_ID") or "",
            "category": category,
            "snap_uuid": snap_uuid,
            "is_authoritative": c.get("IS_AUTHORITATIVE"),
            "created": ms_fmt(c.get("CREATION_TIMESTAMP_MILLIS")),
            "created_sort": c.get("CREATION_TIMESTAMP_MILLIS") or 0,
            "expires": ms_fmt(c.get("EXPIRATION_TIMESTAMP_MILLIS")),
            "deleted": ms_fmt(c.get("DELETED_TIMESTAMP_MILLIS")),
        })

    tomb_by_key = {}
    for t in tombstones:
        tomb_by_key.setdefault(t.get("CACHE_KEY"), []).append({
            "mct": t.get("MEDIA_CONTEXT_TYPE"),
            "reason": t.get("DELETION_REASON"),
            "bytes": t.get("BYTES_DELETED"),
            "deleted": ms_fmt(t.get("DELETED_TIMESTAMP_MILLIS")),
            "user_id": t.get("USER_ID") or "",
        })

    entries = []
    all_keys = set(by_key) | set(tomb_by_key)
    for key in all_keys:
        clist = by_key.get(key, [])
        meta = meta_by_key.get(key, {})
        children = parse_children(meta.get("CHILDREN"))
        retrieval = parse_retrieval(meta.get("CONTENT_RETRIEVAL_METADATA"))
        paths, disk_bytes, found, scope_by_path = _resolve_on_disk(key, children, scfull, scparts)

        # cross-report links to a Memory, in priority order, recording how the link was made
        memory, basis = None, None
        for c in clist:                                        # 1. snap UUID in the EXTERNAL_KEY
            if c["snap_uuid"] and c["snap_uuid"].upper() in snap_ids:
                canonical, user_hash = snap_ids[c["snap_uuid"].upper()]
                memory = {"snap_id": canonical, "user_hash": user_hash}
                basis = (f"The claim EXTERNAL_KEY \"{c['external_key']}\" embeds this Memory's "
                         f"ZSNAPID ({canonical}) — the primary, most direct link.")
                break
        if not memory and key.lower() in url_keys:             # 2. CDN URL token == CACHE_KEY
            canonical, user_hash, field = url_keys[key.lower()]
            memory = {"snap_id": canonical, "user_hash": user_hash}
            basis = (f"Fallback: this file's CACHE_KEY equals SHA-256 of the CDN token in this "
                     f"Memory's {field} (first 16 bytes) — i.e. it is the downloaded copy of that "
                     f"media, even though no snap-scoped claim names the Memory.")
        if not memory:                                         # 3. ZMEDIAID in an EXTERNAL_KEY
            for c in clist:
                mo = _UUID_RE.search(c["external_key"])
                if mo and mo.group(0).upper() in media_ids and mo.group(0).upper() not in snap_ids:
                    canonical, user_hash = media_ids[mo.group(0).upper()]
                    memory = {"snap_id": canonical, "user_hash": user_hash}
                    basis = (f"Fallback: EXTERNAL_KEY UUID {mo.group(0)} matches this Memory's "
                             f"ZMEDIAID (Memory {canonical}).")
                    break
        chats = chat_links.get(key, [])

        users = sorted({c["user_id"] for c in clist if c["user_id"]}
                       or {t["user_id"] for t in tomb_by_key.get(key, []) if t["user_id"]})
        created_sort = min((c["created_sort"] for c in clist if c["created_sort"]), default=0)

        # cross-scope on-disk copies: a physical copy sitting in a *different* account's SCContent
        # folder than any account that claims this file. Untracked/materialized duplicates (e.g. a
        # consolidated copy in the active account's scope) — the claim's USER_ID stays authoritative.
        claim_users_lc = {c["user_id"].lower() for c in clist if c["user_id"]}
        cross_scope = sorted({s for s in scope_by_path.values()
                              if s and claim_users_lc and s.lower() not in claim_users_lc})

        entries.append({
            "cache_key": key,
            "category": _category_of(clist) if clist else "Deleted (tombstone)",
            "claims": clist,
            "users": users,
            "meta": {
                "size": meta.get("FILE_SIZE_BYTES"),
                "disk_used": meta.get("TOTAL_DISK_USED_BYTES"),
                "type": meta.get("TYPE"),
                "storage_type": meta.get("STORAGE_TYPE"),
                "shard_index": meta.get("SHARD_INDEX"),
                "last_read": ms_fmt(meta.get("LAST_READ_TIMESTAMP_MILLIS")),
                "known_len": meta.get("KNOWN_CONTENT_LENGTH_BYTES"),
            },
            "children": children,
            "retrieval": retrieval,
            "on_disk": {"paths": paths, "bytes": disk_bytes, "found": found,
                        "scope_by_path": scope_by_path, "cross_scope": cross_scope},
            "memory": memory,
            "memory_basis": basis,
            "chats": chats,
            "tombstones": tomb_by_key.get(key, []),
            "created_sort": created_sort,
        })

    entries.sort(key=lambda e: (e["category"], -e["created_sort"], e["cache_key"]))
    return entries, virtual


# --------------------------------------------------------------------------- HTML

TYPE_LABELS = {1: "file", 2: "sharded", 3: "bundle"}


def _fmt_bytes(n):
    if not isinstance(n, (int, float)) or not n:
        return ""
    if n < 1024:
        return f"{int(n)} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _mct_label(mct):
    if mct in (None, ""):
        return ""
    lbl = MCT_LABELS.get(mct)
    return f"{mct} ({lbl})" if lbl else str(mct)


def _esc(v):
    return html.escape(str(v)) if v not in (None, "") else ""


def _info(text):
    """A small round '?' the examiner can click for an explanation of how a link/entry was made."""
    if not text:
        return ""
    return ('<span class="hint"><span class="qm" onclick="hint(event,this)">?</span>'
            f'<span class="tip">{html.escape(text)}</span></span>')


def _cross_scope_basis(entry):
    """Explanation for the cross-scope warning: a copy in another account's SCContent scope."""
    users = entry["on_disk"].get("cross_scope") or []
    claimants = sorted({c["user_id"] for c in entry["claims"] if c["user_id"]})
    return (f"{len(users)} on-disk copy(ies) sit in a different account's SCContent scope "
            f"({', '.join(users)}) than the account(s) that claim this file "
            f"({', '.join(claimants) or 'none'}). This is typically an untracked/materialized "
            "duplicate (e.g. a consolidated copy in the active account's cache) — cache_controller.db "
            "does not claim it there. The claim's USER_ID remains authoritative for ownership, so a "
            "copy's containing SCContent_<userId> folder is NOT a reliable owner.")


def _on_disk_basis(entry):
    """Explanation text for how (and whether) the cache file was located on disk."""
    if entry["on_disk"]["found"]:
        n = len(entry["on_disk"]["paths"])
        base = ("The CACHE_KEY is the on-disk filename inside a com.snap.file_manager_*_SCContent_* "
                "folder. Sharded media is stored as <CACHE_KEY>_<start>-<end> byte-range parts "
                "(plus a PREFETCH chunk) which are concatenated in offset order; bundle children "
                f"are resolved by their own cache key. {n} file(s) matched here.")
        if entry["on_disk"].get("cross_scope"):
            base += " ⚠ " + _cross_scope_basis(entry)
        return base
    return ("No file named after this CACHE_KEY (or its parts/children) was found in any "
            "SCContent folder — the claim exists in the index but the bytes are not on disk "
            "(evicted, or not captured by the extraction).")


def _links_html(entry, rel_prefix):
    """Cross-report link chips (Memory / chat) plus the on-disk found/missing chip, each with a
    clickable '?' explaining how the association was derived."""
    chips = []
    if entry["memory"]:
        sid = entry["memory"]["snap_id"]
        chips.append(f'<a class="chip mem" target="scauto_memories" '
                     f'href="{rel_prefix}Memories/Memories_report.html#mem-{_esc(sid)}">'
                     f'🧠 Memory {_esc(sid[:8])}…</a>' + _info(entry.get("memory_basis")))
    for ch in entry["chats"]:
        conv = ch.get("conversation_id", "")
        smid = ch.get("server_message_id", "")
        basis = ("This CACHE_KEY is the attachment file the Communications report recorded for "
                 f"message {smid or '(unknown)'} in conversation {conv or '(unknown)'} "
                 "(via its local_message_references / content-type mapping, exported to "
                 "cache_links.json).")
        chips.append(f'<a class="chip chat" target="scauto_comms" '
                     f'href="{rel_prefix}Communications/Communications_report.html#cf-{_esc(entry["cache_key"])}">'
                     f'💬 Chat{" " + _esc(conv[:8]) + "…" if conv else ""}</a>' + _info(basis))
    if entry["on_disk"]["found"]:
        chips.append('<span class="chip ok">📁 on disk</span>' + _info(_on_disk_basis(entry)))
    elif entry["claims"]:
        chips.append('<span class="chip miss">— not on disk</span>' + _info(_on_disk_basis(entry)))
    if entry["on_disk"].get("cross_scope"):
        chips.append('<span class="chip warn">⚠ cross-scope copy</span>'
                     + _info(_cross_scope_basis(entry)))
    return "".join(chips)


def _detail_html(entry, rel_prefix, src_root, manifest):
    """Expandable detail block for one physical cache file."""
    e = entry
    parts = []

    # claims
    rows = []
    for c in e["claims"]:
        rows.append(f"<tr><td class='mono'>{_esc(c['external_key'])}</td>"
                    f"<td>{_esc(_mct_label(c['mct']))}</td><td class='mono'>{_esc(c['user_id'])}</td>"
                    f"<td>{_esc(c['category'])}</td><td>{_esc(c['created'])}</td>"
                    f"<td>{_esc(c['expires'])}</td><td>{_esc(c['deleted'])}</td></tr>")
    if rows:
        parts.append("<div class='sect'>Claims (CACHE_FILE_CLAIM)</div>"
                     "<table class='sub'><tr><th>EXTERNAL_KEY</th><th>Context type</th><th>User</th>"
                     "<th>Category</th><th>Created</th><th>Expires</th><th>Deleted</th></tr>"
                     + "".join(rows) + "</table>")

    # metadata grid
    m = e["meta"]
    grid = [("Physical type", f"{m['type']} ({TYPE_LABELS.get(m['type'], '?')})" if m["type"] is not None else ""),
            ("File size", _fmt_bytes(m["size"])),
            ("Disk used", _fmt_bytes(m["disk_used"])),
            ("Known content length", _fmt_bytes(m["known_len"])),
            ("Storage type", m["storage_type"]),
            ("Shard index", m["shard_index"]),
            ("Last read", m["last_read"])]
    if e["retrieval"].get("url"):
        grid.append(("CDN URL", e["retrieval"]["url"]))
    ref = e["retrieval"].get("content_ref")
    if ref:
        ref = str(ref)
        if re.fullmatch(r"[0-9a-fA-F]{64}", ref):
            grid.append(("Content SHA-256", ref))
        elif ref.lower() == str(e["cache_key"]).lower():
            grid.append(("Content ref (retrieval field 8 — equals CACHE_KEY)", ref))
        else:
            grid.append(("CDN media token (retrieval field 8)", ref))
    grid_html = "".join(f"<div class='k'>{_esc(k)}</div><div class='v'>{_esc(v)}</div>"
                        for k, v in grid if v not in (None, ""))
    parts.append(f"<div class='sect'>Metadata (CACHE_FILE_METADATA)</div><div class='grid'>{grid_html}</div>")

    # children
    if e["children"]:
        crows = []
        for ch in e["children"]:
            crows.append(f"<tr><td class='mono'>{_esc(ch['name'])}</td>"
                         f"<td>{_fmt_bytes(ch['size'])}</td><td>{_esc(ch['offset'])}</td></tr>")
        parts.append("<div class='sect'>Children (parts / bundle files)</div>"
                     "<table class='sub'><tr><th>Name</th><th>Size</th><th>Offset</th></tr>"
                     + "".join(crows) + "</table>")

    # on-disk paths, grouped by the SCContent account scope each copy lives in, so a copy in a
    # different account's scope than the claim (a cross-scope duplicate) is visually flagged.
    if e["on_disk"]["paths"]:
        sbp = e["on_disk"].get("scope_by_path", {})
        cross = set(e["on_disk"].get("cross_scope") or [])
        groups = {}
        for p in e["on_disk"]["paths"]:
            groups.setdefault(sbp.get(p) or "(unknown scope)", []).append(p)
        blocks = []
        for scope, plist in sorted(groups.items(), key=lambda kv: (kv[0] in cross, kv[0])):
            collapsed = _collapse_part_paths(device_path(p, src_root, manifest) for p in plist)
            listed = "<br>".join(_esc(pp) for pp in collapsed)
            badge = ((" <span class='xscope'>⚠ different account scope</span>" + _info(_cross_scope_basis(e)))
                     if scope in cross else "")
            blocks.append(f"<div class='scopehdr'>SCContent scope: <span class='mono'>{_esc(scope)}</span>"
                          f"{badge}</div><div class='paths'>{listed}</div>")
        parts.append(f"<div class='sect'>Cache file(s) on disk — {_fmt_bytes(e['on_disk']['bytes'])} present</div>"
                     + "".join(blocks))
    else:
        parts.append("<div class='sect'>Cache file(s) on disk</div>"
                     "<div class='muted'>no matching file found in the SCContent folders</div>")

    # tombstones
    if e["tombstones"]:
        trows = []
        for t in e["tombstones"]:
            trows.append(f"<tr><td>{_esc(_mct_label(t['mct']))}</td><td>{_esc(t['reason'])}</td>"
                         f"<td>{_fmt_bytes(t['bytes'])}</td><td>{_esc(t['deleted'])}</td></tr>")
        parts.append("<div class='sect'>Deletion record (CACHE_FILE_SAMPLED_TOMBSTONE)</div>"
                     "<table class='sub'><tr><th>Context type</th><th>Reason</th><th>Bytes deleted</th>"
                     "<th>Deleted</th></tr>" + "".join(trows) + "</table>")

    links = _links_html(e, rel_prefix)
    if links:
        parts.append(f"<div class='sect'>Links</div><div class='chips'>{links}</div>")
    return "".join(parts)


def _external_key_summary(claims):
    """A compact EXTERNAL_KEY summary for the main row (first key + count)."""
    keys = [c["external_key"] for c in claims if c["external_key"]]
    if not keys:
        return ""
    first = keys[0]
    if len(first) > 60:
        first = first[:60] + "…"
    extra = f" <span class='more'>+{len(keys) - 1}</span>" if len(keys) > 1 else ""
    return _esc(first) + extra


def generate_report(entries, virtual, outdir, tz_label, rel_prefix, src_root, manifest,
                    db_display):
    total = len(entries)
    on_disk = sum(1 for e in entries if e["on_disk"]["found"])
    mem_linked = sum(1 for e in entries if e["memory"])
    chat_linked = sum(1 for e in entries if e["chats"])
    deleted = sum(1 for e in entries if e["tombstones"])
    xscope = sum(1 for e in entries if e["on_disk"].get("cross_scope"))
    categories = sorted({e["category"] for e in entries})

    rows = []
    for i, e in enumerate(entries):
        m = e["meta"]
        anchor = f"ck-{e['cache_key']}"
        # sharded files report FILE_SIZE_BYTES=0; fall back to the known content length / disk use
        eff_size = m["size"] or m["known_len"] or m["disk_used"] or 0
        type_lbl = TYPE_LABELS.get(m["type"], "") if m["type"] is not None else ""
        disk = "yes" if e["on_disk"]["found"] else ("no" if e["claims"] else "")
        linkbits = []
        if e["memory"]:
            linkbits.append("Memory")
        if e["chats"]:
            linkbits.append("Chat")
        links_col = ", ".join(linkbits)
        is_xscope = bool(e["on_disk"].get("cross_scope"))
        disk_cell = ('📁' + (" <span class='xwarn'>⚠</span>" if is_xscope else "")) if e["on_disk"]["found"] \
                    else ('—' if e["claims"] else '')
        detail = _detail_html(e, rel_prefix, src_root, manifest)
        rows.append(
            f"<tr class='row' id='{anchor}' data-cat='{_esc(e['category'])}' "
            f"data-disk='{disk}' data-link='{_esc(links_col)}' data-xscope='{'yes' if is_xscope else 'no'}' "
            f"onclick='tog(this)'>"
            f"<td class='tog'>▸</td>"
            f"<td>{_esc(e['category'])}</td>"
            f"<td class='mono key'>{_esc(e['cache_key'])}</td>"
            f"<td class='mono'>{_esc(', '.join(u[:8] + '…' for u in e['users']))}</td>"
            f"<td class='ek'>{_external_key_summary(e['claims'])}</td>"
            f"<td>{_esc(type_lbl)}</td>"
            f"<td data-sort='{eff_size}'>{_fmt_bytes(eff_size)}</td>"
            f"<td class='c'>{disk_cell}</td>"
            f"<td>{_links_html(e, rel_prefix)}</td></tr>"
            f"<tr class='detail' id='d-{i}'><td></td><td colspan='8'>{detail}</td></tr>")

    # virtualization section (unconfirmed semantics — listed only)
    virt_html = ""
    if virtual:
        vrows = "".join(
            f"<tr><td class='mono'>{_esc(v.get('VIRTUAL_CACHE_KEY'))}</td>"
            f"<td class='mono'>{_esc(v.get('CACHE_KEY'))}</td>"
            f"<td class='mono'>{_esc(v.get('USER_ID'))}</td></tr>" for v in virtual)
        virt_html = (
            "<h2>CACHE_KEY_VIRTUALIZATION</h2>"
            "<div class='note'>The exact meaning of the VIRTUAL_CACHE_KEY ↔ CACHE_KEY mapping is "
            "<b>unconfirmed</b> (no populated sample seen yet); rows are listed as-is.</div>"
            "<table class='vtab'><tr><th>VIRTUAL_CACHE_KEY</th><th>CACHE_KEY</th><th>User</th></tr>"
            + vrows + "</table>")

    cat_opts = "".join(f"<option value='{_esc(c)}'>{_esc(c)}</option>" for c in categories)

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Snapchat cache_controller.db</title><style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f4f8;color:#1b1b1f}}
 header{{background:#2d2d71;color:#fff;padding:16px 24px}} header h1{{margin:0;font-size:20px}}
 .sum{{opacity:.85;font-size:13px;margin-top:4px}} .sum b{{color:#fff}}
 .note{{background:#fff8e0;border:1px solid #e6d48a;color:#6a5300;padding:8px 24px;font-size:12.5px}}
 .toolbar{{position:sticky;top:0;background:#ececf4;border-bottom:1px solid #d7d7e2;padding:10px 24px;
   display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:13px;z-index:5}}
 .toolbar input,.toolbar select{{font-size:13px;padding:5px 8px;border:1px solid #bcbcd0;border-radius:5px}}
 .toolbar input[type=search]{{min-width:280px}}
 .toolbar label{{color:#555;font-weight:600}}
 table.main{{border-collapse:collapse;width:100%;font-size:12.5px}}
 table.main th{{background:#1f1f52;color:#fff;text-align:left;padding:7px 10px;position:sticky;top:53px;cursor:pointer;white-space:nowrap}}
 table.main th .ar{{opacity:.5;font-size:10px}}
 table.main td{{border-bottom:1px solid #e2e2ea;padding:6px 10px;vertical-align:top}}
 tr.row{{cursor:pointer}} tr.row:hover{{background:#eef0ff}}
 td.tog{{color:#2d2d71;font-weight:700;width:14px}} tr.row.open td.tog{{color:#8a1f5a}}
 td.c{{text-align:center}} .key{{color:#33367a}}
 .mono{{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}}
 .ek{{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#555;max-width:360px;overflow-wrap:anywhere}}
 .ek .more{{background:#d7d7ee;color:#33367a;border-radius:8px;padding:0 6px;font-size:10px}}
 tr.detail{{display:none;background:#fafaff}} tr.detail.show{{display:table-row}}
 tr.detail td{{padding:10px 16px 16px}}
 .sect{{margin-top:12px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#2d2d71;
   font-weight:700;border-bottom:1px solid #e2e2ee;padding-bottom:2px}}
 .grid{{display:grid;grid-template-columns:auto 1fr;gap:2px 14px;font-size:12px;margin-top:4px;max-width:900px}}
 .grid .k{{color:#666}} .grid .v{{overflow-wrap:anywhere}}
 table.sub{{border-collapse:collapse;margin-top:5px;font-size:11.5px}}
 table.sub th{{background:#e7e7f2;color:#2d2d71;text-align:left;padding:3px 8px}}
 table.sub td{{border:1px solid #e0e0e8;padding:3px 8px;overflow-wrap:anywhere}}
 .paths{{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#555;margin-top:4px;overflow-wrap:anywhere}}
 .muted{{color:#999}}
 .chips{{margin-top:4px}} .chip{{display:inline-block;margin:2px 6px 2px 0;padding:2px 8px;border-radius:10px;
   font-size:11px;text-decoration:none;font-weight:600}}
 .chip.mem{{background:#e7ecff;color:#25348a;border:1px solid #b9c3f0}}
 .chip.chat{{background:#e7f6ea;color:#1f6b39;border:1px solid #b3ddc0}}
 .chip.ok{{background:#eef7ee;color:#2f7d32}} .chip.miss{{background:#f6efef;color:#9a5a5a}}
 .chip.warn{{background:#fff3d6;color:#8a5a00;border:1px solid #e6c983}}
 .xwarn{{color:#b8860b;font-weight:700}}
 .scopehdr{{margin-top:6px;font-size:11px;color:#444;font-weight:600}}
 .xscope{{background:#fff3d6;color:#8a5a00;border:1px solid #e6c983;border-radius:8px;padding:0 6px;font-size:10px;margin-left:6px}}
 .hint{{position:relative;display:inline-block}}
 .qm{{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;border-radius:50%;
   background:#c9cdf0;color:#25348a;font-size:10px;font-weight:700;cursor:pointer;margin:0 4px;user-select:none;vertical-align:middle}}
 .qm:hover{{background:#2d2d71;color:#fff}}
 .tip{{display:none;position:absolute;left:20px;top:-4px;z-index:30;background:#1f1f52;color:#fff;padding:8px 11px;
   border-radius:6px;font-size:11.5px;font-weight:400;width:340px;box-shadow:0 3px 10px rgba(0,0,0,.35);line-height:1.45}}
 .hint.open .tip{{display:block}}
 h2{{margin:24px 0 0;padding:10px 24px;background:#1f1f52;color:#fff;font-size:15px}}
 table.vtab{{border-collapse:collapse;width:100%;font-size:12px}} table.vtab td{{border-bottom:1px solid #e2e2ea;padding:5px 24px}}
 table.vtab th{{background:#1f1f52;color:#fff;text-align:left;padding:6px 24px}}
</style></head><body>
<header><h1>Snapchat cache_controller.db</h1>
 <div class="sum">{total} physical cache files &middot; <b>{on_disk}</b> present on disk &middot;
 <b>{mem_linked}</b> linked to a Memory &middot; <b>{chat_linked}</b> linked to a chat &middot;
 <b>{xscope}</b> with a cross-scope copy &middot; {deleted} with a deletion record &middot;
 times in <b>{html.escape(tz_label)}</b></div>
 <div class="sum">Source: {html.escape(db_display)}</div></header>
<div class="toolbar">
 <input type="search" id="q" placeholder="Search cache key, EXTERNAL_KEY, category, user…" oninput="flt()">
 <label>Category <select id="cat" onchange="flt()"><option value="">all</option>{cat_opts}</select></label>
 <label>On disk <select id="disk" onchange="flt()"><option value="">any</option>
   <option value="yes">on disk</option><option value="no">not on disk</option></select></label>
 <label>Linked <select id="link" onchange="flt()"><option value="">any</option>
   <option value="Memory">Memory</option><option value="Chat">Chat</option></select></label>
 <label title="Only files with an on-disk copy in a different account's SCContent scope than the claim">
   <input type="checkbox" id="xscope" onchange="flt()"> ⚠ cross-scope only</label>
 <span id="count" style="color:#555"></span>
</div>
<table class="main" id="tbl">
 <thead><tr>
  <th></th>
  <th onclick="srt(1)">Category <span class="ar">↕</span></th>
  <th onclick="srt(2)">CACHE_KEY <span class="ar">↕</span></th>
  <th onclick="srt(3)">User <span class="ar">↕</span></th>
  <th onclick="srt(4)">EXTERNAL_KEY <span class="ar">↕</span></th>
  <th onclick="srt(5)">Type <span class="ar">↕</span></th>
  <th onclick="srt(6)">Size <span class="ar">↕</span></th>
  <th onclick="srt(7)">On disk <span class="ar">↕</span></th>
  <th>Links</th>
 </tr></thead>
 <tbody>{''.join(rows)}</tbody>
</table>
{virt_html}
<script>
function hint(ev,el){{ev.stopPropagation();
 var h=el.parentNode,was=h.classList.contains('open');
 document.querySelectorAll('.hint.open').forEach(function(x){{x.classList.remove('open');}});
 if(!was)h.classList.add('open');}}
document.addEventListener('click',function(){{
 document.querySelectorAll('.hint.open').forEach(function(x){{x.classList.remove('open');}});}});
function tog(r){{r.classList.toggle('open');var d=r.nextElementSibling;
 if(d&&d.classList.contains('detail'))d.classList.toggle('show');}}
function flt(){{
 var q=document.getElementById('q').value.toLowerCase();
 var cat=document.getElementById('cat').value, disk=document.getElementById('disk').value,
     lk=document.getElementById('link').value, xs=document.getElementById('xscope').checked;
 var rows=document.querySelectorAll('#tbl tbody tr.row'), n=0;
 rows.forEach(function(r){{
  var d=r.nextElementSibling;
  var txt=r.textContent.toLowerCase()+' '+(d?d.textContent.toLowerCase():'');
  var ok=(!q||txt.indexOf(q)>-1)&&(!cat||r.dataset.cat===cat)&&(!disk||r.dataset.disk===disk)
       &&(!lk||(r.dataset.link||'').indexOf(lk)>-1)&&(!xs||r.dataset.xscope==='yes');
  r.style.display=ok?'':'none';
  if(d&&d.classList.contains('detail')&&!ok){{d.classList.remove('show');r.classList.remove('open');}}
  if(ok)n++;
 }});
 document.getElementById('count').textContent=n+' shown';
}}
function srt(col){{
 var tb=document.querySelector('#tbl tbody');
 var pairs=[];var rows=tb.querySelectorAll('tr.row');
 rows.forEach(function(r){{pairs.push([r,r.nextElementSibling]);}});
 var dir=tb.getAttribute('data-dir')==='asc'?1:-1;tb.setAttribute('data-dir',dir===1?'desc':'asc');
 pairs.sort(function(a,b){{
  var ca=a[0].children[col],cb=b[0].children[col];
  var va=ca.getAttribute('data-sort'),vb=cb.getAttribute('data-sort');
  if(va!==null&&vb!==null){{return (Number(va)-Number(vb))*dir;}}
  return ca.textContent.localeCompare(cb.textContent)*dir;
 }});
 pairs.forEach(function(p){{tb.appendChild(p[0]);if(p[1])tb.appendChild(p[1]);}});
}}
flt();
if(location.hash){{var el=document.querySelector(location.hash.replace(/[^#\\w-]/g,''));
 if(el&&el.classList.contains('row')){{tog(el);el.scrollIntoView();}}}}
</script>
</body></html>"""

    os.makedirs(outdir, exist_ok=True)
    report = os.path.join(outdir, "CacheController_report.html")
    with open(report, "w", encoding="utf-8") as f:
        f.write(doc)
    return report, {"total": total, "on_disk": on_disk, "mem": mem_linked,
                    "chat": chat_linked, "deleted": deleted}


# --------------------------------------------------------------------------- entry

def main(app_or_root, outdir=None, tz="local", src_root=None, report_dir=None):
    """
    Build a cache_controller.db report.

    app_or_root : Snapchat app-container path, or any extraction root containing it.
    outdir      : output directory (default: ./Snapchat_CacheController_report_<timestamp>).
    tz          : timezone for displayed timestamps — 'local', 'utc', an IANA name, or '±HH:MM'.
    src_root    : extraction root the files were unzipped under (for archive-relative source paths).
    report_dir  : the sibling reports root (…/Reports). Used to find the Communications chat-link
                  manifest and to compute relative links to the Memories/Communications reports.
    """
    app = find_app_container(app_or_root)
    dbs = find_cache_controllers(app)
    if not dbs:
        logger.warning(f"No cache_controller.db found under {app}")
        return None

    manifest = load_path_manifest(src_root, app_or_root, app)
    outdir = outdir or ("./Snapchat_CacheController_report_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    ms_fmt, tz_label = make_ms_formatter(tz)

    scfull, scparts = index_sccontent(app)
    mem_index = load_memory_index(app)
    # report_dir defaults to the parent of outdir when the report is placed under …/Reports/CacheController
    rdir = report_dir or os.path.dirname(os.path.abspath(outdir))
    chat_links = load_chat_links(rdir)
    # links to the sibling reports are relative to CacheController_report.html (…/Reports/CacheController/)
    rel_prefix = "../"

    all_entries, virtual = [], []
    for db in dbs:
        entries, virt = build_entries(db, app, scfull, scparts, mem_index, chat_links, ms_fmt)
        all_entries.extend(entries)
        virtual.extend(virt)

    db_display = device_path(dbs[0], src_root, manifest) if dbs else ""
    report, stats = generate_report(all_entries, virtual, outdir, tz_label, rel_prefix,
                                    src_root, manifest, db_display)
    logger.info(f"cache_controller report: {os.path.abspath(report)}")
    logger.info(f"  {stats['total']} cache files, {stats['on_disk']} on disk, "
                f"{stats['mem']} linked to Memories, {stats['chat']} linked to chats, "
                f"{stats['deleted']} deleted")
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    tz, args = "local", []
    it = iter(sys.argv[1:])
    for a in it:
        if a == "--tz":
            tz = next(it, "local")
        else:
            args.append(a)
    if not args:
        print("usage: python -m scripts.cache_controller_report "
              "<extraction_root_or_app_container> [outdir] [--tz local|utc|<IANA>|<±HH:MM>]")
        sys.exit(1)
    main(args[0], args[1] if len(args) > 1 else None, tz=tz)
