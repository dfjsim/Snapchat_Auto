"""
Snapchat iOS ``Library/Caches`` report — the cached media and documents no index reaches.

The cache_controller report covers exactly what ``cache_controller.db`` claims: the
``com.snap.file_manager_*_SCContent_*`` folders. **This report covers everything else under
``Library/Caches``** — and the two are deliberately disjoint, so the same file never appears in
both under two different identifiers.

What lives there (see ``docs/snapchat_ios_cache_media.md`` for the artifact analysis):

* **story renders** at the ``Caches`` root and in ``Caches/tmp`` — plaintext MP4/JPEG needing no
  key, which can exist for content that was never saved to Memories. Their filename UUID is
  **ephemeral** and must never be presented as a snap id;
* ``sccache.gallery-stories-snap.data`` — AES-256-CBC with a key + fixed IV from
  ``Documents/ClientEncryptionService.plist``, **no keychain required**. On newer app versions this
  is the only place the full-resolution story video exists;
* the ``bplist00``-wrapped story thumbnails, and the URL-keyed PINCache stores (``SCCache``,
  ``global_scoped/sccache.*``, ``user_scoped/**``) whose filename **is** the CDN URL;
* ``SCPersistentMedia`` (saved chat media), and the ``caching-media`` packs the Memories report
  owns — both inventoried here and cross-linked rather than decoded a second time;
* **documents**: the ``cronet`` DNS host cache and HTTP blockfile cache, the ``NSURLCache``
  ``Cache.db``, ``sccache.nyc-impala`` API responses and ``KSCrash`` session state.

The report unit is **one row per distinct recovered content (SHA-256)**, not per file: the same
video appears at the ``Caches`` root and in ``Caches/tmp`` under two different UUIDs, and reporting
both would overstate what is on the device.

Attribution is **exact only** — an ``EXTERNAL_KEY`` lookup, a full conversation/message/part
triple, byte-identical content, or a CDN URL token. There is deliberately no duration/size
correlation pass.
"""

import os
import re
import io
import sys
import json
import html
import glob
import gzip
import base64
import struct
import hashlib
import logging
import plistlib
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse

from scripts import report_ui
from scripts.data import ccl_bplist
from scripts.data import sqlite_open
from scripts.data import sniff
from scripts.memories_media_report import (
    find_app_container, index_sccontent, device_path, load_path_manifest, make_time_formatter,
    guess_media, url_token, _UUID_RE,
)
from scripts.cache_controller_report import (
    find_cache_controllers, publish_view, publish_posters, load_chat_links, load_memory_index,
    load_memory_pages, load_memory_packs, POSTER_BASIS, PLAYABLE_EXTS, _fmt_bytes, _esc, _info,
)

try:
    from Crypto.Cipher import AES
except Exception:                                              # pragma: no cover
    AES = None

try:
    # Zstandard is in the standard library from Python 3.14 (PEP 784), which is what this project
    # targets — no third-party binding needed. Snapchat's Cronet HTTP cache stores response bodies
    # zstd-compressed, so without this those entries can only be identified, not read.
    from compression import zstd as _zstd
except ImportError:                                            # pragma: no cover - Python < 3.14
    _zstd = None

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- scope

# Folders under Library/Caches that belong to the cache_controller report. Matched on the folder
# NAME wherever it appears, not on its parent: SCContent folders exist under Library/Caches as well
# as under Documents (com.snap.file_manager_1_SCContent_* vs _3_*), and excluding them by parent
# would let every Caches-side one be reported twice under two different identifiers.
_SCCONTENT_DIR_RE = re.compile(r"^com\.snap\.file_manager_\d+_SCContent_", re.I)

SCOPE_NOTE = ("Every file under Library/Caches that cache_controller.db does not index — the story "
              "renders, PINCache stores, saved chat media, Memories packs and the cached documents. "
              "The com.snap.file_manager_*_SCContent_* folders are excluded wherever they appear "
              "(they are under both Documents/ and Library/Caches on some devices); those are the "
              "cache_controller report's subject, and nothing is listed by both reports.")


# --------------------------------------------------------------------------- content sniffing

# Lives in scripts/data/sniff.py so the cache_controller report can use the same identification —
# it used to know only jpg/mp4/png/webp and called everything else "encrypted".
sniff_content = sniff.sniff_content


_IMAGE_EXTS = ("jpg", "png", "webp", "gif")

# Everything sniff.guess_media resolves an "....ftyp" container to. They all carry the same magic
# bytes and only the brand tells them apart, so the mvhd atom (creation/modification time, duration)
# is worth looking for in any of them — an audio recording's timestamps are as much evidence as a
# video's. read_mvhd returns {} when the atom is absent, which is the answer for the still-image
# brands.
_ISOBMFF_EXTS = ("mp4", "mov", "m4v", "m4a", "3gp", "heic", "avif")


# --------------------------------------------------------------------------- key material

# Documents/ClientEncryptionService.plist is a Snap **TSAF container**, not a plist despite the
# name — plistlib.loads() raises "Invalid file" on it (header "TSAF\x03\x00\x04\x00"). The layout
# is a field-name string followed by its value string, so the key is read by locating the markers
# and taking the next printable run after each.
_ASCII_RUN_RE = re.compile(rb"[ -~]{4,}")

CLIENT_KEY_BASIS = (
    "The key and IV for sccache.gallery-stories-snap.data come from "
    "Documents/ClientEncryptionService.plist, which is a Snap TSAF container (not a plist — "
    "plistlib cannot read it). The fields 'encryption_key' and 'initialization_vector' are "
    "base64 and decode to 32 and 16 bytes: AES-256-CBC with an IV that is fixed per install, "
    "applied from offset 0 with PKCS#7 padding. No keychain is involved, so this works on any "
    "extraction that captured Documents/. The key values themselves are deliberately not shown "
    "or logged — they are live decryption keys for this device's media.")


def read_client_encryption(app):
    """Read the AES key/IV from ``Documents/ClientEncryptionService.plist``.

    Returns ``{"key", "iv", "identifier", "path", "note"}``; ``key``/``iv`` are None when the file
    is absent or unusable, which is a normal outcome, not an error. **The key material is never
    logged or written into the report** — only the fact that it was recovered.
    """
    out = {"key": None, "iv": None, "identifier": "", "path": "", "note": ""}
    hits = glob.glob(os.path.join(app, "Documents", "ClientEncryptionService.plist"))
    if not hits:
        out["note"] = ("Documents/ClientEncryptionService.plist is not in this extraction, so "
                       "sccache.gallery-stories-snap.data cannot be decrypted. On devices that "
                       "have no such cache this makes no difference.")
        return out
    out["path"] = hits[0]
    try:
        with open(hits[0], "rb") as fh:
            raw = fh.read()
    except OSError as error:
        out["note"] = f"could not be read: {error}"
        return out

    runs = [m.group(0) for m in _ASCII_RUN_RE.finditer(raw)]
    def after(marker, length):
        """The first printable run following ``marker`` whose length matches."""
        for i, run in enumerate(runs):
            if run == marker:
                for candidate in runs[i + 1:i + 4]:
                    if len(candidate) == length:
                        return candidate
        return None

    key_b64 = after(b"encryption_key", 44)                     # 32 bytes base64-encoded
    iv_b64 = after(b"initialization_vector", 24)               # 16 bytes base64-encoded
    ident = after(b"identifier", 36)
    try:
        key = base64.b64decode(key_b64) if key_b64 else None
        iv = base64.b64decode(iv_b64) if iv_b64 else None
    except Exception as error:
        out["note"] = f"the key fields are not valid base64 ({error})"
        return out
    if not (key and iv and len(key) == 32 and len(iv) == 16):
        out["note"] = ("the file is present but its encryption_key / initialization_vector fields "
                       "did not decode to 32 and 16 bytes, so it cannot be used")
        return out
    out["key"], out["iv"] = key, iv
    out["identifier"] = (ident or b"").decode("ascii", "replace")
    out["note"] = ("a 32-byte AES key and 16-byte fixed IV were recovered (values withheld); "
                   "no keychain was required")
    return out


# --------------------------------------------------------------------------- decode chain

def _largest_data_blob(plist_bytes):
    """The biggest data blob in an NSKeyedArchiver plist — where these caches put the media."""
    try:
        parsed = plistlib.loads(plist_bytes)
    except Exception:
        try:
            parsed = ccl_bplist.load(io.BytesIO(plist_bytes))
        except Exception:
            return None
    objects = parsed.get("$objects") if isinstance(parsed, dict) else None
    if not isinstance(objects, list):
        objects = [parsed] if not isinstance(parsed, dict) else list(parsed.values())
    blobs = [o for o in objects if isinstance(o, (bytes, bytearray)) and len(o) > 64]
    return bytes(max(blobs, key=len)) if blobs else None


def _strip_pkcs7(data):
    if not data:
        return data
    n = data[-1]
    return data[:-n] if 1 <= n <= 16 and len(data) >= n and data[-n:] == bytes([n]) * n else data


def decode_payload(raw, key=None, iv=None):
    """Recover the real content of one cache file.

    Returns ``(payload, kind, ext, steps)`` — ``steps`` is the plain-language chain the report puts
    behind the file's "?" so the examiner can repeat it. ``payload`` is None when nothing could be
    recovered, which is reported as such rather than hidden.
    """
    steps = []
    kind, ext = sniff_content(raw[:16])

    if kind == "media":
        return raw, kind, ext, ["The bytes on disk are already plaintext media (recognised by its "
                                "magic bytes); nothing was decoded or decrypted."]

    if kind == "gzip":
        try:
            inner = gzip.decompress(raw)
            steps.append("gzip-decompressed.")
            k2, e2 = sniff_content(inner[:16])
            if k2 == "media":
                return inner, k2, e2, steps + ["The result is plaintext media."]
            raw, kind, ext = inner, k2, e2
        except Exception as error:
            steps.append(f"gzip decompression failed ({error}).")

    if kind == "bplist":
        blob = _largest_data_blob(raw)
        steps.append("Unarchived the bplist00 NSKeyedArchiver wrapper and took the largest data "
                     "blob in $objects — this is how these caches store an image.")
        if blob:
            k2, e2 = sniff_content(blob[:16])
            if k2 == "media":
                return blob, k2, e2, steps + [f"The blob is a plaintext {e2}."]
            return blob, k2, e2, steps + ["The blob is not recognisable media."]
        return None, kind, ext, steps + ["No data blob large enough to be media was found."]

    # Opaque and block-aligned: try the ClientEncryptionService key (gallery-stories-snap).
    if kind == "unknown" and key and iv and AES is not None and raw and len(raw) % 16 == 0:
        try:
            plain = _strip_pkcs7(AES.new(key, AES.MODE_CBC, iv).decrypt(raw))
            if plain[:8] == b"bplist00":
                steps.append("Decrypted with AES-256-CBC from offset 0 using the key and fixed IV "
                             "in Documents/ClientEncryptionService.plist, then stripped PKCS#7 "
                             "padding. The result is an NSKeyedArchiver plist.")
                blob = _largest_data_blob(plain)
                if blob:
                    k2, e2 = sniff_content(blob[:16])
                    steps.append("Took the largest data blob in $objects, which is the media.")
                    return blob, k2, e2, steps
                return plain, "bplist", "plist", steps + ["No media blob inside the plist."]
            k2, e2 = sniff_content(plain[:16])
            if k2 == "media":
                return plain, k2, e2, steps + ["Decrypted with the ClientEncryptionService key "
                                               "(AES-256-CBC, fixed IV) straight to media."]
        except Exception as error:
            logger.debug(f"gallery-stories-snap decrypt attempt failed: {error}")

    if kind == "zstd":
        if _zstd is None:                                      # pragma: no cover - Python < 3.14
            return None, kind, ext, [
                "zstd-compressed. Identified by magic bytes but not decompressed: this Python has "
                "no compression.zstd module (it is standard from Python 3.14)."]
        try:
            inner = _zstd.decompress(raw)
        except Exception as error:
            # A truncated frame is the normal failure here — the cache holds only part of the body
            return None, kind, ext, [
                f"zstd-compressed, but the frame could not be decompressed ({error}). The usual "
                f"cause is a partially cached response: the frame is cut short."]
        steps.append(f"zstd-decompressed ({len(raw)} → {len(inner)} bytes).")
        k2, e2 = sniff_content(inner[:16])
        if k2 == "media":
            return inner, k2, e2, steps + [f"The result is a plaintext {e2}."]
        if k2 == "bplist":
            blob = _largest_data_blob(inner)
            if blob:
                k3, e3 = sniff_content(blob[:16])
                return blob, k3, e3, steps + ["Unarchived the bplist inside and took its largest "
                                              "data blob."]
        members = bundle_members(inner)
        if members:
            return inner, "bundle", "bin", steps + [
                f"The decompressed content is a Snapchat resource bundle holding "
                f"{len(members)} named member(s) (see the member list). These are UI assets the "
                f"app downloaded, not user media."]
        return inner, k2, e2, steps + [
            "The decompressed content is not recognisable media — these are usually Snapchat "
            "resource bundles fetched by the app."]
    if kind == "font":
        return raw, kind, ext, ["A TrueType/OpenType font. sccache.dynamic-caption.data looks "
                                "encrypted at a glance but is a font cache — one file per caption "
                                "style, keyed by the CDN URL it was fetched from. Not media."]
    if kind == "lzc":
        return raw, kind, ext, ["A Snap 'LZC' asset container (lens/asset bundle). Not user media."]
    if kind == "tsaf":
        return raw, kind, ext, ["A Snap 'TSAF' container — a structured API record, stored "
                                "plaintext."]
    return (raw if kind != "unknown" else None), kind, ext, steps + [
        "The bytes match no format this tool recognises, and no available key decrypts them."]


# --------------------------------------------------------------------------- mvhd

# A Snapchat resource bundle (the zstd-compressed bodies in the Cronet HTTP cache) is a run of
# length-prefixed members: a name like "res/theme_lightpurple_background.png" followed by the
# member's bytes. The exact header layout is not documented here, so members are located by their
# names rather than by walking the structure — enough to say what the bundle contains without
# claiming a parse that has not been verified.
_BUNDLE_MEMBER_RE = re.compile(rb"(?:res|assets|fonts)/[A-Za-z0-9_./-]{2,120}")


def bundle_members(data, limit=200):
    """Named members inside a Snapchat resource bundle, in order of appearance."""
    seen, out = set(), []
    for mo in _BUNDLE_MEMBER_RE.finditer(data or b""):
        name = mo.group(0).decode("ascii", "replace")
        if name not in seen:
            seen.add(name)
            out.append(name)
        if len(out) >= limit:
            break
    return out


_MVHD_EPOCH = datetime(1904, 1, 1)


def read_mvhd(path):
    """``{created, modified, duration_s, timescale}`` from an MP4/MOV ``moov/mvhd``, or {}.

    These are the only timestamps on a root-level cache render that are not filesystem-derived, so
    they are worth having even though the file has no database row anywhere.
    """
    try:
        with open(path, "rb") as fh:
            return _find_mvhd(fh, 0, os.path.getsize(path))
    except (OSError, struct.error, ValueError):
        return {}


def _find_mvhd(fh, start, end, depth=0):
    """Walk the atom tree for moov/mvhd. Only containers on the path are descended into."""
    if depth > 4:
        return {}
    pos = start
    while pos < end - 8:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return {}
        size, kind = struct.unpack(">I4s", header)
        body = pos + 8
        if size == 1:                                          # 64-bit extended size
            size = struct.unpack(">Q", fh.read(8))[0]
            body += 8
        elif size == 0:
            size = end - pos
        if size < 8:
            return {}
        if kind == b"mvhd":
            fh.seek(body)
            data = fh.read(min(size - (body - pos), 120))
            return _parse_mvhd(data)
        if kind in (b"moov", b"trak", b"mdia"):
            found = _find_mvhd(fh, body, pos + size, depth + 1)
            if found:
                return found
        pos += size
    return {}


def _parse_mvhd(data):
    if len(data) < 4:
        return {}
    version = data[0]
    try:
        if version == 1:
            created, modified, timescale, duration = struct.unpack(">QQIQ", data[4:36])
        else:
            created, modified, timescale, duration = struct.unpack(">IIII", data[4:20])
    except struct.error:
        return {}
    if not timescale:
        return {}

    def when(seconds):
        try:
            return (_MVHD_EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return ""
    return {"created": when(created), "modified": when(modified),
            "duration_s": round(duration / timescale, 2), "timescale": timescale}


MVHD_BASIS = (
    "Read from the MP4/MOV container's own moov/mvhd atom, not from any database or from the "
    "filesystem — for a render at the Caches root it is the only timestamp that exists. mvhd times "
    "are specified as UTC, but Apple encoders are not consistently faithful to that, so corroborate "
    "against the filesystem timestamps before stating a timezone in a report.")


# --------------------------------------------------------------------------- producer tag

# The prefix/suffix around a cache filename's UUID identifies what produced the file. It is the
# only meaningful part of the name — the UUID itself is ephemeral at the Caches root.
_PRODUCERS = [
    (re.compile(r"^filtered-", re.I), "filter / render pass"),
    (re.compile(r"~thumbnail-generation", re.I), "thumbnail pass"),
    (re.compile(r"^recorded-", re.I), "camera recording"),
    (re.compile(r"^cm-chat-media", re.I), "saved chat media"),
    (re.compile(r"^carousel-thumbnail-v2-", re.I), "story carousel thumbnail"),
    (re.compile(r"^large-thumbnail-v2-", re.I), "story thumbnail (large)"),
    (re.compile(r"^small-thumbnail-v2-", re.I), "story thumbnail (small)"),
    (re.compile(r"^profile_management_thumbnail", re.I), "profile thumbnail"),
    (re.compile(r"^discover-feed-thumbnail-v2-", re.I), "Discover feed thumbnail"),
]


def producer_of(name):
    """What produced this file, from its filename prefix/suffix. '' when the name says nothing.

    An empty producer is a normal result: the AFU test device names its root renders with a
    bare UUID and no prefix at all.
    """
    for pattern, label in _PRODUCERS:
        if pattern.search(name):
            return label
    return ""


# --------------------------------------------------------------------------- URL-keyed names

def decode_cache_key_url(name):
    """The CDN URL a PINCache/sccache entry is keyed by, or ''.

    Those caches use the URL-encoded request key as the on-disk filename, so the URL — and through
    it the media token that joins to ZGALLERYSNAP — is recoverable straight from the name.
    """
    if "%3A%2F%2F" not in name and not name.lower().startswith(("http%3a", "https%3a")):
        if "%3A" not in name and "%2F" not in name:
            return ""
    decoded = unquote(name.replace("%2E", ".").replace("%2e", "."))
    mo = re.search(r"https?://[^\s]+", decoded)
    return mo.group(0) if mo else ""


def inner_bolt_url(url):
    """Snap's bolt CDN wraps a base64 of the real URL in its path; return it when present."""
    mo = re.search(r"/(?:bolt|bolt_df)/([A-Za-z0-9+/=_-]{16,})", url or "")
    if not mo:
        return ""
    token = mo.group(1)
    try:
        decoded = base64.b64decode(token + "=" * (-len(token) % 4)).decode("ascii", "replace")
    except Exception:
        return ""
    return decoded if decoded.startswith("http") else ""


# --------------------------------------------------------------------------- document parsers

# Chromium counts microseconds from 1601-01-01 (the Windows FILETIME epoch), which is what the
# cronet host cache stores its expirations in.
_CHROME_EPOCH = datetime(1601, 1, 1)


def _chrome_time(value, ms_fmt=None):
    try:
        dt = _CHROME_EPOCH + timedelta(microseconds=int(value))
    except (TypeError, ValueError, OverflowError):
        return str(value or "")
    return dt.strftime("%Y-%m-%d %H:%M:%S") + " UTC"


CRONET_BASIS = (
    "Snapchat embeds Chromium's network stack (Cronet). local_prefs.json is its preference file; "
    "net.host_cache holds the DNS results the app resolved — hostname, the IP addresses returned, "
    "and an expiry. Expirations are microseconds since 1601-01-01 (Chromium's epoch) and are shown "
    "converted to UTC. This is evidence of which hosts the app contacted, and roughly when, "
    "independent of any Snapchat database.")


def parse_cronet_prefs(path):
    """``[{hostname, addresses, expiration, secure}]`` from a cronet ``local_prefs.json``."""
    try:
        with open(path, "rb") as fh:
            data = json.load(fh)
    except Exception as error:
        logger.debug(f"cronet prefs {path}: {error}")
        return []
    entries = (((data or {}).get("net") or {}).get("host_cache")) or []
    out = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        out.append({
            "hostname": str(entry.get("hostname") or ""),
            "addresses": ", ".join(str(a) for a in (entry.get("addresses") or [])),
            "expiration": _chrome_time(entry.get("expiration")),
            "secure": "yes" if entry.get("secure") else "no",
        })
    return sorted(out, key=lambda r: r["hostname"])


CRONET_CACHE_BASIS = (
    "Chromium's blockfile disk cache: an 'index', block files (data_0..data_3) holding one 256-byte "
    "EntryStore record per cached response, and one f_XXXXXX file per body too large to inline. "
    "Each EntryStore carries the request key (the URL), the response's creation time, and the "
    "addresses of its data streams; a stream address whose type field is 0 names a separate "
    "f_XXXXXX file, which is how a URL is joined to the bytes of its response. Bodies are then run "
    "through the same sniff/decode pipeline as every other file in this report. Snapchat CDN URLs "
    "here carry the same /d/<token> media token that joins to a Memory's download URL. Entries "
    "whose EntryStore could not be parsed fall back to a raw scan of the block files, which "
    "recovers the URL but not which body belongs to it — those rows say so.")

# Chromium disk_format.h. An EntryStore is exactly 256 bytes at a 256-byte-aligned offset in
# data_1, after that block file's 8 KB header:
#
#   32  int32     key_len
#   56  CacheAddr data_addr[4]      stream 1 is the response body
#   96+ char      key[]             the request URL, inline when it is short enough
#
# The key offset moved between Chromium versions (96 in the classic layout, 100 in the Cronet build
# on the AFU test device, which carries one more padding word), so both are tried and the one
# whose bytes actually parse as a URL of the declared length wins. Everything before the key is
# unchanged between the two, which is why key_len and data_addr can be read at fixed offsets —
# verified on that device: all 42 external stream addresses resolved to an f_* file that exists.
_BLOCK_HEADER = 8192
_ENTRY_SIZE = 256
_KEY_OFFSETS = (100, 96)
_KEY_LEN_OFF = 32
_DATA_ADDR_OFF = 56
_DATA_SIZE_OFF = 40


def _cache_addr(value):
    """Decode a Chromium CacheAddr. Returns ``(file_type, file_number)`` or None if uninitialised.

    Bit 31 marks the address initialised; bits 28-30 are the file type, where **0 means a separate
    ``f_XXXXXX`` file** and 1-4 are the block files. For a separate file the low 28 bits are the
    file number.
    """
    if not value & 0x80000000:
        return None
    return ((value >> 28) & 0x7, value & 0x0FFFFFFF)


def parse_blockfile_entries(cache_dir):
    """``[{url, body_file, size, created}]`` by reading the EntryStore records in data_1.

    This is what joins a cached request URL to the file holding its response body — the raw scan
    below can only list the two separately.
    """
    out = []
    path = os.path.join(cache_dir, "data_1")
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return out
    existing = {n for n in os.listdir(cache_dir) if n.startswith("f_")}
    for base in range(_BLOCK_HEADER, len(blob) - _ENTRY_SIZE + 1, _ENTRY_SIZE):
        try:
            key_len = struct.unpack_from("<i", blob, base + _KEY_LEN_OFF)[0]
            addrs = struct.unpack_from("<4I", blob, base + _DATA_ADDR_OFF)
            sizes = struct.unpack_from("<4i", blob, base + _DATA_SIZE_OFF)
        except struct.error:
            continue
        if not 0 < key_len <= _ENTRY_SIZE - min(_KEY_OFFSETS):
            continue
        url = ""
        for key_off in _KEY_OFFSETS:
            raw = blob[base + key_off:base + key_off + key_len]
            if len(raw) < key_len:
                continue
            try:
                candidate = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            # Chromium may prefix a key with its cache partition ("_dk_<…> <url>")
            mo = re.search(r"https?://[!-~]+$", candidate)
            if mo:
                url = mo.group(0)
                break
        if not url:
            continue
        body = ""
        for index in (1, 0, 2, 3):                             # stream 1 is the response body
            decoded = _cache_addr(addrs[index])
            if decoded and decoded[0] == 0:                    # 0 = a separate f_XXXXXX file
                name = "f_%06x" % decoded[1]
                if name in existing:
                    body = name
                    break
        out.append({"url": url, "body_file": body,
                    "size": sizes[1] if sizes[1] > 0 else 0})
    return out


def scan_cronet_cache(cache_dir):
    """``{urls, bodies}`` for a ``cronet/disk_cache`` directory.

    Deliberately a scan rather than a blockfile parse: the evidentiary value is the set of URLs the
    app fetched and the bodies it kept, and a scan recovers both without depending on a Chromium
    cache-format version. The limitation is stated in the report, not hidden.
    """
    urls, bodies = [], []
    if not os.path.isdir(cache_dir):
        return {"urls": [], "bodies": []}
    url_re = re.compile(rb"https?://[\x21-\x7e]{8,300}")
    for name in sorted(os.listdir(cache_dir)):
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        if name == "index" or name.startswith("data_"):
            try:
                with open(path, "rb") as fh:
                    blob = fh.read()
            except OSError:
                continue
            for mo in url_re.finditer(blob):
                url = mo.group(0).decode("ascii", "replace")
                url = re.split(r"[\x00-\x1f]", url)[0]
                if len(url) > 12:
                    urls.append(url)
        elif name.startswith("f_"):
            bodies.append(path)
    # dedupe, keep order
    seen, ordered = set(), []
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return {"urls": ordered, "bodies": bodies}


NSURLCACHE_BASIS = (
    "Library/Caches/com.toyopagroup.picaboo/Cache.db is a standard iOS NSURLCache "
    "(cfurl_cache_response / cfurl_cache_blob_data / cfurl_cache_receiver_data). It is read with "
    "and without its write-ahead log. On every device examined so far it is present but holds zero "
    "entries — that is reported explicitly rather than the section being omitted, so an empty "
    "result is distinguishable from one that was never looked at.")


def parse_nsurlcache(path):
    """``(rows, note)`` from an NSURLCache ``Cache.db``, read both with and without its -wal."""
    query = ("select entry_ID, request_key, time_stamp, partition "
             "from cfurl_cache_response order by entry_ID")
    try:
        views = sqlite_open.open_views(path)
    except Exception as error:
        return [], f"could not be opened: {error}"
    try:
        columns = set(sqlite_open._columns(views.merged, "cfurl_cache_response"))
        if not columns:
            return [], "the cfurl_cache_response table is not present"
        select = [c for c in ("entry_ID", "request_key", "time_stamp", "partition") if c in columns]
        rows, marks = sqlite_open.query_both(
            views, f"select {', '.join(select)} from cfurl_cache_response")
        out = [dict(zip(select, row), _wal=mark) for row, mark in zip(rows, marks)]
        note = ("present, 0 entries — the cache index exists but holds no cached response"
                if not out else f"{len(out)} cached response(s)")
        return out, note
    finally:
        views.close()


def parse_crashstate(path):
    """KSCrash ``CrashState.json`` as ordered key/value pairs."""
    try:
        with open(path, "rb") as fh:
            data = json.load(fh)
    except Exception:
        return []
    labels = {
        "crashedLastLaunch": "crashed on the previous launch",
        "activeDurationSinceLastCrash": "seconds active since the last crash",
        "backgroundDurationSinceLastCrash": "seconds backgrounded since the last crash",
        "launchesSinceLastCrash": "app launches since the last crash",
        "sessionsSinceLastCrash": "sessions since the last crash",
        "sessionIdLastLaunch": "session id of the previous launch",
    }
    return [(f"{k} ({labels[k]})" if k in labels else k, v) for k, v in (data or {}).items()]


def tsaf_fields(raw):
    """The printable field names and values in a TSAF container, in order."""
    runs = [m.group(0).decode("ascii", "replace") for m in _ASCII_RUN_RE.finditer(raw or b"")]
    return runs[1:41]                                          # skip the "TSAF" magic itself


# --------------------------------------------------------------------------- categories

CAT_MEDIA = "Evidentiary media"
CAT_DOC = "Document"
CAT_ASSET = "App asset"
CAT_ELSEWHERE = "Covered by another report"
CAT_UNKNOWN = "Not recovered"

UNRECOVERED_BASIS = (
    "Files whose content this report could not produce: the bytes are not a format it recognises "
    "and no key applies. It deliberately EXCLUDES two groups that are not failures — the "
    "caching-media packs and SCPersistentMedia files, which another report decodes in full and "
    "which are listed here only so the Library/Caches inventory is complete, and app assets (lens "
    "bundles, fonts, shader and CoreML caches) that hold no user content. Counting those as "
    "'not recovered' made this number an order of magnitude too large.")

# Which report already owns a location, so it is inventoried and cross-linked here but never
# decoded a second time.
_OWNED_ELSEWHERE = {
    "caching-media": ("Memories", "The Memories report decrypts these .pack files and links each "
                                  "to its Memory; they are listed here only so the inventory of "
                                  "Library/Caches is complete."),
    "SCPersistentMedia": ("Conversations", "These are chat media the user saved. The Conversations "
                                           "report renders them in the message they belong to; the "
                                           "row here links to that message."),
}

# Directories whose content is app machinery rather than user data. Still listed — a URL-keyed name
# records that the asset was fetched — but hidden by the default filter.
_ASSET_DIRS = ("Lenses", "LensUserData", "app.aifactory.splendidSDK", "com.apple.dyld",
               "com.apple.metal", "com.apple.metalfe", "com.apple.opengl", "com.apple.gpuarchiver",
               "sccache.dynamic-caption.data", "coreml_compiled", "mlmodelc", "SCCache/com.pinterest"
               ".PINDiskCache.bitmoji-builder-asset-cache")


def classify(rel_path, kind, ext):
    """(category, note) for one file, from where it lives and what its bytes turned out to be."""
    top = rel_path.split("/")[0]
    owner = _OWNED_ELSEWHERE.get(top)
    if owner:
        return CAT_ELSEWHERE, owner[1]
    if kind == "font":
        return CAT_ASSET, "a downloaded caption font, not media"
    if kind in ("lzc", "zstd", "bundle"):
        return CAT_ASSET, "an app resource/lens bundle, not media"
    if any(part in rel_path for part in _ASSET_DIRS):
        return CAT_ASSET, "app machinery (lens models, shader caches, SDK assets)"
    if kind == "media":
        return CAT_MEDIA, ""
    if kind in ("tsaf", "sqlite", "text"):
        return CAT_DOC, ""
    if kind == "empty":
        return CAT_UNKNOWN, "the file is 0 bytes on disk"
    return CAT_UNKNOWN, ""


# --------------------------------------------------------------------------- the walk

def walk_caches(app):
    """Every file under ``Library/Caches`` that is not in an SCContent folder.

    Yields ``(absolute path, path relative to Library/Caches)``.
    """
    root = os.path.join(app, "Library", "Caches")
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # prune the cache_controller report's folders wherever they appear (see _SCCONTENT_DIR_RE)
        dirnames[:] = [d for d in dirnames if not _SCCONTENT_DIR_RE.match(d)]
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.isfile(full):
                yield full, os.path.relpath(full, root).replace("\\", "/")


RENAMED_BASIS = (
    "iOS allows characters in a filename that Windows does not — the URL-keyed cache stores name "
    "each entry after the CDN URL it was fetched from, query string and all, so those names carry "
    "a '?'. Extraction percent-encodes them so the file can be written at all (before this they "
    "were silently dropped). The name shown here is the exact one the file had on the device, read "
    "back from extraction_manifest.json; percent-decoding either spelling yields the same URL.")


def load_renamed(src_root, app):
    """``{relative path on disk: exact on-device name}`` from the extraction manifest."""
    for root in (src_root, app, os.path.dirname(app or ""), os.path.dirname(app or "") + "/.."):
        if not root:
            continue
        candidate = os.path.join(root, "extraction_manifest.json")
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as fh:
                    return json.load(fh).get("renamed") or {}
            except Exception as error:
                logger.debug(f"could not read {candidate}: {error}")
    return {}


def _device_name(full, rel, renamed):
    """The file's exact on-device name, when extraction had to sanitise it; '' otherwise."""
    if not renamed:
        return ""
    tail = full.replace("\\", "/")
    for sanitised, original in renamed.items():
        if tail.endswith(sanitised) or sanitised.endswith(rel):
            return original.split("/")[-1]
    return ""


def _read(path, limit=None):
    try:
        with open(path, "rb") as fh:
            return fh.read() if limit is None else fh.read(limit)
    except OSError as error:
        logger.debug(f"could not read {path}: {error}")
        return b""


def _hashes(data):
    return hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- exact attribution

# A chat claim's EXTERNAL_KEY, and an SCPersistentMedia filename, both carry
# "<type>:<conversation>:<message>:<part>". Matching on that whole triple is required: the embedded
# UUID alone is conversation-level and matched 20 unrelated claims for a single file on the 2026
# device, so a UUID-only join is far too coarse to state as an attribution.
_TRIPLE_RE = re.compile(r"(?P<type>[^:_]*)[:_](?P<conv>[0-9a-fA-F-]{36})[:_](?P<msg>\d+)[:_]"
                        r"(?P<part>\d+)")


def load_claims(app):
    """``(by_uuid, by_triple, cache_keys)`` from every ``cache_controller.db``.

    Read with **and** without each database's -wal, so a claim the write-ahead log has dropped
    still attributes a file that is on disk.
    """
    by_uuid, by_triple, cache_keys = {}, {}, set()
    for db in find_cache_controllers(app):
        rows, marks, _info = sqlite_open.read_all(db, "CACHE_FILE_CLAIM")
        for row, mark in zip(rows, marks):
            ek, ck = row.get("EXTERNAL_KEY") or "", row.get("CACHE_KEY") or ""
            if not ck:
                continue
            cache_keys.add(ck.lower())
            rec = {"external_key": ek, "cache_key": ck, "mct": row.get("MEDIA_CONTEXT_TYPE"),
                   "user_id": row.get("USER_ID") or "", "wal": mark}
            # "<USERNAME>~<snapId>" (context 3) or a bare "<snapId>" (context 4): the owner
            # username is recoverable here and nowhere else in the filename
            owner, _sep, tail = ek.partition("~")
            mo = _UUID_RE.search(tail or ek)
            if mo:
                rec["owner"] = owner if _sep and not _UUID_RE.match(owner) else ""
                by_uuid.setdefault(mo.group(0).upper(), []).append(rec)
            mt = _TRIPLE_RE.search(ek)
            if mt:
                by_triple.setdefault((mt.group("conv").lower(), mt.group("msg"),
                                      mt.group("part")), []).append(rec)
    return by_uuid, by_triple, cache_keys


# caching-media/<folder>/<item hash>-<chunk>.pack — the manifest the Memories report writes is
# keyed by "<folder>/<item hash>", because a single item is stored as several numbered chunks and
# they are decrypted as one concatenation.
_PACK_REL_RE = re.compile(r"^caching-media/([0-9a-f]{2,64})/([0-9a-f]{64})-\d+\.pack$", re.I)


def _pack_keys(entry, packs):
    """[(manifest key, media rows)] for every copy of this entry that is a caching-media pack.

    An entry is one distinct *content*, and the same bytes can appear as several files, so every
    copy's path is checked rather than just the representative one.
    """
    out = []
    seen = set()
    for copy in entry.get("copies") or [{"rel": entry.get("rel", "")}]:
        mo = _PACK_REL_RE.match((copy.get("rel") or "").replace("\\", "/"))
        if not mo:
            continue
        key = f"{mo.group(1)}/{mo.group(2)}".lower()
        if key in seen:
            continue
        seen.add(key)
        if packs.get(key):
            out.append((key, packs[key]))
    return out


def attribute(entry, claims_by_uuid, claims_by_triple, sc_by_size, mem_index, memory_pages,
              chat_by_key, chat_by_message, packs=None):
    """Attach every exact link this file supports. Each records the method that produced it.

    Priority: the claim a UUID in the filename names, then the conversation/message/part triple,
    then byte-identical content in SCContent, then the CDN token in a URL-keyed filename. Nothing
    here is a heuristic — a link is only made when an identifier or the bytes themselves match.
    """
    links = []
    name = entry["name"]
    at_root = "/" not in entry["rel"]

    # 0. a caching-media pack the Memories report decrypted. Its name is an opaque hash indexed by
    #    no database, so this manifest is the only thing that can attribute it — and without it
    #    every pack showed here as a padlock with no link, despite having been fully decrypted.
    #    The decrypted file itself is carried on the link so this report can SHOW it: saying "the
    #    Memories report decoded this" while displaying a padlock reads as "not recovered", which
    #    is the opposite of what happened to these bytes.
    for item_key, media in _pack_keys(entry, packs or {}):
        for rec in media[:2]:
            links.append({
                "kind": "memory", "snap_id": rec["snap_id"],
                "page": memory_pages.get(rec["snap_id"]),
                "media_path": rec.get("path", ""), "media_ext": rec.get("ext", ""),
                "media_role": rec.get("role", ""), "media_bytes": rec.get("bytes", 0),
                "basis": (f"The Memories report decrypted this caching-media pack ({item_key}) with "
                          f"the AES-256-CBC key of Memory {rec['snap_id']} and recovered "
                          f"{rec.get('ext', '')} media from it. Pack filenames are opaque hashes "
                          f"that no database indexes, so decrypt-and-match is the only link there "
                          f"is: the key either produces valid media or it does not."),
            })

    # 1. a UUID in the filename that a CACHE_FILE_CLAIM names
    for mo in _UUID_RE.finditer(name):
        if at_root:
            break                                              # root UUIDs are ephemeral (below)
        for rec in claims_by_uuid.get(mo.group(0).upper(), [])[:4]:
            links.append({
                "kind": "cache", "key": rec["cache_key"], "owner": rec.get("owner", ""),
                "basis": (f"The UUID {mo.group(0)} in this filename is the one in the "
                          f"cache_controller claim EXTERNAL_KEY \"{rec['external_key']}\", whose "
                          f"CACHE_KEY is {rec['cache_key']}"
                          + (f". That claim carries the owner username \"{rec['owner']}\", which "
                             f"the filename alone never gives." if rec.get("owner") else ".")),
            })
        if links:
            break

    # 2. the full <conversation>:<message>:<part> triple (SCPersistentMedia)
    mt = _TRIPLE_RE.search(name)
    if mt:
        key = (mt.group("conv").lower(), mt.group("msg"), mt.group("part"))
        smid = f"{mt.group('msg')}.{mt.group('part')}"
        for rec in chat_by_message.get(f"{mt.group('conv')}|{smid}", [])[:2]:
            links.append({
                "kind": "chat", "rec": rec,
                "basis": (f"This filename carries conversation {mt.group('conv')} and message "
                          f"{smid}, which the Conversations report reported for that message. The "
                          f"match is on the whole conversation/message/part triple, not on the "
                          f"embedded UUID alone — that UUID is conversation-level and would match "
                          f"many unrelated files."),
            })
        for rec in claims_by_triple.get(key, [])[:2]:
            links.append({
                "kind": "cache", "key": rec["cache_key"],
                "basis": (f"The same conversation/message/part triple appears in the "
                          f"cache_controller claim \"{rec['external_key']}\" (CACHE_KEY "
                          f"{rec['cache_key']})."),
            })

    # 3. byte-identical content in SCContent. Only files whose SIZE matches an SCContent file are
    #    hashed, so this never walks the whole cache tree. This is how a root render attributes at
    #    all: its own UUID is ephemeral and is referenced nowhere.
    if entry.get("sha256"):
        for path in sc_by_size.get(entry["bytes"], []):
            other = _read(path)
            if hashlib.sha256(other).hexdigest() == entry["sha256"]:
                key = os.path.basename(path)
                links.append({
                    "kind": "cache", "key": key,
                    "basis": (f"The recovered bytes are byte-identical (SHA-256) to the SCContent "
                              f"cache file {key}. This is how a file at the Caches root attributes: "
                              f"its own filename UUID is ephemeral and is referenced nowhere on the "
                              f"device, so content equality is the only exact link. SCContent files "
                              f"are hashed as stored, not only after decryption — a linker that "
                              f"only hashes decrypted output misses every plaintext story snap."),
                })
                break

    # 4. a CDN token in a URL-keyed filename
    if entry.get("url"):
        token = url_token(entry["url"])
        if token:
            digest = hashlib.sha256(token.encode()).hexdigest()[:32]
            hit = mem_index["url_keys"].get(digest)
            if hit:
                canonical, user_hash, field = hit
                links.append({
                    "kind": "memory", "snap_id": canonical,
                    "page": memory_pages.get(canonical),
                    "basis": (f"This file is keyed by the CDN URL {entry['url']}. SHA-256 of its "
                              f"media token \"{token}\" (first 16 bytes) is {digest}, which equals "
                              f"the cache key of a Memory's {field}."),
                })
    return links


def _root_uuid_warning(entry):
    """The warning a root-level filename UUID must always carry."""
    if "/" in entry["rel"] or not _UUID_RE.search(entry["name"]):
        return ""
    return ("This filename contains a UUID, but a UUID at the Library/Caches ROOT is a scratch "
            "identifier minted when the file was written — it is NOT a snap id. This was established "
            "by searching every root UUID across every file in the app container, as ASCII and as "
            "its 16-byte binary form, with zero hits. Files here are attributed by content hash "
            "instead; never quote this UUID as an identifier for the media.")


# --------------------------------------------------------------------------- entries

# Reading a whole file into memory to hash and decode it is fine for a cache tree, but a stray
# multi-gigabyte file should not be. Above this, the file is hashed in chunks and not decoded.
MAX_DECODE_BYTES = 256 * 1024 * 1024


def _stream_hashes(path):
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


def build_entries(app, key_info, ms_fmt, src_root=None, manifest=None, renamed=None):
    """One entry per file under Library/Caches, deduplicated by recovered content.

    Returns ``(entries, stats)``. Entries are keyed by the SHA-256 of the **recovered payload** (or
    of the raw bytes when nothing was recovered), so the same video stored at the Caches root and
    in Caches/tmp under two different UUIDs is one row with two copies — reporting it twice would
    overstate what is on the device.
    """
    renamed = renamed or {}
    by_content, stats = {}, {"files": 0, "bytes": 0, "decoded": 0, "failed": 0}
    for full, rel in walk_caches(app):
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        stats["files"] += 1
        stats["bytes"] += size
        name = os.path.basename(rel)

        if size > MAX_DECODE_BYTES:
            md5, sha, _total = _stream_hashes(full)
            raw, payload, steps = b"", None, [f"{_fmt_bytes(size)} — too large to decode here; "
                                              f"hashed in chunks and left as stored."]
            kind, ext = sniff_content(_read(full, 16))
        else:
            raw = _read(full)
            md5, sha = _hashes(raw) if raw else ("", "")
            payload, kind, ext, steps = decode_payload(raw, key_info.get("key"), key_info.get("iv"))

        if payload is None:
            stats["failed"] += 1
            content, cmd5, csha = raw, md5, sha
        else:
            content = payload
            if payload is raw:
                cmd5, csha = md5, sha
            else:
                stats["decoded"] += 1
                cmd5, csha = _hashes(payload)

        category, cat_note = classify(rel, kind, ext)
        url = decode_cache_key_url(name)
        copy = {
            "path": full, "rel": rel, "name": name, "bytes": size,
            "raw_md5": md5, "raw_sha256": sha,
            "producer": producer_of(name),
            "mtime": ms_fmt(int(os.path.getmtime(full) * 1000)) if os.path.exists(full) else "",
            "src": device_path(full, src_root, manifest),
            # the exact name on the device, when extraction had to sanitise it
            "device_name": _device_name(full, rel, renamed),
        }
        key = csha or sha or f"{rel}:{size}"
        entry = by_content.get(key)
        if entry is None:
            entry = by_content[key] = {
                "sha256": csha, "md5": cmd5, "bytes": len(content) if content else size,
                "raw_bytes": size, "kind": kind, "ext": ext, "category": category,
                "cat_note": cat_note, "steps": steps, "copies": [],
                "name": name, "rel": rel, "url": url,
                "inner_url": inner_bolt_url(url),
                "decoded": payload is not None and payload is not raw,
                "recovered": payload is not None,
                "mvhd": read_mvhd(full) if ext in _ISOBMFF_EXTS else {},
                "tsaf": tsaf_fields(content) if kind == "tsaf" else [],
            "members": bundle_members(content) if kind == "bundle" else [],
                "links": [],
                "_content": content if (content and len(content) <= MAX_DECODE_BYTES) else None,
            }
        entry["copies"].append(copy)
        if url and not entry["url"]:
            entry["url"] = url
            entry["inner_url"] = inner_bolt_url(url)
    return list(by_content.values()), stats


def index_sccontent_by_size(app):
    """``{size: [paths]}`` for every SCContent file, from ``stat`` alone.

    The content-hash attribution only needs to hash SCContent files whose size matches a cache file
    it could not otherwise attribute, so the tree is never hashed wholesale.
    """
    full, parts = index_sccontent(app)
    by_size = {}
    for paths in list(full.values()) + [[p for _off, p in v] for v in parts.values()]:
        for path in paths:
            try:
                by_size.setdefault(os.path.getsize(path), []).append(path)
            except OSError:
                continue
    return by_size


def publish_entries(entries, files_dir):
    """Make every recovered plaintext media file openable from the report."""
    os.makedirs(files_dir, exist_ok=True)
    for entry in entries:
        if entry["kind"] != "media" or not entry["ext"]:
            continue
        base = (entry["sha256"] or "")[:16] or re.sub(r"[^0-9A-Za-z_.-]", "_", entry["name"])[:40]
        content = entry.pop("_content", None)
        if entry["decoded"]:
            # a decoded payload has no file of its own on disk, so it must be written out
            dst = os.path.join(files_dir, f"{base}.{entry['ext']}")
            try:
                if content is not None and not os.path.exists(dst):
                    with open(dst, "wb") as fh:
                        fh.write(content)
                entry["view"] = f"files/{base}.{entry['ext']}"
                entry["view_note"] = ("written out from the decoded/decrypted payload — a derived "
                                      "file, not a copy of the bytes on disk")
            except OSError as error:
                logger.debug(f"could not publish {base}: {error}")
        else:
            view, note = publish_view([entry["copies"][0]["path"]], files_dir, base, entry["ext"],
                                      entry["bytes"], 1024 * 1024 * 1024)
            entry["view"], entry["view_note"] = view, note
        entry["view_is_image"] = entry["ext"] in _IMAGE_EXTS
    for entry in entries:
        entry.pop("_content", None)


# --------------------------------------------------------------------------- HTML

# Same column ORDER as the cache_controller report (see CC_COLS) — toggle, category, what
# identifies the file, its context, then type / size / the file itself / links. The two reports
# describe the same kind of thing from two sides, and laying them out differently made every move
# between them a re-orientation. Widths differ where the content does (a path needs the room a
# CACHE_KEY does not).
CM_COLS = "24px 152px minmax(200px,1fr) 132px 74px 86px 74px 150px minmax(150px,260px)"
CM_ROW_H = 46


def _decrypted_elsewhere(entry):
    """The best file another report decrypted out of these bytes, or None.

    Only the caching-media packs are in this position: the Memories report holds the per-snap key,
    so the plaintext exists — under that report's ``media/`` folder — even though nothing here can
    produce it.
    """
    best = None
    for link in entry.get("links") or ():
        if link.get("kind") == "memory" and link.get("media_path"):
            if best is None or (link.get("media_bytes") or 0) > (best.get("media_bytes") or 0):
                best = link
    return best


def _file_cell(entry, rel_prefix="../"):
    if entry.get("view") and entry.get("view_is_image"):
        return (f'<a class="filebtn img" href="{_esc(entry["view"])}" target="_blank">'
                f'<img src="{_esc(entry["view"])}" loading="lazy">'
                f'<span class="lbl">{_esc(entry["ext"])}</span></a>')
    if entry.get("view") and entry.get("poster"):
        # the still is this tool's own frame, not device data — POSTER_BASIS says so on the row
        return (f'<a class="filebtn img vid" href="{_esc(entry["view"])}" target="_blank" '
                f'title="open the {_esc(entry["ext"])} (the still is a frame extracted by this '
                f'tool, not a cached file)">'
                f'<img src="{_esc(entry["poster"])}" loading="lazy">'
                f'<span class="lbl">▶ {_esc(entry["ext"])}</span></a>')
    if entry.get("view") and entry["ext"] not in PLAYABLE_EXTS:
        # recognised media this report cannot render inline (a HEIC/AVIF still): openable, and named
        # for what it is, but not dressed up as something that plays
        return (f'<a class="filebtn" href="{_esc(entry["view"])}" target="_blank">'
                f'{_esc(entry["ext"])}</a>')
    if entry.get("view"):
        return (f'<a class="filebtn play" href="{_esc(entry["view"])}" target="_blank">'
                f'▶ <span class="lbl">{_esc(entry["ext"])}</span></a>')
    if entry["kind"] == "empty":
        return '<span class="filenone">0 bytes</span>'
    # Bytes another report decrypted: show ITS copy. These rows said "decoded in the Memories
    # report" next to no image at all, which an examiner reads as a failure — while the plaintext
    # was sitting in the next report the whole time. The copy is clearly marked as that report's.
    dec = _decrypted_elsewhere(entry)
    if dec:
        url = f'{rel_prefix}Memories/{dec["media_path"]}'
        ext = (dec.get("media_ext") or "").lower()
        title = (f'decrypted by the Memories report from Memory {dec["snap_id"][:8]}… '
                 f'({dec.get("media_role") or "media"})')
        if ext in _IMAGE_EXTS:
            return (f'<a class="filebtn img dec" href="{_esc(url)}" target="scauto_memories" '
                    f'title="{_esc(title)}"><img src="{_esc(url)}" loading="lazy">'
                    f'<span class="lbl">🔓 {_esc(ext)}</span></a>')
        return (f'<a class="filebtn play dec" href="{_esc(url)}" target="scauto_memories" '
                f'title="{_esc(title)}">🔓 <span class="lbl">{_esc(ext or "media")}</span></a>')
    if not entry["recovered"]:
        # A padlock here used to mean three different things. Say which one this row is: the
        # caching-media packs are decrypted in full by the Memories report, so telling the examiner
        # they were "not recovered" was simply wrong.
        if entry["category"] == CAT_ELSEWHERE:
            owner = _OWNED_ELSEWHERE.get(entry["rel"].split("/")[0], ("another report",))[0]
            return (f'<span class="filenone">↗ decoded in the {_esc(owner)} report</span>')
        if entry["category"] == CAT_ASSET:
            return f'<span class="filenone">{_esc(entry["ext"] or entry["kind"])} app asset</span>'
        return '<span class="filenone">🔒 not recovered</span>'
    return f'<span class="filenone">{_esc(entry["ext"] or entry["kind"])}</span>'


MULTI_TARGET_BASIS = (
    "This file corresponds to SEVERAL rows in the linked report, so the link opens that report "
    "filtered to this file's identifiers with every matching row expanded, rather than jumping to "
    "one of them. What you land on is the complete set of matches — the search box shows the query "
    "that produced it, and clearing it restores the full report.")


def _links_cell(entry, rel_prefix, compact=True):
    chips = []
    # Cache entries first, as one chip: the same cached content is regularly claimed under several
    # CACHE_KEYs, and a chip each made the cell unreadable while a single anchor hid all but one.
    cache_keys, cache_basis = [], ""
    for link in entry["links"]:
        if link["kind"] == "cache" and link.get("key") not in cache_keys:
            cache_keys.append(link["key"])
            cache_basis = cache_basis or link["basis"]
    if len(cache_keys) == 1:
        href = f'{rel_prefix}CacheController/CacheController_report.html#ck-{cache_keys[0]}'
        chips.append(f'<a class="chip cc" href="{_esc(href)}" target="scauto_cache">cache</a>'
                     + _info(cache_basis))
    elif cache_keys:
        href = (f'{rel_prefix}CacheController/CacheController_report.html'
                + report_ui.find_fragment(cache_keys))
        chips.append(f'<a class="chip cc" href="{_esc(href)}" target="scauto_cache" '
                     f'title="open the cache_controller report filtered to this file\'s '
                     f'{len(cache_keys)} cache entries, all expanded">'
                     f'cache ({len(cache_keys)})</a>'
                     + _info(MULTI_TARGET_BASIS + " " + cache_basis))
    seen_mem = set()
    for link in entry["links"]:
        if link["kind"] == "memory":
            sid = link["snap_id"]
            if sid in seen_mem:
                continue
            seen_mem.add(sid)
            href = f'{rel_prefix}Memories/Memories_report.html#mem-{sid}'
            chips.append(f'<a class="chip mem" href="{_esc(href)}" target="scauto_memories">'
                         f'Memory {_esc(sid[:8])}… (index)</a>' + _info(link["basis"]))
            # ...and the Memory's own detail page, as the cache_controller report does: the index
            # row is a summary, the detail page is where that Memory's media and metadata are.
            if link.get("page"):
                chips.append(f'<a class="chip mem" target="scauto_memories" '
                             f'href="{_esc(rel_prefix)}Memories/{_esc(link["page"])}#mem-'
                             f'{_esc(sid)}">detail</a>')
        elif link["kind"] == "chat":
            rec = link["rec"]
            href = rel_prefix + (rec.get("href") or "")
            chips.append(f'<a class="chip chat" href="{_esc(href)}" target="scauto_convs">'
                         f'message {_esc(rec.get("server_message_id") or "")}</a>'
                         + _info(link["basis"]))
    return "".join(chips) or '<span class="muted">—</span>'


def _detail_html(entry, rel_prefix):
    parts = []
    warn = _root_uuid_warning(entry)
    if warn:
        parts.append(f'<div class="warnbox">⚠ The UUID in this filename is <b>not</b> a snap id'
                     f'{_info(warn)}</div>')
    if entry.get("cat_note"):
        parts.append(f'<div class="note-inline">{_esc(entry["cat_note"])}</div>')
    if entry.get("poster"):
        parts.append(f'<a href="{_esc(entry["view"])}" target="_blank">'
                     f'<img class="cacheview" src="{_esc(entry["poster"])}" loading="lazy"></a>'
                     f'<div class="muted">poster frame extracted by this tool from the video — a '
                     f'derived image, not a cached file{_info(POSTER_BASIS)}</div>')
    dec = _decrypted_elsewhere(entry)
    if dec:
        url = f'{rel_prefix}Memories/{dec["media_path"]}'
        if (dec.get("media_ext") or "").lower() in _IMAGE_EXTS:
            parts.append(f'<a href="{_esc(url)}" target="scauto_memories">'
                         f'<img class="cacheview" src="{_esc(url)}" loading="lazy"></a>')
        parts.append(f'<div class="note-inline">🔓 These bytes WERE recovered — the Memories report '
                     f'decrypted them from Memory {_esc(dec["snap_id"])} and holds the plaintext '
                     f'as <span class="mono">Memories/{_esc(dec["media_path"])}</span>. This report '
                     f'does not decode them a second time.{_info(dec["basis"])}</div>')

    grid = [("Recovered content SHA-256", entry["sha256"]),
            ("Recovered content MD5", entry["md5"]),
            ("Recovered size", _fmt_bytes(entry["bytes"])),
            ("Detected type", f'{entry["ext"] or entry["kind"]} ({entry["kind"]})')]
    if entry.get("url"):
        grid.append(("CDN URL (from the filename)", entry["url"]))
    if entry.get("inner_url"):
        grid.append(("CDN URL (base64 inside the bolt path)", entry["inner_url"]))
    parts.append("<div class='sect'>Recovered content</div><div class='grid'>"
                 + "".join(f"<div class='k'>{_esc(k)}</div><div class='v'>{_esc(v)}</div>"
                           for k, v in grid if v) + "</div>")

    if entry["steps"]:
        parts.append("<div class='sect'>How it was recovered" + _info(
            "Every step applied to the bytes on disk to get to the content above. Repeat these in "
            "order to reproduce the result independently.") + "</div><ol class='steps'>"
            + "".join(f"<li>{_esc(s)}</li>" for s in entry["steps"]) + "</ol>")

    if entry.get("mvhd"):
        m = entry["mvhd"]
        parts.append("<div class='sect'>MP4/MOV mvhd atom" + _info(MVHD_BASIS) + "</div>"
                     "<div class='grid'>"
                     f"<div class='k'>creation time</div><div class='v'>{_esc(m.get('created'))}</div>"
                     f"<div class='k'>modification time</div><div class='v'>{_esc(m.get('modified'))}</div>"
                     f"<div class='k'>duration</div><div class='v'>{_esc(m.get('duration_s'))} s</div>"
                     "</div>")

    if entry.get("tsaf"):
        parts.append("<div class='sect'>TSAF fields</div><div class='paths'>"
                     + "<br>".join(_esc(f) for f in entry["tsaf"]) + "</div>")

    if entry.get("members"):
        parts.append("<div class='sect'>Resource-bundle members" + _info(
            "The names inside this bundle after zstd decompression. Members are located by their "
            "names rather than by walking the bundle's structure, which is not documented here — "
            "so this is what the bundle contains, not a byte-exact extraction of each member. "
            "These are UI assets the app downloaded, not user media.") + "</div>"
            "<div class='paths'>" + "<br>".join(_esc(m) for m in entry["members"][:60])
            + (f"<br><span class='muted'>+{len(entry['members']) - 60} more</span>"
               if len(entry["members"]) > 60 else "") + "</div>")

    rows = []
    for c in entry["copies"]:
        shown = _esc(c["rel"])
        if c.get("device_name"):
            shown += (f"<div class='devname'>on the device: {_esc(c['device_name'])}"
                      f"{_info(RENAMED_BASIS)}</div>")
        rows.append(f"<tr><td class='mono'>{shown}</td><td>{_fmt_bytes(c['bytes'])}</td>"
                    f"<td>{_esc(c['producer']) or '<span class=muted>none</span>'}</td>"
                    f"<td class='hex'>{_esc(c['raw_sha256'][:32])}…</td>"
                    f"<td>{_esc(c['mtime'])}</td></tr>")
    parts.append("<div class='sect'>Copies on disk" + _info(
        "Every file under Library/Caches whose recovered content is these exact bytes. The same "
        "media is often written more than once under different names — at the Caches root and in "
        "Caches/tmp, for instance — so they are one row here with each copy listed. The SHA-256 in "
        "this table is of the file AS STORED, which differs from the recovered content's hash "
        "whenever the file had to be decoded or decrypted.") + "</div>"
        "<table class='sub'><tr><th>path under Library/Caches</th><th>size</th><th>producer</th>"
        "<th>SHA-256 as stored</th><th>modified</th></tr>" + "".join(rows) + "</table>")
    parts.append("<div class='sect'>Source path(s)</div><div class='paths'>"
                 + "<br>".join(_esc(c["src"]) for c in entry["copies"]) + "</div>")

    if entry.get("view_note"):
        parts.append(f"<div class='sect'>Published file</div><div class='note-inline'>"
                     f"{_esc(entry['view_note'])}</div>")

    links = _links_cell(entry, rel_prefix, compact=False)
    parts.append("<div class='sect'>Links</div><div class='chips'>" + links + "</div>")
    return "".join(parts)


def _documents_html(docs):
    """The parsed-document sections (cronet, NSURLCache, KSCrash), or a note that there are none."""
    out = []
    for doc in docs:
        out.append(f"<h2>{_esc(doc['title'])}</h2>")
        out.append(f"<div class='note'>{_esc(doc['source'])}{_info(doc['basis'])}</div>")
        if doc.get("empty_note"):
            out.append(f"<div class='note-inline' style='margin:8px 24px'>"
                       f"{_esc(doc['empty_note'])}</div>")
        if doc.get("table"):
            head = "".join(f"<th>{_esc(h)}</th>" for h in doc["columns"])
            body = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>"
                           for row in doc["table"])
            out.append(f"<table class='vtab'><tr>{head}</tr>{body}</table>")
    return "".join(out)


def generate_report(entries, docs, outdir, tz_label, rel_prefix, key_info, stats, app_display,
                    run_id="default"):
    total = len(entries)
    media = sum(1 for e in entries if e["category"] == CAT_MEDIA)
    decoded = sum(1 for e in entries if e["decoded"])
    linked = sum(1 for e in entries if e["links"])
    # "Not recovered" used to lump three unrelated things together, and the headline number was
    # dominated by the one that is not a failure at all: the caching-media packs, which this report
    # deliberately does not decode because the Memories report already decrypts every one of them.
    # On one test device that read "227 not recovered" when 188 were those packs and the real
    # figure was 4. They are counted apart now, and so are the app assets nobody needs decoded.
    elsewhere = sum(1 for e in entries if not e["recovered"] and e["category"] == CAT_ELSEWHERE)
    assets = sum(1 for e in entries if not e["recovered"] and e["category"] == CAT_ASSET)
    unrecovered = sum(1 for e in entries if not e["recovered"]
                      and e["category"] not in (CAT_ELSEWHERE, CAT_ASSET))
    categories = sorted({e["category"] for e in entries})
    locations = sorted({e["rel"].split("/")[0] if "/" in e["rel"] else "(root)" for e in entries})

    data_dir = os.path.join(outdir, "data")
    details = [(f"cm-{e['sha256'] or e['rel']}", _detail_html(e, rel_prefix)) for e in entries]
    chunk_of = report_ui.write_details(data_dir, details)

    rows = []
    for e in entries:
        anchor = f"cm-{e['sha256'] or e['rel']}"
        loc = e["rel"].split("/")[0] if "/" in e["rel"] else "(root)"
        cells = [
            "▸",
            _esc(e["category"]),
            _esc(e["rel"]),
            _esc(e["copies"][0]["producer"]) or '<span class="muted">none</span>',
            f'{len(e["copies"])} cop{"y" if len(e["copies"]) == 1 else "ies"}',
            _esc(e["ext"] or e["kind"]),
            _fmt_bytes(e["bytes"]),
            _file_cell(e, rel_prefix),
            _links_cell(e, rel_prefix),
        ]
        searchable = [e["rel"], e["name"], e["category"], e["ext"], e["kind"], e["sha256"],
                      e["md5"], e.get("url") or "", e.get("inner_url") or "",
                      e["copies"][0]["producer"]]
        for c in e["copies"]:
            searchable += [c["rel"], c["raw_sha256"], c["raw_md5"]]
        for link in e["links"]:
            searchable += [link.get("key", ""), link.get("snap_id", ""),
                           (link.get("rec") or {}).get("conversation_id", "")]
        rows.append([
            anchor, cells, " ".join(s for s in searchable if s).lower(),
            {"1": e["category"], "2": e["rel"], "4": len(e["copies"]),
             "5": e["ext"] or e["kind"], "6": e["bytes"]},
            chunk_of.get(anchor),
            {"cat": e["category"], "loc": loc,
             "rec": "y" if e["recovered"] else "n",
             "link": "y" if e["links"] else "n"},
        ])
    report_ui.write_rows(data_dir, rows)

    cat_opts = "".join(f"<option value='{_esc(c)}'>{_esc(c)}</option>" for c in categories)
    loc_opts = "".join(f"<option value='{_esc(c)}'>{_esc(c)}</option>" for c in locations)
    key_line = (f"Story-cache key: {html.escape(key_info.get('note') or 'not looked for')}"
                + (_info(CLIENT_KEY_BASIS) if key_info.get("key") else ""))

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Snapchat Library/Caches media</title><style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f4f8;color:#1b1b1f}}
 header{{background:#2d2d71;color:#fff;padding:16px 24px}} header h1{{margin:0;font-size:20px}}
 .sum{{opacity:.85;font-size:13px;margin-top:4px}} .sum b{{color:#fff}}
 .note{{background:#fff8e0;border:1px solid #e6d48a;color:#6a5300;padding:8px 24px;font-size:12.5px}}
 .note-inline{{background:#fff8e0;border:1px solid #e6d48a;color:#6a5300;padding:6px 10px;
   border-radius:5px;font-size:12px;margin:6px 0}}
 .warnbox{{background:#ffe9e0;border:1px solid #e8bfae;color:#8a3a1c;padding:8px 11px;
   border-radius:5px;font-size:12.5px;margin-bottom:6px;font-weight:600}}
 .toolbar{{background:#ececf4;border-bottom:1px solid #d7d7e2;padding:10px 24px;
   display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:13px}}
 .toolbar input,.toolbar select{{font-size:13px;padding:5px 8px;border:1px solid #bcbcd0;border-radius:5px}}
 .toolbar input[type=search]{{min-width:280px}} .toolbar label{{color:#555;font-weight:600}}
 .mono{{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}}
 .muted{{color:#999}}
 .vcells>.vc.c0{{color:#2d2d71;font-weight:700;text-align:center;padding-left:4px;padding-right:4px}}
 .vr.open .vc.c0{{color:#8a1f5a}}
 .vcells>.vc.c1{{line-height:15px}}
 .vcells>.vc.c2{{font-family:ui-monospace,Consolas,monospace;font-size:11px;overflow-wrap:anywhere}}
 .vcells>.vc.c4,.vcells>.vc.c6{{text-align:right;color:#555}}
 .filebtn{{display:inline-flex;align-items:center;gap:5px;text-decoration:none;font-weight:700;
   font-size:11px;color:#25348a;background:#e7ecff;border:1px solid #b9c3f0;border-radius:6px;padding:2px 7px}}
 .filebtn img{{width:34px;height:34px;object-fit:cover;border-radius:4px;display:block}}
 .filebtn.img{{padding:2px;gap:4px}} .filenone{{color:#999;font-size:11px}}
 /* a copy another report decrypted, so it reads as recovered rather than as a failure */
 .filebtn.dec{{background:#e7f6ea;border-color:#b3ddc0;color:#1f6b39}}
 .filebtn.dec:hover{{background:#d5efdb}}
 img.cacheview{{max-width:220px;max-height:300px;border-radius:5px;
   box-shadow:0 1px 4px rgba(0,0,0,.25);margin-top:6px}}
 .sect{{margin-top:12px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#2d2d71;
   font-weight:700;border-bottom:1px solid #e2e2ee;padding-bottom:2px}}
 .grid{{display:grid;grid-template-columns:auto 1fr;gap:2px 14px;font-size:12px;margin-top:4px;max-width:900px}}
 .grid .k{{color:#666}} .grid .v{{overflow-wrap:anywhere}}
 ol.steps{{font-size:12px;margin:6px 0 0 18px;padding:0}} ol.steps li{{margin:2px 0}}
 table.sub{{border-collapse:collapse;margin-top:5px;font-size:11.5px}}
 table.sub th{{background:#e7e7f2;color:#2d2d71;text-align:left;padding:3px 8px}}
 table.sub td{{border:1px solid #e0e0e8;padding:3px 8px;overflow-wrap:anywhere}}
 table.sub td.hex{{font-family:ui-monospace,Consolas,monospace;font-size:10px;color:#7a1f5a}}
 .paths{{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#555;margin-top:4px;overflow-wrap:anywhere}}
 .devname{{color:#8a5a00;font-size:10.5px;margin-top:2px;overflow-wrap:anywhere}}
 .chips{{margin-top:4px}} .chip{{display:inline-block;margin:2px 6px 2px 0;padding:2px 8px;border-radius:10px;
   font-size:11px;text-decoration:none;font-weight:600}}
 .chip.cc{{background:#e7ecff;color:#25348a;border:1px solid #b9c3f0}}
 .chip.mem{{background:#f3e8f2;color:#8a1f5a;border:1px solid #e0c2d8}}
 .chip.chat{{background:#e7f6ea;color:#1f6b39;border:1px solid #b3ddc0}}
 h2{{margin:24px 0 0;padding:10px 24px;background:#1f1f52;color:#fff;font-size:15px}}
 table.vtab{{border-collapse:collapse;width:100%;font-size:12px}}
 table.vtab td{{border-bottom:1px solid #e2e2ea;padding:5px 24px}}
 table.vtab th{{background:#1f1f52;color:#fff;text-align:left;padding:6px 24px}}
{report_ui.HINT_CSS}{report_ui.VTABLE_CSS}{report_ui.NAV_CSS}{report_ui.SELECT_CSS}
 .vcells>.vc{{font-size:12.5px}}
</style>
<script>window.SCAUTO_RUN={json.dumps(run_id)};window.SCAUTO_SELKIND="cm";</script>
<script>{report_ui.SELECT_JS}</script>
<script src="{rel_prefix}selection.js"></script>
<script>{report_ui.VTABLE_JS}</script></head><body>
<header><h1>Snapchat cached media &amp; documents — Library/Caches</h1>
 <div class="sum">{total} distinct file(s) recovered from {stats['files']} file(s)
 ({_fmt_bytes(stats['bytes'])}) &middot; <b>{media}</b> evidentiary media &middot;
 <b>{decoded}</b> decoded or decrypted &middot; <b>{linked}</b> linked to another report &middot;
 times in <b>{html.escape(tz_label)}</b></div>
 <div class="sum"><b>{unrecovered}</b> file(s) not recovered{_info(UNRECOVERED_BASIS)} &middot;
 {elsewhere} left to the report that owns them (decoded there, not here) &middot;
 {assets} app assets not decoded</div>
 <div class="sum">{key_line}</div>
 <div class="sum">Scope: {html.escape(SCOPE_NOTE)}</div>
 <div class="sum">Source: {html.escape(app_display)}</div></header>
{report_ui.missing_data_banner('CacheMedia_report.html')}
<div class="stickytop">
<div class="toolbar">
 <input type="search" id="q" placeholder="Search path, filename, hash, URL, snap id…" oninput="flt()">
 <label>Category <select id="cat" onchange="flt()"><option value="">all</option>{cat_opts}</select></label>
 <label>Location <select id="loc" onchange="flt()"><option value="">all</option>{loc_opts}</select></label>
 <label>Recovered <select id="rec" onchange="flt()"><option value="">any</option>
   <option value="y">recovered</option><option value="n">not recovered</option></select></label>
 <label>Linked <select id="link" onchange="flt()"><option value="">any</option>
   <option value="y">linked</option><option value="n">not linked</option></select></label>
 <label title="App fonts, lens models and shader caches are hidden unless this is ticked">
   <input type="checkbox" id="assets" onchange="flt()"> show app assets</label>
 <span id="count" style="color:#555"></span>
</div>
<div class="toolbar">{report_ui.selection_toolbar('file')}</div>
<div class="pager" id="pager"></div>
<div class="vhdr" id="vhdr" style="grid-template-columns:30px {CM_COLS}">
 <div class="vc sel"><input type="checkbox" class="selall" onclick="SCV.selectShown(this.checked)"></div>
 <div class="vc nosort"></div>
 <div class="vc" onclick="SCV.setSort(1)">Category <span class="ar">↕</span></div>
 <div class="vc" onclick="SCV.setSort(2)">Path under Library/Caches <span class="ar">↕</span></div>
 <div class="vc nosort">Producer</div>
 <div class="vc" onclick="SCV.setSort(4)">Copies <span class="ar">↕</span></div>
 <div class="vc" onclick="SCV.setSort(5)">Type <span class="ar">↕</span></div>
 <div class="vc" onclick="SCV.setSort(6)">Size <span class="ar">↕</span></div>
 <div class="vc nosort">File</div>
 <div class="vc nosort">Links</div>
</div>
</div>
<div class="vwrap" id="vwrap"><div class="vpad" id="vpad"></div><div class="vwin" id="vwin"></div></div>
<div class="vempty" id="vempty" style="display:none">No file matches the current filters.</div>
{_documents_html(docs)}
<script src="data/index.js"></script>
<script>
{report_ui.HINT_JS}
{report_ui.NAV_JS}
{report_ui.SELECT_TOOLBAR_JS}
var flt_t=0;
function flt(){{clearTimeout(flt_t);flt_t=setTimeout(function(){{SCV.refilter();}},120);}}
SCV.init({{
 mount:'vwrap',win:'vwin',pad:'vpad',header:'#vhdr',missing:'vmiss',empty:'vempty',
 pager:'pager',pageSize:500,selKind:'cm',
 rowHeight:{CM_ROW_H},estDetail:320,cols:'{CM_COLS}',detailBase:'data/detail-',
 query:function(){{return document.getElementById('q').value;}},
 match:function(m,r){{
  var cat=document.getElementById('cat').value,loc=document.getElementById('loc').value,
      rec=document.getElementById('rec').value,lk=document.getElementById('link').value,
      assets=document.getElementById('assets').checked;
  return (!cat||m.cat===cat)&&(!loc||m.loc===loc)&&(!rec||m.rec===rec)&&(!lk||m.link===lk)
       &&(assets||m.cat!=={json.dumps(CAT_ASSET)})
       &&(!document.getElementById('selonly').checked||SCSel.get('cm',r[0]));}},
 selectedOnly:function(){{return document.getElementById('selonly').checked;}},
 selCount:function(n){{document.getElementById('selcount').textContent=n+' selected';scSelNote();}},
 count:function(n,t){{document.getElementById('count').textContent=
   n===t?(n+' files'):(n+' of '+t+' shown');}},
 reset:function(){{
  document.getElementById('q').value='';document.getElementById('cat').value='';
  document.getElementById('loc').value='';document.getElementById('rec').value='';
  document.getElementById('link').value='';
  /* reset means "stop hiding anything", because this runs when a link from another report has to
     reach a row. App assets are hidden by default, so clearing this box (rather than ticking it)
     left every link to an app-asset row — an icon or a lens resource the cache_controller report
     matched — landing on nothing at all. */
  document.getElementById('assets').checked=true;
  document.getElementById('selonly').checked=false;}}
}});
scSelNote();
scConsumeHash();
</script>
</body></html>"""

    os.makedirs(outdir, exist_ok=True)
    report = os.path.join(outdir, "CacheMedia_report.html")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return report, {"total": total, "media": media, "decoded": decoded, "linked": linked,
                    "unrecovered": unrecovered, "elsewhere": elsewhere, "assets": assets}


def collect_documents(app, ms_fmt, src_root=None, manifest=None):
    """Parse the non-media records under Library/Caches into report sections."""
    docs = []
    caches = os.path.join(app, "Library", "Caches")

    for path in sorted(glob.glob(os.path.join(app, "**", "cronet", "prefs", "local_prefs.json"),
                                 recursive=True)):
        rows = parse_cronet_prefs(path)
        docs.append({
            "title": "Cronet DNS host cache", "basis": CRONET_BASIS,
            "source": device_path(path, src_root, manifest),
            "columns": ["hostname", "resolved addresses", "expires (UTC)", "secure"],
            "table": [[r["hostname"], r["addresses"], r["expiration"], r["secure"]] for r in rows],
            "empty_note": "" if rows else "present, but it holds no host-cache entries",
        })

    for cronet_cache in sorted(glob.glob(os.path.join(app, "**", "cronet", "disk_cache"),
                                         recursive=True)):
        if not os.path.isdir(cronet_cache):
            continue
        entries = parse_blockfile_entries(cronet_cache)
        found = scan_cronet_cache(cronet_cache)
        joined = {e["url"] for e in entries}
        rows = [[e["url"], e["body_file"] or "(inline / not a separate file)",
                 _fmt_bytes(e["size"])] for e in entries]
        # URLs the raw scan saw but no EntryStore accounted for: listed, but explicitly without a
        # body, so a link that was never established is never implied
        rows += [[u, "(not joined — recovered by scanning the block files)", ""]
                 for u in found["urls"] if u not in joined][:2000]
        docs.append({
            "title": "Cronet HTTP cache", "basis": CRONET_CACHE_BASIS,
            "source": device_path(cronet_cache, src_root, manifest),
            "columns": ["cached request URL", "response body file", "size"],
            "table": rows[:2000],
            "empty_note": ("" if rows else
                           "no cached entries could be recovered from the block files"),
        })

    for path in sorted(glob.glob(os.path.join(caches, "com.toyopagroup.picaboo", "Cache.db"))):
        rows, note = parse_nsurlcache(path)
        docs.append({
            "title": "NSURLCache (Cache.db)", "basis": NSURLCACHE_BASIS,
            "source": device_path(path, src_root, manifest),
            "columns": ["entry_ID", "request_key", "time_stamp", "read from"],
            "table": [[r.get("entry_ID"), r.get("request_key"), r.get("time_stamp"), r.get("_wal")]
                      for r in rows[:2000]],
            "empty_note": note if not rows else "",
        })

    for path in sorted(glob.glob(os.path.join(caches, "KSCrash", "*", "Data", "CrashState.json"))):
        pairs = parse_crashstate(path)
        docs.append({
            "title": "KSCrash session state", "basis":
                "KSCrash is the crash reporter Snapchat embeds. CrashState.json is its running "
                "tally of app launches, sessions and time active since the last crash — an "
                "app-usage record independent of any Snapchat database. The Reports/ folder "
                "beside it holds full crash reports when there are any.",
            "source": device_path(path, src_root, manifest),
            "columns": ["field", "value"],
            "table": [[k, v] for k, v in pairs],
            "empty_note": "" if pairs else "present but empty",
        })
    return docs


# --------------------------------------------------------------------------- entry

def main(app_or_root, outdir=None, tz="local", src_root=None, report_dir=None):
    """Build the Library/Caches media + documents report.

    app_or_root : Snapchat app-container path, or any extraction root containing it.
    outdir      : output directory (default: ./Snapchat_CacheMedia_report_<timestamp>).
    tz          : timezone for displayed timestamps.
    src_root    : extraction root, for archive-relative source paths.
    report_dir  : the sibling reports root (…/Reports), for cross-report links.
    """
    app = find_app_container(app_or_root)
    if not os.path.isdir(os.path.join(app, "Library", "Caches")):
        logger.warning(f"No Library/Caches under {app} — nothing for the cached-media report. "
                       f"If the extraction ZIP was unpacked by an older version of this tool, "
                       f"re-extract it: Library/Caches was not being pulled out before.")
        return None

    manifest = load_path_manifest(src_root, app_or_root, app)
    outdir = outdir or ("./Snapchat_CacheMedia_report_"
                        + datetime.now().strftime("%Y%m%d_%H%M%S"))
    ms_fmt, tz_label = _ms_formatter(tz)
    rdir = report_dir or os.path.dirname(os.path.abspath(outdir))

    key_info = read_client_encryption(app)
    logger.info(f"Cached media: {key_info['note']}")

    renamed = load_renamed(src_root, app)
    entries, stats = build_entries(app, key_info, ms_fmt, src_root, manifest, renamed)
    logger.info(f"Cached media: {stats['files']} file(s) under Library/Caches "
                f"({_fmt_bytes(stats['bytes'])}) → {len(entries)} distinct file(s), "
                f"{stats['decoded']} decoded/decrypted")

    claims_by_uuid, claims_by_triple, _keys = load_claims(app)
    sc_by_size = index_sccontent_by_size(app)
    mem_index = load_memory_index(app)
    memory_pages = load_memory_pages(rdir)
    memory_packs = load_memory_packs(rdir)
    chat_by_key, chat_by_message = load_chat_links(rdir)
    for entry in entries:
        entry["links"] = attribute(entry, claims_by_uuid, claims_by_triple, sc_by_size,
                                   mem_index, memory_pages, chat_by_key, chat_by_message,
                                   packs=memory_packs)

    publish_entries(entries, os.path.join(outdir, "files"))
    posters, no_poster = publish_posters(entries, os.path.join(outdir, "files"))
    if posters or no_poster:
        logger.info(f"Cached media: {posters} poster frame(s) extracted from cached video "
                    f"(derived thumbnails, labelled as such in the report)"
                    + (f"; {no_poster} could not be decoded and are listed without one"
                       if no_poster else ""))
    entries.sort(key=lambda e: (e["category"], e["rel"]))
    docs = collect_documents(app, ms_fmt, src_root, manifest)

    report_ui.write_selection_stub(rdir, report_ui.run_id(rdir))
    report, done = generate_report(entries, docs, outdir, tz_label, "../", key_info, stats,
                                   device_path(app, src_root, manifest),
                                   report_ui.run_id(rdir))
    _write_manifest(entries, outdir)
    logger.info(f"Cached media report: {os.path.abspath(report)}")
    logger.info(f"  {done['total']} distinct file(s), {done['media']} evidentiary media, "
                f"{done['linked']} linked to another report")
    logger.info(f"  {done['unrecovered']} not recovered; excluded from that count: "
                f"{done['elsewhere']} decoded by the report that owns them (caching-media packs, "
                f"SCPersistentMedia) and {done['assets']} app assets")
    return report


def _ms_formatter(tz):
    """(fmt, label) where fmt(unix_ms) -> a localized time string."""
    cocoa_fmt, label = make_time_formatter(tz)
    cocoa_epoch = 978307200

    def fmt(ms):
        if not ms:
            return ""
        try:
            return cocoa_fmt(float(ms) / 1000.0 - cocoa_epoch)
        except Exception:
            return ""
    return fmt, label


def _write_manifest(entries, outdir):
    """``by_cache_key.json``: the cache_controller report's back-link into this one."""
    out = {}
    for entry in entries:
        for link in entry["links"]:
            if link["kind"] != "cache":
                continue
            out.setdefault(str(link["key"]).lower(), []).append({
                "sha256": entry["sha256"], "rel": entry["rel"],
                "producer": entry["copies"][0]["producer"],
                "anchor": f"cm-{entry['sha256'] or entry['rel']}",
                "basis": link["basis"],
            })
    try:
        with open(os.path.join(outdir, "by_cache_key.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh)
    except OSError as error:
        logger.debug(f"could not write by_cache_key.json: {error}")
    return out


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
        print("usage: python -m scripts.cache_media_report "
              "<extraction_root_or_app_container> [outdir] [--tz local|utc|<IANA>|<±HH:MM>]")
        sys.exit(1)
    main(args[0], args[1] if len(args) > 1 else None, tz=tz)
