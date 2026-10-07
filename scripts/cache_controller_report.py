"""
Snapchat iOS ``cache_controller.db`` report.

``Documents/global_scoped/cachecontroller/cache_controller.db`` is Snapchat's index of every
file it has cached on the device. This report surfaces that index, one row per **physical cache
file** (``CACHE_KEY``), and links each entry to:

* the on-disk cache file(s) under ``Documents/com.snap.file_manager_*_SCContent_*`` (whole file,
  byte-range parts, or the child files of a bundle), and
* the other Snapchat Auto reports — a Memory (``Memories_report.html``) or a chat message
  (``Conversations_report.html``) — with two-way anchors so you can jump between them.

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
import shutil
import sqlite3
import hashlib
import logging
from datetime import datetime
from urllib.parse import urlparse

from scripts import report_ui
from scripts import app_version
from scripts import partial_report
from scripts import android_layout
from scripts.data import sqlite_open
from scripts.data import sniff
from scripts.data import arroyo_content
from scripts.data import snap_overlay
from scripts.data import snap_session
from scripts.data import ctp_items
from scripts import memory_leads
from scripts import progress
from scripts import parallel
# Pure helpers reused from the Memories media report (path rendering, SCContent indexing).
from scripts.data import device_fs
from scripts.memories_media_report import (
    load_device_mtimes, load_fs_records, manifest_key, _collapse_paths,
    find_app_container, find_profiles, index_sccontent, device_path,
    load_path_manifest, make_time_formatter, guess_media,
    has_video_track, _scope_user, _UUID_RE, _SC_SPLIT_RE, classify_snap_claim,
    cache_controller_paths, decode_memdata, account_matches, map_userids, _account_label,
)
from scripts.data import ffmpeg_log
from scripts.data import poster_worker
from scripts.data import media_meta

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
    34: "Snap editor working copy",
}

#: Why a context is named, where the name rests on more than the number seen beside a key shape.
MCT_BASIS = {
    34: ("Named from the device's own record. The app's preference row "
         "'SnapEditor-SnapSessionContext' (Documents/user_scoped/<hash>/userPreferences/"
         "pref.docobjects) names the file(s) of the snap being edited by their CACHE_KEY, together "
         "with a claim key '<UUID>~<position>' and this context — and the claims on those files are "
         "exactly that. Where such a record survives for a file it is shown under the claims. The "
         "label says what kind of claim this is; on its own it links the file to nothing."),
}

# A claim key the snap editor writes: a UUID and the item's position in the snap.
_EDITOR_KEY = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                         r"[0-9A-Fa-f]{12}~\d+$")


def classify_external_key(ek, mct):
    """Return (category, snap_uuid_or_None) for one EXTERNAL_KEY.

    snap_uuid is set only for Memory-scoped keys, so the caller can link the entry to a Memory.
    Which shapes those are is decided by ``classify_snap_claim`` in the Memories report — the one
    list both reports read, so a file this report ties to a Memory is a file that report also
    finds, and neither can quietly recognise a shape the other drops. Everything else is bucketed
    for filtering/sorting in the report.
    """
    if not ek:
        return ("Unknown", None)
    low = ek.lower()
    snap_uuid, category, _role = classify_snap_claim(ek)
    if snap_uuid:
        return (category, snap_uuid)
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
    if mct == 34 and _EDITOR_KEY.match(ek):
        return ("Snap editor", None)
    return ("Other", None)


def _category_of(claims):
    """Pick the most meaningful category across a physical file's claims (Memory beats Other)."""
    order = ["Memory media", "Memory overlay", "Memory thumbnail", "Chat media", "Snap editor",
             "Video / Discover", "Lens", "Preview", "App install", "CDN media", "Other", "Unknown"]
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
        # field 1 is usually the child name (a byte-range part or a child cache key), but in some
        # app versions it is a nested descriptor dict — keep a name only when it is actually text.
        raw = it.get("1")
        name = _as_text(raw) if isinstance(raw, (bytes, bytearray, str)) else None
        size = offset = None
        meta = it.get("2")
        if isinstance(meta, dict):
            size = meta.get("1") if isinstance(meta.get("1"), (int, float)) else None
            inner = meta.get("2")
            if isinstance(inner, dict) and isinstance(inner.get("1"), (int, float)):
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

def _read_all(views, table):
    """Read a whole table **both with and without the database's -wal**.

    Returns ``[(row dict, wal marker)]`` — see :mod:`scripts.data.sqlite_open`. A ``main-only`` row
    is one the write-ahead log later changed or deleted, i.e. recoverable prior state rather than
    the app's current state, which is why the marker travels with the row all the way to the report.
    """
    rows, markers = sqlite_open.read_table(views, table)
    return list(zip(rows, markers))


def find_cache_controllers(app):
    """Locate every cache_controller.db under the app container — the iOS location
    (``Documents/global_scoped/cachecontroller/``) and the Android one
    (``databases/native_content_manager/``). See ``memories_media_report.cache_controller_paths``."""
    return cache_controller_paths(app)


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


def load_memory_index(app, overlays=False):
    """Return three maps used to link cache entries to Memories, in priority order:

    * ``snap_ids``  : {UPPER(ZSNAPID): (ZSNAPID, user_hash)} — the primary link (a snap UUID
      embedded in a ``snap-*``/``g-media-`` EXTERNAL_KEY).
    * ``url_keys``  : {cache_key_lower: (ZSNAPID, user_hash, url_field)} — the fallback for
      CDN-downloaded media: SHA-256 of a Memory URL's token (first 16 bytes) IS the CACHE_KEY.
    * ``media_ids`` : {UPPER(ZMEDIAID): (ZSNAPID, user_hash)} — last-resort fallback for an
      EXTERNAL_KEY carrying the Memory's ZMEDIAID instead of its ZSNAPID.

    Plus ``snap_urls`` : {ZSNAPID: [CDN URL, …]} — the Memory's download URLs, so a cache file
    linked to a Memory can be found by searching that URL (only ~1 cache entry in 3 carries a
    ``CONTENT_RETRIEVAL_METADATA`` URL of its own).

    And ``memdata_ids`` : {UPPER(uuid): {(ZSNAPID, user_hash, field), …}} — the MemData identifiers
    a Memory records about itself (``ZGALLERYSNAP.ZMEMDATAIDS``, and its entry's
    ``ZGALLERYENTRY.ZMEMDATAID``; see ``memories_media_report.decode_memdata``). An entry's id is
    shared by every snap of that entry, so a caller links through one only when it names one snap.

    And, with ``overlays=True``, ``overlay_urls`` : {asset URL: [(ZSNAPID, user_hash, asset)]} — the
    asset URLs (``snap_overlay.ASSET_FIELDS``) of every geofilter a Memory's overlay record
    (``ZGALLERYSNAPDETAIL.ZOVERLAY``) lists, keyed by ``snap_overlay.normalise_url``. Not a link to
    the Memory's media: see :func:`_overlay_links_for`. Opt-in, because decoding every record is the
    costly part of this index and only the callers that match filter assets read it (this report's
    ``index`` and the claim-link survey; the Library/Caches report does not); without it the key is
    ``{}``. iOS only — the Android index has no such key, so callers ``.get`` it.
    """
    if android_layout.is_app_dir(app):
        # the Android app keeps its Memories in memories.db, not in a Core Data store
        from scripts import memories_android_report
        return memories_android_report.memory_index(app)
    snap_ids, url_keys, media_ids, snap_urls, memdata_ids = {}, {}, {}, {}, {}
    overlay_urls = {}
    points = {}            # snap id -> {"kind", "points"}: what memory_leads compares files with
    for p in find_profiles(app):
        # Both readings, through sqlite_open like every other evidence database: staged copies, so
        # nothing is ever opened (or given a -shm) in place, and a Memory row the -wal has since
        # changed or removed still links its cache files. Current rows come first, so they win.
        views = None
        try:
            views = sqlite_open.open_views(p["scdb"])
            rows, _markers = sqlite_open.read_table(views, "ZGALLERYSNAP")
            entries, _markers = sqlite_open.read_table(views, "ZGALLERYENTRY")
            records = snap_overlay.read_overlays(views) if overlays else []
        except (sqlite3.DatabaseError, OSError) as error:
            logger.debug(f"Could not read memory index from {p['scdb']}: {error}")
            rows, entries, records = [], [], []
        finally:
            if views is not None:
                views.close()
        for rec in records:
            for asset in rec["assets"]:
                # each record's assets are its own dicts (read_overlays): marked in place, not copied
                asset["has_overlay_image"] = rec["has_overlay_image"]
                overlay_urls.setdefault(asset["key"], []).append((rec["snap_id"], p["userHash"], asset))
        for row in rows:
            if not row.get("ZSNAPID"):
                continue
            sid = str(row["ZSNAPID"])
            current = sid.upper() not in snap_ids          # the first row of a snap is the current one
            snap_ids.setdefault(sid.upper(), (sid, p["userHash"]))
            if row.get("ZMEDIAID"):
                media_ids.setdefault(str(row["ZMEDIAID"]).upper(), (sid, p["userHash"]))
            for c in _MEM_URL_COLS:
                url = row.get(c)
                if url and (current or str(url) not in snap_urls.get(sid, [])):
                    snap_urls.setdefault(sid, []).append(str(url))
                tok = _url_token(url)
                if tok:
                    ck = hashlib.sha256(tok.encode()).hexdigest()[:32]
                    url_keys.setdefault(ck.lower(), (sid, p["userHash"], _MEM_URL_COLS[c]))
            for field, rec in _memdata_of(row, entries):
                memdata_ids.setdefault(rec["uuid"], set()).add((sid, p["userHash"], field))
            if current:
                points[sid] = _memory_points(row, entries)
    return {"snap_ids": snap_ids, "url_keys": url_keys, "media_ids": media_ids,
            "snap_urls": snap_urls, "memdata_ids": memdata_ids, "points": points,
            "overlay_urls": overlay_urls}


_COCOA = 978307200


def _memory_points(row, entries):
    """``{"kind", "points"}`` of one Memory row: its kind and its times as Unix seconds."""
    kind = {0: "image", 1: "video"}.get(row.get("ZMEDIATYPE"))
    pts = [(label, row[col] + _COCOA) for col, label in (("ZCREATETIMEUTC", "ZGALLERYSNAP.ZCREATETIMEUTC"),
                                                         ("ZCAPTURETIMEUTC", "ZGALLERYSNAP.ZCAPTURETIMEUTC"))
           if isinstance(row.get(col), (int, float)) and row.get(col)]
    pk = row.get("ZENTRY")
    for entry in entries:
        if pk is not None and entry.get("Z_PK") == pk:
            if isinstance(entry.get("ZCREATETIMEUTC"), (int, float)) and entry.get("ZCREATETIMEUTC"):
                pts.append(("ZGALLERYENTRY.ZCREATETIMEUTC", entry["ZCREATETIMEUTC"] + _COCOA))
            break
    return {"kind": kind, "points": pts}


def _memdata_of(row, entries):
    """``[(field, record)]`` — the MemData identifiers of one ZGALLERYSNAP row and of its entry."""
    out = [("ZGALLERYSNAP.ZMEMDATAIDS" + (f" › {rec['slot']}" if rec["slot"] else ""), rec)
           for rec in decode_memdata(row.get("ZMEMDATAIDS")) or []]
    pk = row.get("ZENTRY")
    for entry in entries:
        if pk is not None and entry.get("Z_PK") == pk:
            out += [("ZGALLERYENTRY.ZMEMDATAID", rec)
                    for rec in decode_memdata(entry.get("ZMEMDATAID")) or []]
            break
    return out


def _memdata_link(clist, memdata_ids):
    """``(memory, basis)`` for the first claim whose EXTERNAL_KEY carries a MemData id that exactly
    one Memory records about itself, else ``(None, None)``.

    An identifier stored in the Memory's own row is a recorded reference, like rule 3's ZMEDIAID —
    not a match by time or by content. An entry's id that several snaps share names none of them.
    """
    for c in clist:
        for mo in _UUID_RE.finditer(c["external_key"] or ""):
            owners = memdata_ids.get(mo.group(0).upper()) or set()
            if len({sid for sid, _uh, _field in owners}) != 1:
                continue
            canonical, user_hash, field = sorted(owners)[0]
            return ({"snap_id": canonical, "user_hash": user_hash},
                    f"The claim EXTERNAL_KEY \"{c['external_key']}\" carries {mo.group(0)}, which "
                    f"Memory {canonical} records about itself in {field} — an identifier stored "
                    f"in the Memory's own row, not a match by time or by content.")
    return None, None


FILTER_LISTED_BASIS = (
    "A Memory's overlay record (scdb-27.sqlite3 ZGALLERYSNAPDETAIL.ZOVERLAY, an NSKeyedArchiver "
    "archive of SOJUGallerySnapOverlay) lists the snap's geofilters, and a geofilter gives the URL of "
    "its image, of its sky image and of the font of its text — and its sky item's blimpUrl, read the "
    "same way when it holds a URL. A claim whose EXTERNAL_KEY is exactly "
    "one of those URLs — the whole URL, query included, not an id inside it — is a cached asset of a "
    "filter that Memory's record lists. The record names the selected geofilter separately "
    "(filters.geoFilterSelectedId / geoFilterSelectedIds), and often names none, so a listed filter is "
    "not shown to be on the Memory. This file is not the Memory's media: it is none of the Memory "
    "links, is not counted as linked to a Memory, and is never decrypted with the Memory's key.")

FILTER_MANY_LINK_BASIS = (
    "This asset is listed in the overlay records of SEVERAL Memories, so the link opens the Memories "
    "report filtered to those Memories' snap ids, with every matching row expanded, rather than "
    "jumping to one of them. What you land on is the complete set — the search box shows the query "
    "that produced it, and clearing it restores the full report.")

FILTER_MANY_BASIS = (
    "The same asset URL is listed in the overlay records of several Memories, under the same filter "
    "or under different ones (a font, for one, can be shared by different filters). Each Memory's "
    "own basis, with the filter (idValue) that lists the asset, is in this entry's detail.")

#: How each answer of the record about the selected geofilter reads in an explanation.
_SELECTED_SENTENCE = {True: "here it names this filter", False: "here it names another filter",
                      None: "here it names none"}


def _url_difference(ek, url):
    """What differs between a claim key and the record's URL that :func:`snap_overlay.normalise_url`
    makes the same — "" when they are the same text."""
    a, b = str(ek or ""), str(url or "")
    if a == b:
        return ""
    (scheme_a, _sep, rest_a), (scheme_b, _sep, rest_b) = a.partition("://"), b.partition("://")
    bits = ["the letter case of its scheme"] if scheme_a != scheme_b else []
    if rest_a != rest_b:
        # the rule drops nothing but one empty "?" or "#", so a side it shortens ends with one
        bits += [f"the empty \"{text[-1]}\" {whose} ends with"
                 for text, whose in ((a, "the claim key"), (b, "the record"))
                 if len(snap_overlay.normalise_url(text)) < len(text)]
    return " and ".join(bits)


def _filter_facts(asset):
    """What the record says of the filter an asset belongs to: its type, carousel group and id."""
    facts = [f"a {asset['filter_type']} geofilter" if asset.get("filter_type") else "a geofilter"]
    facts += [f"carousel group {asset['group']}"] if asset.get("group") else []
    facts += [f"idValue {asset['filter_id']}"] if asset.get("filter_id") else []
    return ", ".join(facts)


def _overlay_flag_sentence(flag):
    """The Memory's own ZHASOVERLAYIMAGE, as a sentence ("" when the schema has no such column)."""
    if flag is None:
        return ""
    return (f" The Memory's own row has ZGALLERYSNAP.ZHASOVERLAYIMAGE = {flag}"
            + (" (it records an overlay image; the record does not say which listed filter, if "
               "any, is on it)." if flag else " (it records no overlay image)."))


def _filter_link_caveats(claim_user, same_account, wal):
    """Whose claim it is, when not the Memory's account, and a record only the checkpointed
    reading holds."""
    text = ""
    if same_account is False:
        text += (f" The claim was made by account {claim_user}, and this Memory is another "
                 f"account's (its userHash is not SHA-256 of that USER_ID): the same asset URL, "
                 f"cached by the claim's account.")
    if wal == sqlite_open.MAIN_ONLY:
        text += (" This overlay record is in scdb-27 only without its -wal: the write-ahead log has "
                 "since changed or removed it.")
    return text


def _filter_basis(ek, claim_user, sid, asset, same_account):
    """The explanation of one link from a cache entry to a Memory whose overlay record lists it."""
    differs = _url_difference(ek, asset["url"])
    text = (f"The claim EXTERNAL_KEY \"{ek}\" is the URL"
            + (f" \"{asset['url']}\" (the same URL but for {differs})" if differs else "")
            + f" that Memory {sid}'s overlay record gives as the {asset['role']} of one of its "
            f"geofilters: scdb-27.sqlite3 ZGALLERYSNAPDETAIL.ZOVERLAY, the row whose ZSNAP is this "
            f"Memory's Z_PK, an NSKeyedArchiver archive of SOJUGallerySnapOverlay, at "
            f"{asset['field']} ({_filter_facts(asset)}). The record lists the snap's geofilters and "
            f"names the selected one separately, in filters.geoFilterSelectedId / "
            f"geoFilterSelectedIds: {_SELECTED_SENTENCE[asset.get('selected')]}.")
    text += _overlay_flag_sentence(asset.get("has_overlay_image"))
    text += (" So this cached file is an asset of a filter listed with the Memory. It is not the "
             "Memory's media, and a listed filter is not shown to be on the Memory. This is an "
             "exact match of the whole URL, not of an id inside it.")
    return text + _filter_link_caveats(claim_user, same_account, asset.get("wal"))


def _filter_row_basis(fm):
    """One Memory's row of the detail section (:func:`_filter_memories_html`): only what is that
    Memory's own. The method — the record, the rule, what a listing is not — is stated once, in the
    section's "?" (:data:`FILTER_LISTED_BASIS`); repeated per row it made a widely listed asset's
    detail grow by its whole length for every Memory."""
    differs = _url_difference(fm["external_key"], fm.get("url") or fm["external_key"])
    text = (f"Memory {fm['snap_id']}'s overlay record (ZGALLERYSNAPDETAIL.ZOVERLAY) gives this "
            f"claim's URL"
            + (f" — as \"{fm['url']}\", the same URL but for {differs} —" if differs else "")
            + f" as the {fm['role']} of one of its geofilters, at {fm['field']} ({_filter_facts(fm)})"
            + "".join(f", and at {field}" for field in fm.get("fields") or ())
            + f". The record names the selected geofilter separately: "
            f"{_SELECTED_SENTENCE[fm.get('selected')]}.")
    text += _overlay_flag_sentence(fm.get("has_overlay_image"))
    return text + _filter_link_caveats(fm.get("claim_user"), False if fm.get("cross_account") else None,
                                       fm.get("wal"))


def _overlay_links_for(clist, overlay_urls, memory_pages=None, skip_sid=None):
    """The Memories whose overlay record lists one of this entry's claim keys as an asset URL.

    The one rule, which the survey (``--survey-claim-links``) calls too: a claim's EXTERNAL_KEY and a
    URL the record gives an asset of a listed geofilter are the same URL by
    ``snap_overlay.normalise_url`` — scheme, host, path and the whole query. An id inside the URL is
    not enough (the same last path segment recurs under other hosts and paths, and one ``mo=`` /
    ``bo=`` value under other ids), and the claim's context and account are not restricted: the
    account is stated in the explanation instead. ``skip_sid`` is the Memory the entry already links
    to as its media.

    One link per Memory, by snap id: the field of the filter the record names as selected when one
    of the matching fields is that filter's, else the record's first matching field; any others in
    ``fields``; and the claim of the Memory's own account when the entry has one.
    """
    memory_pages = memory_pages or {}
    found = {}                                     # snap id -> (user hash, [(claim, its key, asset)])
    for c in clist:
        ek = str(c.get("external_key") or "")
        key = snap_overlay.normalise_url(ek)
        if not key:
            continue
        for sid, user_hash, asset in overlay_urls.get(key) or ():
            if sid != skip_sid:
                found.setdefault(sid, (user_hash, []))[1].append((c, ek, asset))
    links = []
    for sid in sorted(found):
        user_hash, hits = found[sid]
        # the claim of the Memory's own account first, when the entry has one; then an asset of the
        # filter the record names as selected (one asset can be listed under several filters, and the
        # link's "selected" must be what the record says of any of them); else record order
        hits.sort(key=lambda hit: (account_matches(hit[0].get("user_id") or "", user_hash) is False,
                                   hit[2].get("selected") is not True))
        claim, ek, asset = hits[0]
        user = claim.get("user_id") or ""
        same = account_matches(user, user_hash)
        links.append({
            "snap_id": sid, "user_hash": user_hash, "page": memory_pages.get(sid),
            "role": asset["role"], "field": asset["field"], "url": asset["url"],
            "fields": list(dict.fromkeys(a["field"] for _c, _ek, a in hits
                                         if a["field"] != asset["field"])),
            "filter_id": asset.get("filter_id") or "", "filter_type": asset.get("filter_type") or "",
            "group": asset.get("group") or "", "selected": asset.get("selected"),
            "has_overlay_image": asset.get("has_overlay_image"), "wal": asset.get("wal"),
            "external_key": ek, "claim_user": user, "cross_account": same is False,
            "basis": _filter_basis(ek, user, sid, asset, same)})
    return links


def _ctp_basis(ek, hit, claim_user, same_account):
    """The explanation of one creative-tools item that names a claim key (``ctp_items.match``)."""
    where = " and ".join(w for w, _text in hit["where"])
    held = hit["where"][0][1]                              # the text as the item stores it
    item = f"item {hit['item_id']}"
    if hit["rule"] == "id_bytes":
        text = (f"The claim EXTERNAL_KEY \"{ek}\" is \"{hit['prefix']}\" followed by base64 of the same "
                f"bytes as {where} of {item} (\"{held}\")"
                + (": the same id, in another base64 alphabet or padding."
                   if ek[len(hit["prefix"]):] != held else "."))
    else:
        rest = ek if hit["rule"] == "key" else ek[len(hit["prefix"]):]
        differs = _url_difference(rest, held)
        text = (f"The claim EXTERNAL_KEY \"{ek}\" is "
                + (f"\"{hit['prefix']}\" followed by " if hit["rule"] == "after_prefix" else "")
                + "the text" + (f" \"{held}\" (the same URL but for {differs})" if differs else "")
                + f" at {where} of {item}.")
    info = hit.get("feed_info")
    if info:
        tree = ("only the checkpointed version of the store's feed tree" if info.get("prior")
                else "the store's feed tree")
        text += (f" The item is of feed {info['feed']}"
                 + (f", which {tree} names {info['short']}" if info["short"]
                    else "" if info["in_tree"]
                    else ", which is not named: a document of the store's feed tree could not be read"
                    if info.get("tree_unread") else ", which the store's feed tree does not list")
                 + (f" (payload field 2.{hit['kind']})" if hit.get("kind") is not None else "") + ".")
    if not hit.get("decoded"):
        text += (" Its document (column p) does not have the layout this report reads, so it is not "
                 "decoded: the match is on the item_id column alone.")
    same = [i for i in hit.get("same_item") or () if i != hit["item_id"]]
    if same:
        text += (f" The store lists the same item — the same own id — under {len(same) + 1} item_ids, "
                 f"one per feed; each is shown: {', '.join([hit['item_id']] + same)}.")
    if hit.get("references"):
        refs = hit["references"]
        text += (f" {len(refs)} other item(s) of the store hold the same text in their payload — a "
                 f"reference to this item — and are not shown: {', '.join(refs)}.")
    if same_account is True:
        text += " The store is the claiming account's own: its folder is SHA-256 of the claim's USER_ID."
    elif same_account is False:
        text += (f" The claim was made by account {claim_user}, and this store is another account's: "
                 f"the same text, kept in that account's store.")
    if hit.get("rewritten"):
        text += (" The version shown is the current one, written after the store's last checkpoint "
                 "(-wal only). The checkpointed version of the item, without the -wal, holds this "
                 "text too: the -wal rewrote the item's row, and that version's other texts are not "
                 "shown.")
    elif hit.get("wal") == sqlite_open.MAIN_ONLY:
        text += (" This version of the item is in primary.docobjects only without its -wal: the "
                 "write-ahead log has since changed or removed it, so it is prior state, not the "
                 "store's current content.")
    return text


def _ctp_hits(clist, ctp_index):
    """The creative-tools items that name one of this entry's claim keys — the one rule, which the
    survey (``--survey-claim-links``) calls too (``ctp_items.match``). One per item and store, from the
    claim of the store's own account when the entry has one, sorted by account and item_id."""
    if not ctp_index or not ctp_index.get("stores"):
        return []
    found = {}
    for c in clist:
        ek = str(c.get("external_key") or "")
        user = c.get("user_id") or ""
        for hit in ctp_items.match(ek, ctp_index):
            ident = (hit["store"], hit["item_id"])
            same = account_matches(user, hit["user_hash"])
            if ident in found and not (same is True and found[ident]["own_account"] is not True):
                continue
            found[ident] = dict(hit, claim_key=ek, claim_user=user, own_account=same,
                                basis=_ctp_basis(ek, hit, user, same))
    return sorted(found.values(), key=lambda h: (h["user_hash"], h["item_id"]))


def _ctp_store_state(store):
    """What was read of one creative-tools store (``ctp_items.read``'s store record), for the header:
    its sizes and -wal, and whether the two readings differ — said of the tables read (ctp__item_5,
    ctp__feedtree) only, never of the whole store, whose other tables (the contacts among them) are
    not compared. Without a -wal the readings are one, and that is true of the whole file."""
    info = store.get("info") or {}
    if not info.get("wal_bytes"):
        return sqlite_open.describe(info)
    text = sqlite_open.describe(dict(info, differs=None))
    for table, differs in (store.get("differs") or {}).items():
        text += f"; {table}: " + ("the two readings DIFFER" if differs else "both readings agree")
    return text


def load_chat_links(report_dir):
    """Load the chat attachment manifest written by the chat report, if present.

    Returns ``(by_key, by_message)``:

    * ``by_key``     : CACHE_KEY -> [{conversation_id, server_message_id, anchor[, href]}]
    * ``by_message`` : "<conversation>|<server message id>" -> the same records

    ``by_message`` is the fallback that links **every** cache entry belonging to a message (a chat
    video is typically a full-media claim, a thumbnail claim and a raw content claim), not only the
    single file the chat report chose to display. Empty when no chat report ran.

    The **Conversations** report's manifest (version 3) wins over the legacy Communications one,
    because its records carry an ``href`` — with one page per conversation the target is no longer a
    single document, so the anchor alone is not enough to build the link. Version 2 (Communications:
    the two indexes, anchors into ``Communications_legacy_report.html``) and version 1 (a bare
    CACHE_KEY -> records map) are still understood.
    """
    for report, document in (("Conversations", None),
                             ("Communications_legacy", "Communications_legacy_report.html"),
                             ("Communications", "Communications_report.html")):
        cand = os.path.join(report_dir or "", report, "cache_links.json")
        if not os.path.isfile(cand):
            continue
        try:
            with open(cand, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception as error:
            logger.debug(f"Could not read chat link manifest {cand}: {error}")
            continue
        if data.get("version") in (2, 3):
            by_key, by_message = data.get("by_key") or {}, data.get("by_message") or {}
        else:
            by_key, by_message = data, {}                      # legacy (v1) manifest
        # The Conversations manifest also lists every message, attachment or not: a claim's key can
        # name a message whose file the chat join did not attach (see write_cache_links).
        for conv, info in (data.get("messages") or {}).items():
            for smid, anchor in (info.get("anchors") or {}).items():
                by_message.setdefault(f"{conv}|{smid}", [{
                    "conversation_id": conv, "server_message_id": smid, "anchor": anchor,
                    "title": info.get("title") or "", "href": f'{info.get("href")}#{anchor}'}])
        if document:                                           # single-document report: one base
            for records in list(by_key.values()) + list(by_message.values()):
                for rec in records:
                    rec.setdefault("base", f"{report}/{document}")
        return by_key, by_message
    return {}, {}


class ChatIdIndex:
    """What the chat manifest says about messages beyond the files it attached to them.

    * ``by_number``: every message by its conversation and bare message number, so a claim key that
      names message 12 finds it whether the report lists it as ``12.0`` or under another part;
    * the ids each message names its media by (``by_content_id``, see
      ``arroyo_content.content_ids``), indexed by the token a claim key would carry them as;
    * every conversation the report lists (``conversations``), and what the arroyo.db the run read
      holds (``arroyo``: whether its messages and its conversation tables were read, its account,
      the message numbers of each conversation) — for a claim whose key names a message no row is
      there for
      (:func:`_conversation_links_for`). Without an ``arroyo`` section (an older manifest, or the
      legacy Communications one) neither is known, and nothing is tied to a conversation.
    """

    def __init__(self, by_message, by_content_id=None, conversations=None, arroyo=None):
        self.by_number = {}
        for key, records in by_message.items():
            conv, _bar, smid = key.partition("|")
            number = arroyo_content.message_number(smid)
            if number:
                self.by_number.setdefault(f"{conv.lower()}|{number}", []).extend(records)
        self.by_token = {}
        for cid, refs in (by_content_id or {}).items():
            records = []
            for ref in refs:
                found = by_message.get(f'{ref.get("conversation_id")}|{ref.get("server_message_id")}')
                if found:
                    records.append((ref.get("rule") or "", found[0]))
            token = _content_token(cid)
            if records and token:
                self.by_token.setdefault(token, []).append((cid, records))
        arroyo = arroyo if isinstance(arroyo, dict) else None
        # by lower-case id, each carrying the id as the report spells it (its anchor is built from it)
        self.conversations = {str(conv).lower(): dict(rec, id=conv)
                              for conv, rec in ((conversations or {}).items() if arroyo else ())
                              if isinstance(rec, dict)}
        self.arroyo_read = bool(arroyo and arroyo.get("read"))
        # whether its conversation tables were read in full: what saying none of them names a
        # conversation rests on (a manifest without the flag has not said so)
        self.conversations_read = bool(arroyo and arroyo.get("conversations_read"))
        self.arroyo_account = str((arroyo or {}).get("account") or "").strip().lower()
        self.held = None                         # conversation -> {message number}; None: not read
        if self.arroyo_read:
            self.held = {}
            for conv, numbers in (arroyo.get("held") or {}).items():
                self.held[str(conv).lower()] = {n for n in map(_as_number, numbers or ())
                                                if n is not None}

    def conversation(self, conv):
        """The Conversations report's record of conversation ``conv`` (any letter case), or None
        when it does not list it."""
        return self.conversations.get(str(conv or "").lower())

    def holds(self, conv, number):
        """Whether the arroyo.db the run read holds message ``number`` of conversation ``conv``, in
        either reading: True / False, or None when its messages were not read."""
        if self.held is None:
            return None
        return _as_number(number) in self.held.get(str(conv or "").lower(), ())

    def holds_any(self, conv):
        """Whether that arroyo.db holds any message of ``conv``; None when its messages were not
        read."""
        if self.held is None:
            return None
        return bool(self.held.get(str(conv or "").lower()))

    def message(self, conv, number):
        """Every record of message ``number`` in conversation ``conv``, lowest part first."""
        records = self.by_number.get(f"{str(conv).lower()}|{arroyo_content.message_number(number)}")
        return sorted(records or (), key=lambda r: str(r.get("server_message_id")))

    def content_links(self, external_key):
        """``[(id, rule, record)]`` for each message id a claim key contains."""
        ek = str(external_key or "")
        tokens = {m.group(0).lower() for m in _CONTENT_UUID.finditer(ek)}
        tokens |= {r.lower() for pattern in (_CONTENT_RUN, _CONTENT_B64) for r in pattern.findall(ek)}
        out = []
        for token in tokens:
            for cid, records in self.by_token.get(token, ()):
                # a UUID-based id in any letter case; anything else (base64 above all) exactly
                inside = (cid.lower() in ek.lower()) if _CONTENT_UUID.search(cid) else (cid in ek)
                if inside:
                    out.extend((cid, rule, record) for rule, record in records)
        return out


_CONTENT_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                           r"[0-9a-fA-F]{12}")
_CONTENT_RUN = re.compile(r"[A-Za-z0-9+/=_-]{8,}")
_CONTENT_B64 = re.compile(r"[A-Za-z0-9+/=]{8,}")


def _as_number(value):
    """A message number as an int (``12``, ``"12"``, ``"012"``, ``"12.0"`` are all 12), or None."""
    number = arroyo_content.message_number(value)
    return int(number) if number else None


def _content_token(cid):
    """What a claim key holds a content id as: its UUID when it has one, else its longest run."""
    mo = _CONTENT_UUID.search(cid)
    if mo:
        return mo.group(0).lower()
    runs = _CONTENT_RUN.findall(cid)
    return max(runs, key=len).lower() if runs else ""


def load_chat_ids(report_dir, by_message):
    """A :class:`ChatIdIndex` over the chat manifest ``load_chat_links`` read (Conversations only:
    the legacy manifests carry no content ids, conversations or arroyo.db facts)."""
    data = {}
    cand = os.path.join(report_dir or "", "Conversations", "cache_links.json")
    if os.path.isfile(cand):
        try:
            with open(cand, encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except Exception as error:                                 # noqa: BLE001
            logger.debug(f"Could not read the content ids of {cand}: {error}")
    data = data if isinstance(data, dict) else {}
    return ChatIdIndex(by_message, data.get("by_content_id") or {},
                       conversations=data.get("conversations"), arroyo=data.get("arroyo"))


def load_memory_media(report_dir):
    """Load the Memories report's ``media_by_cache_key.json`` (CACHE_KEY -> decrypted media files).

    Most Memory media is stored **encrypted** in the SCContent cache, so its bytes are not viewable
    here. The Memories report has already decrypted those files with the snap's AES key; this
    manifest lets each cache entry link straight to that decrypted copy instead of leaving the
    examiner with an unopenable blob. Empty when the Memories report didn't run.
    """
    cand = os.path.join(report_dir or "", "Memories", "media_by_cache_key.json")
    if os.path.isfile(cand):
        try:
            with open(cand, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as error:
            logger.debug(f"Could not read decrypted-media manifest {cand}: {error}")
    return {}


def load_memory_content(report_dir):
    """The Memories report's ``media_by_content.json``: the cache files on the device proven
    byte-identical to a Memory's media — recovered from the device, or retrieved from Snapchat's
    servers (cloud_memories.find_identical). ``{}`` when there is none."""
    cand = os.path.join(report_dir or "", "Memories", "media_by_content.json")
    if os.path.isfile(cand):
        try:
            with open(cand, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as error:
            logger.debug(f"Could not read content manifest {cand}: {error}")
    return {}


CONTENT_BASIS = (
    "Proven by content: this file is byte-identical (SHA-256 {sha}) to the copy of Memory {sid}'s "
    "{role} retrieved from Snapchat's servers on {when} (UTC){what}, at the examiner's request under: "
    "{note}. The server copy is not device evidence; it is the reference that identifies this file, "
    "which no identifier on the device connects to the Memory.")

DEVICE_CONTENT_BASIS = (
    "Proven by content: this file is byte-identical (SHA-256 {sha}) to Memory {sid}'s {role} as this "
    "run recovered it from the device — {source}. No identifier on the device connects this file to "
    "the Memory; the bytes do, and both copies are device evidence. A file like this is typically the "
    "snap editor's working copy of a snap that was then saved to Memories.")


def content_basis(rec, sid=None):
    """The basis of a link proven by content, for a record of cloud_memories.find_identical."""
    sid = sid or rec.get("snap_id", "")
    if rec.get("what") == "device":
        src, ref = rec.get("source") or "", rec.get("from") or ""
        if src.startswith("caching-media"):
            source = f"from the caching-media pack {ref}, decrypted with the Memory's own key"
        elif ref:
            source = f"from its {src or 'SCContent'} cache file {ref}"
        else:
            source = f"from {src or 'its cache'}"
        return DEVICE_CONTENT_BASIS.format(sha=rec.get("sha256", ""), sid=sid,
                                           role=rec.get("role") or "media", source=source)
    return CONTENT_BASIS.format(
        sha=rec.get("sha256", ""), sid=sid, role=rec.get("role") or "media",
        when=rec.get("retrieved_utc", ""), note=rec.get("authority_note", ""),
        what=" as received, before decryption" if rec.get("what") == "encrypted"
        else ", decrypted with that Memory's own key")


#: How a Memory chip says the link was proven by content: ≡ the device's own copy, ☁ a server copy.
CONTENT_MARKS = {"device": " ≡", "cloud": " ☁"}


def load_memory_pages(report_dir):
    """Load the Memories report's snap_id -> detail-sub-page manifest, if present.

    Lets each memory-linked cache entry link straight to that memory's detail page (in addition to
    the index row). Empty when the Memories report didn't run or is the old single-file layout.
    """
    cand = os.path.join(report_dir or "", "Memories", "memory_pages.json")
    if os.path.isfile(cand):
        try:
            with open(cand, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as error:
            logger.debug(f"Could not read memory page manifest {cand}: {error}")
    return {}


def load_memory_packs(report_dir):
    """Load the Memories report's ``caching-media`` manifest: ``<folder>/<item> -> [media]``.

    A ``.pack`` file's name is an opaque hash that no database indexes, so nothing but this
    manifest can tie one to a Memory — the link exists only because a Memory's key decrypted it,
    which happens in the Memories report. Empty when that report did not run.
    """
    cand = os.path.join(report_dir or "", "Memories", "media_by_pack.json")
    if os.path.isfile(cand):
        try:
            with open(cand, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as error:
            logger.debug(f"Could not read pack manifest {cand}: {error}")
    return {}


def load_cache_media(report_dir):
    """``CACHE_KEY -> [rows]`` from the cached-media report's ``by_cache_key.json``.

    That report covers everything under ``Library/Caches`` this one does not, and records where a
    file it found is byte-identical to — or otherwise attributes to — one of these cache entries.
    Present only when that report ran first (it does, in the pipeline order).
    """
    if not report_dir:
        return {}
    candidate = os.path.join(report_dir, "CacheMedia", "by_cache_key.json")
    try:
        if os.path.isfile(candidate):
            with open(candidate, encoding="utf-8") as fh:
                return json.load(fh) or {}
    except Exception as error:
        logger.debug(f"Could not read {candidate}: {error}")
    return {}


def _is_range_child(name):
    """True for a CHILDREN entry that names a byte range of the parent (handled via ``scparts``)."""
    if not isinstance(name, str):
        return False
    return (name == "PREFETCH" or bool(re.fullmatch(r"\d+-\d+", name))
            or bool(_SC_SPLIT_RE.match(name)))


def child_ondisk_paths(cache_key, name, scfull, scparts):
    """On-disk paths for one **bundle child**, in read order.

    A bundle (``TYPE=3``) is unpacked into one file per child, named ``<CACHE_KEY>_<child name>``
    — e.g. ``<CACHE_KEY>_z<hex>`` for a child named ``z<hex>``. Other layouts store the child under
    its own cache key, so both spellings are tried. This is what makes a bundle's actual media
    reachable (e.g. the .mp4 of a chat video and its .webp overlay): the parent ``<CACHE_KEY>``
    file itself only holds the small CHILDREN descriptor.
    """
    if not isinstance(name, str) or _is_range_child(name):
        return []
    bare = name[1:] if (len(name) == 33 and name[:1].isalpha()) else name
    paths, seen = [], set()
    for cand in (f"{cache_key}_{name}", name, bare):
        for p in scfull.get(cand, []):
            if p not in seen:
                seen.add(p)
                paths.append(p)
        for _off, p in sorted(scparts.get(cand.lower(), [])):
            if p not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def _resolve_on_disk(cache_key, children, scfull, scparts):
    """Resolve a cache key to on-disk source paths + total bytes present.

    Looks for a whole ``<cache_key>`` file, its byte-range parts, and — for bundles — the files of
    each named child (``<cache_key>_<child>`` or the child's own cache key). Returns (paths,
    bytes_on_disk, found_bool, scope_by_path), where scope_by_path maps each path to the SCContent
    account UUID it physically lives under.
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
    # bundle children — stored as <cache_key>_<child name> (see child_ondisk_paths)
    for ch in children:
        for p in child_ondisk_paths(cache_key, ch.get("name"), scfull, scparts):
            add(p)
    return paths, total, bool(paths), scope_by_path


def _ondisk_paths_ordered(cache_key, scfull, scparts):
    """The source files making up the logical cached file, in read order: a single whole
    ``<cache_key>`` file, else its byte-range parts in offset order (deduped). Returns
    ``(paths, single_whole_path_or_None)``."""
    fulls = scfull.get(cache_key, [])
    if fulls:
        return [fulls[0]], fulls[0]
    parts = scparts.get(cache_key.lower(), [])
    if not parts:
        return [], None
    seen, chunks = set(), []
    for off, p in sorted(parts):
        if off in seen:
            continue
        seen.add(off)
        chunks.append(p)
    return chunks, None


# Enough of the start of a file to identify it. 16 bytes covers every magic-byte test, but the
# entropy measurement that decides whether bytes are *encrypted* needs a real sample — at 16 bytes
# the maximum possible entropy is 4 bits/byte, so every file looked unencrypted.
_HEAD_BYTES = 8192


def _hash_stream(paths):
    """Stream ``paths`` in order; return (md5, sha256, first _HEAD_BYTES, total). Any size is safe."""
    md5, sha, head, total = hashlib.md5(), hashlib.sha256(), bytearray(), 0
    for p in paths:
        with open(p, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                md5.update(chunk)
                sha.update(chunk)
                total += len(chunk)
                if len(head) < _HEAD_BYTES:
                    head += chunk[:_HEAD_BYTES - len(head)]
    return md5.hexdigest(), sha.hexdigest(), bytes(head), total


def publish_view(paths, files_dir, name_base, ext, total, max_reconstruct_bytes):
    """Make recognizable plaintext media openable from the report as ``files/<name_base>.<ext>``.

    Returns ``(relative url or None, note)``. Cache files on disk are named after their CACHE_KEY
    with **no extension**, which browsers handle inconsistently (Chrome downloads it, Firefox may
    show it as text, ``<video>`` refuses it) — so every viewable file gets a name that ends in its
    real extension. Data is not duplicated where it can be avoided:

    * one whole file → a **hard link** to the original extracted file (same bytes on disk, no copy),
      falling back to a real copy only when the filesystem refuses the link;
    * byte-range parts → concatenated into one file, which is the only way to view them, up to
      ``max_reconstruct_bytes``.
    """
    dst = os.path.join(files_dir, f"{name_base}.{ext}")
    rel = "files/" + f"{name_base}.{ext}"
    if os.path.exists(dst):                                    # left by an earlier run into this dir
        try:
            linked = os.stat(dst).st_nlink > 1
        except OSError:
            linked = False
        return rel, ("hard link to the original cache file (no data duplicated)" if linked else
                     (f"reconstructed from {len(paths)} parts" if len(paths) > 1 else "copied"))
    if len(paths) == 1:
        try:
            os.link(paths[0], dst)
            return rel, "hard link to the original cache file (no data duplicated)"
        except OSError:
            pass
        if total <= max_reconstruct_bytes:
            try:
                shutil.copy2(paths[0], dst)
                return rel, "copied (the filesystem does not support linking here)"
            except OSError as error:
                logger.debug(f"Could not publish {name_base}: {error}")
        return None, (f"{ext}, {_fmt_bytes(total)} — open it from the source path above "
                      "(could not be published next to the report)")
    if total > max_reconstruct_bytes:
        return None, (f"{ext}, {_fmt_bytes(total)} split into {len(paths)} parts — too large to "
                      "reconstruct here; rebuild from the part files listed above")
    try:
        with open(dst, "wb") as fh:
            for p in paths:
                with open(p, "rb") as src:
                    shutil.copyfileobj(src, fh)
        return rel, f"reconstructed from {len(paths)} parts"
    except OSError as error:
        logger.debug(f"Could not write reconstructed copy for {name_base}: {error}")
        return None, f"{ext}, {_fmt_bytes(total)} — could not be reconstructed"


# Extensions worth a still frame. A play button says "this is a video"; a frame says which video —
# without one, a page of cached video tells the examiner nothing about any of it.
POSTER_EXTS = ("mp4", "mov", "m4v", "webm")

# What a ▶ may be put on. Now that an "....ftyp" container is typed by its brand rather than all
# being called .mp4, the cache holds recognised media that does not play — a HEIC or AVIF still —
# and a play button on a photograph is the same kind of wrong statement as calling it a video.
PLAYABLE_EXTS = ("mp4", "mov", "m4v", "webm", "3gp", "m4a", "mp3", "ogg")

POSTER_BASIS = (
    "This still is DERIVED by this tool from the video next to it (OpenCV, the frame at about one "
    "second, or the first frame that decodes when the cached video is incomplete). It is not data "
    "from the device and carries no evidential weight of its own — it is a thumbnail so the index "
    "can be read at a glance. Open the video itself for the content.")


def publish_posters(entries, files_dir, get_view=None,
                    file_timeout=poster_worker.FILE_TIMEOUT_S, budget=None):
    """Extract a poster frame beside every published video: ``files/<name>_poster.jpg``.

    Sets ``entry["poster"]`` (a URL relative to the report) on each entry that gets one and returns
    ``(made, undecodable, not_attempted)``. A poster left by an earlier run into the same folder is
    reused rather than re-extracted, which keeps a re-run into an existing report folder cheap.

    The work runs in **killable subprocesses**, one video at a time each, because a cached video
    that cannot be decoded does not fail — it blocks the decoder forever, and roughly one in six of
    them does. ``budget`` (seconds) limits the pass; by default the run's setting applies, which is
    no limit (see :mod:`scripts.data.poster_worker`). The worker announces each file before it starts, so when it stops answering the parent
    knows which file to skip and restarts it on the rest. Nothing is ever merely abandoned: see
    :mod:`scripts.data.poster_worker` for what abandoning it cost.

    ``complete=False`` (set by the worker) is the only safe setting here: a cache holds whatever
    byte ranges the device streamed, so a cached video is routinely truncated, and seeking to one
    second in a truncated file fails *and* costs a full re-read. Reading forward from the start
    works on complete and partial files alike; the frame is a labelled thumbnail, so which frame it
    is does not matter evidentially.
    """
    jobs = []
    for entry in entries:
        view = (get_view(entry) if get_view else entry.get("view")) or ""
        ext = (entry.get("view_ext") or entry.get("ext") or "").lower()
        if not view or entry.get("view_is_image") or ext not in POSTER_EXTS:
            continue
        name = os.path.basename(view)
        src = os.path.join(files_dir, name)
        if not os.path.isfile(src):
            continue
        poster = os.path.splitext(name)[0] + "_poster.jpg"
        dst = os.path.join(files_dir, poster)
        if os.path.exists(dst):                                # left by an earlier run
            entry["poster"] = "files/" + poster
            continue
        if not has_video_track(src):                           # audio or a still: no frame exists
            entry["poster_note"] = ("no poster frame: this file carries no video track that a "
                                    "decoder can read")
            continue
        jobs.append((src, dst, entry, "files/" + poster))
    if not jobs:
        return 0, 0, 0

    # complete=False: a cache holds whatever byte ranges the device streamed, so seeking into a
    # cached video regularly lands past the bytes that are there.
    done, stderr_chunks = poster_worker.run_jobs([(j[0], j[1], False) for j in jobs],
                                                 file_timeout,
                                                 **({"budget": budget} if budget else {}))
    # "moov atom not found" here means the device cached only part of that video — a finding, so
    # FFmpeg's chatter is summarised into the log rather than dropped on the floor.
    ffmpeg_log.log_summary(stderr_chunks, "poster-frame extraction from cached video", logger)
    made, undecodable = 0, 0
    for src, _dst, entry, rel in jobs:
        if done.get(src):
            entry["poster"] = rel
            made += 1
        elif src in done:
            # Say why the thumbnail is absent. A video listed with a bare link, next to videos with
            # a frame, otherwise reads as a defect in the report rather than as what it is: this
            # file did not decode, which for a cache usually means only part of it was stored.
            undecodable += 1
            entry["poster_note"] = ("no poster frame could be extracted — this cached video did "
                                    "not decode, which usually means the device stored only part "
                                    "of it (the link still opens the bytes that are there)")
        else:
            # Never attempted: the worker could not be started, or the pass was skipped or ran out
            # of the time it was given. The sentence above would be a finding about the evidence
            # that nothing established, so the absence is attributed where it belongs — to this
            # tool, on this run.
            entry["poster_note"] = ("no poster frame: thumbnail extraction did not run for this "
                                    "file on this run (see the run log) — that is a limit of this "
                                    "tool here, not a statement about whether the video decodes")
    return made, undecodable, len(jobs) - made - undecodable


def materialize_ondisk(entries, scfull, scparts, files_dir, report_dir,
                       max_reconstruct_bytes=1024 * 1024 * 1024, epochfmt=None):
    """For every entry with an on-disk copy, compute the **actual cached bytes'** MD5/SHA-256 and
    make the file viewable when it is recognizable plaintext media, so the examiner can open it
    even when the entry links to no Memory or conversation.

    Three shapes are handled:

    * a **whole** ``<cache_key>`` file, or a file **split** into byte-range parts → hashed as one
      logical file and published through :func:`publish_view`;
    * a **bundle** (``TYPE=3``) → the parent file is only the CHILDREN descriptor, so each child
      (``<cache_key>_<child>``) is hashed and published **separately**. This is what makes e.g. a
      chat video's .mp4 viewable: the bundle itself never looks like media.

    Encrypted cache bytes are still hashed (as stored) but never published — for those, the report
    links to the copy the Memories report already decrypted, when there is one.

    Every file published is also read for what it says about **itself** — EXIF, XMP, an MP4's mvhd
    and QuickTime user data (``embedded`` / ``embedded_times``, see ``scripts/data/media_meta.py``)
    — because a cached JPEG that still carries a camera's EXIF, or a video whose header dates its
    encoding, is evidence the index row knows nothing about. ``epochfmt`` puts those timestamps in
    the run's timezone; without one they are left as the file states them.
    """
    os.makedirs(files_dir, exist_ok=True)
    embedded_todo = []                                      # (target, view), read after the loop

    def read_embedded(target, view):
        if view:
            embedded_todo.append((target, view))

    def hashed(paths):
        """``("ok", (md5, sha256, head, total))`` or ``("err", error)`` — never raises."""
        try:
            return "ok", _hash_stream(paths)
        except OSError as error:
            return "err", error

    def hash_entry(e):
        """Every read this entry needs, done ahead of the loop below and on several threads: the
        reading and hashing are independent per entry; publishing and naming are not, and stay in
        the loop, in its order."""
        if not e["on_disk"]["found"]:
            return None
        paths, _single = _ondisk_paths_ordered(e["cache_key"], scfull, scparts)
        kids = []
        for ch in e["children"]:
            cpaths = child_ondisk_paths(e["cache_key"], ch.get("name"), scfull, scparts)
            kids.append((cpaths, hashed(cpaths) if cpaths else None))
        return paths, (hashed(paths) if paths else None), kids

    for n_e, (e, pre) in enumerate(zip(entries, parallel.ordered_map(hash_entry, entries)), 1):
        progress.step("hashing and publishing cached files", n_e, len(entries))
        if pre is None:
            continue
        paths, main_hash, kid_hashes = pre
        if paths:
            status, value = main_hash
            if status == "ok":
                e["ondisk_md5"], e["ondisk_sha256"], head, total = value
            else:
                logger.debug(f"Could not read on-disk bytes for {e['cache_key']}: {value}")
                head, total = b"", 0
            e["ondisk_bytes"] = total
            ext = guess_media(head)
            e["ondisk_type"] = ext
            # What the bytes are, beyond the four media types publish_view can render. Without this
            # every lens bundle, font, subtitle track and protobuf blob was reported as "encrypted".
            kind, sniffed_ext, label, encrypted = sniff.classify(head, total)
            e["ondisk_kind"], e["ondisk_label"] = kind, label
            e["ondisk_encrypted"] = encrypted
            if sniffed_ext and not ext:
                e["ondisk_type"] = sniffed_ext
            if total == 0:
                e["view_note"] = ("the cached file is 0 bytes on disk — the index entry exists but "
                                  "no content was stored/captured")
            elif ext:
                view, note = publish_view(paths, files_dir, e["cache_key"], ext, total,
                                          max_reconstruct_bytes)
                e["view"], e["view_is_image"] = view, ext in ("jpg", "png", "webp")
                e["view_ext"], e["view_note"] = ext, note
                read_embedded(e, view)

        # bundle children: each is its own file with its own type
        kids = []
        for ch, (cpaths, kid_hash) in zip(e["children"], kid_hashes):
            if not cpaths:
                continue
            kid = {"name": ch.get("name"), "paths": cpaths}
            status, value = kid_hash
            if status != "ok":
                logger.debug(f"Could not read bundle child {ch.get('name')}: {value}")
                continue
            kid["md5"], kid["sha256"], head, total = value
            kid["bytes"] = total
            kid["type"] = guess_media(head)
            _kkind, _kext, kid["label"], kid["encrypted"] = sniff.classify(head, total)
            if not kid["type"] and _kext:
                kid["type"] = _kext
            if kid["type"] and guess_media(head):
                base = f"{e['cache_key']}_{re.sub(r'[^0-9A-Za-z_.-]', '_', str(kid['name']))}"
                kid["view"], kid["note"] = publish_view(cpaths, files_dir, base, kid["type"],
                                                        total, max_reconstruct_bytes)
                kid["view_is_image"] = kid["type"] in ("jpg", "png", "webp")
                read_embedded(kid, kid.get("view"))
            kids.append(kid)
        e["child_files"] = kids
        # the bundle's own "viewable" file is its largest recognizable child
        if not e.get("view") and kids:
            best = max((k for k in kids if k.get("view")), key=lambda k: k["bytes"], default=None)
            if best:
                e["view"], e["view_is_image"] = best["view"], best["view_is_image"]
                e["view_ext"] = best["type"]
                e["view_note"] = (f"bundle child {best['name']} ({best['type']}) — "
                                  f"{best.get('note', '')}")

    # what each published file says about itself — reading headers only, independent per file
    def extract(job):
        return media_meta.extract(os.path.join(files_dir, os.path.basename(job[1])))
    for (target, _view), meta in zip(embedded_todo, parallel.ordered_map(extract, embedded_todo)):
        target["embedded"] = meta
        target["embedded_times"] = report_ui.file_time_rows(meta, epochfmt or (lambda sec: ""))
        if epochfmt is None:                                # nothing to convert with: as written
            for t in target["embedded_times"]:
                t["shown"] = t["wall"]


# A chat claim's EXTERNAL_KEY is "<type>:<conversation id>:<message id>:<part>[:…]" — e.g.
# "thumbnail~1:19e0693c-…:12:0:0". The (conversation, message.part) it carries is what ties a cache
# entry to a chat message even when the chat report attached a *different* file to it.
_CHAT_EK_RE = re.compile(r"^(?P<type>[^:]*):(?P<conv>[0-9a-fA-F-]{36}):(?P<msg>\d+):(?P<part>\d+)")


def _chat_links_for(clist, cache_key, by_key, by_message, ids=None):
    """Chat messages this cache entry belongs to, with an explanation of how each was matched.

    In order: the file the chat report attached (``route`` "file"); the message a claim key names by
    conversation and number ("key"); the message whose own id for its media a claim key carries
    ("content" — ``ids``, a :class:`ChatIdIndex`).
    """
    out, seen = [], set()                                      # one chip per (conversation, message)
    for rec in by_key.get(cache_key, []):
        anchor = rec.get("anchor") or f"cf-{cache_key}"
        ident = (rec.get("conversation_id"), rec.get("server_message_id"))
        if ident in seen:
            continue
        seen.add(ident)
        out.append(dict(rec, anchor=anchor, route="file", basis=(
            f"This CACHE_KEY is the attachment file the chat report recorded for "
            f"message {rec.get('server_message_id') or '(unknown)'} in conversation "
            f"{rec.get('conversation_id') or '(unknown)'} (via its local_message_references / "
            f"content-type mapping, exported to cache_links.json).")))
    for c in clist:
        mo = _CHAT_EK_RE.match(c["external_key"] or "")
        if not mo:
            continue
        smid = f"{mo.group('msg')}.{mo.group('part')}"
        records = by_message.get(f"{mo.group('conv')}|{smid}", [])
        exact = bool(records)
        if not records and ids is not None:
            # the report lists the message under another part (a message is "12.0" there unless a
            # claim of that part was joined onto it), or a file it did not attach: still message 12
            records = ids.message(mo.group("conv"), mo.group("msg"))[:1]
        for rec in records:
            anchor = rec.get("anchor")
            ident = (rec.get("conversation_id"), rec.get("server_message_id"))
            if not anchor or ident in seen:
                continue
            seen.add(ident)
            listed = (f"message {smid}, which the chat report reported for that message" if exact
                      else f"message number {mo.group('msg')} (part {mo.group('part')}), which "
                           f"the chat report lists as message "
                           f"{rec.get('server_message_id')}")
            out.append(dict(rec, anchor=anchor, route="key", basis=(
                f"The claim EXTERNAL_KEY \"{c['external_key']}\" carries the conversation id "
                f"{mo.group('conv')} and {listed}. The link therefore points at the message "
                f"rather than at this exact file — a message can have several cached files (full "
                f"media, thumbnail, raw content claim), and only one of them is displayed in the "
                f"chat report.")))
    for c in clist if ids is not None else ():
        for cid, rule, rec in ids.content_links(c["external_key"]):
            anchor = rec.get("anchor")
            ident = (rec.get("conversation_id"), rec.get("server_message_id"))
            if not anchor or ident in seen:
                continue
            seen.add(ident)
            out.append(dict(rec, anchor=anchor, route="content", basis=(
                f"The claim EXTERNAL_KEY \"{c['external_key']}\" contains {cid}: "
                f"{arroyo_content.CONTENT_ID_RULES.get(rule, rule)} of message "
                f"{rec.get('server_message_id')} in conversation {rec.get('conversation_id')} "
                f"(arroyo.db). An exact identifier match, not one by time or content. The link "
                f"points at the message: the chat report displays at most one of its cached files, "
                f"and this is another one stored under the same id.")))
    return out


CONV_TIE_BASIS = (
    "A chat claim's EXTERNAL_KEY names a conversation and a message "
    "(<type>:<conversation>:<message>:<part>), and there is no message row to link it to: the "
    "arroyo.db this run read holds no message of that number in that conversation, in either reading "
    "(with and without its -wal) — or, for a conversation the Conversations report lists, its "
    "messages could not be read. Such an entry is tied to the conversation the key names, never to a "
    "message: the chip opens the conversation when the Conversations report lists it, and otherwise "
    "filters this report to every entry whose claim names the same conversation. Each chip's \"?\" "
    "says which, and whose account made the claim. Counted apart from the entries linked to a chat.")


def _conversation_links_for(clist, chats, ids):
    """The conversations this entry's claims name, for a message that is not there to link to.

    A claim key of the chat shape (:data:`_CHAT_EK_RE`) names a conversation and a message. When no
    chat link of the entry (``chats``, :func:`_chat_links_for`) reaches that message and the
    Conversations report lists no message of that number, the key still names the conversation —
    and that is all it is tied to:

    * a message the arroyo.db the run read **holds** makes no tie: a report that does not list it is
      missing a rule, which ``--survey-claim-links`` is for, not this;
    * a conversation the report lists is a link (``route`` "conversation"), to its page;
    * one it does not list is a stated fact (``route`` "named"), and only when that arroyo.db's
      messages were read — absent from both its readings, not merely absent from the report.

    ``ids`` is the :class:`ChatIdIndex`; with none, or one whose manifest has no ``arroyo`` section,
    nothing is tied. One record per (conversation, message), the parts of every claim naming it in
    ``parts`` and every claim's key and account in ``claims``: one entry can be claimed by two
    accounts, and the "?" says whose each claim is. ``user_id`` is arroyo.db's own account when a
    claim is that account's (as :func:`_ctp_hits` prefers its own-account hit), else the first account
    a claim names.
    """
    if ids is None:
        return []
    reached = {(str(ch.get("conversation_id") or "").lower(),
                _as_number(ch.get("server_message_id"))) for ch in chats or ()}
    ties = {}
    for c in clist:
        ek = str(c.get("external_key") or "")
        mo = _CHAT_EK_RE.match(ek)
        if not mo:
            continue
        conv, number = mo.group("conv").lower(), _as_number(mo.group("msg"))
        if (conv, number) in reached or ids.message(conv, mo.group("msg")):
            continue
        held = ids.holds(conv, number)
        record = ids.conversation(conv)
        if held is True or (held is None and record is None):
            continue
        tie = ties.get((conv, number))
        if tie is None:
            listed = record is not None
            tie = ties[(conv, number)] = {
                "route": "conversation" if listed else "named",
                "conversation_id": record["id"] if listed else mo.group("conv"),
                "number": str(number), "parts": [], "external_keys": [], "claims": [],
                "external_key": ek, "user_id": "",
                "listed": listed, "held": held,
                "in_arroyo": bool(listed and record.get("in_arroyo")) or bool(ids.holds_any(conv)),
            }
            if listed:
                tie.update(href=record.get("href") or "", title=record.get("title") or "",
                           anchor=record.get("anchor") or f"conv-{record['id']}")
        if mo.group("part") not in tie["parts"]:
            tie["parts"].append(mo.group("part"))
        if ek not in tie["external_keys"]:
            tie["external_keys"].append(ek)
        claim = (ek, str(c.get("user_id") or "").strip())
        if claim not in tie["claims"]:
            tie["claims"].append(claim)
    out = []
    for tie in ties.values():
        tie["parts"].sort(key=int)
        users = [user for _ek, user in tie["claims"] if user]
        tie["user_id"] = next((u for u in users if u.lower() == ids.arroyo_account),
                              users[0] if users else "")
        tie["basis"] = _conversation_basis(tie, ids)
        out.append(tie)
    return out


def _conversation_basis(tie, ids):
    """The explanation of one conversation tie (:func:`_conversation_links_for`).

    What the arroyo.db the run read holds is said of **that** database — "the arroyo.db this run
    read", in both readings — never of the extraction; and never "no longer": nothing in either
    reading shows the message was ever there. Every claim's account is compared with that
    database's own (its ``required_values`` USERID) when both are known (:func:`_claim_accounts`),
    because a second account's cache sits beside the first account's chat database on a phone with
    two, and its claims name conversations and messages that database need never have held. That no
    conversation table names a conversation is said only when they were read in full
    (``ChatIdIndex.conversations_read``).
    """
    conv, n, keys = tie["conversation_id"], tie["number"], tie["external_keys"]
    parts = tie["parts"]
    named = (f'The claim EXTERNAL_KEY "{keys[0]}" names' if len(keys) == 1 else
             "The claim EXTERNAL_KEYs " + ", ".join(f'"{k}"' for k in keys) + " name")
    text = (f"{named} conversation {conv}, message {n} ({'part' if len(parts) == 1 else 'parts'} "
            f"{', '.join(parts)}) — the <type>:<conversation>:<message>:<part> shape of a chat claim "
            f"key.")
    whose, own, others, theirs = _claim_accounts(tie, ids.arroyo_account)
    absent = (f"The arroyo.db this run read holds no message {n} of this conversation, in either "
              f"reading (with and without its -wal)")
    opens = " The link opens the conversation, not a message: there is no message row to point at."
    unread = ("arroyo.db's conversation tables would not read in full in this run, so whether a "
              "conversation, feed_entry or user_conversation row names it is not known")
    if tie["listed"] and tie["held"] is None:                                       # E
        return (text + whose + " arroyo.db's messages were not read in this run (no arroyo.db was "
                f"read, or its conversation_message table was not read in full, in both readings), "
                f"so whether it holds message {n} is not known; the Conversations report lists this "
                f"conversation, with no message {n} in it." + opens)
    if tie["listed"] and tie["in_arroyo"]:                                          # A, B
        text += (f" {absent}, though it holds the conversation itself — a message of it, or a "
                 f"conversation, feed_entry or user_conversation row: no conversation_message row "
                 f"has this client_conversation_id and message number {n} (its server_message_id, or "
                 f"the client_message_id of a message the server never numbered).")
        if own:
            return (text + whose + " So the account cached a file of a message its own chat database "
                    "does not hold; only a recovery of deleted records could say whether it ever "
                    "held one." + opens)
        if others:
            return (text + whose + f" {theirs}, the message may never have been in this database."
                    + opens)
        return text + opens
    if tie["listed"]:                                                               # C
        tables = (", and no conversation, feed_entry or user_conversation row; the Conversations "
                  "report lists it from the friends / groups lists or from cached chat files, not "
                  "from arroyo.db." if ids.conversations_read else
                  f"; {unread}. The Conversations report lists it from the friends / groups lists or "
                  f"from cached chat files.")
        return text + f" {absent} — no message of it at all" + tables + whose + opens
    holds = ids.holds_any(conv)                                                    # D
    text += f" {absent}"
    if holds:
        text += ("; it holds other messages of this conversation, but the Conversations report does "
                 "not list it, so there is no page to link to.")
    elif ids.conversations_read:
        text += (", nor any other message of it, and no report lists the conversation: neither "
                 "arroyo.db's conversation tables nor the friends / groups lists name it.")
    else:
        text += (", nor any other message of it, and no report lists the conversation: the friends "
                 f"/ groups lists do not name it, and {unread}.")
    text += whose
    if others and not own:
        # a database that holds messages of the conversation held the conversation: only the
        # message can be one it never held
        text += (f" {theirs}, the message may never have been in this database." if holds else
                 " The conversation may be one of " + " or ".join(f"{u}'s" for u in others)
                 + " that the database this run read never held.")
    return (text + " The chip filters this report to every cache entry whose claim names the same "
            "conversation.")


def _claim_accounts(tie, account):
    """Whose claims name a tie's message, against ``account`` — arroyo.db's own, lower case, "" when
    not known: ``(sentence, own, others, theirs)``.

    ``sentence`` is what the "?" says of it; ``own`` whether a claim is that account's; ``others`` the
    other accounts that made one, as their claims spell them; ``theirs`` the subject of the sentence
    that draws the other-account conclusion ("The claim being another account's"). Every claim's
    account, never only the first's: one entry can be claimed by two accounts, and the own account's
    claim is the stronger evidence. Nothing is said when ``account`` is not known or no claim names
    an account.
    """
    claims = tie.get("claims") or [(tie.get("external_key") or "", tie.get("user_id") or "")]
    by_user = {}                                   # lower-case account -> (as spelled, [its keys])
    for ek, user in claims:
        keys = by_user.setdefault(user.strip().lower(), (user.strip(), []))[1]
        if ek not in keys:
            keys.append(ek)
    others = [spelled for low, (spelled, _keys) in by_user.items() if low and low != account]
    own = bool(account) and account in by_user
    if not (account and (own or others)):
        return "", False, [], ""
    belongs = f"the arroyo.db this run read belongs to account {account} (its required_values USERID)"
    many = len(claims) > 1
    if len(by_user) == 1:                          # every claim one account's
        if own:
            return (f" The claim{'s were' if many else ' was'} made by the account that arroyo.db "
                    f"belongs to (its required_values USERID).", True, [], "")
        return (f" {'These claims were' if many else 'This claim was'} made by account {others[0]}; "
                f"{belongs}.", False, others,
                f"The claim{'s' if many else ''} being another account's")
    said = []
    for low, (spelled, keys) in by_user.items():
        quoted, one = ", ".join(f'"{k}"' for k in keys), len(keys) == 1
        said.append(f"{quoted} {'was' if one else 'were'} made by the account that arroyo.db belongs "
                    f"to" if low == account else
                    f"{quoted} {'was' if one else 'were'} made by account {spelled}" if low else
                    f"{quoted} {'names' if one else 'name'} no account (no USER_ID)")
    lowered = {u.lower() for u in others}
    theirs_n = sum(1 for _ek, user in claims if user.strip().lower() in lowered)
    theirs = (f"The claim{'s' if theirs_n > 1 else ''} of "
              + " and ".join(f"account {u}" for u in others)
              + (" being another account's" if len(others) == 1 else " being other accounts'"))
    return (f" Of the claims naming it, {'; '.join(said)}. {belongs[0].upper()}{belongs[1:]}.",
            own, others, theirs)


def build_entries(db, app, scfull, scparts, mem_index, chat_links, ms_fmt, memory_pages=None,
                  chat_by_message=None, workdir=None, memory_content=None, chat_ids=None,
                  ctp_index=None):
    """Build one entry dict per physical cache file (CACHE_KEY) from a cache_controller.db.

    Returns (entries, virtualization_rows, wal_info). Each entry aggregates its claims, metadata,
    on-disk resolution and cross-report links. ``ctp_index`` is ``ctp_items.read``'s: the items of
    the creative-tools stores that name a file are attached to it (``ctp_items``), and give it
    :data:`CTP_CATEGORY` in place of a category of :data:`CTP_REPLACES` when no chat message,
    conversation tie or Memory link says what it is.

    The database is read **twice** — with and without its ``-wal`` — so claims the write-ahead log
    has already superseded or deleted are recovered instead of silently lost. Each claim carries
    the view it came from; ``wal_info`` describes what was found on disk, for the source block.
    """
    snap_ids = mem_index["snap_ids"]
    url_keys = mem_index["url_keys"]
    media_ids = mem_index["media_ids"]
    snap_urls = mem_index.get("snap_urls") or {}
    memdata_ids = mem_index.get("memdata_ids") or {}
    overlay_urls = mem_index.get("overlay_urls") or {}
    # the id columns in the words the link's explanation uses (iOS: the Core Data columns)
    labels = mem_index.get("labels") or {"snap": "ZSNAPID", "media": "ZMEDIAID"}
    memory_pages = memory_pages or {}
    views = sqlite_open.open_views(db, workdir)
    try:
        claims = _read_all(views, "CACHE_FILE_CLAIM")
        metas = _read_all(views, "CACHE_FILE_METADATA")
        tombstones = _read_all(views, "CACHE_FILE_SAMPLED_TOMBSTONE")
        virtual = [row for row, _wal in _read_all(views, "CACHE_KEY_VIRTUALIZATION")]
        wal_info = dict(views.info)
    finally:
        views.close()

    # One metadata row per physical file — but the two database readings can each hold a *different*
    # version of it, which is common rather than theoretical: a row is rewritten every time the file
    # is re-read or re-fetched. The current version always wins; the superseded one is kept alongside
    # so the detail panel can show what the row said before, which is otherwise unrecoverable.
    meta_by_key, meta_prior_by_key = {}, {}
    for m, wal in metas:
        key = m.get("CACHE_KEY")
        if wal == sqlite_open.MAIN_ONLY:
            meta_prior_by_key.setdefault(key, []).append(m)
        else:
            meta_by_key.setdefault(key, m)                     # first current row wins
    for key, priors in meta_prior_by_key.items():
        # a file whose only metadata row is the checkpointed one (the -wal deleted it)
        if key not in meta_by_key:
            meta_by_key[key] = priors[0]

    # group claims by physical file
    by_key = {}
    for c, wal in claims:
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
            "wal": wal,
        })

    tomb_by_key = {}
    for t, wal in tombstones:
        tomb_by_key.setdefault(t.get("CACHE_KEY"), []).append({
            "mct": t.get("MEDIA_CONTEXT_TYPE"),
            "reason": t.get("DELETION_REASON"),
            "bytes": t.get("BYTES_DELETED"),
            "deleted": ms_fmt(t.get("DELETED_TIMESTAMP_MILLIS")),
            "user_id": t.get("USER_ID") or "",
            "wal": wal,
        })

    entries = []
    all_keys = set(by_key) | set(tomb_by_key)
    for n_key, key in enumerate(all_keys, 1):
        progress.step("reading cache entries", n_key, len(all_keys))
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
                         f"{labels['snap']} ({canonical}) — the primary, most direct link.")
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
                             f"{labels['media']} (Memory {canonical}).")
                    break
        if not memory:                                         # 3b. ZSNAPID in a full-media
            for c in clist:                                    #     key of no Memory-scoped shape
                mo = _UUID_RE.search(c["external_key"])
                if mo and not c["snap_uuid"] and c["mct"] == 19 \
                        and mo.group(0).upper() in snap_ids:
                    canonical, user_hash = snap_ids[mo.group(0).upper()]
                    memory = {"snap_id": canonical, "user_hash": user_hash}
                    basis = (f"The claim EXTERNAL_KEY \"{c['external_key']}\" carries "
                             f"{mo.group(0)}, this Memory's {labels['snap']} — an exact identifier, "
                             f"in a key that is not one of the Memory-scoped shapes "
                             f"(MEDIA_CONTEXT_TYPE {c['mct']}).")
                    break
        if not memory:                                         # 4. a MemData id the Memory records
            memory, basis = _memdata_link(clist, memdata_ids)
        proofs = (memory_content or {}).get(key.lower()) or []
        if not memory and proofs:                              # 5. byte-identical to its media
            rec = proofs[0]                                    # the device's own copy first
            canonical, user_hash = snap_ids.get(str(rec["snap_id"]).upper(), (rec["snap_id"], ""))
            memory = {"snap_id": canonical, "user_hash": user_hash,
                      "by_content": "device" if rec.get("what") == "device" else "cloud"}
            basis = content_basis(rec, canonical)
        if memory:                                             # detail sub-page, when available
            memory["page"] = memory_pages.get(memory["snap_id"])
            memory["urls"] = snap_urls.get(memory["snap_id"]) or []
        chats = _chat_links_for(clist, key, chat_links, chat_by_message or {}, chat_ids)
        # a key naming a message no row is there for: tied to the conversation, never to a message
        conv_links = _conversation_links_for(clist, chats, chat_ids)
        # an asset of a filter a Memory's overlay record lists: a relation of its own, never the
        # Memory link above (see FILTER_LISTED_BASIS)
        filters = _overlay_links_for(clist, overlay_urls, memory_pages,
                                     skip_sid=(memory or {}).get("snap_id"))
        # the creative-tools items that name the file: information, not a link — they say what the
        # file is when no chat message, conversation tie or Memory link does (a tie does: the key
        # names whose conversation's file it is; the filter listing above does not: it is a
        # relation, not what the file is)
        items = _ctp_hits(clist, ctp_index)
        category = _category_of(clist) if clist else "Deleted (tombstone)"
        if items and category in CTP_REPLACES and not chats and not conv_links and not memory:
            category = CTP_CATEGORY

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
            "wal": _entry_wal([r["wal"] for r in clist] +
                              [t["wal"] for t in tomb_by_key.get(key, [])]),
            # the checkpointed version(s) of this file's metadata row, when the -wal changed it
            "meta_prior": [p for p in meta_prior_by_key.get(key, []) if p is not meta],
            "category": category,
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
            # the row as read, for diffing against its superseded version (see _meta_prior_html)
            "meta_raw": meta,
            "children": children,
            "retrieval": retrieval,
            "on_disk": {"paths": paths, "bytes": disk_bytes, "found": found,
                        "scope_by_path": scope_by_path, "cross_scope": cross_scope},
            "memory": memory,
            "memory_basis": basis,
            "filter_memories": filters,
            "ctp_items": items,
            "content_proof": proofs,
            "chats": chats,
            "conv_links": conv_links,
            "tombstones": tomb_by_key.get(key, []),
            "created_sort": created_sort,
        })

    entries.sort(key=lambda e: (e["category"], -e["created_sort"], e["cache_key"]))
    return entries, virtual, wal_info


def _entry_wal(markers):
    """Roll a physical file's per-row WAL markers up to one marker for the whole entry.

    An entry is only ``main-only`` when **every** row behind it is — i.e. the write-ahead log has
    superseded or deleted the whole thing. Any surviving row makes the entry part of the app's
    current state, and a mix is reported as such so the detail panel is worth opening.
    """
    seen = {m for m in markers if m}
    if not seen or seen == {sqlite_open.BOTH}:
        return sqlite_open.BOTH
    if seen == {sqlite_open.MAIN_ONLY}:
        return sqlite_open.MAIN_ONLY
    if seen == {sqlite_open.WAL_ONLY}:
        return sqlite_open.WAL_ONLY
    return sqlite_open.BOTH                                    # mixed: it exists either way


# The category given to a cache file that is on disk but that no row of cache_controller.db
# references. They are real recovered files and must not be invisible just because the index has
# forgotten them.
CC_SCOPE_NOTE = (
    "Every file cache_controller.db indexes, i.e. the com.snap.file_manager_*_SCContent_* "
    "cache folders. Everything else under Library/Caches — the story renders, the URL-keyed "
    "PINCache stores, saved chat media and the cached documents — is the Cached media (Library/Caches) "
    "report's subject, and no file is listed by both.")

# The words a few of this report's explanations use that differ between the platforms: the database
# the Memories come from and the column that identifies one. The cached-file index itself, its tables
# and its SCContent folders are the same on both — written by the content cache the two apps share.
PLATFORM_WORDS = {
    "ios": {
        "scope": CC_SCOPE_NOTE,
        "mem_id": "ZSNAPID",
        "mem_key_src": "ZGALLERYSNAP / gallery.encrypteddb",
        "mem_url": "scdb-27 ZGALLERYSNAP → linked Memory's CDN URL",
    },
    "android": {
        "scope": ("Every file cache_controller.db (databases/native_content_manager) indexes, i.e. "
                  "the com.snap.file_manager_*_SCContent_* folders under "
                  "files/native_content_manager. The app's other cache folders "
                  "(files/file_manager/…, cache/…) are not indexed by it and are not listed here."),
        "mem_id": "memories_snap._id",
        "mem_key_src": "memories.db memories_snap.media_key / media_iv",
        "mem_url": "memories.db → linked Memory's download URL",
    },
}


def _words(entry_or_platform):
    """The :data:`PLATFORM_WORDS` for an entry (or a platform name) — iOS unless it says Android."""
    platform = (entry_or_platform.get("platform") if isinstance(entry_or_platform, dict)
                else entry_or_platform)
    return PLATFORM_WORDS.get(platform or "ios", PLATFORM_WORDS["ios"])

ORPHAN_CATEGORY = "Not in the index"

ORPHAN_BASIS = (
    "This file is in an SCContent cache folder but NOTHING in cache_controller.db refers to it — no "
    "claim, no metadata, no deletion record and no virtualization row. cache_controller.db does not "
    "index every physical file: copies get materialized or consolidated outside the index (an "
    "example is documented in docs/report_cache_controller.md), and an index entry can be dropped "
    "while its file stays on disk. So there is no EXTERNAL_KEY, no owning account and no timestamp "
    "for it here — only the bytes, their hashes, and what the content itself shows. Its filename is "
    "still treated as a CACHE_KEY, which is how it is matched to the Memories and chat reports.")


def orphan_entries(scfull, scparts, claimed_paths, ms_fmt):
    """One entry per on-disk cache file that no ``cache_controller.db`` row accounts for.

    ``claimed_paths`` is every path the indexed entries already resolved to (including bundle
    children and byte-range parts), so a file is only an orphan when nothing in the index led to it.
    Byte-range parts of the same logical file are grouped back under their cache key.
    """
    seen, orphans = set(), []
    def add(key, paths):
        if not paths:
            return
        orphans.append({
            "cache_key": key,
            # an orphan comes from the filesystem, not from a database, so no view applies
            "wal": sqlite_open.BOTH,
            "category": ORPHAN_CATEGORY,
            "claims": [], "users": [],
            "meta": {"size": None, "disk_used": None, "type": None, "storage_type": None,
                     "shard_index": None, "last_read": "", "known_len": None},
            "meta_raw": {},
            "children": [], "retrieval": {},
            "on_disk": {"paths": paths, "bytes": sum(_size(p) for p in paths), "found": True,
                        "scope_by_path": {p: _scope_user(p) for p in paths}, "cross_scope": []},
            "memory": None, "memory_basis": None, "chats": [], "conv_links": [], "tombstones": [],
            "meta_prior": [], "cache_media": [],
            "created_sort": 0, "orphan": True,
        })
    for key, paths in scfull.items():
        keep = [p for p in paths if p.replace("\\", "/") not in claimed_paths]
        if keep and key not in seen:
            seen.add(key)
            add(key, keep)
    for key, parts in scparts.items():
        keep = [p for _off, p in sorted(parts) if p.replace("\\", "/") not in claimed_paths]
        if keep and key not in seen:
            seen.add(key)
            add(key, keep)
    return orphans


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# --------------------------------------------------------------------------- HTML

TYPE_LABELS = {1: "file", 2: "sharded", 3: "bundle"}

# Index-table geometry. The virtual table draws fixed-height rows, so the column track list is
# shared by the header and every row, and cells that overflow are clipped (the full value is always
# in the row's detail).
# Column order shared with the Library/Caches report (see CM_COLS): toggle, category, what
# identifies the file, its context, then type / size / the file itself / links. The two reports
# describe the same kind of thing and used to lay it out differently, which made moving between
# them a re-orientation every time.
# Category is wide enough for its badges: the row is a fixed height, so a third line of content was
# not clipped away but sliced through the middle — which is how a "?" icon came out cut in half.
CC_COLS = ("24px 152px 260px minmax(150px,1fr) 96px 66px 82px 132px minmax(180px,300px)")
CC_ROW_H = 46


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


SESSION_BASIS = (
    "The app's own record of the snap its editor is working on: the row 'SnapEditor-SnapSessionContext' "
    "of docprefitem in userPreferences/pref.docobjects. It names this file by its CACHE_KEY and gives "
    "the claim key ('<UUID>~<position>') and context the file is claimed under, with when the record "
    "was saved and when the snap was edited (Unix seconds and milliseconds, converted to this report's "
    "timezone). Only the latest session is a live row; earlier versions survive in write-ahead-log "
    "frames a later write superseded, and are carved from them — kept only when a claim in "
    "cache_controller.db says the same (same CACHE_KEY, claim key and context). It says which editing "
    "session the file belonged to; it does not say what became of the snap afterwards.")


#: The category of a file an item of an account's creative-tools store names (scripts/data/ctp_items.py)
#: — given only in place of these, and only when no chat message, conversation tie or Memory link says
#: what the file is.
CTP_CATEGORY = "Creative tools asset"
CTP_REPLACES = frozenset(("CDN media", "Other", "Chat media"))

CTP_BASIS = (
    "This file is named by an item of an account's creative-tools item store: a claim's EXTERNAL_KEY, "
    "or the part of it after a word and ':' or '~' (music:<url>, customSticker~<id>; never '://'), is "
    "a text the item holds — a text of its payload such as the URL of one of its assets, its item_id or "
    "its own id — or, failing that, that part read as base64 is the same bytes as the item's own id "
    "(payload field 6, or its item_id or own id read as base64), in either base64 alphabet, padded or "
    "not. The store is table ctp__item_5 of Documents/user_scoped/<userHash>/DocObjects/"
    "primary.docobjects, one per account (userHash is SHA-256 of the account's user id), where the app "
    "keeps the items of the feeds its camera's creative tools (captions, filters, stickers) are filled "
    "from. Each row is one item: its item_id column and a document (column p). In the layout this "
    "report reads — a FlatBuffers document whose slot 0 repeats the item_id — slot 3 holds the item's "
    "own id, slot 4 its feed and slot 2 a protobuf message whose texts include the item's asset URLs; "
    "a document of another layout is not read. The feed is named from the store's feed tree "
    "(ctp__feedtree: each feed's TYPE and CONTEXT and the creativetools service it is fetched from) "
    "when the tree lists it. Texts are compared whole and exactly (a URL in the one form the reports "
    "share, so an empty trailing '?' or '#' and the letter case of its scheme aside), and the rule that "
    "matched is stated per item: never a URL's query parameters on their own (bo= is a set of fetch "
    "options many cached files share), never a part of a text, and a text several items of one store "
    "hold is attributed only to the one whose own id it is — one item listed in several feeds is one "
    "item, shown in each; different items holding it, to none. Both readings of the store are read, "
    "with and without its -wal. On the "
    "stores examined, an item in that layout carries no date, and nothing in it says the account put "
    "the item in a snap, or when. Its texts are shown as stored, under their protobuf field numbers, "
    "which are numbers and not names.")

#: Beside a context-2/3 claim of a file a creative-tools item names, when no chat message, conversation
#: tie or Memory link says what the file is (and never beside a key of a chat message's shape): the
#: context's name is a reading of the number, and these contexts claim more than chat media.
MCT_CTP_NOTE = (
    "The label \"Chat media\" is read from the context number alone. This file is claimed under that "
    "context, no chat message links to it, and an item of a creative-tools store names it instead "
    "(see the section on the creative-tools item below): the context claims creative-tools assets — "
    "custom stickers among them — as well as chat media.")

FEED_NOT_IN_TREE_BASIS = (
    "The store's feed tree (ctp__feedtree) does not list this feed, so it is not named: the feed id "
    "is shown as the item stores it — feed:<TYPE>-<CONTEXT>-<n> — and its numbers are not read as a "
    "name.")

#: A feed missing from the trees that were read, in a store with a tree document that was not.
FEED_TREE_UNDECODED_BASIS = (
    "The store's feed tree (ctp__feedtree) has a document this report cannot read (it does not hold "
    "an archive of a CTPFeed), so whether the tree lists this feed is not known: the feed id is shown "
    "as the item stores it and is not named.")

UNDECODED_ITEM_BASIS = (
    "This item's document (ctp__item_5.p) does not have the layout this report reads — its FlatBuffers "
    "slot 0 is not the row's item_id — so nothing is read from it, and what it holds (a date, a snap, "
    "anything else) is not known. The item is still an item: its item_id column is what the claim key "
    "names.")


#: The categories whose files can be a lead: the snap editor's working copies, files no claim names,
#: and Memory-shaped claims whose Memory row is gone. A file of no recognised shape ("Other") only when
#: the app itself claimed it as Memories media (context 19): otherwise every image the app cached
#: during a busy session would be a "possible Memory".
LEAD_CATEGORIES = {"Snap editor", ORPHAN_CATEGORY, "Memory media"}


def _head_of(paths, size=64):
    """The first bytes of a cached file (its whole copy, else its first part), for sniffing."""
    for path in sorted(paths):
        try:
            with open(path, "rb") as fh:
                return fh.read(size)
        except OSError:
            continue
    return b""


def _leads_html(entry, rel_prefix, closure=None):
    """The *Possible Memory — NOT proven* panel; ``""`` when the file has no lead."""
    found = entry.get("leads")
    if not found:
        return ""
    rows = []
    for lead in found["leads"]:
        sid = lead["snap_id"]
        link = report_ui.xref(
            f'<a class="chip lead" target="scauto_memories" '
            f'href="{rel_prefix}Memories/Memories_report.html#mem-{_esc(sid)}">possible: '
            f'{_esc(sid)}</a>', [("mem", f"mem-{sid}")], closure=closure)
        pairs = "<br>".join(f"{_esc(p['file'])} vs {_esc(p['memory'])}: "
                            f"{_signed(p['delta_s'])}" for p in lead["pairs"][:4])
        rows.append(f"<tr><td>{link}</td><td>{_signed(lead['best_delta_s'])}</td>"
                    f"<td class='mono'>{pairs}</td></tr>")
    ids = _esc(json.dumps([lead["snap_id"] for lead in found["leads"]]))
    copy_button = (f"<button class='copyids' onclick='scCopySnapIds({ids},"
                   f"&quot;possible Memories of this file&quot;)'>📋 Copy snap IDs</button>")
    more = found["in_window"] - len(found["leads"])
    return ("<div class='sect'>Possible Memory — NOT proven"
            + _info(memory_leads.basis(found["window_s"])) + "</div>"
            f"<div class='leadnote'>{found['in_window']} Memory/Memories of this kind have a time within "
            f"{found['window_s'] // 60} minutes of this file's"
            + (f" (the closest {len(found['leads'])} shown)" if more > 0 else "")
            + (" · the app claimed this file as Memories media (context 19)"
               if found["leads"] and found["leads"][0].get("ctx19") else "")
            + " " + copy_button + " <span class='muted'>to retrieve them from Snapchat&#39;s "
              "servers and compare</span></div>"
            "<table class='sub'><tr><th>Memory</th><th>closest</th>"
            "<th>file time vs Memory time (Memory minus file)</th></tr>" + "".join(rows) + "</table>")


def _tc(records, epochfmt):
    """``{"tc": [...]}`` — the inode-change times of device records, when there are any (paid per
    row, so absent rather than empty). See report_ui.FS_TIME_KINDS."""
    keys = report_ui.ts_keys(*report_ui.fs_times(records, epochfmt or (lambda seconds: ""),
                                                 kinds=("ctime",)))
    return {"tc": keys} if keys else {}


def _signed(seconds):
    """A difference as the leads show it; tenths under ten seconds, where "−0 s" would hide them.

    memory_leads.MEMORY_JS ``scLeadSigned`` writes the same text on the Memory's side.
    """
    sign = "+" if seconds >= 0 else "−"
    seconds = abs(seconds)
    if seconds < 10:
        return f"{sign}{seconds:.1f} s"
    if seconds < 120:
        return f"{sign}{seconds:.0f} s"
    return f"{sign}{seconds / 60:.1f} min"


def _session_html(entry, src_root, manifest):
    """The snap editor's session record(s) naming this file, when any survives."""
    rows = []
    for rec in entry.get("session") or []:
        if rec["wal"] == sqlite_open.CARVED:
            read = ('<span class="walbadge mainonly">carved</span>'
                    + _info(sqlite_open.MARKER_HELP[sqlite_open.CARVED]))
        else:
            read = _wal_cell(rec["wal"])
        rows.append(f"<tr><td>{_esc(rec.get('saved'))}</td><td>{_esc(rec.get('edited'))}</td>"
                    f"<td class='mono'>{_esc(rec['claim_uuid'])}~{_esc(rec['position'])}</td>"
                    f"<td>{_esc(_mct_label(rec['context']))}</td>"
                    f"<td class='mono'>{_esc(device_path(rec['store'], src_root, manifest))}</td>"
                    f"<td>{read}</td></tr>")
    if not rows:
        return ""
    return ("<div class='sect'>Snap editor session record — pref.docobjects › docprefitem "
            "'SnapEditor-SnapSessionContext'" + _info(SESSION_BASIS) + "</div>"
            "<table class='sub'><tr><th>saved</th><th>snap edited</th><th>claim key</th>"
            "<th>context</th><th>store</th><th>(read from)</th></tr>" + "".join(rows) + "</table>")


def _ctp_feed_cell(hit):
    """The item's feed, named only as the store's feed tree names it."""
    info = hit.get("feed_info")
    if not info:
        return ""
    feed = f"<span class='mono'>{_esc(info['feed'])}</span>"
    if not info["in_tree"]:
        if info.get("tree_unread"):
            return feed + " — ctp__feedtree not decoded" + _info(FEED_TREE_UNDECODED_BASIS)
        return feed + " — not in ctp__feedtree" + _info(FEED_NOT_IN_TREE_BASIS)
    said = " · ".join(bit for bit in (f"NAME {info['name']}" if info["name"] else "",
                                       f"COMPUTE_ENDPOINT {info['endpoint']}" if info["endpoint"]
                                       else "") if bit)
    lists = ("Only the checkpointed version of the store's feed tree (ctp__feedtree read without its "
             "-wal) lists this feed" if info.get("prior")
             else "The store's feed tree (ctp__feedtree) lists this feed")
    return (feed + (f" — {_esc(info['short'])}" if info["short"] else " — unnamed in ctp__feedtree")
            + (" (prior state)" if info.get("prior") else "")
            + _info(f"{lists} — TYPE {info['type']}, CONTEXT {info['context']} — "
                    + (f"with {said}" if said else "with no NAME and no COMPUTE_ENDPOINT")
                    + ". The short name is the word after '.creativetools.' in the endpoint, else "
                      "the NAME, as stored."
                    + (" The -wal's version of the tree does not list it, so the name is prior "
                       "state, not the tree as the app last left it." if info.get("prior") else "")))


def _ctp_texts(texts):
    """An item's texts as the detail lists them — its URLs first, then the rest in field order — and
    how many more there are than are shown."""
    ordered = ([(p, t) for p, t in texts if snap_overlay.normalise_url(t)]
               + [(p, t) for p, t in texts if not snap_overlay.normalise_url(t)])
    return ordered[:ctp_items.MAX_TEXTS], max(0, len(ordered) - ctp_items.MAX_TEXTS)


def _ctp_items_html(entry, src_root, manifest):
    """The creative-tools items that name this file (``entry["ctp_items"]``); ``""`` when none.

    Information on the file, not a link: the item store has no report of its own. One row per item,
    each with its own "?", and under the table the texts each item holds, as stored.
    """
    hits = entry.get("ctp_items") or []
    if not hits:
        return ""
    rows, texts = [], []
    for hit in hits:
        who = _esc(hit.get("account") or _account_label(hit["user_hash"]))
        whose = {True: " — the claiming account&#39;s own store",
                 False: " — another account&#39;s store"}.get(hit.get("own_account"), "")
        item = f"<span class='mono'>{_esc(hit['item_id'])}</span>"
        if hit.get("own_id") and hit["own_id"] != hit["item_id"]:
            item += f"<br>own id <span class='mono'>{_esc(hit['own_id'])}</span>"
        if not hit.get("decoded"):
            item += ("<br><span class='muted'>document not decoded (layout differs)</span>"
                     + _info(UNDECODED_ITEM_BASIS))
        kind = (f"payload field 2.{hit['kind']}" if hit.get("kind") is not None
                else "payload not readable" if hit.get("decoded") and not hit.get("payload_ok")
                else "")
        rule = ctp_items.RULE_LABELS.get(hit.get("rule"), "").format(prefix=hit.get("prefix") or "")
        where = "<br>".join(_esc(w) for w, _text in hit.get("where") or ())
        rows.append(f"<tr><td>{who}{whose}<br><span class='mono'>"
                    f"{_esc(device_path(hit['store'], src_root, manifest))}</span></td>"
                    f"<td>{item}</td><td>{_ctp_feed_cell(hit)}</td><td>{_esc(kind)}</td>"
                    f"<td>{_esc(rule)}{_info(hit.get('basis'))}<br><span class='mono'>{where}</span></td>"
                    f"<td>{_wal_cell(hit.get('wal'))}</td></tr>")
        shown, more = _ctp_texts(hit.get("texts") or ())
        if shown:
            texts.append(f"<div class='scopehdr'>texts in item <span class='mono'>"
                         f"{_esc(hit['item_id'])}</span> (as stored)</div><div class='grid'>"
                         + "".join(f"<div class='k'>payload {_esc(path)}</div>"
                                   f"<div class='v'>{_esc(text)}</div>" for path, text in shown)
                         + "</div>"
                         + (f"<div class='muted'>+{more} more not shown</div>" if more else ""))
    return ("<div class='sect'>Named by a creative-tools item — primary.docobjects › ctp__item_5"
            + _info(CTP_BASIS) + "</div>"
            "<table class='sub'><tr><th>account (store)</th><th>item_id</th><th>feed</th>"
            "<th>kind</th><th>the claim&#39;s key is</th><th>(read from)</th></tr>"
            + "".join(rows) + "</table>" + "".join(texts))


# What the search box should match for a row read from only one of the two database views, so an
# examiner can type "deleted since checkpoint" or "wal" and find them.
_WAL_SEARCH = {
    sqlite_open.WAL_ONLY: "wal-only write-ahead log not yet checkpointed recent",
    sqlite_open.MAIN_ONLY: ("main-only without wal superseded deleted since checkpoint prior state "
                            "recovered"),
}

_WAL_LABEL = {sqlite_open.WAL_ONLY: "-wal only", sqlite_open.MAIN_ONLY: "no -wal only"}


WAL_SUMMARY_BASIS = (
    "This database was read twice: once with its write-ahead log (-wal) applied, which is the "
    "app's current state and what any ordinary SQLite tool shows, and once from the database file "
    "alone, which is the state as of the last checkpoint. Rows found in only one of the two are "
    "badged in the table. \"-wal only\" rows are recent and not yet checkpointed. \"no -wal only\" "
    "rows were changed or deleted after the last checkpoint, so they are recoverable prior state "
    "and must NOT be reported as current. Both readings are taken from copies staged in the "
    "report's working directory; the source database is never modified or checkpointed.")


def _wal_summary(wal_infos, wal_only, main_only, meta_changed=0):
    """The header line describing each source database's -wal and what the two readings found."""
    bits = []
    for info in (wal_infos or []):
        described = sqlite_open.describe(info)
        if described:
            bits.append(f"{html.escape(os.path.basename(info.get('path') or ''))}: "
                        f"{html.escape(described)}")
    if not bits:
        return ""
    counts = []
    if wal_only:
        counts.append(f"<b>{wal_only}</b> only with the -wal applied")
    if main_only:
        counts.append(f"<b>{main_only}</b> only without it")
    if meta_changed:
        counts.append(f"<b>{meta_changed}</b> whose metadata changed since the checkpoint")
    tail = (" &middot; " + ", ".join(counts)) if counts else ""
    return (f'<div class="sum">Write-ahead log: {" &middot; ".join(bits)}{tail}'
            f'{_info(WAL_SUMMARY_BASIS)}</div>')


def _wal_badge(marker):
    """A badge + '?' for a row that only one of the two database readings contains."""
    if marker not in _WAL_LABEL:
        return ""                                              # the ordinary case needs no badge
    cls = "walonly" if marker == sqlite_open.WAL_ONLY else "mainonly"
    return (f'<span class="walbadge {cls}">{_WAL_LABEL[marker]}</span>'
            + _info(sqlite_open.MARKER_HELP[marker]))


def _wal_cell(marker):
    """Which of the two database readings a detail row came from, spelled out."""
    if marker == sqlite_open.WAL_ONLY:
        return _wal_badge(marker)
    if marker == sqlite_open.MAIN_ONLY:
        return _wal_badge(marker)
    return ('<span class="muted">both</span>'
            + _info(sqlite_open.MARKER_HELP[sqlite_open.BOTH]))


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
    if entry.get("orphan"):
        return ORPHAN_BASIS
    if entry["on_disk"]["found"]:
        n = len(entry["on_disk"]["paths"])
        base = ("The CACHE_KEY is the on-disk filename inside a com.snap.file_manager_*_SCContent_* "
                "folder. Sharded media is stored as <CACHE_KEY>_<start>-<end> byte-range parts "
                "(plus a PREFETCH chunk) which are concatenated in offset order; a bundle's "
                "children are stored as <CACHE_KEY>_<child name> and are resolved individually. "
                f"{n} file(s) matched here.")
        if entry["on_disk"].get("cross_scope"):
            base += " ⚠ " + _cross_scope_basis(entry)
        return base
    return ("No file named after this CACHE_KEY (or its parts/children) was found in any "
            "SCContent folder — the claim exists in the index but the bytes are not on disk "
            "(evicted, or not captured by the extraction).")


def _decrypted_basis(entry):
    """Explanation for the link to a Memories-report copy of an encrypted cache file."""
    snaps = sorted({d.get("snap_id", "") for d in entry.get("decrypted") or []})
    return ("The bytes cached here are encrypted, so they cannot be displayed as they are stored. "
            "The Memories report decrypted this exact CACHE_KEY with the AES-256-CBC key/IV of "
            f"Memory {', '.join(s for s in snaps if s) or '(unknown)'} (from "
            f"{_words(entry)['mem_key_src']}) and wrote the plaintext media beside its report; this "
            "links to that decrypted copy, which is a derived file — the original cached bytes' "
            "hashes are shown above.")


MULTI_TARGET_BASIS = (
    "This entry corresponds to SEVERAL rows in the linked report, so the link opens that report "
    "filtered to this entry's identifier with every matching row expanded, rather than jumping to "
    "one of them. What you land on is the complete set of matches — the search box shows the query "
    "that produced it, and clearing it restores the full report.")


def _links_html(entry, rel_prefix, compact=False, closure=None):
    """Cross-report link chips (Memory / chat) plus the on-disk found/missing chip.

    ``compact`` is the index-row form: only the cross-report links, without the "?" explanations
    (which are long) and without the on-disk/cross-scope chips, which would duplicate the row's File
    cell and overflow the row. The row's expanded detail repeats all of it with the explanations.
    """
    def why(text):
        return "" if compact else _info(text)

    chips = []
    if entry["memory"]:
        sid = entry["memory"]["snap_id"]
        page = entry["memory"].get("page")
        chips.append(report_ui.xref(
            f'<a class="chip mem" target="scauto_memories" '
            f'title="open this Memory\'s row in the Memories index" '
            f'href="{rel_prefix}Memories/Memories_report.html#mem-{_esc(sid)}">'
            f'🧠 Memory {_esc(sid[:8])}…'
            + CONTENT_MARKS.get(entry["memory"].get("by_content"), '') + '</a>',
            [("mem", f"mem-{sid}")], closure=closure, brief=compact)
            + why(entry.get("memory_basis")))
        if page:
            chips.append(report_ui.xref(
                f'<a class="chip mem" target="scauto_memories" '
                f'title="open this Memory\'s own detail page" '
                f'href="{rel_prefix}Memories/{_esc(page)}#mem-{_esc(sid)}">📄 detail</a>',
                [("mem", f"mem-{sid}")], closure=closure, brief=compact))
    # An asset of a filter a Memory's overlay record lists — dashed, and worded so it cannot be read
    # as the Memory link above. One asset is commonly listed for many Memories, so several are ONE
    # chip that opens the Memories report filtered to all of them (as the Library/Caches chip does).
    filters = entry.get("filter_memories") or []
    if len(filters) == 1:
        fm = filters[0]
        sid = fm["snap_id"]
        chips.append(report_ui.xref(
            f'<a class="chip mem filt" target="scauto_memories" '
            f'title="an asset of a geofilter this Memory\'s overlay record lists — not the Memory\'s '
            f'media" href="{rel_prefix}Memories/Memories_report.html#mem-{_esc(sid)}">'
            f'🧠 Memory {_esc(sid[:8])}… · filter {"selected" if fm.get("selected") else "listed"}'
            f'</a>', [("mem", f"mem-{sid}")], closure=closure, brief=compact)
            + why(fm.get("basis")))
        if fm.get("page"):
            chips.append(report_ui.xref(
                f'<a class="chip mem filt" target="scauto_memories" '
                f'title="open this Memory\'s own detail page" '
                f'href="{rel_prefix}Memories/{_esc(fm["page"])}#mem-{_esc(sid)}">📄 detail</a>',
                [("mem", f"mem-{sid}")], closure=closure, brief=compact))
    elif filters:
        sids = [fm["snap_id"] for fm in filters]
        # narrowed first, so the link does not open the Memories report filtered to rows it lacks
        kept, dropped = report_ui.narrow(closure, "mem", sids, lambda s: f"mem-{s}")
        shown = kept or sids
        # one only where a partial report narrowed the list: a full report has two or more here
        one = len(shown) == 1
        whom = "the Memory" if one else f"the {len(shown)} Memories"
        chips.append(report_ui.xref(
            f'<a class="chip mem filt" target="scauto_memories" '
            f'href="{rel_prefix}Memories/Memories_report.html{report_ui.find_fragment(shown)}" '
            f'title="open the Memories report filtered to {whom} whose overlay record lists this '
            f'asset, {"expanded — not its media" if one else "all expanded — not their media"}">'
            f'🧠 {len(shown)} {"Memory" if one else "Memories"} · filter listed</a>',
            [("mem", f"mem-{s}") for s in shown], closure=closure, brief=compact)
            + why(FILTER_MANY_LINK_BASIS + " " + FILTER_MANY_BASIS
                  + (f" {dropped} further {'Memory' if dropped == 1 else 'Memories'} whose record "
                     f"lists it {'is' if dropped == 1 else 'are'} not part of this partial report."
                     if kept and dropped else "")))
    for ch in entry["chats"]:
        conv = ch.get("conversation_id", "")
        smid = ch.get("server_message_id", "")
        anchor = _esc(ch.get("anchor") or ("cf-" + entry["cache_key"]))
        # The Conversations report has one page per conversation, so its manifest states the exact
        # page in `href` (relative to the reports root); the legacy report is one document.
        if ch.get("href"):
            url, target = f'{rel_prefix}{_esc(ch["href"])}', "scauto_convs"
            name = (ch.get("title") or "")[:24] or (conv[:8] + "…" if conv else "")
        else:
            base = ch.get("base") or "Communications/Communications_report.html"
            url = f'{rel_prefix}{_esc(base)}#{anchor}'
            target, name = "scauto_comms_legacy", (conv[:8] + "…" if conv else "")
        label = f' {_esc(name)} msg {_esc(smid)}' if name else ""
        # the legacy report has no row selection, so a link into it names no target and stays as it is
        target_rows = [("msg", f"conv-{conv}|msg-{smid}")] if (ch.get("href") and conv and smid) else []
        chips.append(report_ui.xref(f'<a class="chip chat" target="{target}" href="{url}">'
                                    f'💬 Chat{label}</a>',
                                    target_rows, closure=closure, brief=compact)
                     + why(ch.get("basis")))
    # A claim naming a message no row is there for: tied to the conversation, dashed, and never
    # worded as a message link. A conversation the Conversations report lists opens at its page's
    # header (the same target window and fragment the Contacts report uses); one it does not list has
    # nothing to open, so its chip filters this report to every entry naming the same conversation.
    for tie in entry.get("conv_links") or ():
        if tie.get("listed"):
            name = (tie.get("title") or "")[:24] or (tie["conversation_id"][:8] + "…")
            state = "not in arroyo.db" if tie.get("held") is False else "not listed"
            chips.append(report_ui.xref(
                f'<a class="chip chat gone" target="scauto_conv_page" '
                f'href="{rel_prefix}{_esc(tie["href"])}#{_esc(tie["anchor"])}" '
                f'title="open the conversation this claim names — not a message">'
                f'💬 {_esc(name)} · msg {_esc(tie["number"])} — {state}</a>',
                [("conv", tie["anchor"])], closure=closure, brief=compact)
                + why(tie.get("basis")))
        else:
            chips.append(
                f'<a class="chip chat gone" '
                f'href="{_esc(report_ui.find_fragment([tie["conversation_id"]]))}" '
                f'title="show every cache entry whose claim names this conversation">'
                f'💬 conversation {_esc(tie["conversation_id"][:8])}… — in no report</a>'
                + why(tie.get("basis")))
    # A copy of these bytes found under Library/Caches by the cached-media report. The same cached
    # content routinely sits under several paths there, so when there is more than one the chip is
    # ONE link that opens that report filtered to this CACHE_KEY with every match expanded — the
    # complete set — instead of a chip per row, or a chip that silently shows only the first.
    cms = entry.get("cache_media") or []
    if len(cms) == 1:
        chips.append(report_ui.xref(
            f'<a class="chip cm" target="scauto_cachemedia" '
            f'href="{rel_prefix}CacheMedia/CacheMedia_report.html#{_esc(cms[0]["anchor"])}">'
            f'🗂 Library/Caches</a>',
            [("cm", cms[0].get("anchor"))], closure=closure, brief=compact)
            + why(cms[0].get("basis")))
    elif cms:
        # One CACHE_KEY, several rows over there — the fragment cannot be narrowed row by row, so the
        # count states how many of them this extract actually holds.
        rows_here = [cm for cm in cms
                     if closure is None or closure.has("cm", cm.get("anchor"))]
        shown = rows_here or cms
        chips.append(report_ui.xref(
            f'<a class="chip cm" target="scauto_cachemedia" '
            f'href="{rel_prefix}CacheMedia/CacheMedia_report.html'
            f'{report_ui.find_fragment([entry["cache_key"]])}" '
            f'title="open the Library/Caches report filtered to this CACHE_KEY, with all '
            f'{len(shown)} matching file(s) expanded">'
            f'🗂 Library/Caches ({len(shown)})</a>',
            [("cm", cm.get("anchor")) for cm in shown], closure=closure, brief=compact)
            + why(MULTI_TARGET_BASIS + " " + (cms[0].get("basis") or "")
                  + (f" {len(cms) - len(rows_here)} further copy/copies of these bytes are not part "
                     f"of this partial report." if rows_here and len(rows_here) != len(cms) else "")))
    if not compact:
        if entry["on_disk"]["found"]:
            chips.append('<span class="chip ok">📁 on disk</span>' + why(_on_disk_basis(entry)))
        elif entry["claims"]:
            chips.append('<span class="chip miss">— not on disk</span>' + why(_on_disk_basis(entry)))
        if entry["on_disk"].get("cross_scope"):
            chips.append('<span class="chip warn">⚠ cross-scope copy</span>'
                         + why(_cross_scope_basis(entry)))
    if not chips:
        return ""
    if not compact:
        return "".join(chips)
    # One line for the index row: a collapsed virtual row is exactly CC_ROW_H tall, so chips that
    # wrap onto a second line are sliced through rather than shown short.
    return '<div class="chiprow">' + "".join(chips) + "</div>"


def _decrypted_here(entry, closure):
    """The Memories-decrypted copies of this entry that are actually **in this folder**.

    The manifest lists a copy per Memory that decrypted these bytes; a partial extract holds only the
    Memories it includes, and prunes the rest of the plaintext. Filtering here rather than at the link
    is what keeps a row from showing a preview that 404s and a hash table for a file nobody can open.
    """
    dec = entry.get("decrypted") or []
    if closure is None:
        return list(dec)
    return [d for d in dec if closure.has("mem", f'mem-{d.get("snap_id")}')]


def _file_cell(entry, rel_prefix, closure=None):
    """The index row's file cell: a real preview / play button for the bytes, not a tiny glyph.

    Order of preference — the plaintext cached file itself, then the copy the Memories report
    decrypted, then a plain statement of why there is nothing to open.
    """
    if entry.get("view"):
        ext = entry.get("view_ext") or ""
        if entry.get("view_is_image"):
            return (f'<a class="filebtn img" href="{_esc(entry["view"])}" target="_blank" '
                    f'title="open the cached {_esc(ext)}">'
                    f'<img src="{_esc(entry["view"])}" loading="lazy">'
                    f'<span class="lbl">{_esc(ext)}</span></a>')
        if entry.get("poster"):
            # the still is this tool's own frame, not device data — POSTER_BASIS says so on the row
            return (f'<a class="filebtn img vid" href="{_esc(entry["view"])}" target="_blank" '
                    f'title="open the cached {_esc(ext)} (the still is a frame extracted by this '
                    f'tool, not a cached file)">'
                    f'<img src="{_esc(entry["poster"])}" loading="lazy">'
                    f'<span class="lbl">▶ {_esc(ext)}</span></a>')
        if ext not in PLAYABLE_EXTS:
            # recognised media this report cannot render inline (a HEIC/AVIF still): openable, and
            # named for what it is, but not dressed up as something that plays
            return (f'<a class="filebtn" href="{_esc(entry["view"])}" target="_blank" '
                    f'title="open the cached {_esc(ext)}">{_esc(ext)}</a>')
        return (f'<a class="filebtn play" href="{_esc(entry["view"])}" target="_blank" '
                f'title="open the cached {_esc(ext)}">▶ <span class="lbl">{_esc(ext)}</span></a>')
    dec = _decrypted_here(entry, closure)
    if not dec and (entry.get("decrypted") or []):
        # The plaintext exists, but it belongs to a Memory this extract leaves out, so the Memories
        # folder does not hold it either. Saying that beats a broken image, and beats falling through
        # to "not on disk" — these bytes are on disk, and they were decrypted.
        return ('<span class="filenone" title="A Memory that is not part of this partial report '
                'decrypted these bytes, so its plaintext copy is not in this folder.">'
                '&#128275; decrypted elsewhere &#8856;</span>')
    if dec:
        best = max(dec, key=lambda d: d.get("bytes") or 0)
        url = f'{rel_prefix}Memories/{best.get("path", "")}'
        if best.get("ext") in ("jpg", "png", "webp"):
            return (f'<a class="filebtn img dec" href="{_esc(url)}" target="scauto_memories" '
                    f'title="decrypted by the Memories report"><img src="{_esc(url)}" loading="lazy">'
                    f'<span class="lbl">🔓 {_esc(best.get("ext"))}</span></a>')
        return (f'<a class="filebtn play dec" href="{_esc(url)}" target="scauto_memories" '
                f'title="decrypted by the Memories report">🔓 <span class="lbl">'
                f'{_esc(best.get("ext"))}</span></a>')
    if not entry["on_disk"]["found"]:
        return '<span class="filenone">not on disk</span>'
    if entry.get("ondisk_bytes") == 0:
        return '<span class="filenone">0 bytes</span>'
    # Name what the bytes are. This cell used to read "🔒 encrypted" for everything that was not
    # one of four media types, which across the test corpus was wrong for 97% of the files it
    # marked — lens bundles, fonts, subtitles, HTML, JSON and protobuf were all reported as locked.
    label = entry.get("ondisk_label") or "unrecognized"
    if entry.get("ondisk_encrypted"):
        return f'<span class="filenone">🔒 {_esc(label)}</span>'
    return f'<span class="filenone">{_esc(label)}</span>'


# Metadata columns worth diffing between the two database readings. The rest (blobs, and columns
# that vary by app version) are compared too — this only fixes a sensible display order.
_META_DIFF_COLS = ("FILE_SIZE_BYTES", "TOTAL_DISK_USED_BYTES", "KNOWN_CONTENT_LENGTH_BYTES",
                   "TYPE", "STORAGE_TYPE", "SHARD_INDEX", "LAST_READ_TIMESTAMP_MILLIS")

DEVICE_MTIME_BASIS = device_fs.DEVICE_FS_BASIS

ENCRYPTED_BASIS = (
    "The bytes on disk match no known file signature, their Shannon entropy is at least 7.5 bits "
    "per byte, and the file's length is a multiple of the AES block size (16 bytes) — the "
    "signature of block-cipher output. Snapchat encrypts locally-captured Memory media this way "
    "(AES-256-CBC with a per-snap key), which is why the Memories report can decrypt those and "
    "this report links to its output. A file marked here with no such link had no key available.")

NOT_MEDIA_BASIS = (
    "Identified by magic bytes. It is NOT encrypted — it simply is not one of the image/video "
    "formats this report can display inline. Lens bundles (LZC), fonts, subtitle tracks, HTML, "
    "JSON and protobuf blobs all land here. The file is on disk at the path(s) above and can be "
    "opened with a suitable tool. (Earlier versions of this report labelled every one of these "
    "'encrypted', which overstated what was locked away.)")

META_PRIOR_BASIS = (
    "This file's CACHE_FILE_METADATA row was rewritten after the database's last checkpoint, so "
    "the database file and its write-ahead log (-wal) hold two different versions of it. The "
    "values above are the current ones (the -wal applied); the values here are what the row said "
    "before, recovered by reading the database file without its -wal. A tool that reads the "
    "database normally can only ever show the current version.")


def _meta_prior_html(entry):
    """The checkpointed (superseded) version of a file's metadata row, when the -wal changed it."""
    priors = entry.get("meta_prior") or []
    if not priors:
        return ""
    current = entry.get("meta_raw") or {}
    out = []
    for prior in priors:
        cols = [c for c in _META_DIFF_COLS if c in prior]
        cols += [c for c in prior if c not in cols and not isinstance(prior[c], (bytes, bytearray))]
        rows = []
        for col in cols:
            was, now = prior.get(col), current.get(col)
            if was == now:
                continue
            rows.append(f"<tr><td class='mono'>{_esc(col)}</td><td>{_esc(was)}</td>"
                        f"<td>{_esc(now)}</td></tr>")
        if rows:
            out.append("<table class='sub'><tr><th>column</th>"
                       "<th>before the last checkpoint</th><th>current (-wal applied)</th></tr>"
                       + "".join(rows) + "</table>")
    if not out:
        return ""
    return ("<div class='sect'>CACHE_FILE_METADATA — superseded version"
            + _info(META_PRIOR_BASIS) + "</div>" + "".join(out))


#: The most Memories the detail section of one file lists. One asset (a font, say) can be listed by a
#: large share of a gallery's Memories, and a row each would make that file's detail — and the chunk
#: of 250 details it is in — grow with the gallery. The chip opens all of them, the search finds the
#: file by every one's snap id, and each Memory's page lists its cached filter assets.
FILTER_DETAIL_ROWS = 200


def _filter_rows_shown(listed, closure=None):
    """The links :func:`_filter_memories_html` gives a row, in their own order (by snap id): all of
    them up to :data:`FILTER_DETAIL_ROWS`, else first those whose record names the filter as
    selected, then (in a partial report) those whose Memory it holds, then by snap id."""
    if len(listed) <= FILTER_DETAIL_ROWS:
        return list(listed)
    held = set(report_ui.narrow(closure, "mem", [fm["snap_id"] for fm in listed],
                                lambda s: f"mem-{s}")[0]) if closure is not None else set()
    ranked = sorted(listed, key=lambda fm: (fm.get("selected") is not True,
                                            fm["snap_id"] not in held, fm["snap_id"]))
    keep = {id(fm) for fm in ranked[:FILTER_DETAIL_ROWS]}
    return [fm for fm in listed if id(fm) in keep]


def _filter_memories_html(entry, rel_prefix, closure=None):
    """The Memories whose overlay record lists this file as an asset of a geofilter; ``""`` when none.

    One row per Memory, each with its own "?" — the field and what the record says about the selected
    filter differ from one Memory to the next; the method is the section's "?" — up to
    :data:`FILTER_DETAIL_ROWS` rows, and a line saying how many more there are and where they are.
    """
    listed = entry.get("filter_memories") or ()
    shown = _filter_rows_shown(listed, closure)
    rows = []
    for fm in shown:
        sid = fm["snap_id"]
        link = report_ui.xref(
            f'<a target="scauto_memories" '
            f'href="{rel_prefix}Memories/Memories_report.html#mem-{_esc(sid)}">{_esc(sid)}</a>',
            [("mem", f"mem-{sid}")], closure=closure)
        if fm.get("page"):
            link += " " + report_ui.xref(
                f'<a target="scauto_memories" title="open this Memory\'s own detail page" '
                f'href="{rel_prefix}Memories/{_esc(fm["page"])}#mem-{_esc(sid)}">📄</a>',
                [("mem", f"mem-{sid}")], closure=closure)
        where = _esc(fm["field"]) + "".join(f"<br>also {_esc(f)}" for f in fm.get("fields") or ())
        kind = " · ".join(bit for bit in (fm.get("filter_type"), fm.get("group"),
                                           f"idValue {fm['filter_id']}" if fm.get("filter_id") else "")
                          if bit)
        claim = (f"<span class='mono'>{_esc(fm['external_key'])}</span><br>"
                 f"<span class='mono'>{_esc(fm['claim_user'])}</span>"
                 + (" <span class='xscope'>another account&#39;s claim</span>"
                    if fm.get("cross_account") else ""))
        rows.append(f"<tr><td class='mono'>{link}{_wal_badge(fm.get('wal'))}"
                    f"{_info(_filter_row_basis(fm))}</td>"
                    f"<td>{_esc(fm['role'])}</td><td class='mono'>{where}</td>"
                    f"<td>{_esc(kind)}</td>"
                    f"<td>{_esc(snap_overlay.SELECTED_TEXT[fm.get('selected')])}</td>"
                    f"<td>{claim}</td></tr>")
    if not rows:
        return ""
    return ("<div class='sect'>Listed with a Memory&#39;s filters — not its media"
            + _info(FILTER_LISTED_BASIS) + "</div>"
            "<table class='sub'><tr><th>Memory</th><th>asset</th><th>where in the overlay record</th>"
            "<th>filter</th><th>selected, per the record</th><th>claim (EXTERNAL_KEY, USER_ID)</th>"
            "</tr>" + "".join(rows) + "</table>" + _filter_rows_more(len(listed) - len(shown)))


def _filter_rows_more(more):
    """The line under a capped detail section: how many listing Memories it does not show, and
    where they are (``""`` when it shows them all)."""
    if not more:
        return ""
    return (f"<div class='muted'>+{more} more {'Memory lists' if more == 1 else 'Memories list'} "
            f"this asset, not shown here: the link above opens the Memories report filtered to all "
            f"of them (in a partial report, those it holds), this report's search finds this file by "
            f"each one's snap id, and each Memory's page lists its cached filter assets.</div>")


def _detail_html(entry, rel_prefix, src_root, manifest, closure=None):
    """Expandable detail block for one physical cache file."""
    e = entry
    parts = []

    if e.get("orphan"):
        parts.append('<div class="orphan">This file is <b>not referenced by cache_controller.db</b>'
                     + _info(ORPHAN_BASIS)
                     + '<br>Everything below comes from the bytes on disk, not from the index.</div>')

    # claims — headers are the real CACHE_FILE_CLAIM column names (description in parentheses)
    rows = []
    # beside a file a creative-tools item names and no chat links to, the label "Chat media" explained
    # — never beside a key of a chat message's shape, which names a conversation and a message itself
    ctp_named = e.get("category") == CTP_CATEGORY
    for c in e["claims"]:
        note = (MCT_CTP_NOTE if ctp_named and c["mct"] in (2, 3)
                and not _CHAT_EK_RE.match(str(c["external_key"] or "")) else None)
        rows.append(f"<tr><td class='mono'>{_esc(c['external_key'])}</td>"
                    f"<td>{_esc(_mct_label(c['mct']))}{_info(MCT_BASIS.get(c['mct']))}{_info(note)}</td>"
                    f"<td class='mono'>{_esc(c['user_id'])}</td>"
                    f"<td>{_esc(c['category'])}</td><td>{_esc(c['created'])}</td>"
                    f"<td>{_esc(c['expires'])}</td><td>{_esc(c['deleted'])}</td>"
                    f"<td>{_wal_cell(c.get('wal'))}</td></tr>")
    if rows:
        parts.append("<div class='sect'>CACHE_FILE_CLAIM</div>"
                     "<table class='sub'><tr><th>EXTERNAL_KEY</th><th>MEDIA_CONTEXT_TYPE (context type)</th>"
                     "<th>USER_ID</th><th>(category)</th><th>CREATION_TIMESTAMP_MILLIS (created)</th>"
                     "<th>EXPIRATION_TIMESTAMP_MILLIS (expires)</th>"
                     "<th>DELETED_TIMESTAMP_MILLIS (deleted)</th><th>(read from)</th></tr>"
                     + "".join(rows) + "</table>")
    session = _session_html(e, src_root, manifest)
    if session:
        parts.append(session)
    items = _ctp_items_html(e, src_root, manifest)
    if items:
        parts.append(items)
    leads = _leads_html(e, rel_prefix, closure)
    if leads:
        parts.append(leads)
    device = [r for r in e.get("content_proof") or () if r.get("what") == "device"]
    cloud = [r for r in e.get("content_proof") or () if r.get("what") != "device"]
    if device:
        rows = "".join(
            f"<tr><td class='mono'>{_esc(r.get('snap_id'))}</td><td>{_esc(r.get('role'))}</td>"
            f"<td>{_esc(r.get('source'))}</td><td class='mono'>{_esc(r.get('from'))}</td></tr>"
            for r in device)
        parts.append("<div class='sect'>≡ Identical to a Memory&#39;s media recovered on this device"
                     + _info(content_basis(device[0])) + "</div>"
                     "<table class='sub'><tr><th>Memory</th><th>role</th><th>recovered from</th>"
                     "<th>cache file / pack</th></tr>" + rows + "</table>")
    if cloud:
        rows = "".join(
            f"<tr><td class='mono'>{_esc(r.get('snap_id'))}</td><td>{_esc(r.get('role'))}</td>"
            f"<td>{'as received' if r.get('what') == 'encrypted' else 'decrypted'}</td>"
            f"<td>{_esc(r.get('retrieved_utc'))}</td><td>{_esc(r.get('authority_note'))}</td></tr>"
            for r in cloud)
        parts.append("<div class='sect'>☁ Identical to media retrieved from Snapchat&#39;s servers"
                     + _info(CONTENT_BASIS.format(sha="…", sid="…", role="media", when="…", what="",
                                                  note="the authority recorded")) + "</div>"
                     "<table class='sub'><tr><th>Memory</th><th>role</th><th>compared</th>"
                     "<th>retrieved (UTC)</th><th>legal authority</th></tr>" + rows + "</table>")
    filters = _filter_memories_html(e, rel_prefix, closure)
    if filters:
        parts.append(filters)

    # metadata grid — real CACHE_FILE_METADATA column names with descriptions in parentheses
    m = e["meta"]
    grid = [("TYPE (physical type)", f"{m['type']} ({TYPE_LABELS.get(m['type'], '?')})" if m["type"] is not None else ""),
            ("FILE_SIZE_BYTES (file size)", _fmt_bytes(m["size"])),
            ("TOTAL_DISK_USED_BYTES (disk used)", _fmt_bytes(m["disk_used"])),
            ("KNOWN_CONTENT_LENGTH_BYTES (known content length)", _fmt_bytes(m["known_len"])),
            ("STORAGE_TYPE (storage type)", m["storage_type"]),
            ("SHARD_INDEX (shard index)", m["shard_index"]),
            ("LAST_READ_TIMESTAMP_MILLIS (last read)", m["last_read"])]
    if e["retrieval"].get("url"):
        grid.append(("CONTENT_RETRIEVAL_METADATA → source URL", e["retrieval"]["url"]))
    # The linked Memory's own CDN URLs. Shown (and searchable from the index) because most cache
    # entries carry no CONTENT_RETRIEVAL_METADATA of their own, so this is the only URL that
    # identifies the file's source.
    for u in (e["memory"] or {}).get("urls") or []:
        grid.append((_words(e)["mem_url"], u))
    ref = e["retrieval"].get("content_ref")
    if ref:
        ref = str(ref)
        note = _info("This is CONTENT_RETRIEVAL_METADATA field 8. Its form varies (a CDN media "
                     "token, a 64-hex hash, or the CACHE_KEY). When it is a 64-hex hash it is a "
                     "server-/source-side content hash that DOES NOT necessarily match the actual "
                     "cached bytes on disk — verified on an app_install_screenshot where field 8 "
                     "differed from both the cached file's SHA-256 and the download's. Use the "
                     "'cached file on disk' SHA-256 below for the bytes actually present.")
        if re.fullmatch(r"[0-9a-fA-F]{64}", ref):
            grid.append((f"CONTENT_RETRIEVAL_METADATA field 8 — source content hash (SHA-256; may "
                         f"differ from cached bytes){note}", ref))
        elif ref.lower() == str(e["cache_key"]).lower():
            grid.append((f"CONTENT_RETRIEVAL_METADATA field 8 (equals CACHE_KEY){note}", ref))
        else:
            grid.append((f"CONTENT_RETRIEVAL_METADATA field 8 — CDN media token{note}", ref))
    grid_html = "".join(f"<div class='k'>{k}</div><div class='v'>{_esc(v)}</div>"
                        for k, v in grid if v not in (None, ""))
    parts.append(f"<div class='sect'>CACHE_FILE_METADATA</div><div class='grid'>{grid_html}</div>")
    parts.append(_meta_prior_html(e))

    # children
    if e["children"]:
        crows = []
        for ch in e["children"]:
            crows.append(f"<tr><td class='mono'>{_esc(ch['name'])}</td>"
                         f"<td>{_fmt_bytes(ch['size'])}</td><td>{_esc(ch['offset'])}</td></tr>")
        parts.append("<div class='sect'>CHILDREN (byte-range parts / bundle files)</div>"
                     "<table class='sub'><tr><th>name</th><th>size</th><th>offset</th></tr>"
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
        fs_records = e.get("ondisk_fs") or {}
        epochfmt = e.get("_epochfmt") or (lambda seconds: "")
        for scope, plist in sorted(groups.items(), key=lambda kv: (kv[0] in cross, kv[0])):
            # under each path, what the DEVICE's filesystem recorded about the file — created,
            # modified, accessed, inode changed, protection class — as opposed to the claim's own
            # timestamps above (see the basis)
            by_shown = {device_path(p, src_root, manifest): p for p in plist}
            listed = "<br>".join(
                _esc(line) + report_ui.device_fs_html([fs_records.get(by_shown.get(p)) for p in group],
                                                      epochfmt)
                for line, group in _collapse_paths(list(by_shown)))
            badge = ((" <span class='xscope'>⚠ different account scope</span>" + _info(_cross_scope_basis(e)))
                     if scope in cross else "")
            blocks.append(f"<div class='scopehdr'>SCContent scope: <span class='mono'>{_esc(scope)}</span>"
                          f"{badge}</div><div class='paths'>{listed}</div>")
        # the actual bytes present on disk: their real hashes (NOT the metadata field-8 value) plus
        # a viewer when the bytes are recognizable plaintext media.
        hview = []
        if e.get("ondisk_sha256"):
            if e.get("ondisk_type"):
                type_txt = _esc(e["ondisk_type"])
            elif e["meta"]["type"] == 3:
                type_txt = ("not media — the file named after the CACHE_KEY of a bundle holds only "
                            "the CHILDREN descriptor; the content is in the child files below"
                            + _info("A bundle (CACHE_FILE_METADATA.TYPE = 3) is a container: the "
                                    "<CACHE_KEY> file itself is a small protobuf listing the "
                                    "children, and each child is stored on disk as "
                                    "<CACHE_KEY>_<child name>. The hashes on this line are of the "
                                    "descriptor, not of any media — see the per-child hashes below."))
            elif e.get("ondisk_encrypted"):
                type_txt = ("🔒 <b>encrypted</b>" + _info(ENCRYPTED_BASIS))
            else:
                type_txt = (_esc(e.get("ondisk_label") or "unrecognized")
                            + " — not a media type this report can render"
                            + _info(NOT_MEDIA_BASIS))
            hview.append(f"<div class='grid'>"
                         f"<div class='k'>cached file MD5</div><div class='v hex'>{_esc(e['ondisk_md5'])}</div>"
                         f"<div class='k'>cached file SHA-256</div><div class='v hex'>{_esc(e['ondisk_sha256'])}</div>"
                         f"<div class='k'>cached file size</div><div class='v'>{_fmt_bytes(e.get('ondisk_bytes'))}</div>"
                         f"<div class='k'>detected type</div><div class='v'>{type_txt}</div></div>")
        note = f" <span class='muted'>({_esc(e['view_note'])})</span>" if e.get("view_note") else ""
        if str(e.get("view_note", "")).startswith("bundle child"):
            # the bytes worth opening are the children's, listed with their own hashes just below
            hview.append("<div class='muted'>▶ the viewable content of this bundle is in its child "
                         "files, listed below with their own type and hashes</div>")
        elif e.get("view"):
            if e.get("view_is_image"):
                hview.append(f"<a href='{_esc(e['view'])}' target='_blank'>"
                             f"<img class='cacheview' src='{_esc(e['view'])}' loading='lazy'></a>{note}")
            else:
                # the poster is this tool's own frame; it is shown as a way in to the video, and
                # says so, so it can never be mistaken for a cached file of the device's
                poster = (f"<a href='{_esc(e['view'])}' target='_blank'>"
                          f"<img class='cacheview' src='{_esc(e['poster'])}' loading='lazy'></a>"
                          f"<div class='muted'>poster frame extracted by this tool from the cached "
                          f"video — a derived image, not a cached file{_info(POSTER_BASIS)}</div>"
                          if e.get("poster") else "")
                why = (f"<div class='muted'>{_esc(e['poster_note'])}</div>"
                       if e.get("poster_note") and not e.get("poster") else "")
                hview.append(poster + f"<a class='cclink' href='{_esc(e['view'])}' target='_blank'>"
                                      f"▶ view cached file</a>{note}" + why)
        elif e.get("view_note"):                               # recognized media too large to embed
            hview.append(f"<div class='muted'>▶ {_esc(e['view_note'])}</div>")
        parts.append(f"<div class='sect'>Cache file(s) on disk — {_fmt_bytes(e['on_disk']['bytes'])} present"
                     + _info(DEVICE_MTIME_BASIS) + "</div>" + "".join(blocks) + "".join(hview))
        if e.get("view") and "embedded" in e and not str(e.get("view_note", "")).startswith("bundle child"):
            parts.append("<div class='sect'>Embedded metadata — inside the cached file, with its own "
                         "timestamps" + _info(report_ui.EMBEDDED_BASIS + " " + report_ui.FILE_TIME_BASIS)
                         + "</div>" + report_ui.embedded_meta_html(e["embedded"], e.get("embedded_times"),
                                                                   label=os.path.basename(e["view"]),
                                                                   href=e["view"]))
    else:
        parts.append("<div class='sect'>Cache file(s) on disk</div>"
                     "<div class='muted'>no matching file found in the SCContent folders</div>")

    # bundle children present on disk — each is a real file with its own type/hashes/viewer
    if e.get("child_files"):
        krows = []
        for k in e["child_files"]:
            if k.get("view"):
                if k.get("view_is_image"):
                    view = (f"<a href='{_esc(k['view'])}' target='_blank'>"
                            f"<img class='childview' src='{_esc(k['view'])}' loading='lazy'></a>")
                else:
                    view = (f"<a class='filebtn play' href='{_esc(k['view'])}' target='_blank'>"
                            f"▶ <span class='lbl'>{_esc(k['type'])}</span></a>")
                view += f" <span class='muted'>{_esc(k.get('note') or '')}</span>"
            elif k.get("encrypted"):
                view = f"<span class='muted'>🔒 encrypted{_info(ENCRYPTED_BASIS)}</span>"
            else:
                view = (f"<span class='muted'>{_esc(k.get('label') or 'unrecognized')}"
                        f"{_info(NOT_MEDIA_BASIS)}</span>")
            krows.append(f"<tr><td class='mono'>{_esc(k['name'])}</td>"
                         f"<td>{_esc(k.get('type') or '')}</td>"
                         f"<td>{_fmt_bytes(k.get('bytes'))}</td>"
                         f"<td class='hex'>{_esc(k.get('md5'))}<br>{_esc(k.get('sha256'))}</td>"
                         f"<td>{view}</td></tr>")
        parts.append("<div class='sect'>Bundle child files on disk"
                     + _info("A bundle's children are stored as separate files named "
                             "<CACHE_KEY>_<child name>. Each is hashed and typed on its own — this "
                             "is where a bundle's actual media (e.g. the .mp4 of a chat video and "
                             "its .webp overlay) lives, since the <CACHE_KEY> file itself is only "
                             "the descriptor.")
                     + "</div><table class='sub'><tr><th>child</th><th>detected type</th>"
                       "<th>size</th><th>MD5 / SHA-256 of the child</th><th>view</th></tr>"
                     + "".join(krows) + "</table>")
        read = [k for k in e["child_files"] if "embedded" in k]
        if read:
            parts.append("<div class='sect'>Embedded metadata — inside the child files, with their own "
                         "timestamps" + _info(report_ui.EMBEDDED_BASIS + " " + report_ui.FILE_TIME_BASIS)
                         + "</div>" + "".join(
                             report_ui.embedded_meta_html(k["embedded"], k.get("embedded_times"),
                                                          label=str(k.get("name")), href=k.get("view", ""))
                             for k in read))

    # decrypted copy produced by the Memories report (encrypted cache bytes)
    here = _decrypted_here(e, closure)
    if not here and e.get("decrypted"):
        parts.append("<div class='sect'>Decrypted copy (Memories report)"
                     + _info(_decrypted_basis(e)) + "</div><div class='muted'>"
                     + f"{len(e['decrypted'])} decrypted copy/copies of these bytes exist, from "
                     + f"Memory/Memories {_esc(', '.join(sorted({str(d.get('snap_id')) for d in e['decrypted']})))} "
                     + "&mdash; <b>not part of this partial report</b>, so the plaintext is not in "
                       "this folder.</div>")
    if here:
        drows = []
        for d in here:
            url = f"{rel_prefix}Memories/{d.get('path', '')}"
            thumb = (f"<a href='{_esc(url)}' target='_blank'>"
                     f"<img class='childview' src='{_esc(url)}' loading='lazy'></a>"
                     if d.get("ext") in ("jpg", "png", "webp") else
                     f"<a class='filebtn play' href='{_esc(url)}' target='_blank'>▶ "
                     f"<span class='lbl'>{_esc(d.get('ext'))}</span></a>")
            drows.append(f"<tr><td>{_esc(d.get('role'))}</td><td>{_esc(d.get('ext'))}</td>"
                         f"<td>{_fmt_bytes(d.get('bytes'))}</td>"
                         f"<td class='hex'>{_esc(d.get('md5'))}<br>{_esc(d.get('sha256'))}</td>"
                         f"<td class='mono'>{_esc(d.get('snap_id'))}</td><td>{thumb}</td></tr>")
        parts.append("<div class='sect'>Decrypted copy (Memories report)" + _info(_decrypted_basis(e))
                     + "</div><table class='sub'><tr><th>role</th><th>type</th><th>size</th>"
                       "<th>MD5 / SHA-256 of the decrypted media</th>"
                       f"<th>Memory ({_words(e)['mem_id']})</th>"
                       "<th>view</th></tr>" + "".join(drows) + "</table>")

    # tombstones
    if e["tombstones"]:
        trows = []
        for t in e["tombstones"]:
            trows.append(f"<tr><td>{_esc(_mct_label(t['mct']))}</td><td>{_esc(t['reason'])}</td>"
                         f"<td>{_fmt_bytes(t['bytes'])}</td><td>{_esc(t['deleted'])}</td>"
                         f"<td>{_wal_cell(t.get('wal'))}</td></tr>")
        parts.append("<div class='sect'>CACHE_FILE_SAMPLED_TOMBSTONE (deletion record)</div>"
                     "<table class='sub'><tr><th>MEDIA_CONTEXT_TYPE</th><th>DELETION_REASON</th>"
                     "<th>BYTES_DELETED</th><th>DELETED_TIMESTAMP_MILLIS</th><th>(read from)</th></tr>"
                     + "".join(trows) + "</table>")

    links = _links_html(e, rel_prefix, closure=closure)
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
                    db_display, run_id="default", wal_infos=None, closure=None, prov=None,
                    platform="ios", ctp_stores=None):
    # The source fingerprints this run recorded, so the examiner's saved selection carries
    # them and a later partial run can check the extraction it is handed against this one.
    sources_js = report_ui.sources_script(os.path.dirname(os.path.abspath(outdir)))
    total = len(entries)
    on_disk = sum(1 for e in entries if e["on_disk"]["found"])
    mem_linked = sum(1 for e in entries if e["memory"])
    chat_linked = sum(1 for e in entries if e["chats"])
    # a claim naming a message no row is there for, and nothing else tying the entry to a chat: not
    # counted as linked to a chat, which is a message
    conv_tied = sum(1 for e in entries if e.get("conv_links") and not e["chats"])
    # every tie draws a dashed chip, one beside a chat link too: its style goes with any tie
    conv_chips = any(e.get("conv_links") for e in entries)
    # counted apart from mem_linked: an asset of a listed filter is not linked to a Memory's media
    filter_linked = sum(1 for e in entries if e.get("filter_memories"))
    # information on the file, not a link: counted on a line of its own
    ctp_named = sum(1 for e in entries if e.get("ctp_items"))
    deleted = sum(1 for e in entries if e["tombstones"])
    xscope = sum(1 for e in entries if e["on_disk"].get("cross_scope"))
    orphans = sum(1 for e in entries if e.get("orphan"))
    # Measured from the bytes (scripts/data/sniff.classify), not "this report cannot display it".
    encrypted_total = sum(1 for e in entries if e.get("ondisk_encrypted"))
    encrypted_open = sum(1 for e in entries if e.get("ondisk_encrypted") and e.get("decrypted"))
    encrypted_locked = encrypted_total - encrypted_open
    wal_only = sum(1 for e in entries if e.get("wal") == sqlite_open.WAL_ONLY)
    main_only = sum(1 for e in entries if e.get("wal") == sqlite_open.MAIN_ONLY)
    meta_changed = sum(1 for e in entries if e.get("meta_prior"))
    categories = sorted({e["category"] for e in entries})
    partial_css, banner, figures = partial_report.page_chrome(closure, "cc", prov)

    # Row data + per-row detail go to sibling data/*.js files, and only the rows in the viewport are
    # ever built into the DOM (see scripts/report_ui.py). The document below stays a few KB whatever
    # the number of cache entries.
    data_dir = os.path.join(outdir, "data")
    details = [(f"ck-{e['cache_key']}", _detail_html(e, rel_prefix, src_root, manifest, closure))
               for e in entries]
    chunk_of = report_ui.write_details(data_dir, details)

    rows = []
    for e in entries:
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
        elif e.get("conv_links"):
            linkbits.append("Conversation")                # tied to a conversation only
        if e.get("filter_memories"):
            linkbits.append("Filter")                      # not "Memory": the filter matches by indexOf
        is_xscope = bool(e["on_disk"].get("cross_scope"))
        users = ", ".join(u[:8] + "…" for u in e["users"])
        # cells carry as little markup as possible — per-column styling is in the CSS (.vc.cN),
        # because every byte here is multiplied by the number of cache entries in data/index.js
        # The badges (and the "?" that explains them) are kept on one line with each other so the
        # cell is at most two lines tall: a third line does not fit the fixed row height and is cut
        # through the middle, which is what sliced the "?" icon in half.
        badges = (_wal_badge(e.get("wal"))
                  + (f'<span class="walbadge changed">changed</span>{_info(META_PRIOR_BASIS)}'
                     if e.get("meta_prior") else ""))
        cells = [
            "▸",
            (f'<span class="orphanbadge">{_esc(e["category"])}</span>' if e.get("orphan")
             else _esc(e["category"]))
            + (f'<span class="badges">{badges}</span>' if badges else ""),
            _esc(e["cache_key"]),
            _external_key_summary(e["claims"]),
            _esc(users),
            _esc(type_lbl),
            _fmt_bytes(eff_size),
            _file_cell(e, rel_prefix, closure) + (" <span class='xwarn' title='a copy sits in another "
                                         "account&#39;s SCContent scope'>⚠</span>" if is_xscope else ""),
            _links_html(e, rel_prefix, compact=True, closure=closure),
        ]
        # what the search box matches on: everything identifying, without the HTML around it
        searchable = [e["cache_key"], e["category"], type_lbl, users,
                      "orphan unclaimed not indexed" if e.get("orphan") else "",
                      _WAL_SEARCH.get(e.get("wal"), ""),
                      e.get("ondisk_md5", ""), e.get("ondisk_sha256", ""),
                      e.get("ondisk_type") or "", e.get("ondisk_label") or "",
                      "encrypted" if e.get("ondisk_encrypted") else "",
                      e["retrieval"].get("url") or "",
                      str(e["retrieval"].get("content_ref") or "")]
        searchable += [c["external_key"] for c in e["claims"]]
        searchable += [c["user_id"] for c in e["claims"]]
        if e["memory"]:
            searchable.append(e["memory"]["snap_id"])
            # the linked Memory's CDN URLs, so a URL pasted from the Memories report (or from
            # scdb) finds this cache file even when it has no retrieval metadata of its own
            searchable += e["memory"].get("urls") or []
        for ch in e["chats"]:
            searchable += [ch.get("conversation_id", ""), ch.get("server_message_id", "")]
        # a tied conversation's id as the Conversations report spells it (the key's spelling is in
        # the EXTERNAL_KEY already)
        searchable += [tie["conversation_id"] for tie in e.get("conv_links") or ()]
        # the Memories whose overlay record lists this asset, so their snap id finds it
        searchable += [fm["snap_id"] for fm in e.get("filter_memories") or ()]
        # the creative-tools items that name it: their ids, feed and texts (not the category word: a
        # file that kept its category is not in it, and the search must agree with the filter)
        for hit in e.get("ctp_items") or ():
            info = hit.get("feed_info") or {}
            searchable += ["ctp__item_5", hit["item_id"], hit.get("own_id") or "",
                           info.get("feed") or "", info.get("short") or "", info.get("endpoint") or "",
                           f"payload 2.{hit['kind']}" if hit.get("kind") is not None else ""]
            searchable += [text for _path, text in _ctp_texts(hit.get("texts") or ())[0]]
        for k in e.get("child_files") or []:
            searchable += [str(k.get("name") or ""), k.get("md5") or "", k.get("sha256") or ""]
            searchable += report_ui.embedded_search_terms(k.get("embedded"), k.get("embedded_times"),
                                                          media_meta.STRUCTURAL)
        # what the cached file says about itself, and when the device last wrote it
        searchable += report_ui.embedded_search_terms(e.get("embedded"), e.get("embedded_times"),
                                                      media_meta.STRUCTURAL)
        searchable += [stamp for stamp in (e.get("ondisk_mtimes") or {}).values() if stamp]
        for p in e["on_disk"]["paths"]:
            searchable.append(os.path.basename(p))
        rows.append([
            anchor, cells,
            " ".join(s for s in searchable if s).lower(),
            {"1": e["category"], "2": e["cache_key"],
             "3": _external_key_summary(e["claims"]), "4": users, "5": type_lbl, "6": eff_size,
             "7": ("2" if e.get("view") else "1" if e["on_disk"]["found"] else "0")},
            chunk_of.get(anchor),
            {"cat": e["category"], "disk": disk,
             "link": ",".join(linkbits + (["Possible"] if e.get("leads") else [])),
             "xs": "yes" if is_xscope else "no",
             # "enc" is the *measured* state of the bytes, not "we could not display it"
             "enc": ("y" if e.get("ondisk_encrypted") and not e.get("decrypted") else
                     "dec" if e.get("ondisk_encrypted") else "n"),
             "wal": ("changed" if (e.get("meta_prior") and e.get("wal") == sqlite_open.BOTH)
                     else (e.get("wal") or sqlite_open.BOTH)),
             # The hash of the bytes on disk, recorded with a selection as a fallback match. The
             # CACHE_KEY itself is a key in cache_controller.db and no parsing change can move it,
             # so this is belt and braces rather than the primary route.
             **({"sha": e["ondisk_sha256"]} if e.get("ondisk_sha256") else {}),
             # Every time the row shows — the claims, the last read, the device's own record of the
             # file, and what the file says about itself where it states its zone — as the wall
             # clocks displayed (report_ui.ts_key), for the search over every report.
             "ts": report_ui.ts_keys(
                 *[c.get("created") for c in e["claims"]], e["meta"].get("last_read"),
                 *(e.get("ondisk_mtimes") or {}).values(),
                 *report_ui.fs_times((e.get("ondisk_fs") or {}).values(),
                                     e.get("_epochfmt") or (lambda seconds: "")),
                 *[t["shown"] for t in e.get("embedded_times") or () if not t.get("naive")]),
             # the device's inode-change times, apart: searched only when asked for
             **_tc((e.get("ondisk_fs") or {}).values(), e.get("_epochfmt"))},
        ])
    report_ui.write_rows(data_dir, rows)

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
    # Written only when an entry has one, so a report with none is byte for byte what it was.
    filter_css = "\n .chip.mem.filt{border-style:dashed}" if filter_linked else ""
    filter_opt = ('<option value="Filter">filter listed with a Memory (not its media)</option>'
                  if filter_linked else "")
    filter_sum = (f'<div class="sum"><b>{filter_linked}</b> cached file(s) are an asset of a filter '
                  f'listed with a Memory — not its media{_info(FILTER_LISTED_BASIS)}</div>'
                  if filter_linked else "")
    # the same for a conversation tie: the dashed chip's style when any entry has a tie, the Linked
    # option and the count when one is tied to nothing else. The option's words hold for every tie it
    # selects — one whose arroyo.db messages were not read too, where an absence is not known
    conv_css = ("\n .chip.chat.gone{background:#fff;border:1px dashed #8cc49e}" if conv_chips else "")
    conv_opt = ('<option value="Conversation">chat conversation only (no message row)</option>'
                if conv_tied else "")
    conv_sum = (f" &middot; <b>{conv_tied}</b> tied only to a conversation{_info(CONV_TIE_BASIS)}"
                if conv_tied else "")
    read_from = " · ".join(f"{html.escape(device_path(s['path'], src_root, manifest))} "
                           f"({html.escape(_ctp_store_state(s))})" for s in ctp_stores or ())
    ctp_sum = (f'<div class="sum"><b>{ctp_named}</b> cached file(s) are named by an item of an '
               f"account's creative-tools store (primary.docobjects ctp__item_5){_info(CTP_BASIS)}"
               + (f" — read from {read_from}" if read_from else "") + "</div>"
               if ctp_named else "")

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Snapchat cache_controller.db</title>{report_ui.emoji_font_link(rel_prefix)}<style>{report_ui.EMBEDDED_CSS}{report_ui.DEVICE_FS_CSS}
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif,"Apple Color Emoji","Snapchat Auto Emoji";
   margin:0;background:#f4f4f8;color:#1b1b1f}}
 header{{background:#2d2d71;color:#fff;padding:16px 24px}} header h1{{margin:0;font-size:20px}}
 .sum{{opacity:.85;font-size:13px;margin-top:4px}} .sum b{{color:#fff}}
 .note{{background:#fff8e0;border:1px solid #e6d48a;color:#6a5300;padding:8px 24px;font-size:12.5px}}
 .toolbar{{background:#ececf4;border-bottom:1px solid #d7d7e2;padding:10px 24px;
   display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:13px}}
 .toolbar input,.toolbar select{{font-size:13px;padding:5px 8px;border:1px solid #bcbcd0;border-radius:5px}}
 .toolbar input[type=search]{{min-width:280px}}
 .toolbar label{{color:#555;font-weight:600}}
 .toolbar button{{font-size:13px;padding:5px 10px;border:1px solid #bcbcd0;border-radius:5px;background:#fff;cursor:pointer;font-weight:600;color:#2d2d71}}
 .toolbar button:hover{{background:#e7e7f4}}
 img.cacheview{{max-width:220px;max-height:300px;border-radius:5px;box-shadow:0 1px 4px rgba(0,0,0,.25);margin-top:6px}}
 img.childview{{max-width:120px;max-height:90px;border-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,.25);vertical-align:middle}}
 .mono{{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}}
 .more{{background:#d7d7ee;color:#33367a;border-radius:8px;padding:0 6px;font-size:10px}}
 /* per-column styling for the index rows (keeps the row data in data/index.js markup-free) */
 .vcells>.vc.c0{{color:#2d2d71;font-weight:700}} .vr.open .vc.c0{{color:#8a1f5a}}
 .vcells>.vc.c2{{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:#33367a}}
 .vcells>.vc.c3{{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#555;overflow-wrap:anywhere}}
 .vcells>.vc.c4{{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}}
 /* The row is a fixed height, so a line that does not fit is cut through the middle rather than
    dropped — which is how the "?" beside a badge came out sliced. Keeping the badges on one line
    of their own holds this cell to two lines, and the line box is sized so two of them fit. */
 .vcells>.vc.c1{{line-height:15px}}
 .vcells>.vc.c1 .badges{{display:block;white-space:nowrap;margin-top:1px}}
 .filebtn{{display:inline-flex;align-items:center;gap:5px;text-decoration:none;font-weight:700;
   font-size:11px;color:#25348a;background:#e7ecff;border:1px solid #b9c3f0;border-radius:6px;
   padding:2px 7px;max-width:100%}}
 .filebtn:hover{{background:#d5deff}}
 .filebtn img{{width:34px;height:34px;object-fit:cover;border-radius:4px;display:block}}
 .filebtn.img{{padding:2px;gap:4px}} .filebtn.img .lbl{{padding-right:5px;text-transform:uppercase}}
 .filebtn.play{{padding:5px 9px;font-size:12px}}
 .filebtn.dec{{background:#e7f6ea;border-color:#b3ddc0;color:#1f6b39}}
 .filebtn.dec:hover{{background:#d3ecda}}
 .filenone{{color:#999;font-size:11px}}
 .sect{{margin-top:12px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#2d2d71;
   font-weight:700;border-bottom:1px solid #e2e2ee;padding-bottom:2px}}
 .grid{{display:grid;grid-template-columns:auto 1fr;gap:2px 14px;font-size:12px;margin-top:4px;max-width:900px}}
 .grid .k{{color:#666}} .grid .v{{overflow-wrap:anywhere}}
 table.sub{{border-collapse:collapse;margin-top:5px;font-size:11.5px}}
 table.sub th{{background:#e7e7f2;color:#2d2d71;text-align:left;padding:3px 8px}}
 table.sub td{{border:1px solid #e0e0e8;padding:3px 8px;overflow-wrap:anywhere;vertical-align:middle}}
 table.sub td.hex{{font-family:ui-monospace,Consolas,monospace;font-size:10px;color:#7a1f5a}}
 .paths{{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#555;margin-top:4px;overflow-wrap:anywhere}}
 .paths .devfs{{white-space:normal}}
 .muted{{color:#999}}
 /* The index row's Links cell — see the note in _links_html. No mask/filter/transform on this:
    they would become the containing block for the "?" popover, which is position:fixed exactly so
    that it escapes the cell's overflow:hidden. */
 .chiprow{{display:flex;align-items:center;gap:6px;flex-wrap:nowrap;overflow:hidden;margin-top:2px}}
 .chiprow>*{{flex:0 0 auto}} .chiprow .chip{{margin:0}}
 .chips{{margin-top:4px}} .chip{{display:inline-block;margin:2px 6px 2px 0;padding:2px 8px;border-radius:10px;
   font-size:11px;text-decoration:none;font-weight:600}}
 .chip.mem{{background:#e7ecff;color:#25348a;border:1px solid #b9c3f0}}{filter_css}
 .chip.lead{{background:#fff;color:#6b5a00;border:1px dashed #c9a400;text-decoration:none}}
 .leadnote{{font-size:12px;color:#6b5a00;margin:2px 0 4px}}
 button.copyids{{font-size:11.5px;padding:2px 8px;border:1px solid #bcbcd0;border-radius:5px;
   background:#fff;cursor:pointer;font-weight:600;color:#2d2d71}}
 .chip.chat{{background:#e7f6ea;color:#1f6b39;border:1px solid #b3ddc0}}{conv_css}
 .chip.cm{{background:#fdf0e3;color:#8a5a1c;border:1px solid #e8cfae}}
 .chip.ok{{background:#eef7ee;color:#2f7d32}} .chip.miss{{background:#f6efef;color:#9a5a5a}}
 .chip.warn{{background:#fff3d6;color:#8a5a00;border:1px solid #e6c983}}
 .xwarn{{color:#b8860b;font-weight:700}}
 .orphanbadge{{background:#f3e8f2;color:#8a1f5a;border:1px solid #e0c2d8;border-radius:8px;
   padding:1px 6px;font-size:10.5px;font-weight:700;white-space:nowrap}}
 .walbadge{{border-radius:8px;padding:1px 6px;font-size:10px;font-weight:700;white-space:nowrap;
   margin-left:4px}}
 .walbadge.walonly{{background:#e7f0ff;color:#1c4b8a;border:1px solid #b3ccea}}
 .walbadge.mainonly{{background:#ffe9e0;color:#8a3a1c;border:1px solid #e8bfae}}
 .walbadge.changed{{background:#fff3d6;color:#8a5a00;border:1px solid #e6c983}}
 .orphan{{background:#f3e8f2;border:1px solid #e0c2d8;color:#8a1f5a;border-radius:5px;
   padding:6px 10px;font-size:12px;margin-bottom:6px}}
 .scopehdr{{margin-top:6px;font-size:11px;color:#444;font-weight:600}}
 .xscope{{background:#fff3d6;color:#8a5a00;border:1px solid #e6c983;border-radius:8px;padding:0 6px;font-size:10px;margin-left:6px}}
 .hint{{position:relative;display:inline-block}}
 .qm{{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;border-radius:50%;
   background:#c9cdf0;color:#25348a;font-size:10px;font-weight:700;cursor:pointer;margin:0 4px;user-select:none;vertical-align:middle}}
 .qm:hover{{background:#2d2d71;color:#fff}}
 .tip{{display:none;position:absolute;left:20px;top:-4px;z-index:30;background:#1f1f52;color:#fff;padding:8px 11px;
   border-radius:6px;font-size:11.5px;font-weight:400;width:340px;box-shadow:0 3px 10px rgba(0,0,0,.35);line-height:1.45;
   white-space:normal;text-align:left;text-transform:none;letter-spacing:normal}}
 .hint.open .tip{{display:block}}
 h2{{margin:24px 0 0;padding:10px 24px;background:#1f1f52;color:#fff;font-size:15px}}
 table.vtab{{border-collapse:collapse;width:100%;font-size:12px}} table.vtab td{{border-bottom:1px solid #e2e2ea;padding:5px 24px}}
 table.vtab th{{background:#1f1f52;color:#fff;text-align:left;padding:6px 24px}}
{report_ui.VTABLE_CSS}{report_ui.NAV_CSS}{report_ui.SELECT_CSS}{partial_css}
 .vcells>.vc{{font-size:12.5px}}
</style>
<script>window.SCAUTO_RUN={json.dumps(run_id)};window.SCAUTO_VERSION={json.dumps(app_version.get_version())};{sources_js}window.SCAUTO_SELKIND="cc";</script>
<script>{report_ui.SELECT_JS}</script>
<script src="{rel_prefix}selection.js"></script>
<script>{report_ui.VTABLE_JS}</script></head><body>
<header><h1>Snapchat cache_controller.db</h1>
 <div class="sum">{total} physical cache files &middot; <b>{on_disk}</b> present on disk &middot;
 <b>{mem_linked}</b> linked to a Memory &middot; <b>{chat_linked}</b> linked to a chat{conv_sum} &middot;
 <b>{xscope}</b> with a cross-scope copy &middot; {deleted} with a deletion record &middot;
 times in <b>{html.escape(tz_label)}</b></div>
 <div class="sum"><b>{encrypted_total}</b> cached file(s) hold encrypted bytes
 {_info(ENCRYPTED_BASIS)} &middot; <b>{encrypted_open}</b> of those are readable here through the
 Memories report's decrypted copy &middot; <b>{encrypted_locked}</b> have no key available</div>
 <div class="sum"><b>{orphans}</b> file(s) on disk are not referenced by cache_controller.db
 {_info(ORPHAN_BASIS) if orphans else ''}</div>
 <div class="sum">Scope: {html.escape(_words(platform)["scope"])}</div>
 <div class="sum">Source: {html.escape(db_display)}</div>
 {filter_sum}{ctp_sum}{figures}{_wal_summary(wal_infos, wal_only, main_only, meta_changed)}</header>
{banner}{report_ui.missing_data_banner('CacheController_report.html')}
<div class="stickytop">
<div class="toolbar">
 <input type="search" id="q" placeholder="Search cache key, EXTERNAL_KEY, hash, URL, user…"
   title="Separate several terms with | to match any of them — that is what a cross-report link
with more than one target fills in here." oninput="flt()">
 {report_ui.search_all_link("../")}
 <label>Category <select id="cat" onchange="flt()"><option value="">all</option>{cat_opts}</select></label>
 <label>On disk <select id="disk" onchange="flt()"><option value="">any</option>
   <option value="yes">on disk</option><option value="no">not on disk</option></select></label>
 <label>Linked <select id="link" onchange="flt()"><option value="">any</option>
   <option value="Memory">Memory</option><option value="Chat">Chat</option>
   <option value="Possible">possible Memory (not proven)</option>{conv_opt}{filter_opt}</select></label>
 <label title="Only files with an on-disk copy in a different account's SCContent scope than the claim">
   <input type="checkbox" id="xscope" onchange="flt()"> ⚠ cross-scope only</label>
 <label title="Measured from the bytes: high entropy and a length that is a multiple of the AES
block size. Files that merely are not displayable media (lens bundles, fonts, subtitles) are NOT
counted as encrypted.">Encrypted <select id="enc" onchange="flt()"><option value="">any</option>
   <option value="y">encrypted, no key</option>
   <option value="dec">encrypted, decrypted elsewhere</option>
   <option value="n">not encrypted</option></select></label>
 <label title="Rows that only one of the two database readings contains">-wal
   <select id="wal" onchange="flt()"><option value="">any</option>
   <option value="{sqlite_open.WAL_ONLY}">only with -wal (recent)</option>
   <option value="{sqlite_open.MAIN_ONLY}">only without -wal (superseded/deleted)</option>
   <option value="changed">metadata changed since the checkpoint</option>
   </select></label>
 <button id="xallbtn" data-o="0" onclick="xall(this)">Expand all</button>
 {report_ui.clear_filters_button("cache entry")}
 <span id="count" style="color:#555"></span>
</div>
<div class="toolbar">{report_ui.selection_toolbar('cache entry')}</div>
<div class="pager" id="pager"></div>
<div class="vhdr" id="vhdr" style="grid-template-columns:30px {CC_COLS}">
 <div class="vc sel"><input type="checkbox" class="selall"
   title="Select / unselect every entry matching the current filters"
   onclick="SCV.selectShown(this.checked)"></div>
 <div class="vc nosort"></div>
 <div class="vc" onclick="SCV.setSort(1)">Category <span class="ar">↕</span></div>
 <div class="vc" onclick="SCV.setSort(2)">CACHE_KEY <span class="ar">↕</span></div>
 <div class="vc" onclick="SCV.setSort(3)">EXTERNAL_KEY <span class="ar">↕</span></div>
 <div class="vc" onclick="SCV.setSort(4)">User <span class="ar">↕</span></div>
 <div class="vc" onclick="SCV.setSort(5)">Type <span class="ar">↕</span></div>
 <div class="vc" onclick="SCV.setSort(6)">Size <span class="ar">↕</span></div>
 <div class="vc" onclick="SCV.setSort(7)">File <span class="ar">↕</span></div>
 <div class="vc nosort">Links</div>
</div>
</div>
<div class="vwrap" id="vwrap"><div class="vpad" id="vpad"></div><div class="vwin" id="vwin"></div></div>
<div class="vempty" id="vempty" style="display:none">No cache entry matches the current filters.</div>
{virt_html}
<script src="data/index.js"></script>
<script>
{report_ui.HINT_JS}
{report_ui.NAV_JS}
{report_ui.SELECT_TOOLBAR_JS}
{report_ui.CLIPBOARD_JS}
var flt_t=0;
function flt(){{clearTimeout(flt_t);flt_t=setTimeout(function(){{SCV.refilter();}},120);}}
function xall(btn){{
 var op=btn.dataset.o==='1';
 if(!SCV.expandAll(!op,500)){{
  alert('Too many rows on this page to expand at once. Narrow the filters or use a smaller '
        +'"rows per page" first.');
  return;}}
 btn.dataset.o=op?'0':'1';btn.textContent=op?'Expand all':'Collapse all';}}
SCV.init({{
 mount:'vwrap',win:'vwin',pad:'vpad',header:'#vhdr',missing:'vmiss',empty:'vempty',
 emptyAll:'This extract contains no cache_controller entry.',
 pager:'pager',pageSize:500,selKind:'cc',
 /* The CACHE_KEY is a key in cache_controller.db, so the anchor is stable; the hash of the
    bytes on disk is recorded as a fallback match. */
 selKeys:function(r){{var m=r[5]||{{}},k={{key:r[0].slice(3)}};if(m.sha)k.sha=m.sha;return k;}},
 rowHeight:{CC_ROW_H},estDetail:320,cols:'{CC_COLS}',detailBase:'data/detail-',
 {partial_report.pulled_config(closure, 'cc')}
 query:function(){{return document.getElementById('q').value;}},
 match:function(m,r){{
  var cat=document.getElementById('cat').value,disk=document.getElementById('disk').value,
      lk=document.getElementById('link').value,xs=document.getElementById('xscope').checked,
      wal=document.getElementById('wal').value,enc=document.getElementById('enc').value;
  return (!cat||m.cat===cat)&&(!disk||m.disk===disk)&&(!lk||(m.link||'').indexOf(lk)>-1)
       &&(!xs||m.xs==='yes')&&(!wal||m.wal===wal)&&(!enc||m.enc===enc)
       &&scSelPass('cc',SCV.selId(r[0]));}},
 selectedOnly:scSelOnly,
 selCount:scSelCount,
 count:function(n,t){{document.getElementById('count').textContent=
   n===t?(n+' entries'):(n+' of '+t+' shown');}},
 reset:function(){{
  document.getElementById('q').value='';document.getElementById('cat').value='';
  document.getElementById('disk').value='';document.getElementById('link').value='';
  document.getElementById('xscope').checked=false;document.getElementById('wal').value='';
  document.getElementById('enc').value='';
  document.getElementById('selonly').value='';}}
}});
scSelNote();
scConsumeHash();
</script>
</body></html>"""

    os.makedirs(outdir, exist_ok=True)
    report = os.path.join(outdir, "CacheController_report.html")
    with open(report, "w", encoding="utf-8") as f:
        f.write(doc)
    return report, {"total": total, "on_disk": on_disk, "mem": mem_linked,
                    "chat": chat_linked, "conv": conv_tied, "filter": filter_linked, "ctp": ctp_named,
                    "deleted": deleted, "orphans": orphans,
                    "wal_only": wal_only, "main_only": main_only,
                    "meta_changed": meta_changed, "encrypted": encrypted_total,
                    "encrypted_locked": encrypted_locked}


# --------------------------------------------------------------------------- entry

def index(app_or_root, outdir=None, tz="local", src_root=None, report_dir=None, links_dir=None):
    """Work out which cache entries exist and what each links to, without hashing or publishing.

    Reading the database and joining the claims happens here — the closure cannot be decided without
    the rows. What it defers is the expensive per-row half: hashing the cached bytes on disk,
    reconstructing byte-range files, and extracting poster frames. A partial run does that only for
    the entries it will show. See :func:`main` for the arguments.

    Returns a :class:`partial_report.Stage`, or ``None`` when there is no ``cache_controller.db``.
    """
    app = find_app_container(app_or_root)
    dbs = find_cache_controllers(app)
    if not dbs:
        logger.warning(f"No cache_controller.db found under {app}")
        return None

    manifest = load_path_manifest(src_root, app_or_root, app)
    outdir = outdir or ("./Snapchat_CacheController_report_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    ms_fmt, tz_label = make_ms_formatter(tz)
    # the cache files' mtimes ON THE DEVICE, from the extraction archive — never the extracted
    # copy's own, which is when we unzipped it (see memories_media_report.load_device_mtimes)
    device_mtimes = load_device_mtimes(src_root, app_or_root, app)
    device_fs_records = load_fs_records(src_root, app_or_root, app)

    scfull, scparts = index_sccontent(app)
    mem_index = load_memory_index(app, overlays=True)
    # every account's creative-tools item store, staged in a temporary folder (never beside the
    # report: it is the contacts' store too), and the userIds its folders are named after. Optional
    # evidence: a store that cannot be read loses its items, never the report.
    try:
        ctp_index = ctp_items.read(app)
    except Exception as error:                                     # noqa: BLE001 - optional store
        logger.warning(f"  creative-tools item stores (primary.docobjects ctp__item_5) not read: "
                       f"{error}")
        ctp_index = ctp_items.empty_index()
    userids = map_userids(app)
    # report_dir defaults to the parent of outdir when the report is placed under …/Reports/CacheController
    rdir = report_dir or os.path.dirname(os.path.abspath(outdir))
    # The manifests the Memories, Conversations and Library/Caches reports write are read from
    # `links_dir` when one is given. A partial run decides its whole closure before rendering anything,
    # so nothing has written them into its own folder yet — it points this at the full report folder the
    # selection was made in instead, which the version and source gates have already proved is the same
    # evidence read by the same build (see docs/report_partial.md).
    ldir = links_dir or rdir
    chat_links, chat_by_message = load_chat_links(ldir)
    chat_ids = load_chat_ids(ldir, chat_by_message)
    memory_pages = load_memory_pages(ldir)
    memory_media = load_memory_media(ldir)
    memory_content = (load_memory_content(ldir) or {}).get("by_cache_key") or {}
    cache_media = load_cache_media(ldir)
    # the shared, examiner-owned selection file every report of this run loads
    report_ui.write_selection_stub(rdir, report_ui.run_id(rdir))
    report_ui.write_emoji_font(rdir)
    # links to the sibling reports are relative to CacheController_report.html (…/Reports/CacheController/)
    rel_prefix = "../"

    all_entries, virtual, wal_infos = [], [], []
    for db in dbs:
        entries, virt, wal_info = build_entries(db, app, scfull, scparts, mem_index, chat_links,
                                                ms_fmt, memory_pages, chat_by_message,
                                                workdir=outdir, memory_content=memory_content,
                                                chat_ids=chat_ids, ctp_index=ctp_index)
        all_entries.extend(entries)
        virtual.extend(virt)
        wal_infos.append(wal_info)
    for e in all_entries:                                  # whose store, as every panel names one
        for hit in e.get("ctp_items") or ():
            hit["account"] = _account_label(hit["user_hash"], userids)

    # Files that are on disk but that the index does not account for. Without these the report only
    # shows what cache_controller.db remembers, and a recovered file it has forgotten is invisible.
    claimed = {p.replace("\\", "/") for e in all_entries for p in e["on_disk"]["paths"]}
    orphans = orphan_entries(scfull, scparts, claimed, ms_fmt)
    if orphans:
        logger.info(f"  {len(orphans)} cache file(s) on disk are not referenced by "
                    f"cache_controller.db — listed as \"{ORPHAN_CATEGORY}\"")
        all_entries.extend(orphans)
        all_entries.sort(key=lambda e: (e["category"], -e["created_sort"], e["cache_key"]))

    # The snap editor's session records, attached to the files they name (see snap_session).
    claims_by_key = {}
    for e in all_entries:
        for c in e["claims"]:
            claims_by_key.setdefault(e["cache_key"].lower(), []).append((c["external_key"], c["mct"]))
    sessions = snap_session.read(app, claims_by_key)
    for e in all_entries:
        e["session"] = [dict(rec, saved=ms_fmt(rec["saved_unix"] * 1000),
                             edited=ms_fmt(rec["edited_ms"]) if rec["edited_ms"] else "")
                        for rec in sessions.get(e["cache_key"].lower(), [])]

    for e in all_entries:
        stamps, records = {}, {}
        for path in e["on_disk"]["paths"]:
            key = manifest_key(path)
            record = device_fs_records.get(key)
            stamp = device_mtimes.get(key) if device_mtimes else None
            if record is None and stamp is not None:      # an older extraction folder: mtime only
                record = {"source": "zip-ut", "precision": "s", "mtime": stamp * device_fs.NS}
            records[path] = record
            stamps[path] = (device_fs.format_ns(record["mtime"], lambda s: ms_fmt(s * 1000),
                                                record.get("precision", "s"))
                            if record and record.get("mtime") is not None else "")
        e["ondisk_mtimes"] = stamps
        e["ondisk_fs"] = records

    # Possible Memory — leads, never links (scripts/memory_leads.py): for each on-disk media file no
    # identifier, chat or byte comparison connects to anything, the Memories of its kind whose times
    # fall near the file's own. Kept in e["leads"] only: never a link, a count or a closure edge.
    lead_files = {}
    for e in all_entries:
        # Only files nothing else accounts for: a category that names what the file is (a lens, a
        # story preview, a Discover video…) already says it is not a Memory's media.
        ctx19 = any(c.get("mct") == 19 for c in e["claims"])
        # an asset of a filter a Memory's record lists is accounted for, though not as its media; a
        # file a creative-tools item names is explained by the item, whatever its category; and one
        # whose claim names a chat conversation is that conversation's, though no message row is there
        if (e.get("memory") or e.get("chats") or e.get("filter_memories") or e.get("ctp_items")
                or e.get("conv_links") or not e["on_disk"]["found"]
                or not (e["category"] in LEAD_CATEGORIES or (e["category"] == "Other" and ctx19))):
            continue
        kind = memory_leads.kind_of_ext(guess_media(_head_of(e["on_disk"]["paths"])))
        if not kind:
            continue
        pts = [(f"claim, context {c['mct']}", c["created_sort"] / 1000) for c in e["claims"]
               if c.get("created_sort")]
        for rec in (e.get("ondisk_fs") or {}).values():
            for field, label in (("btime", "file created"), ("mtime", "file modified"),
                                 ("atime", "file last read")):
                if rec and rec.get(field):
                    pts.append((label, rec[field] / device_fs.NS))
        lead_files[e["cache_key"]] = {"kind": kind, "points": pts, "ctx19": ctx19}
    leads = memory_leads.find_leads(lead_files, mem_index.get("points") or {})
    for e in all_entries:
        e["leads"] = leads.get(e["cache_key"])

    # which platform's words the explanations use (see PLATFORM_WORDS)
    platform = "android" if android_layout.is_app_dir(app) else "ios"
    if platform != "ios":
        for e in all_entries:
            e["platform"] = platform

    # The closure's view. Rows keep the order established above, which `render` preserves: poster
    # extraction runs under an overall budget, so re-ordering the publish could change which frames
    # get extracted.
    sel = partial_report.Index("cc")
    for e in all_entries:
        row_id = f"ck-{e['cache_key']}"
        sel.add(row_id, e, key=e["cache_key"])
        if e.get("memory"):
            sel.link(partial_report.EDGE_MEMORY_CACHE, row_id, "mem",
                     f"mem-{e['memory']['snap_id']}")
        # an edge of its own, never EDGE_MEMORY_CACHE: following a Memory to its media must not
        # bring the assets of every filter its record lists (relations mem_filter_assets and
        # cache_filter_memories, both off by default)
        for fm in e.get("filter_memories") or ():
            sel.link(partial_report.EDGE_MEMORY_FILTER_ASSET, row_id, "mem", f"mem-{fm['snap_id']}")
        for chat in e.get("chats") or ():
            if chat.get("conversation_id") and chat.get("server_message_id"):
                sel.link(partial_report.EDGE_MESSAGE_CACHE, row_id, "msg",
                         f"conv-{chat['conversation_id']}|msg-{chat['server_message_id']}")
        # a conversation tie, to the row the Conversations report lists (its anchor is built from the
        # id as that report spells it, not as the key does); one it does not list has no row to reach
        for tie in e.get("conv_links") or ():
            if tie.get("listed") and tie.get("anchor"):
                sel.link(partial_report.EDGE_CONV_CACHE, row_id, "conv", tie["anchor"])
        # a creative-tools item that names the file is no edge: the store has no report of its own

    return partial_report.Stage("cc", all_entries, sel, app=app, outdir=outdir, dbs=dbs,
                                virtual=virtual, wal_infos=wal_infos, tz_label=tz_label,
                                rel_prefix=rel_prefix, rdir=rdir, scfull=scfull, scparts=scparts,
                                manifest=manifest, src_root=src_root, memory_media=memory_media,
                                cache_media=cache_media, ms_fmt=ms_fmt, platform=platform,
                                ctp_stores=ctp_index["stores"])


def _drop_sqlite_views(outdir):
    """Remove the staged database copies from a partial extract. True when there were any.

    ``sqlite_open`` stages the database twice — with its ``-wal`` applied and without — and this report
    keeps both next to itself, so an examiner can open the exact state each figure was read from. In a
    **full** report that is transparency. In an extract of selected rows it is a complete copy of
    ``cache_controller.db``: every row, every claim, every deleted-file record, none of it filtered. The
    provenance says the copies were left out, which is a smaller loss than the alternative.
    """
    views = os.path.join(outdir, "sqlite_views")
    if not os.path.isdir(views):
        return False
    shutil.rmtree(views, ignore_errors=True)
    return True


def render(stage, closure=None, prov=None):
    """Hash, publish and render. ``closure=None`` does the whole index, exactly as before."""
    all_entries = [record for _row_id, record in stage.sel.keep(closure)]
    outdir, app, rdir = stage["outdir"], stage["app"], stage["rdir"]
    scfull, scparts = stage["scfull"], stage["scparts"]
    memory_media, cache_media = stage["memory_media"], stage["cache_media"]
    dbs, src_root, manifest = stage["dbs"], stage["src_root"], stage["manifest"]

    # hash the actual cached bytes and publish viewable plaintext media (hard-linked where possible,
    # always under a name with a real extension so browsers open it).
    ms_fmt = stage["ms_fmt"]
    epochfmt = lambda seconds: ms_fmt(int(seconds) * 1000)   # noqa: E731 - one formatter, two readers
    for e in all_entries:
        e["_epochfmt"] = epochfmt
    materialize_ondisk(all_entries, scfull, scparts, os.path.join(outdir, "files"), outdir,
                       epochfmt=epochfmt)
    posters, no_poster, not_tried = publish_posters(all_entries, os.path.join(outdir, "files"))
    if posters or no_poster or not_tried:
        logger.info(f"  {posters} poster frame(s) extracted from cached video (derived thumbnails, "
                    f"labelled as such in the report)"
                    + (f"; {no_poster} cached video(s) could not be decoded and are listed without "
                       f"one" if no_poster else "")
                    + (f"; {not_tried} never attempted — listed without one, and not reported as "
                       f"undecodable" if not_tried else ""))
    # for entries whose cached bytes are encrypted, point at the copy the Memories report decrypted
    for e in all_entries:
        e["decrypted"] = memory_media.get(e["cache_key"].lower(), [])
        e["cache_media"] = cache_media.get(e["cache_key"].lower(), [])

    db_display = device_path(dbs[0], src_root, manifest) if dbs else ""
    report, stats = generate_report(all_entries, stage["virtual"], outdir, stage["tz_label"],
                                    stage["rel_prefix"], src_root, manifest, db_display,
                                    report_ui.run_id(rdir), stage["wal_infos"],
                                    closure=closure, prov=prov,
                                    platform=stage.get("platform") or "ios",
                                    ctp_stores=stage.get("ctp_stores"))
    # The same leads, seen from the Memory: the Memories pages, already written, load this file. A
    # partial extract's names only the Memories it holds.
    memory_leads.write_script(os.path.join(outdir, "data"),
                              {e["cache_key"]: e.get("leads") for e in all_entries},
                              report_ui.run_id(rdir),
                              keep=None if closure is None else
                              (lambda sid: closure.has("mem", f"mem-{sid}")))
    logger.info(f"cache_controller report: {os.path.abspath(report)}")
    if closure is not None:
        removed = _drop_sqlite_views(outdir)
        logger.info(f"  {len(all_entries)} of {len(stage.model)} cache entry/entries in this extract")
        if removed:
            logger.info(f"  sqlite_views/ removed: it holds complete copies of cache_controller.db, "
                        f"which would put every row of the database into this extract")
    logger.info(f"  {stats['total']} cache files, {stats['on_disk']} on disk, "
                f"{stats['mem']} linked to Memories, {stats['chat']} linked to chats, "
                f"{stats['deleted']} deleted")
    if stats.get("conv"):
        logger.info(f"  {stats['conv']} tied only to a conversation: a claim key names a message no "
                    f"row is there for — not counted as linked to a chat")
    if stats.get("filter"):
        logger.info(f"  {stats['filter']} an asset of a filter a Memory's overlay record lists "
                    f"(ZGALLERYSNAPDETAIL.ZOVERLAY) — not the Memory's media, and not counted above")
    if stats.get("ctp"):
        logger.info(f"  {stats['ctp']} named by an item of an account's creative-tools store "
                    f"(primary.docobjects ctp__item_5) — information on the file, not a link")
    logger.info(f"  {stats['encrypted']} hold encrypted bytes (high entropy + AES block "
                f"alignment), {stats['encrypted_locked']} of them with no key available; "
                f"everything else on disk was identified by its magic bytes")
    if stats["wal_only"] or stats["main_only"] or stats["meta_changed"]:
        logger.info(f"  -wal: {stats['wal_only']} entry/entries exist only with it applied, "
                    f"{stats['main_only']} only without it, {stats['meta_changed']} whose metadata "
                    f"row changed since the last checkpoint (both versions reported)")
    return report


def main(app_or_root, outdir=None, tz="local", src_root=None, report_dir=None):
    """
    Build a cache_controller.db report.

    app_or_root : Snapchat app-container path, or any extraction root containing it.
    outdir      : output directory (default: ./Snapchat_CacheController_report_<timestamp>).
    tz          : timezone for displayed timestamps — 'local', 'utc', an IANA name, or '±HH:MM'.
    src_root    : extraction root the files were unzipped under (for archive-relative source paths).
    report_dir  : the sibling reports root (…/Reports). Used to find the chat report's chat-link
                  manifest and to compute relative links to the Memories/chat reports.

    A full run in one call: :func:`index` then :func:`render`. A partial run calls the two halves
    separately, so the closure is decided before anything is hashed or published.
    """
    stage = index(app_or_root, outdir=outdir, tz=tz, src_root=src_root, report_dir=report_dir)
    if stage is None:
        return None
    return render(stage)


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
