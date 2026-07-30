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
import shutil
import sqlite3
import hashlib
import logging
from datetime import datetime
from urllib.parse import urlparse

from scripts import report_ui
# Pure helpers reused from the Memories media report (path rendering, SCContent indexing).
from scripts.memories_media_report import (
    find_app_container, find_profiles, index_sccontent, device_path,
    load_path_manifest, make_time_formatter, _collapse_part_paths, guess_media,
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

    Returns ``(by_key, by_message)``:

    * ``by_key``     : CACHE_KEY -> [{conversation_id, server_message_id, anchor}]
    * ``by_message`` : "<conversation>|<server message id>" -> the same records

    ``by_message`` is the fallback that links **every** cache entry belonging to a message (a chat
    video is typically a full-media claim, a thumbnail claim and a raw content claim), not only the
    single file the Communications report chose to display. Empty when that report didn't run.
    Version-1 manifests (a bare CACHE_KEY -> records map) are still understood.
    """
    cand = os.path.join(report_dir or "", "Communications", "cache_links.json")
    if not (cand and os.path.isfile(cand)):
        return {}, {}
    try:
        with open(cand, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as error:
        logger.debug(f"Could not read chat link manifest {cand}: {error}")
        return {}, {}
    if data.get("version") == 2:
        return data.get("by_key") or {}, data.get("by_message") or {}
    return data, {}                                            # legacy (v1) manifest


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


def _is_range_child(name):
    """True for a CHILDREN entry that names a byte range of the parent (handled via ``scparts``)."""
    if not isinstance(name, str):
        return False
    return (name == "PREFETCH" or bool(re.fullmatch(r"\d+-\d+", name))
            or bool(_SC_SPLIT_RE.match(name)))


def child_ondisk_paths(cache_key, name, scfull, scparts):
    """On-disk paths for one **bundle child**, in read order.

    A bundle (``TYPE=3``) is unpacked into one file per child, named ``<CACHE_KEY>_<child name>``
    — e.g. ``4bfc4bba…_z2a132f1f…`` for child ``z2a132f1f…``. Other layouts store the child under
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


def _hash_stream(paths):
    """Stream ``paths`` in order; return (md5, sha256, first 16 bytes, total). Any size is safe."""
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
                if len(head) < 16:
                    head += chunk[:16 - len(head)]
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


def materialize_ondisk(entries, scfull, scparts, files_dir, report_dir,
                       max_reconstruct_bytes=1024 * 1024 * 1024):
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
    """
    os.makedirs(files_dir, exist_ok=True)
    for e in entries:
        if not e["on_disk"]["found"]:
            continue
        paths, _single = _ondisk_paths_ordered(e["cache_key"], scfull, scparts)
        if paths:
            try:
                e["ondisk_md5"], e["ondisk_sha256"], head, total = _hash_stream(paths)
            except OSError as error:
                logger.debug(f"Could not read on-disk bytes for {e['cache_key']}: {error}")
                head, total = b"", 0
            e["ondisk_bytes"] = total
            ext = guess_media(head)
            e["ondisk_type"] = ext
            if total == 0:
                e["view_note"] = ("the cached file is 0 bytes on disk — the index entry exists but "
                                  "no content was stored/captured")
            elif ext:
                view, note = publish_view(paths, files_dir, e["cache_key"], ext, total,
                                          max_reconstruct_bytes)
                e["view"], e["view_is_image"] = view, ext in ("jpg", "png", "webp")
                e["view_ext"], e["view_note"] = ext, note

        # bundle children: each is its own file with its own type
        kids = []
        for ch in e["children"]:
            cpaths = child_ondisk_paths(e["cache_key"], ch.get("name"), scfull, scparts)
            if not cpaths:
                continue
            kid = {"name": ch.get("name"), "paths": cpaths}
            try:
                kid["md5"], kid["sha256"], head, total = _hash_stream(cpaths)
            except OSError as error:
                logger.debug(f"Could not read bundle child {ch.get('name')}: {error}")
                continue
            kid["bytes"] = total
            kid["type"] = guess_media(head)
            if kid["type"]:
                base = f"{e['cache_key']}_{re.sub(r'[^0-9A-Za-z_.-]', '_', str(kid['name']))}"
                kid["view"], kid["note"] = publish_view(cpaths, files_dir, base, kid["type"],
                                                        total, max_reconstruct_bytes)
                kid["view_is_image"] = kid["type"] in ("jpg", "png", "webp")
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


# A chat claim's EXTERNAL_KEY is "<type>:<conversation id>:<message id>:<part>[:…]" — e.g.
# "thumbnail~1:19e0693c-…:12:0:0". The (conversation, message.part) it carries is what ties a cache
# entry to a chat message even when the Communications report attached a *different* file to it.
_CHAT_EK_RE = re.compile(r"^(?P<type>[^:]*):(?P<conv>[0-9a-fA-F-]{36}):(?P<msg>\d+):(?P<part>\d+)")


def _chat_links_for(clist, cache_key, by_key, by_message):
    """Chat messages this cache entry belongs to, with an explanation of how each was matched."""
    out, seen = [], set()                                      # one chip per (conversation, message)
    for rec in by_key.get(cache_key, []):
        anchor = rec.get("anchor") or f"cf-{cache_key}"
        seen.add((rec.get("conversation_id"), rec.get("server_message_id")))
        out.append(dict(rec, anchor=anchor, basis=(
            f"This CACHE_KEY is the attachment file the Communications report recorded for "
            f"message {rec.get('server_message_id') or '(unknown)'} in conversation "
            f"{rec.get('conversation_id') or '(unknown)'} (via its local_message_references / "
            f"content-type mapping, exported to cache_links.json).")))
    for c in clist:
        mo = _CHAT_EK_RE.match(c["external_key"] or "")
        if not mo:
            continue
        smid = f"{mo.group('msg')}.{mo.group('part')}"
        for rec in by_message.get(f"{mo.group('conv')}|{smid}", []):
            anchor = rec.get("anchor")
            ident = (rec.get("conversation_id"), rec.get("server_message_id"))
            if not anchor or ident in seen:
                continue
            seen.add(ident)
            out.append(dict(rec, anchor=anchor, basis=(
                f"The claim EXTERNAL_KEY \"{c['external_key']}\" carries the conversation id "
                f"{mo.group('conv')} and message {smid}, which the Communications report reported "
                f"for that message. The link therefore points at the message rather than at this "
                f"exact file — a message can have several cached files (full media, thumbnail, raw "
                f"content claim), and only one of them is displayed in the chat report.")))
    return out


def build_entries(db, app, scfull, scparts, mem_index, chat_links, ms_fmt, memory_pages=None,
                  chat_by_message=None):
    """Build one entry dict per physical cache file (CACHE_KEY) from a cache_controller.db.

    Returns (entries, virtualization_rows). Each entry aggregates its claims, metadata, on-disk
    resolution and cross-report links.
    """
    snap_ids = mem_index["snap_ids"]
    url_keys = mem_index["url_keys"]
    media_ids = mem_index["media_ids"]
    memory_pages = memory_pages or {}
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
        if memory:                                             # detail sub-page, when available
            memory["page"] = memory_pages.get(memory["snap_id"])
        chats = _chat_links_for(clist, key, chat_links, chat_by_message or {})

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

# Index-table geometry. The virtual table draws fixed-height rows, so the column track list is
# shared by the header and every row, and cells that overflow are clipped (the full value is always
# in the row's detail).
CC_COLS = ("24px 118px 260px 96px minmax(150px,1fr) 66px 82px 132px minmax(180px,300px)")
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
            f"Memory {', '.join(s for s in snaps if s) or '(unknown)'} (from ZGALLERYSNAP / "
            "gallery.encrypteddb) and wrote the plaintext media beside its report; this links to "
            "that decrypted copy, which is a derived file — the original cached bytes' hashes are "
            "shown above.")


def _links_html(entry, rel_prefix, compact=False):
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
        chips.append(f'<a class="chip mem" target="scauto_memories" '
                     f'href="{rel_prefix}Memories/Memories_report.html#mem-{_esc(sid)}">'
                     f'🧠 Memory {_esc(sid[:8])}… (index)</a>' + why(entry.get("memory_basis")))
        if page:
            chips.append(f'<a class="chip mem" target="scauto_memories" '
                         f'href="{rel_prefix}Memories/{_esc(page)}#mem-{_esc(sid)}">📄 detail</a>')
    for ch in entry["chats"]:
        conv = ch.get("conversation_id", "")
        smid = ch.get("server_message_id", "")
        label = f' {_esc(conv[:8])}… msg {_esc(smid)}' if conv else ""
        chips.append(f'<a class="chip chat" target="scauto_comms" '
                     f'href="{rel_prefix}Communications/Communications_report.html'
                     f'#{_esc(ch.get("anchor") or ("cf-" + entry["cache_key"]))}">'
                     f'💬 Chat{label}</a>' + why(ch.get("basis")))
    if not compact:
        if entry["on_disk"]["found"]:
            chips.append('<span class="chip ok">📁 on disk</span>' + why(_on_disk_basis(entry)))
        elif entry["claims"]:
            chips.append('<span class="chip miss">— not on disk</span>' + why(_on_disk_basis(entry)))
        if entry["on_disk"].get("cross_scope"):
            chips.append('<span class="chip warn">⚠ cross-scope copy</span>'
                         + why(_cross_scope_basis(entry)))
    return "".join(chips)


def _file_cell(entry, rel_prefix):
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
        return (f'<a class="filebtn play" href="{_esc(entry["view"])}" target="_blank" '
                f'title="open the cached {_esc(ext)}">▶ <span class="lbl">{_esc(ext)}</span></a>')
    dec = (entry.get("decrypted") or [])
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
    return '<span class="filenone">🔒 encrypted</span>'


def _detail_html(entry, rel_prefix, src_root, manifest):
    """Expandable detail block for one physical cache file."""
    e = entry
    parts = []

    # claims — headers are the real CACHE_FILE_CLAIM column names (description in parentheses)
    rows = []
    for c in e["claims"]:
        rows.append(f"<tr><td class='mono'>{_esc(c['external_key'])}</td>"
                    f"<td>{_esc(_mct_label(c['mct']))}</td><td class='mono'>{_esc(c['user_id'])}</td>"
                    f"<td>{_esc(c['category'])}</td><td>{_esc(c['created'])}</td>"
                    f"<td>{_esc(c['expires'])}</td><td>{_esc(c['deleted'])}</td></tr>")
    if rows:
        parts.append("<div class='sect'>CACHE_FILE_CLAIM</div>"
                     "<table class='sub'><tr><th>EXTERNAL_KEY</th><th>MEDIA_CONTEXT_TYPE (context type)</th>"
                     "<th>USER_ID</th><th>(category)</th><th>CREATION_TIMESTAMP_MILLIS (created)</th>"
                     "<th>EXPIRATION_TIMESTAMP_MILLIS (expires)</th>"
                     "<th>DELETED_TIMESTAMP_MILLIS (deleted)</th></tr>"
                     + "".join(rows) + "</table>")

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
        for scope, plist in sorted(groups.items(), key=lambda kv: (kv[0] in cross, kv[0])):
            collapsed = _collapse_part_paths(device_path(p, src_root, manifest) for p in plist)
            listed = "<br>".join(_esc(pp) for pp in collapsed)
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
            else:
                type_txt = "not a recognized plaintext media (encrypted or other)"
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
                hview.append(f"<a class='cclink' href='{_esc(e['view'])}' target='_blank'>▶ view cached file</a>{note}")
        elif e.get("view_note"):                               # recognized media too large to embed
            hview.append(f"<div class='muted'>▶ {_esc(e['view_note'])}</div>")
        parts.append(f"<div class='sect'>Cache file(s) on disk — {_fmt_bytes(e['on_disk']['bytes'])} present</div>"
                     + "".join(blocks) + "".join(hview))
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
            else:
                view = ("<span class='muted'>not a recognized plaintext media "
                        "(encrypted or other)</span>")
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

    # decrypted copy produced by the Memories report (encrypted cache bytes)
    if e.get("decrypted"):
        drows = []
        for d in e["decrypted"]:
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
                       "<th>MD5 / SHA-256 of the decrypted media</th><th>Memory (ZSNAPID)</th>"
                       "<th>view</th></tr>" + "".join(drows) + "</table>")

    # tombstones
    if e["tombstones"]:
        trows = []
        for t in e["tombstones"]:
            trows.append(f"<tr><td>{_esc(_mct_label(t['mct']))}</td><td>{_esc(t['reason'])}</td>"
                         f"<td>{_fmt_bytes(t['bytes'])}</td><td>{_esc(t['deleted'])}</td></tr>")
        parts.append("<div class='sect'>CACHE_FILE_SAMPLED_TOMBSTONE (deletion record)</div>"
                     "<table class='sub'><tr><th>MEDIA_CONTEXT_TYPE</th><th>DELETION_REASON</th>"
                     "<th>BYTES_DELETED</th><th>DELETED_TIMESTAMP_MILLIS</th></tr>"
                     + "".join(trows) + "</table>")

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
                    db_display, run_id="default"):
    total = len(entries)
    on_disk = sum(1 for e in entries if e["on_disk"]["found"])
    mem_linked = sum(1 for e in entries if e["memory"])
    chat_linked = sum(1 for e in entries if e["chats"])
    deleted = sum(1 for e in entries if e["tombstones"])
    xscope = sum(1 for e in entries if e["on_disk"].get("cross_scope"))
    categories = sorted({e["category"] for e in entries})

    # Row data + per-row detail go to sibling data/*.js files, and only the rows in the viewport are
    # ever built into the DOM (see scripts/report_ui.py). The document below stays a few KB whatever
    # the number of cache entries.
    data_dir = os.path.join(outdir, "data")
    details = [(f"ck-{e['cache_key']}", _detail_html(e, rel_prefix, src_root, manifest))
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
        is_xscope = bool(e["on_disk"].get("cross_scope"))
        users = ", ".join(u[:8] + "…" for u in e["users"])
        # cells carry as little markup as possible — per-column styling is in the CSS (.vc.cN),
        # because every byte here is multiplied by the number of cache entries in data/index.js
        cells = [
            "▸",
            _esc(e["category"]),
            _esc(e["cache_key"]),
            _esc(users),
            _external_key_summary(e["claims"]),
            _esc(type_lbl),
            _fmt_bytes(eff_size),
            _file_cell(e, rel_prefix) + (" <span class='xwarn' title='a copy sits in another "
                                         "account&#39;s SCContent scope'>⚠</span>" if is_xscope else ""),
            _links_html(e, rel_prefix, compact=True),
        ]
        # what the search box matches on: everything identifying, without the HTML around it
        searchable = [e["cache_key"], e["category"], type_lbl, users,
                      e.get("ondisk_md5", ""), e.get("ondisk_sha256", ""),
                      e.get("ondisk_type") or "", e["retrieval"].get("url") or "",
                      str(e["retrieval"].get("content_ref") or "")]
        searchable += [c["external_key"] for c in e["claims"]]
        searchable += [c["user_id"] for c in e["claims"]]
        if e["memory"]:
            searchable.append(e["memory"]["snap_id"])
        for ch in e["chats"]:
            searchable += [ch.get("conversation_id", ""), ch.get("server_message_id", "")]
        for k in e.get("child_files") or []:
            searchable += [str(k.get("name") or ""), k.get("md5") or "", k.get("sha256") or ""]
        for p in e["on_disk"]["paths"]:
            searchable.append(os.path.basename(p))
        rows.append([
            anchor, cells,
            " ".join(s for s in searchable if s).lower(),
            {"1": e["category"], "2": e["cache_key"], "3": users,
             "4": _external_key_summary(e["claims"]), "5": type_lbl, "6": eff_size,
             "7": ("2" if e.get("view") else "1" if e["on_disk"]["found"] else "0")},
            chunk_of.get(anchor),
            {"cat": e["category"], "disk": disk,
             "link": ",".join(linkbits), "xs": "yes" if is_xscope else "no"},
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

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Snapchat cache_controller.db</title><style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f4f8;color:#1b1b1f}}
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
 .vcells>.vc.c3{{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}}
 .vcells>.vc.c4{{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#555;overflow-wrap:anywhere}}
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
{report_ui.VTABLE_CSS}{report_ui.NAV_CSS}{report_ui.SELECT_CSS}
 .vcells>.vc{{font-size:12.5px}}
</style>
<script>window.SCAUTO_RUN={json.dumps(run_id)};window.SCAUTO_SELKIND="cc";</script>
<script>{report_ui.SELECT_JS}</script>
<script src="{rel_prefix}selection.js"></script>
<script>{report_ui.VTABLE_JS}</script></head><body>
<header><h1>Snapchat cache_controller.db</h1>
 <div class="sum">{total} physical cache files &middot; <b>{on_disk}</b> present on disk &middot;
 <b>{mem_linked}</b> linked to a Memory &middot; <b>{chat_linked}</b> linked to a chat &middot;
 <b>{xscope}</b> with a cross-scope copy &middot; {deleted} with a deletion record &middot;
 times in <b>{html.escape(tz_label)}</b></div>
 <div class="sum">Source: {html.escape(db_display)}</div></header>
{report_ui.missing_data_banner('CacheController_report.html')}
<div class="stickytop">
<div class="toolbar">
 <input type="search" id="q" placeholder="Search cache key, EXTERNAL_KEY, hash, user…" oninput="flt()">
 <label>Category <select id="cat" onchange="flt()"><option value="">all</option>{cat_opts}</select></label>
 <label>On disk <select id="disk" onchange="flt()"><option value="">any</option>
   <option value="yes">on disk</option><option value="no">not on disk</option></select></label>
 <label>Linked <select id="link" onchange="flt()"><option value="">any</option>
   <option value="Memory">Memory</option><option value="Chat">Chat</option></select></label>
 <label title="Only files with an on-disk copy in a different account's SCContent scope than the claim">
   <input type="checkbox" id="xscope" onchange="flt()"> ⚠ cross-scope only</label>
 <button id="xallbtn" data-o="0" onclick="xall(this)">Expand all</button>
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
 <div class="vc" onclick="SCV.setSort(3)">User <span class="ar">↕</span></div>
 <div class="vc" onclick="SCV.setSort(4)">EXTERNAL_KEY <span class="ar">↕</span></div>
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
 pager:'pager',pageSize:500,selKind:'cc',
 rowHeight:{CC_ROW_H},estDetail:320,cols:'{CC_COLS}',detailBase:'data/detail-',
 query:function(){{return document.getElementById('q').value;}},
 match:function(m,r){{
  var cat=document.getElementById('cat').value,disk=document.getElementById('disk').value,
      lk=document.getElementById('link').value,xs=document.getElementById('xscope').checked;
  return (!cat||m.cat===cat)&&(!disk||m.disk===disk)&&(!lk||(m.link||'').indexOf(lk)>-1)
       &&(!xs||m.xs==='yes')
       &&(!document.getElementById('selonly').checked||SCSel.get('cc',r[0]));}},
 selectedOnly:function(){{return document.getElementById('selonly').checked;}},
 selCount:function(n){{document.getElementById('selcount').textContent=n+' selected';
   scSelNote();}},
 count:function(n,t){{document.getElementById('count').textContent=
   n===t?(n+' entries'):(n+' of '+t+' shown');}},
 reset:function(){{
  document.getElementById('q').value='';document.getElementById('cat').value='';
  document.getElementById('disk').value='';document.getElementById('link').value='';
  document.getElementById('xscope').checked=false;
  document.getElementById('selonly').checked=false;}}
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
    chat_links, chat_by_message = load_chat_links(rdir)
    memory_pages = load_memory_pages(rdir)
    memory_media = load_memory_media(rdir)
    # the shared, examiner-owned selection file every report of this run loads
    report_ui.write_selection_stub(rdir, report_ui.run_id(rdir))
    # links to the sibling reports are relative to CacheController_report.html (…/Reports/CacheController/)
    rel_prefix = "../"

    all_entries, virtual = [], []
    for db in dbs:
        entries, virt = build_entries(db, app, scfull, scparts, mem_index, chat_links, ms_fmt,
                                      memory_pages, chat_by_message)
        all_entries.extend(entries)
        virtual.extend(virt)

    # hash the actual cached bytes and publish viewable plaintext media (hard-linked where possible,
    # always under a name with a real extension so browsers open it).
    materialize_ondisk(all_entries, scfull, scparts, os.path.join(outdir, "files"), outdir)
    # for entries whose cached bytes are encrypted, point at the copy the Memories report decrypted
    for e in all_entries:
        e["decrypted"] = memory_media.get(e["cache_key"].lower(), [])

    db_display = device_path(dbs[0], src_root, manifest) if dbs else ""
    report, stats = generate_report(all_entries, virtual, outdir, tz_label, rel_prefix,
                                    src_root, manifest, db_display, report_ui.run_id(rdir))
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
