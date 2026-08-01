"""
Snapchat iOS Memories media report.

Recovers Snapchat *Memories* media from an iOS extraction and links every media file
back to its Memory row in ``scdb-27.sqlite3``, including geolocation. Handles both
storage schemas, multiple user profiles, the ``SCContent`` cache and the
``caching-media`` ``.pack`` cache.

See ``docs/snapchat_ios_memories_decryption.md`` for the full reverse-engineering write-up.

Storage schemas
---------------
* **new** - per-snap AES key/IV are stored in ``ZGALLERYSNAP.ZENCRYPTION`` (plaintext for
  regular memories); no keychain needed for the imagery.
* **old** - keys live in the SQLCipher ``gallery.encrypteddb`` (``snap_key_iv``); the
  ``egocipher`` keychain key is required.

Geolocation (``snap_location_table``) always lives in ``gallery.encrypteddb`` and therefore
always needs the keychain. My Eyes Only memories additionally need ``persistedkey``.
"""

import os
import re
import sys
import glob
import html
import json
import time
import shutil
import hashlib
import sqlite3
import logging
import subprocess
import contextlib
from io import BytesIO
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from binascii import hexlify, unhexlify

try:
    from zoneinfo import ZoneInfo
except Exception:                                          # pragma: no cover
    ZoneInfo = None

from Crypto.Cipher import AES
from PIL import Image

from scripts.data import ccl_bplist
from scripts import DecryptLocalMemories_iOS as _memkeys  # reuse readKeychain
from scripts import report_ui
from scripts import offline_maps

logger = logging.getLogger(__name__)

# 8-byte header that prefixes decrypted caching-media payloads: 01 00 00 00 + uint32-LE length
PACK_HEADER_MARKER = b"\x01\x00\x00\x00"
PACK_RE = re.compile(r"([0-9a-f]{64})-(\d+)\.pack$")


# --------------------------------------------------------------------------- helpers

def _resolve_sqlcipher_module():
    """Return a DB-API compatible SQLCipher module (must expose connect), or None.

    Any of sqlcipher3 / sqlcipher3.dbapi2 / pysqlcipher3.dbapi2 will do, so an install or
    a frozen build can ship whichever wheel is available for its platform.
    """
    try:                                                   # preferred when available
        import sqlcipher3 as candidate                     # type: ignore
    except ImportError:
        candidate = None

    if candidate is not None and not hasattr(candidate, "connect"):
        try:                                               # some distributions expose dbapi2
            from sqlcipher3 import dbapi2 as candidate     # type: ignore
        except Exception:
            candidate = None

    if candidate is None:
        try:
            from pysqlcipher3 import dbapi2 as candidate   # type: ignore
        except ImportError:
            candidate = None

    if candidate is not None and not hasattr(candidate, "connect"):
        return None
    return candidate


_SQLCIPHER = _resolve_sqlcipher_module()


def _sqlcipher_exe():
    """Locate a sqlcipher CLI, or None if there isn't one.

    Note: Nuitka's --include-data-dir drops .exe files (they are in its
    default_ignored_suffixes), so a onefile build must include sqlcipher3.exe with an
    explicit --include-data-files, or rely on the module route above.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)               # PyInstaller
    if meipass:
        candidates += [os.path.join(meipass, "scripts", "data", "sqlcipher3.exe"),
                       os.path.join(meipass, "data", "sqlcipher3.exe")]
    here = os.path.dirname(os.path.abspath(__file__))      # source tree / Nuitka bundle
    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))  # beside the built binary
    candidates += [os.path.join(here, "data", "sqlcipher3.exe"),
                   os.path.join(exe_dir, "scripts", "data", "sqlcipher3.exe"),
                   os.path.join(exe_dir, "data", "sqlcipher3.exe")]
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    for name in ("sqlcipher3", "sqlcipher"):               # anything on PATH
        found = shutil.which(name)
        if found:
            return found
    return None


def cocoa_to_dt(ts):
    """Apple Cocoa Core Data timestamp -> aware UTC datetime (or None)."""
    try:
        if ts in (None, "", 0):
            return None
        return datetime.fromtimestamp(float(ts) + 978307200, tz=timezone.utc)
    except Exception:
        return None


def _parse_offset(spec):
    """Parse a fixed UTC offset like '-04:00', '+0530', '-4' -> tzinfo, else None."""
    m = re.fullmatch(r"\s*([+-])(\d{1,2})(?::?(\d{2}))?\s*", spec)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))


def make_time_formatter(tz_spec):
    """
    Return (fmt, label) where fmt(cocoa_ts) -> localized 'YYYY-MM-DD HH:MM:SS <tz>' string.

    tz_spec: 'local' (examiner machine, default), 'utc', an IANA name ('America/Toronto',
    DST-aware), or a fixed offset ('-04:00'). Named zones handle daylight saving per-date.
    """
    spec = (tz_spec or "local").strip()
    low = spec.lower()
    if low == "utc":
        target, label = timezone.utc, "UTC"
    elif low in ("", "local"):
        target, label = None, "Local time (examiner machine)"
    elif ZoneInfo is not None and "/" in spec:
        try:
            target, label = ZoneInfo(spec), spec
        except Exception:
            logger.warning(f"Unknown timezone {spec!r}; falling back to UTC")
            target, label = timezone.utc, "UTC"
    else:
        off = _parse_offset(spec)
        if off is not None:
            target, label = off, "UTC" + spec if spec[0] in "+-" else spec
        elif ZoneInfo is not None:
            try:
                target, label = ZoneInfo(spec), spec
            except Exception:
                logger.warning(f"Unknown timezone {spec!r}; falling back to UTC")
                target, label = timezone.utc, "UTC"
        else:
            target, label = timezone.utc, "UTC"

    def fmt(ts):
        dt = cocoa_to_dt(ts)
        if dt is None:
            return ""
        local = dt.astimezone() if target is None else dt.astimezone(target)
        base = local.strftime("%Y-%m-%d %H:%M:%S")
        if target is timezone.utc:
            return base + " UTC"
        off = local.strftime("%z")                          # e.g. -0400
        off_fmt = f"UTC{off[:3]}:{off[3:]}" if off else ""
        abbr = local.strftime("%Z")                         # e.g. EDT (or verbose on Windows)
        if abbr and len(abbr) <= 5 and abbr[0] not in "+-":
            return f"{base} {abbr} ({off_fmt})" if off_fmt else f"{base} {abbr}"
        return f"{base} {off_fmt}" if off_fmt else base

    return fmt, label


def url_token(url):
    """Last path segment of a CDN URL (the cache token), or None."""
    if not url:
        return None
    seg = urlparse(url).path.rstrip("/").split("/")[-1]
    return seg or None


def guess_media(data):
    """Return a file extension for known media magic bytes, else None."""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[4:8] == b"ftyp":
        return "mp4"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _aes_cbc(key, iv, data):
    n = len(data) - (len(data) % 16)
    return AES.new(key, AES.MODE_CBC, iv).decrypt(data[:n])


def _has_pkcs7(data):
    """True when data ends in valid PKCS#7 padding.

    Used as a **completeness test**: a fully cached SCContent file is CBC + PKCS#7, so its last
    plaintext block always ends in padding. Random plaintext (what a truncated file's final block
    decrypts to) only looks like valid padding about 1 time in 255.
    """
    if not data:
        return False
    n = data[-1]
    return 1 <= n <= 16 and len(data) >= n and data[-n:] == bytes([n]) * n


def _strip_pkcs7(data):
    """Remove PKCS#7 padding if present (SCContent media is CBC + PKCS#7)."""
    return data[:-data[-1]] if _has_pkcs7(data) else data


def decrypt_sccontent(raw, key, iv):
    """Decrypt an SCContent file. Returns (padded, stripped, ext, tail_ok) or a 4-None tuple.

    SCContent media is AES-256-CBC with PKCS#7 padding. ``padded`` is the raw CBC output (as some
    older decryptors emit it); ``stripped`` has the padding removed so it is byte-exact and its
    MD5/SHA-256 match current tools. They are equal when the file was already plaintext.

    ``tail_ok`` reports whether the **end** of the media is present: True when the decrypted bytes
    carry valid PKCS#7 padding, False when they do not (the cache holds only part of the file),
    and None when the file was already plaintext, where padding says nothing either way.

    A ciphertext whose length is not a multiple of the AES block size is a partial cache, not a
    dead loss: the block-aligned prefix still decrypts, so we recover it and report ``tail_ok``
    False rather than discarding the whole file.
    """
    ext = guess_media(raw[:16])
    if ext:
        return raw, raw, ext, None                        # already plaintext, no padding
    if not key or len(key) != 32 or len(iv) != 16 or len(raw) < 16:
        return None, None, None, None
    plain = _aes_cbc(key, iv, raw)
    aligned = len(raw) % 16 == 0
    if guess_media(plain[:16]):
        return plain, _strip_pkcs7(plain), guess_media(plain[:16]), aligned and _has_pkcs7(plain)
    if guess_media(plain[8:24]):                           # some have an 8-byte prefix
        body = plain[8:]
        return body, _strip_pkcs7(body), guess_media(plain[8:24]), aligned and _has_pkcs7(body)
    return None, None, None, None


def decrypt_pack(cipher, key, iv):
    """Decrypt a concatenated caching-media pack. Returns (bytes, ext, declared_len) or 3×None.

    ``declared_len`` is the payload length the pack header states. Comparing it with the payload
    actually returned is how a partially cached pack (missing `-<n>.pack` chunks) is detected; it
    is None for the header-less fallback shapes, where nothing declares the true length.
    """
    if not key or len(key) != 32 or len(iv) != 16:
        return None, None, None
    plain = _aes_cbc(key, iv, cipher)
    if plain[:4] == PACK_HEADER_MARKER:
        length = int.from_bytes(plain[4:8], "little")
        payload = plain[8:8 + length]
        ext = guess_media(payload[:16])
        if ext:
            return payload, ext, length
    # fallbacks (older/variant containers)
    ext = guess_media(plain[:16])
    if ext:
        return plain, ext, None
    ext = guess_media(plain[8:24])
    if ext:
        return plain[8:], ext, None
    return None, None, None


# Every branch of decrypt_pack decides purely on the first 24 plaintext bytes (magic bytes at
# offset 0, or after the 8-byte header). CBC decrypts a prefix independently of the rest, so
# decrypting two blocks answers "does this key own this pack?" exactly as the full decrypt would —
# at 1/1000th the work when the answer is no, which it is for all but one of the candidate keys.
_PACK_PROBE_BYTES = 32


def pack_matches(head, key, iv):
    """True when key/iv decrypt this pack's first bytes to something decrypt_pack would accept."""
    if not key or len(key) != 32 or len(iv) != 16 or len(head) < 16:
        return False
    plain = _aes_cbc(key, iv, head[:_PACK_PROBE_BYTES])
    if plain[:4] == PACK_HEADER_MARKER and guess_media(plain[8:24]):
        return True
    return bool(guess_media(plain[:16]) or guess_media(plain[8:24]))


# --------------------------------------------------------------------------- keychain

def unwrap_meo_key(persisted, enc_key, enc_iv):
    """Unwrap a My Eyes Only key/iv using the keychain persistedkey. Returns (key, iv)."""
    with open("temp_meo.plist", "wb") as f:
        f.write(persisted if isinstance(persisted, bytes) else unhexlify(persisted))
    try:
        with open("temp_meo.plist", "rb") as f:
            obj = ccl_bplist.deserialise_NsKeyedArchiver(ccl_bplist.load(f))
    finally:
        if os.path.exists("temp_meo.plist"):
            os.remove("temp_meo.plist")
    meo_key, meo_iv = obj["masterKey"], obj["initializationVector"]
    dec_key = unhexlify(hexlify(AES.new(meo_key, AES.MODE_CBC, meo_iv).decrypt(enc_key))[:64])
    dec_iv = unhexlify(hexlify(AES.new(meo_key, AES.MODE_CBC, meo_iv).decrypt(enc_iv))[:32])
    return dec_key, dec_iv


# --------------------------------------------------------------------------- discovery

def find_app_container(root):
    """Return the Snapchat app-container path under an extraction root (or root itself)."""
    if glob.glob(os.path.join(root, "Documents", "gallery_data_object")):
        return root
    hits = glob.glob(os.path.join(root, "**", "Documents", "gallery_data_object"), recursive=True)
    if hits:
        return os.path.dirname(os.path.dirname(hits[0]))
    return root


def find_profiles(app):
    """Yield dicts describing each user profile: userHash, scdb, gallery."""
    base = os.path.join(app, "Documents")
    scdbs = glob.glob(os.path.join(base, "gallery_data_object", "*", "*", "scdb-27.sqlite3"))
    profiles = []
    for scdb in scdbs:
        uh = os.path.basename(os.path.dirname(scdb))
        gallery = glob.glob(os.path.join(base, "gallery_encrypted_db", "*", uh, "gallery.encrypteddb"))
        profiles.append({"userHash": uh, "scdb": scdb,
                         "gallery": gallery[0] if gallery else None})
    return profiles


def map_userids(app):
    """Map userHash -> userId by hashing the userId in each SCContent folder name."""
    out = {}
    for d in glob.glob(os.path.join(app, "Documents", "com.snap.file_manager_*_SCContent_*")):
        uid = os.path.basename(d).split("SCContent_")[-1]
        if _UUID_RE.fullmatch(uid):
            out[hashlib.sha256(uid.encode()).hexdigest()] = uid
    return out


def _open_gallery_with_module(local, egocipher_hex):
    """Open the database in-process with a sqlcipher3 binding. Connection or None."""
    if _SQLCIPHER is None:
        return None
    try:
        conn = _SQLCIPHER.connect(local)
        conn.execute('PRAGMA key = "x\'' + egocipher_hex + '\'"')
        conn.execute("PRAGMA cipher_compatibility = 3")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()   # fails on a bad key
        logger.info("Decrypted gallery.encrypteddb with the sqlcipher3 module")
        return conn
    except Exception as error:
        logger.debug(f"sqlcipher3 module could not open gallery.encrypteddb: {error}")
        return None


def _open_gallery_with_exe(local, egocipher_hex, workdir):
    """Dump the database with the sqlcipher CLI and rebuild it as plain SQLite."""
    exe = _sqlcipher_exe()
    if not exe:
        return None
    recovery = os.path.join(workdir, "recovery.sql")
    cmd = [exe, local,
           'pragma key="x\'' + egocipher_hex + '\'"',
           "PRAGMA cipher_compatibility = 3",
           ".output " + recovery.replace("\\", "/"),
           ".dump"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as error:
        logger.warning(f"sqlcipher CLI ({exe}) failed: {error}")
        return None
    if not os.path.exists(recovery) or os.path.getsize(recovery) == 0:
        return None
    decrypted = os.path.join(workdir, "gallery_decrypted.sqlite")
    if os.path.exists(decrypted):
        os.remove(decrypted)
    conn = sqlite3.connect(decrypted)
    try:
        with open(recovery, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    except sqlite3.DatabaseError as error:
        logger.warning(f"Could not load decrypted gallery dump: {error}")
        return None
    logger.info(f"Decrypted gallery.encrypteddb with {os.path.basename(exe)}")
    return conn


def decrypt_gallery_db(gallery_path, egocipher_hex, workdir):
    """Decrypt a SQLCipher gallery.encrypteddb; return a sqlite3-compatible connection or None.

    Tries the sqlcipher3 Python module first (no external binary, so it survives frozen
    builds), then falls back to a bundled/PATH sqlcipher CLI. On the old storage schema the
    Memories keys live here, so failing both means no media and no geolocation.
    """
    if not gallery_path or not os.path.exists(gallery_path) or not egocipher_hex:
        return None
    os.makedirs(workdir, exist_ok=True)
    local = os.path.join(workdir, "gallery.encrypteddb")
    for suffix in ("", "-wal", "-shm"):
        src = gallery_path + suffix
        if os.path.exists(src):
            shutil.copy(src, local + suffix)

    conn = _open_gallery_with_module(local, egocipher_hex)
    if conn is None:
        conn = _open_gallery_with_exe(local, egocipher_hex, workdir)
    if conn is None:
        logger.warning(
            f"Could not decrypt {os.path.basename(gallery_path)}: no working SQLCipher found. "
            "Install a binding (pip install sqlcipher3-wheels, sqlcipher3-binary or sqlcipher3) "
            "or provide sqlcipher3.exe. Memories keys/geolocation will be missing on the old "
            "storage schema.")
    return conn


# --------------------------------------------------------------------------- core

def _clean_text(v):
    """Decode a possibly cp1252/utf-8 text value for display."""
    if isinstance(v, bytes):
        for enc in ("utf-8", "cp1252"):
            try:
                return v.decode(enc)
            except Exception:
                continue
        return v.decode("latin1", "replace")
    return v


def _fmt_other(v):
    """Display value for a catch-all 'other' column: blobs as a size marker, long text trimmed."""
    if isinstance(v, (bytes, bytearray)):
        return f"<blob {len(v)} bytes>"
    v = _clean_text(v)
    if isinstance(v, str) and len(v) > 300:
        return v[:300] + "…"
    return v


def load_memories(profile, egocipher, persisted, workdir, timefmt=None):
    """
    Return (memories, stats) for one profile.

    memories: {snap_id: {meta..., times{}, urls{}, key, iv, is_meo, lat/lon, media_files[]}}
    timefmt : callable(cocoa_ts) -> display string (defaults to UTC).
    """
    if timefmt is None:
        timefmt, _ = make_time_formatter("utc")
    memories = {}
    conn = sqlite3.connect(f"file:{profile['scdb']}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(ZGALLERYSNAP)")]
    colset = set(cols)
    has_zenc = "ZENCRYPTION" in colset

    # every column whose name looks like a Cocoa timestamp (numeric TIME/DATE columns)
    def _timecols(names):
        return [c for c in names if ("TIME" in c or "DATE" in c)
                and "TIMEZONE" not in c and not c.startswith("Z_FOK")]
    time_cols = _timecols(cols)
    url_cols = [c for c in ("ZMEDIADOWNLOADURL", "ZMEDIAREDIRECTURI", "ZOVERLAYDOWNLOADURL",
                            "ZOVERLAYREDIRECTURI", "ZTHUMBNAILDOWNLOADURL", "ZTHUMBNAILREDIRECTURI")
                if c in colset]
    id_cols = [c for c in ("ZMEDIAID", "ZEXTERNALID", "ZSAVERUSERID", "ZDEVICEID",
                           "ZTIMEZONENAME", "ZMULTISNAPGROUPID", "ZCAMERAROLLID") if c in colset]
    # curated list of extra ZGALLERYSNAP columns worth surfacing (see SNAP_OTHER_LABELS)
    snap_other_cols = [c for c in SNAP_OTHER_LABELS if c in colset]

    # ZGALLERYENTRY (the entry/album a snap belongs to) carries its own timestamps and fields.
    # Column names can collide with ZGALLERYSNAP (e.g. ZCREATETIMEUTC) with a different meaning, so
    # entry values are kept in their own dicts and rendered in their own report sections. The
    # label lists (SNAP_OTHER_LABELS / ENTRY_OTHER_LABELS) also gate which columns appear, so
    # schemas from different app versions only surface the fields we've curated.
    entry_times, entry_other = {}, {}
    etcols, entry_other_cols = [], []
    try:
        ecols = [r[1] for r in cur.execute("PRAGMA table_info(ZGALLERYENTRY)")]
        ecolset = set(ecols)
        etcols = _timecols(ecols)
        entry_other_cols = [c for c in ENTRY_OTHER_LABELS if c in ecolset]
        for er in cur.execute("SELECT * FROM ZGALLERYENTRY"):
            er = dict(er)
            pk = er.get("Z_PK")
            if pk is None:
                continue
            # keep every column (None when empty) so the rendered tables share one column set
            entry_times[pk] = {c: (timefmt(er[c]) if isinstance(er.get(c), (int, float)) and er.get(c)
                                   else None) for c in etcols}
            entry_other[pk] = {c: _fmt_other(er.get(c)) for c in entry_other_cols}
    except sqlite3.DatabaseError as error:
        logger.debug(f"ZGALLERYENTRY read failed: {error}")
    empty_entry_times = {c: None for c in etcols}
    empty_entry_other = {c: None for c in entry_other_cols}

    for r in cur.execute("SELECT * FROM ZGALLERYSNAP WHERE ZSNAPID IS NOT NULL"):
        r = dict(r)
        snap = r["ZSNAPID"]
        # keep every timestamp column (None when empty) so all snap tables share one column set
        times = {c: (timefmt(r[c]) if isinstance(r.get(c), (int, float)) and r.get(c) else None)
                 for c in time_cols}
        entry_pk = r.get("ZENTRY")
        m = {
            "snap_id": snap,
            "user_hash": profile["userHash"],
            "media_type": r.get("ZMEDIATYPE"), # usually 0 or 1 (image or video)
            "format": r.get("ZSERVLETMEDIAFORMAT") or "",
            "media_format": r.get("ZMEDIAFORMAT"), # usualy 1, 3 or 4 (video, image, multi-snap?)
            "media_url": r.get("ZMEDIADOWNLOADURL"),
            "overlay_url": r.get("ZOVERLAYDOWNLOADURL"),
            "thumb_url": r.get("ZTHUMBNAILDOWNLOADURL"),
            "create_utc": timefmt(r.get("ZCREATETIMEUTC")),
            "created_sort": r.get("ZCREATETIMEUTC") or 0,
            "duration": r.get("ZDURATION"),
            "width": r.get("ZWIDTH"),
            "height": r.get("ZHEIGHT"),
            "camera": "Front" if r.get("ZCAMERAFRONTFACING") == 1 else "Back",
            "has_location": bool(r.get("ZHASLOCATION")),
            "times": times,
            "entry_times": entry_times.get(entry_pk, empty_entry_times),
            "snap_other": {c: _fmt_other(r.get(c)) for c in snap_other_cols},
            "entry_other": entry_other.get(entry_pk, empty_entry_other),
            "urls": {c: r.get(c) for c in url_cols if r.get(c)},
            "ids": {c: _clean_text(r.get(c)) for c in id_cols if r.get(c)},
            "key": None, "iv": None, "is_meo": False,
            "latitude": None, "longitude": None, "address": None,
            "media_files": [],
        }
        if has_zenc and r.get("ZENCRYPTION"):
            try:
                root = ccl_bplist.deserialise_NsKeyedArchiver(
                    ccl_bplist.load(BytesIO(r["ZENCRYPTION"])), parse_whole_structure=True)["root"]
                m["is_meo"] = bool(root.get("IS_ENCRYPTED"))
                m["key"], m["iv"] = root.get("KEY"), root.get("IV")
            except Exception as error:
                logger.debug(f"ZENCRYPTION decode failed for {snap}: {error}")
        memories[snap] = m

    stats = {"schema": "new" if has_zenc else "old", "gallery_keys": 0, "locations": 0}

    # gallery.encrypteddb: keys (old schema) + geolocation + address (both schemas)
    gconn = decrypt_gallery_db(profile["gallery"], egocipher,
                               os.path.join(workdir, profile["userHash"]))
    if gconn:
        gcur = gconn.cursor()
        tables = {r[0] for r in gcur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "snap_key_iv" in tables:
            for sid, key, iv, enc in gcur.execute("SELECT snap_id,key,iv,encrypted FROM snap_key_iv"):
                if sid not in memories:
                    continue
                m = memories[sid]
                if enc == 1:                              # My Eyes Only - unwrap
                    m["is_meo"] = True
                    if persisted:
                        try:
                            key, iv = unwrap_meo_key(persisted, key, iv)
                        except Exception as error:
                            logger.debug(f"MEO unwrap failed for {sid}: {error}")
                            key = iv = None
                    else:
                        key = iv = None
                if key and iv and not m["key"]:           # prefer scdb keys if already set
                    m["key"], m["iv"] = key, iv
                stats["gallery_keys"] += 1
        if "snap_location_table" in tables:
            for sid, lat, lon in gcur.execute("SELECT snap_id,latitude,longitude FROM snap_location_table"):
                if sid in memories:
                    memories[sid]["latitude"] = lat
                    memories[sid]["longitude"] = lon
                    stats["locations"] += 1
        if "snap_address_title" in tables:
            for sid, title in gcur.execute("SELECT snap_id,address_title FROM snap_address_title"):
                if sid in memories:
                    memories[sid]["address"] = _clean_text(title)
        gconn.close()

    return memories, stats


# split SCContent media: "<cache_key>_<start>-<end>" byte-range parts, plus the initial
# "<cache_key>_PREFETCH" chunk (parseSnapvideos renames PREFETCH -> _0-1 when it runs first).
_SC_SPLIT_RE = re.compile(r"^(.+?)_(?:(\d+)-\d+|PREFETCH)$")


def index_sccontent(app):
    """Index SCContent files by cache key across every per-user container.

    Returns ``(full, parts)``:
      * ``full``  : ``basename -> [paths]`` for whole files (CDN hash-addressed media and full
        local copies). A cache key can have full copies in more than one container.
      * ``parts`` : ``cache_key -> [(start_offset, path)]`` for media stored split into byte-range
        parts (``<cache_key>_<start>-<end>``). These must be concatenated in offset order before
        decrypting — the same reconstruction parseSnapvideos writes to ``SnapFixedVideos`` (but
        those stay encrypted; here we rebuild and decrypt from the parts directly).
    """
    full, parts = {}, {}
    for pat in ("Documents/com.snap.file_manager_*_SCContent_*",
                "Library/Caches/com.snap.file_manager_*_SCContent_*"):
        for d in glob.glob(os.path.join(app, pat)):
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                fp = os.path.join(d, name)
                if not os.path.isfile(fp):
                    continue
                mo = _SC_SPLIT_RE.match(name)
                if mo:
                    start = int(mo.group(2)) if mo.group(2) is not None else 0
                    parts.setdefault(mo.group(1).lower(), []).append((start, fp))
                else:
                    full.setdefault(name, []).append(fp)
    return full, parts


# "<cache_key>_<start>-<end>": the byte range a shard covers. index_sccontent's _SC_SPLIT_RE only
# needs the start (to order the shards); here we also want the declared end, to cross-check it
# against the bytes actually on disk.
_PART_RANGE_RE = re.compile(r"_(\d+)-(\d+)$")


def _part_coverage(ordered):
    """Describe how completely a set of byte-range shards covers the original file.

    The device caches only the ranges it actually streamed, so the shards on disk can start past 0
    or leave holes in the middle. Concatenating them regardless yields a file whose bytes are each
    correct but sit at the wrong offsets — which is exactly what makes a decoder report impossible
    NAL/atom sizes rather than simply refusing the file.

    ``ordered`` is ``[(start_offset, path)]`` sorted by offset. Coverage is measured from each
    shard's **actual size on disk** rather than the ``<start>-<end>`` in its name, so it holds
    whichever end convention the name uses; the name is used only to notice a shard that is itself
    shorter than it claims. Returns ``{"gaps": [(from, to)], "short": [names], "bytes": int}``.
    """
    gaps, short, pos, total = [], [], 0, 0
    for start, path in ordered:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        mo = _PART_RANGE_RE.search(os.path.basename(path))
        if mo:                                             # end may be exclusive or inclusive
            declared = int(mo.group(2)) - int(mo.group(1))
            if size not in (declared, declared + 1):
                short.append(os.path.basename(path))
        if start > pos:
            gaps.append((pos, start))
        pos = max(pos, start + size)
        total += size
    return {"gaps": gaps, "short": short, "bytes": total}


def _resolve_sccontent(cache_key, full, parts):
    """Resolve a cache key to (ciphertext, [full paths], [ordered part paths], coverage) or 4×None.

    Prefers a full copy for the bytes; otherwise concatenates the byte-range parts (deduped by
    start offset). Full copies and parts are all returned so every on-disk copy shows as a source.
    ``coverage`` is ``_part_coverage`` for a file rebuilt from shards, else None — a whole file has
    no shard layout to be missing anything.
    """
    fulls = full.get(cache_key, [])
    ordered, seen_off = [], set()
    for off, p in sorted(parts.get(cache_key.lower(), [])):
        if off in seen_off:                                # e.g. a PREFETCH and a _0-1 both at 0
            continue
        seen_off.add(off)
        ordered.append((off, p))
    paths = [p for _, p in ordered]
    if fulls:
        return open(fulls[0], "rb").read(), fulls, paths, None
    if ordered:
        return _read_concat(paths), [], paths, _part_coverage(ordered)
    return None, [], [], None


_UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")

# The account UUID in a com.snap.file_manager_*_SCContent_<userId> folder name — i.e. which
# account's cache scope an on-disk copy physically lives in (shared with cache_controller_report).
_SCCONTENT_USER_RE = re.compile(r"SCContent_([0-9A-Fa-f-]{36})")


def _scope_user(path):
    """The SCContent account UUID a path lives under, or None."""
    mo = _SCCONTENT_USER_RE.search(path.replace("\\", "/"))
    return mo.group(1) if mo else None


def index_cache_controller(app):
    """
    Map memory snap UUID (lower) -> [(cache_key, role)] using cache_controller.db.

    This is how locally-captured media with **no CDN URL** is addressed: the
    CACHE_FILE_CLAIM.EXTERNAL_KEY looks like ``snap-media-<UUID>`` / ``snap-overlay-<UUID>`` /
    ``snap-rendered-lowres-<UUID>`` and points to the SCContent file named CACHE_KEY.
    """
    out = {}
    for db in glob.glob(os.path.join(app, "Documents", "global_scoped", "cachecontroller",
                                     "cache_controller.db")):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute("SELECT EXTERNAL_KEY, CACHE_KEY FROM CACHE_FILE_CLAIM")
        except sqlite3.DatabaseError as error:
            logger.debug(f"cache_controller read failed: {error}")
            continue
        for ek, ck in rows:
            if not ek or not ck:
                continue
            mo = _UUID_RE.search(ek)
            if not mo:
                continue
            prefix = ek[:mo.start()].lower()
            if not any(t in prefix for t in ("media", "overlay", "lowres", "rendered")):
                continue
            role = ("overlay" if "overlay" in prefix else
                    "rendered" if ("lowres" in prefix or "rendered" in prefix) else "full")
            out.setdefault(mo.group(0).lower(), []).append((ck, role))
    return out


def all_cache_keys(app):
    """Return the set of every CACHE_KEY present in cache_controller.db (lowercased).

    Used to decide whether a Memory's media file also has a cache_controller entry, so the report
    can offer a two-way link to the cache_controller report only when that entry actually exists.
    """
    keys = set()
    for db in glob.glob(os.path.join(app, "Documents", "global_scoped", "cachecontroller",
                                     "cache_controller.db")):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for table in ("CACHE_FILE_CLAIM", "CACHE_FILE_METADATA"):
                try:
                    for (ck,) in conn.execute(f"SELECT CACHE_KEY FROM {table}"):
                        if ck:
                            keys.add(str(ck).lower())
                except sqlite3.DatabaseError:
                    continue
            conn.close()
        except sqlite3.DatabaseError as error:
            logger.debug(f"cache_controller key scan failed: {error}")
    return keys


def index_caching_media(app):
    """Return [(folder, {item_hash: [ordered chunk paths]})] for caching-media."""
    root = os.path.join(app, "Library", "Caches", "caching-media")
    folders = []
    if not os.path.isdir(root):
        return folders
    for folder in os.listdir(root):
        fp = os.path.join(root, folder)
        if not os.path.isdir(fp):
            continue
        by_item = {}
        for name in os.listdir(fp):
            mo = PACK_RE.match(name)
            if mo:
                by_item.setdefault(mo.group(1), []).append((int(mo.group(2)), os.path.join(fp, name)))
        for ih in by_item:
            by_item[ih] = [p for _, p in sorted(by_item[ih])]
        if by_item:
            folders.append((folder, by_item))
    return folders


def _read_concat(paths):
    return b"".join(open(p, "rb").read() for p in paths)


def _read_head(paths, n):
    """First n bytes of the concatenation of paths, reading no more files than needed."""
    out = b""
    for p in paths:
        with open(p, "rb") as fh:
            out += fh.read(n - len(out))
        if len(out) >= n:
            break
    return out


def _dims(path):
    try:
        with Image.open(path) as im:
            return f"{im.size[0]}×{im.size[1]}"
    except Exception:
        return ""


def _snap_dim(m):
    """ZGALLERYSNAP dimensions (ZWIDTH×ZHEIGHT) — used for videos, whose container PIL can't read."""
    return f"{m['width']}×{m['height']}" if m.get("width") and m.get("height") else ""


@contextlib.contextmanager
def _quiet_stderr():
    """Silence FFmpeg's decoder chatter at the OS level for the duration of the block.

    OpenCV's FFmpeg writes to file descriptor 2 from C, so ``contextlib.redirect_stderr`` never
    sees it, and the ``OPENCV_FFMPEG_*`` environment variables do not help either: the capture
    options reach only the *demuxer*, while "Invalid NAL unit size" / "Error splitting the input
    into NAL units" come from the decoder context. Partially cached media emits two such lines per
    undecodable frame, which floods the run log and — on a Windows console — costs more time than
    the decoding itself, so fd 2 goes to the null device while the decoder runs.
    """
    try:
        sys.stderr.flush()
    except Exception:                                      # pragma: no cover - detached stderr
        pass
    try:
        saved = os.dup(2)
    except (OSError, ValueError, AttributeError):           # no real fd 2 (pythonw, some hosts)
        yield
        return
    devnull = None
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        yield
    finally:
        try:
            os.dup2(saved, 2)
        finally:
            os.close(saved)
            if devnull is not None:
                os.close(devnull)


# How many frames to try before giving up on a poster. Partially cached video decodes at the start
# and fails after that, so the frame we want is always within the first few reads; the bound is
# what stops a badly damaged file from being decoded end to end for a thumbnail.
_POSTER_MAX_READS = 60


def _first_decodable(cap, limit):
    """The first frame that decodes, reading forward from the current position. None if none does."""
    for _ in range(limit):
        ok, frame = cap.read()
        if ok and frame is not None:
            return frame
        if not ok:                                         # stream ended / unrecoverable
            return None
    return None


def generate_poster(video_path, out_path, at_seconds=1.0, complete=True):
    """Extract a single poster frame from a video into out_path (JPEG). Returns True on success.

    The result is a DERIVED artifact (not original device data) — callers must label it as such.

    Incompletely cached video still gets a poster. What the cache holds starts at the beginning of
    the file, so the opening frames decode even when the sample table points past the bytes on
    disk; for those files we skip the seek (seeking into missing bytes fails, and the failure costs
    a full re-read) and take the first frame that decodes.
    """
    for var in ("OPENCV_LOG_LEVEL", "OPENCV_FFMPEG_LOGLEVEL", "OPENCV_VIDEOIO_DEBUG"):
        os.environ.setdefault(var, "OFF" if "LOG_LEVEL" in var else "0")
    try:
        import cv2
    except Exception as error:
        logger.debug(f"cv2 unavailable, cannot generate poster: {error}")
        return False
    try:
        frame = None
        with _quiet_stderr():
            cap = cv2.VideoCapture(video_path)
            try:
                if complete:
                    fps = cap.get(cv2.CAP_PROP_FPS) or 0
                    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                    if fps and frames:
                        cap.set(cv2.CAP_PROP_POS_FRAMES,
                                min(int(fps * at_seconds), max(int(frames) - 1, 0)))
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        frame = _first_decodable(cap, _POSTER_MAX_READS)
                else:
                    frame = _first_decodable(cap, _POSTER_MAX_READS)
            finally:
                cap.release()
        if frame is None:
            return False
        return bool(cv2.imwrite(out_path, frame))
    except Exception as error:
        logger.debug(f"poster generation failed for {video_path}: {error}")
        return False


def _hashes(data):
    return hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()


def _save_media(outdir, name, data):
    """Write media bytes and return a media_files entry stub with size and dims."""
    out = os.path.join(outdir, name)
    with open(out, "wb") as o:
        o.write(data)
    return {"out": name, "bytes": len(data), "dim": _dims(out)}


def _sccontent_completeness(coverage, tail_ok):
    """Classify a rebuilt SCContent file. Returns (complete, reason) with complete tri-state.

    ``complete`` is True when nothing says bytes are missing, False when something does, and None
    when the file was stored as plaintext — there is no padding to check and no shard layout to
    measure, so we say "unknown" rather than implying a completeness we did not verify.
    """
    problems = []
    gaps = (coverage or {}).get("gaps") or []
    short = (coverage or {}).get("short") or []
    if gaps:
        missing = sum(b - a for a, b in gaps)
        where = ", ".join(f"{a:,}–{b:,}" for a, b in gaps[:4])
        problems.append(f"{len(gaps)} gap(s) in the cached byte ranges — {missing:,} bytes missing "
                        f"at offset {where}{', …' if len(gaps) > 4 else ''}")
    if short:
        problems.append(f"{len(short)} shard(s) hold fewer bytes than the range in their name "
                        f"declares ({', '.join(short[:3])}{', …' if len(short) > 3 else ''})")
    if tail_ok is False:
        problems.append("the end of the file is not cached (the decrypted bytes carry no PKCS#7 "
                        "padding, which a complete file always ends with)")
    if problems:
        return False, ("Partially cached — " + "; ".join(problems) + ". The device stores only the "
                       "byte ranges it actually streamed. This is NOT a decryption failure: the key "
                       "is correct and the recovered bytes are genuine, but the file is not the "
                       "whole media. Bytes after a gap sit at the wrong offsets, so a player or "
                       "decoder will fail from that point on.")
    if tail_ok is None and not coverage:
        return None, ("Completeness not verified: this cache file was stored as plaintext, so "
                      "there is no padding to check and no shard layout to measure.")
    return True, ""


def _pack_completeness(payload, declared):
    """Classify a caching-media pack from its header's declared payload length."""
    if declared is None:
        return None, ("Completeness not verified: this pack uses a header-less variant container, "
                      "so nothing on disk declares how long the payload should be.")
    if len(payload) < declared:
        return False, (f"Partially cached — the pack header declares a {declared:,}-byte payload "
                       f"but only {len(payload):,} bytes are on disk ({declared - len(payload):,} "
                       "missing). Chunks of this item were evicted from the cache or never "
                       "downloaded. The recovered bytes are genuine, just truncated.")
    return True, ""


def collect_media(memories, app, outdir, padding="both"):
    """Decrypt SCContent + caching-media for all memories; write files, fill m['media_files'].

    SCContent files are located two ways: by ``SHA256(url token)[:16]`` (CDN-downloaded media)
    and via ``cache_controller.db`` EXTERNAL_KEY ``snap-media/-overlay/-rendered-lowres-<UUID>``
    (locally-captured media that has no CDN URL, e.g. videos recorded on the device). The cache
    key is the *start* of the on-disk filename: media can be a single ``<cache_key>`` file or
    split into ``<cache_key>_<start>-<end>`` byte-range parts, which are concatenated in order and
    then decrypted. Each output name embeds the cache key / item hash so a Memory whose media
    spans multiple caches never overwrites itself.
    """
    os.makedirs(outdir, exist_ok=True)
    scfull, scparts = index_sccontent(app)
    ccindex = index_cache_controller(app)
    cc_keys = all_cache_keys(app)              # for two-way links to the cache_controller report
    userids = map_userids(app)                 # userHash -> userId, to spot cross-scope on-disk copies
    keyed = [(sid, m) for sid, m in memories.items() if m["key"] and m["iv"]]

    # This function does all the per-file work of the report and can run for a long time on a large
    # gallery, so each phase reports its progress: a silent hour is indistinguishable from a hang.
    logger.info(f"Media: {len(keyed)} memories with a usable key, "
                f"{len(scfull)} whole + {len(scparts)} split SCContent cache file(s)")
    t0 = time.monotonic()

    # --- SCContent (URL-addressed + cache_controller-addressed, whole or split into parts) ---
    for done, (sid, m) in enumerate(keyed, 1):
        if done % 2000 == 0:
            logger.info(f"  SCContent: {done}/{len(keyed)} memories")
        targets = []                                       # (role, cache_key, addressing basis)
        url_fields = {"full": "ZMEDIADOWNLOADURL", "overlay": "ZOVERLAYDOWNLOADURL",
                      "thumbnail": "ZTHUMBNAILDOWNLOADURL"}
        for role, url in (("full", m["media_url"]), ("overlay", m["overlay_url"]),
                          ("thumbnail", m["thumb_url"])):
            tok = url_token(url)
            if tok:
                basis = (f"Located by CDN URL: CACHE_KEY = SHA-256 of the token in "
                         f"{url_fields[role]} (first 16 bytes). Decrypted with the snap's "
                         f"AES-256-CBC key/IV.")
                targets.append((role, hashlib.sha256(tok.encode()).hexdigest()[:32], basis))
        for ck, role in ccindex.get(sid.lower(), []):
            basis = (f"Located via cache_controller.db: a CACHE_FILE_CLAIM EXTERNAL_KEY "
                     f"(snap-{role}-/g-media-<snapid>) names this Memory and points at CACHE_KEY "
                     f"{ck}. Decrypted with the snap's AES-256-CBC key/IV.")
            targets.append((role, ck, basis))

        seen = set()
        for role, cache_key, addr_basis in targets:
            if cache_key in seen:
                continue
            seen.add(cache_key)
            cipher, fulls, pparts, coverage = _resolve_sccontent(cache_key, scfull, scparts)
            if cipher is None:
                continue
            padded, stripped, ext, tail_ok = decrypt_sccontent(cipher, m["key"], m["iv"])
            if padded is None:
                continue
            complete, why_incomplete = _sccontent_completeness(coverage, tail_ok)
            has_pad = padded != stripped
            if padding == "keep":
                write_bytes = padded
                hashes = [("with padding" if has_pad else "", *_hashes(padded))]
            elif padding == "strip":
                write_bytes = stripped
                hashes = [("no padding" if has_pad else "", *_hashes(stripped))]
            else:                                          # both (default)
                write_bytes = stripped
                hashes = ([("no padding", *_hashes(stripped)), ("with padding", *_hashes(padded))]
                          if has_pad else [("", *_hashes(stripped))])
            entry = _save_media(outdir, f"{sid}_{role}_{cache_key[:8]}.{ext}", write_bytes)
            source = (f"SCContent (rebuilt from {len(pparts)} parts)"
                      if pparts and not fulls else "SCContent")
            if pparts and not fulls:
                addr_basis += (f" Reconstructed from {len(pparts)} byte-range parts concatenated "
                               "in offset order before decryption.")
            # cross-scope: an on-disk copy in a different account's SCContent folder than this
            # Memory's owner account (an untracked/materialized duplicate; ownership is unchanged).
            src_paths = fulls + pparts
            owner_uid = userids.get(m["user_hash"])
            scope_by_path = {p: _scope_user(p) for p in src_paths}
            cross_scope = sorted({s for s in scope_by_path.values()
                                  if s and owner_uid and s.lower() != owner_uid.lower()})
            entry.update({"role": role, "source": source, "ext": ext, "src": src_paths,
                          "hashes": hashes, "snap_dim": _snap_dim(m),
                          "cache_key": cache_key,
                          "in_cc": cache_key.lower() in cc_keys,
                          "how": addr_basis,
                          "complete": complete, "why_incomplete": why_incomplete,
                          "owner_uid": owner_uid,
                          "scope_by_path": scope_by_path, "cross_scope": cross_scope})
            m["media_files"].append(entry)

    n_sc = sum(len(m["media_files"]) for _, m in keyed)
    logger.info(f"Media: SCContent done — {n_sc} file(s) in {time.monotonic() - t0:.0f}s")

    # --- caching-media (link by decrypt-and-match; unique names by item hash) ---
    t0 = time.monotonic()
    cm_folders = index_caching_media(app)
    logger.info(f"Media: matching {len(cm_folders)} caching-media folder(s) against "
                f"{len(keyed)} key(s)")
    n_packs = 0
    for done, (folder, by_item) in enumerate(cm_folders, 1):
        if done % 500 == 0:
            logger.info(f"  caching-media: {done}/{len(cm_folders)} folder(s), {n_packs} file(s)")
        # Probe with the first two plaintext blocks only. Every acceptance test in decrypt_pack
        # reads within the first 24 plaintext bytes, and CBC decrypts a prefix independently of the
        # rest, so this identifies the owning key exactly as a full decrypt would — but a wrong key
        # is rejected after 32 bytes instead of after the whole item, which is the difference
        # between minutes and hours once a gallery has thousands of memories.
        head = _read_head(next(iter(by_item.values())), _PACK_PROBE_BYTES)
        match = next(((sid, m) for sid, m in keyed if pack_matches(head, m["key"], m["iv"])), None)
        if not match:
            continue
        sid, m = match
        for item_hash, chunks in by_item.items():
            payload, ext, declared = decrypt_pack(_read_concat(chunks), m["key"], m["iv"])
            if not payload:
                continue
            complete, why_incomplete = _pack_completeness(payload, declared)
            entry = _save_media(outdir, f"{sid}_pack_{item_hash[:12]}.{ext}", payload)
            how = ("Linked by decrypt-and-match: caching-media pack names are opaque, so this "
                   "folder was tried against every Memory's AES key/IV and only this Memory's key "
                   "decrypts it to valid media (magic bytes after the 8-byte header). Not indexed "
                   "by cache_controller.db.")
            entry.update({"role": "cached", "source": "caching-media", "ext": ext,
                          "src": chunks, "folder": folder, "item": item_hash,
                          "hashes": [("", *_hashes(payload))], "snap_dim": _snap_dim(m),
                          "complete": complete, "why_incomplete": why_incomplete,
                          "how": how})
            m["media_files"].append(entry)
            n_packs += 1
    logger.info(f"Media: caching-media done — {n_packs} file(s) in {time.monotonic() - t0:.0f}s")

    # label the smallest caching-media still per memory as the "preview"
    for m in memories.values():
        packs = sorted((f for f in m["media_files"] if f["source"] == "caching-media"),
                       key=lambda f: f["bytes"])
        if packs:
            packs[0]["role"] = "preview"

    # for video memories with a recovered .mp4 but no still, derive a poster frame from the video
    t0 = time.monotonic()
    todo = []
    for sid, m in memories.items():
        if _best_still(m["media_files"]):
            continue
        # A complete video makes the better poster (we can seek into it), so prefer one; among
        # equals the largest file has the most of the media in it.
        vids = sorted((f for f in m["media_files"] if f["ext"] == "mp4"),
                      key=lambda f: (f.get("complete") is False, -f["bytes"]))
        if vids:
            todo.append((sid, m, vids[0]))
    logger.info(f"Media: extracting poster frames from {len(todo)} video(s) with no cached still")
    made = 0
    for done, (sid, m, vid) in enumerate(todo, 1):
        if done % 500 == 0:
            logger.info(f"  posters: {done}/{len(todo)}, {made} extracted")
        video_out = os.path.join(outdir, vid["out"])
        poster_name = f"{sid}_poster.jpg"
        if not generate_poster(video_out, os.path.join(outdir, poster_name),
                               complete=vid.get("complete") is not False):
            continue
        made += 1
        data = open(os.path.join(outdir, poster_name), "rb").read()
        entry = _save_media(outdir, poster_name, data)      # already written; recompute size/dim
        partial = (" The video it came from is only partially cached, so the frame is from the "
                   "part that is present." if vid.get("complete") is False else "")
        entry.update({"role": "poster (generated)", "source": "generated", "ext": "jpg",
                      "src": ["(generated from the decrypted video — not original device data)"]
                             + vid["src"],
                      "hashes": [("", *_hashes(data))], "generated": True, "snap_dim": "",
                      "complete": None, "why_incomplete": "",
                      "how": ("Derived artifact: this Memory is a video with no cached still, so a "
                              "poster frame was extracted from the decrypted .mp4. It is NOT "
                              "original device data." + partial)})
        m["media_files"].append(entry)
    logger.info(f"Media: posters done — {made} of {len(todo)} extracted "
                f"in {time.monotonic() - t0:.0f}s")

    files = [f for m in memories.values() for f in m["media_files"]]
    partial = sum(1 for f in files if f.get("complete") is False)
    if partial:
        logger.info(f"Media: {partial} of {len(files)} recovered file(s) are only partially "
                    f"cached — flagged as incomplete in the report")


# --------------------------------------------------------------------------- report

def _best_still(files):
    imgs = [f for f in files if f["ext"] in ("jpg", "png", "webp")]
    return max(imgs, key=lambda f: f["bytes"]) if imgs else None


# Anchors that mark the start of the device/extraction tree inside a temporary extract path.
# We strip everything before the first one found so the report shows the path as it appears in
# the extraction ZIP (e.g. "/Application/<UUID>/Documents/...") instead of the temp working dir.
# Ordered most-specific first so a full-filesystem path keeps its "/private/var/mobile/…" form.
_DEVICE_ANCHORS = ("/private/var/mobile/", "/private/var/", "/application/", "/applications/")


def load_path_manifest(*roots):
    """Load container_prefixes written by extract_zip (maps ``Application/<UUID>`` -> the ZIP path
    prefix that was truncated off, so we can rebuild the full on-device path). Empty when absent."""
    for root in roots:
        if not root:
            continue
        mf = os.path.join(root, "extraction_manifest.json")
        if os.path.isfile(mf):
            try:
                with open(mf, encoding="utf-8") as f:
                    return json.load(f).get("container_prefixes", {}) or {}
            except Exception as error:
                logger.debug(f"Could not read extraction manifest {mf}: {error}")
    return {}


def _apply_manifest(display, manifest):
    """Prepend the truncated ZIP prefix to a ``/Application/<UUID>/…`` display path when known."""
    if not manifest:
        return display
    rel = display.lstrip("/")
    for key, prefix in manifest.items():
        if prefix and (rel == key or rel.startswith(key + "/")):
            return "/" + prefix + "/" + rel
    return display


def device_path(fp, src_root=None, manifest=None):
    """Render a source path as its in-extraction / on-device path, else the full path.

    Files are unzipped under ``…/Application/<UUID>/…`` (extract_zip drops the ZIP path to the
    left of "Application"). We show that archive-relative path — resolved via ``src_root`` when the
    file lives beneath it, otherwise by anchoring on a known device-tree root — and, when the
    extraction ``manifest`` is available, restore the dropped prefix to give the full device path.
    """
    p = fp.replace("\\", "/")
    display = None
    if src_root:
        root = src_root.replace("\\", "/").rstrip("/")
        if root and p.lower().startswith(root.lower() + "/"):
            display = "/" + p[len(root) + 1:]
    if display is None:
        low = p.lower()
        for anchor in _DEVICE_ANCHORS:
            i = low.find(anchor)
            if i != -1:
                display = p[i:]
                break
    if display is None:
        return p                                            # unrecognised (e.g. generated-note text)
    return _apply_manifest(display, manifest)


def _collapse_part_paths(paths):
    """Collapse split byte-range parts that share a directory + cache key into one
    ``<dir>/<cache_key>_*`` entry, keeping any whole ``<cache_key>`` file as its own entry.
    A media file split into dozens of parts then reads as a single wildcard line. Order preserved.
    """
    out, seen = [], set()
    for p in paths:
        d, _, name = p.replace("\\", "/").rpartition("/")
        mo = _SC_SPLIT_RE.match(name)
        if mo:
            key = (d, mo.group(1))
            if key in seen:
                continue
            seen.add(key)
            out.append(f"{d}/{mo.group(1)}_*" if d else f"{mo.group(1)}_*")
        else:
            out.append(p)
    return out


# friendlier labels for scdb columns. Timestamp labels are per-table because a column name
# (e.g. ZCREATETIMEUTC) exists in BOTH ZGALLERYSNAP and ZGALLERYENTRY with a different meaning;
# each table's values are fetched and displayed independently (the raw column name is also shown
# under every header, so identical labels stay unambiguous).
SNAP_TIME_LABELS = {
    "ZCREATETIMEUTC": "Created",
    "ZCAPTURETIMEUTC": "Captured",
    "ZPLACEHOLDERCREATETIME": "Placeholder created",
}
ENTRY_TIME_LABELS = {
    # confirmed ZGALLERYENTRY timestamp columns (note ZCREATETIMEUTC also exists on ZGALLERYSNAP)
    "ZCREATETIMEUTC": "Entry created",
    "ZEARLIESTSNAPCREATETIMEUTC": "Earliest snap created",
    "ZLATESTSNAPCAPTURETIMEUTC": "Latest snap captured",
    "ZAUTOSAVETIMEUTC": "Auto-saved",
    "ZSYNCEDAUTOSAVETIMEUTC": "Auto-save synced",
    "ZDUPLICATETIMEUTC": "Duplicated",
    "ZFEATUREDEXPIRATIONTIMEUTC": "Featured expiration",
    "ZFEATUREDSTORYACTIVATIONDATEUTC": "Featured story activation",  # newer schema
}
ID_LABELS = {
    "ZMEDIAID": "Media ID", "ZEXTERNALID": "External ID", "ZSAVERUSERID": "Saver user ID",
    "ZDEVICEID": "Device ID", "ZTIMEZONENAME": "Timezone",
    "ZMULTISNAPGROUPID": "Multi-snap group ID", "ZCAMERAROLLID": "Camera roll ID",
}
URL_LABELS = {
    "ZMEDIADOWNLOADURL": "Media (download)", "ZMEDIAREDIRECTURI": "Media (redirect)",
    "ZOVERLAYDOWNLOADURL": "Overlay (download)", "ZOVERLAYREDIRECTURI": "Overlay (redirect)",
    "ZTHUMBNAILDOWNLOADURL": "Thumbnail (download)", "ZTHUMBNAILREDIRECTURI": "Thumbnail (redirect)",
}
# Extra (non-time, non-URL, non-id) columns worth surfacing. Kept in two dicts because a
# column name (e.g. ZSOURCE) can exist in BOTH tables with a different meaning, so each table
# owns its own label set and both values are rendered independently in their own section.
SNAP_OTHER_LABELS = {
    # ZGALLERYSNAP
    "Z_OPT": "OPT", # integer value usually between 1 and over 20
    "ZCAPTUREMODE": "Capture mode",
    "ZCLOUDMEDIASTATE": "Cloud media state",
    "ZHASOVERLAYIMAGE": "Has overlay image", # 0 or 1
    "ZHASSYNCED": "Has synced", # 0 or 1
    "ZINFINITEDURATION": "Infinite duration", # 0 or 1
    "ZISTEMPORARY": "Is temporary", # 0 or 1
    "ZSOURCE": "Source", # Usually 0, 1 or 3
    "ZOWNER": "Owner", # 0 or 1
    "ZOWNERDELETED": "Owner deleted", # 0 or 1
    "ZDEVICEFIRMWAREINFO": "Device firmware info",
    "ZDUPLICATEDFROMSNAPID": "Duplicated from snap ID",
    "ZRETRYFROMSNAPID": "Retry from snap ID",
    "ZTRANSFERBATCHID": "Transfer batch ID",
    # added by newer app versions (prune any that prove to have no forensic value)
    "ZCHROMESUBTITLE": "Chrome subtitle",
    "ZCLIENTPROCESSINGTYPE": "Client processing type",
    "ZCOLLAGEUCOLENSID": "Collage lens ID",
    "ZCREATEDFROMCAMERAROLLITEMIDS": "Created from camera-roll item IDs",
    "ZCREATEDFROMSNAPIDS": "Created from snap IDs",
    "ZEXTERNALMETADATA": "External metadata",
    "ZGROUPNAME": "Group name",
    "ZMEDIAORIGIN": "Media origin",
    "ZMEMDATAIDS": "Mem data IDs",
    "ZTEMPLATEID": "Template ID",
}
ENTRY_OTHER_LABELS = {
    # ZGALLERYENTRY
    "ZENTRYSOURCE": "Entry source", # Observed values: 0 or 16
    "ZGALLERYTYPE": "Gallery type",
    "ZISHIDDEN": "Is hidden", # 0 or 1
    "ZISPRIVATE": "Is private", # 0 or 1
    "ZSOURCES": "Sources", # Observed values: 1, 2, 8 or 9
    "ZSYNCEDISPRIVATE": "Synced private", # 0 or 1
    "ZVIEWTYPE": "View type", # Observed values: 0 or 2
    "ZCREATORUSERID": "Creator user ID",
    "ZENTRYID": "Entry ID",
    "ZSNAPSHASH": "Snaps hash",
    "ZSUBTITLE": "Subtitle",
    "ZSYNCEDTITLE": "Synced title",
    "ZTITLE": "Title",
    # added by newer app versions (prune any that prove to have no forensic value)
    "ZCLIENTGENSTORYITEMORDERS": "Client-gen story item orders",
    "ZCLIENTGENSTORYRETRYCOUNT": "Client-gen story retry count",
    "ZCLIENTPROCESSINGBITMASKTYPE": "Client processing bitmask type",
    "ZCLIENTPROCESSINGTYPE": "Client processing type",
    "ZCOLLAGEUCOLENSID": "Collage lens ID",
    "ZEXPECTEDCLIENTGENSNAPSCOUNT": "Expected client-gen snaps count",
    "ZFALLBACKFEATUREDSTORYCATEGORY": "Fallback featured-story category",
    "ZFEATUREDSTORYLOGGINGINFO": "Featured-story logging info",
    "ZFEATUREDSTORYTEMPLATENAME": "Featured-story template name",
    "ZFOLDERTYPE": "Folder type",
    "ZMEMDATAID": "Mem data ID",
    "ZSNAPFEEDVIEWEDITEMIDS": "Snap-feed viewed item IDs",
    "ZTEMPLATEID": "Template ID",
}


def _union_cols(mems, attr):
    """Ordered union of the keys seen in every memory's `attr` dict (first-seen = DB order)."""
    order, seen = [], set()
    for m in mems:
        for c in m.get(attr, {}):
            if c not in seen:
                seen.add(c)
                order.append(c)
    return order


def _null_cell(v):
    """A table cell: the value, or a muted NULL when it is missing/empty."""
    return "<span class='muted'>NULL</span>" if v in (None, "") else html.escape(str(v))


def _info(text):
    """A small round '?' the examiner can click for an explanation of how a media file was found."""
    if not text:
        return ""
    return ("<span class='hint'><span class='qm' onclick='hint(event,this)'>?</span>"
            f"<span class='tip'>{html.escape(text)}</span></span>")


def _partial_badge(f):
    """Badge + explanation for a media file the cache holds only part of."""
    if f.get("complete") is False:
        return (" <span class='partial'>⚠ incomplete — partially cached</span>"
                + _info(f.get("why_incomplete")))
    if f.get("complete") is None and f.get("why_incomplete"):
        return " <span class='unverified'>completeness not verified</span>" + \
               _info(f.get("why_incomplete"))
    return ""


def _cross_scope_note(f):
    """Explanation text for a media file with an on-disk copy in another account's scope."""
    users = f.get("cross_scope") or []
    owner = f.get("owner_uid") or "(unknown)"
    return (f"{len(users)} on-disk copy(ies) of this media sit in a different account's SCContent "
            f"scope ({', '.join(users)}) than this Memory's owner account ({owner}). This is "
            "typically an untracked/materialized duplicate (e.g. a consolidated copy in the active "
            "account's cache); it does not change ownership, and cache_controller.db does not claim "
            "it there. A copy's containing SCContent_<userId> folder is NOT a reliable owner.")


def _render_src_paths(f, src_root, manifest):
    """Render a media file's source paths, grouped by the account SCContent scope they live in so a
    copy in a different account's scope than the Memory owner is visibly flagged."""
    scope_by = f.get("scope_by_path") or {}
    cross = set(f.get("cross_scope") or [])
    if not scope_by:                                       # caching-media / generated: no scope info
        return "<br>".join(html.escape(s) for s in
                           _collapse_part_paths(device_path(s, src_root, manifest) for s in f["src"]))
    groups = {}
    for s in f["src"]:
        groups.setdefault(scope_by.get(s), []).append(s)
    blocks = []
    for scope, plist in sorted(groups.items(), key=lambda kv: (kv[0] in cross, str(kv[0]))):
        listed = "<br>".join(html.escape(s) for s in
                             _collapse_part_paths(device_path(s, src_root, manifest) for s in plist))
        badge = " <span class='xscope'>⚠ different account scope</span>" if scope in cross else ""
        blocks.append(listed + badge)
    return "<br>".join(blocks)


def _grid(pairs):
    """key/value grid HTML from (label, value) pairs, skipping None/empty (0 is kept)."""
    return "".join(f"<div class='k'>{html.escape(str(k))}</div>"
                   f"<div class='v'>{html.escape(str(v))}</div>"
                   for k, v in pairs if v not in (None, ""))


def _geo_html(m, keychain_available):
    """Location line with OpenStreetMap + Google Maps links on the same line."""
    if m["latitude"] is not None:
        lat, lon = m["latitude"], m["longitude"]
        addr = f" — {html.escape(m['address'])}" if m.get("address") else ""
        return (f'<a href="https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}" '
                f'target="_blank">{lat:.6f}, {lon:.6f}</a>'
                f' &middot; <a href="https://www.google.com/maps?q={lat},{lon}" '
                f'target="_blank">Google Maps</a>{addr}')
    if m["has_location"]:
        return ('<span class="muted">recorded on device — full-filesystem keychain required</span>'
                if not keychain_available else '<span class="muted">flagged but not found</span>')
    return "&mdash;"


def _ts_table(members, cols, attr, labels, single):
    """One timestamp table: a column per field, a row per memory, NULL-filled so every table in
    the report carries the same columns. When a group has several memories, cells are tinted to
    show which values match across the merged memories (shared) vs are unique to one memory."""
    if not cols:
        return "<div class='v muted'>none</div>"
    head = "".join(f"<th>{html.escape(labels.get(c, c))}"
                   f"<div class='col'>{html.escape(c)}</div></th>" for c in cols)
    rid_head = "" if single else "<th class='rid'>Snap</th>"
    # per-column value frequency, so a value shared by ≥2 memories reads as "matching"
    freq = {c: {} for c in cols}
    if not single:
        for m in members:
            d = m.get(attr, {})
            for c in cols:
                v = d.get(c)
                if v not in (None, ""):
                    freq[c][v] = freq[c].get(v, 0) + 1
    body = []
    for m in members:
        d = m.get(attr, {})
        rid = "" if single else (f"<th class='rid' title='{html.escape(m['snap_id'])}'>"
                                 f"{html.escape(m['snap_id'][:8])}…</th>")
        cells = []
        for c in cols:
            v = d.get(c)
            cls = ""
            if not single and v not in (None, ""):
                cls = " class='tsmatch'" if freq[c][v] > 1 else " class='tsuniq'"
            cells.append(f"<td{cls}>{_null_cell(v)}</td>")
        body.append(f"<tr>{rid}{''.join(cells)}</tr>")
    legend = ("" if single else "<div class='tslegend'>"
              "<span class='sw tsmatch'></span> matches another memory &nbsp; "
              "<span class='sw tsuniq'></span> unique to this memory</div>")
    return (f"<div class='tswrap'><table class='ts'><tr>{rid_head}{head}</tr>"
            f"{''.join(body)}</table>{legend}</div>")


def _field_label(col, desc):
    """Display key: the raw DB column name with our short description in parentheses."""
    return f"{col} ({desc})" if desc and desc != col else col


def _meta_grid(m):
    """Media-intrinsic metadata (same for every snap that references the same media)."""
    dur = f"{m['duration']:.1f}s" if isinstance(m["duration"], (int, float)) and m["duration"] else ""
    return _grid([(_field_label("ZMEDIATYPE", "Media type"),
                   "Video" if m["media_type"] == 1 else "Image"),
                  (_field_label("ZSERVLETMEDIAFORMAT", "Servlet format"), m["format"]),
                  (_field_label("ZWIDTH×ZHEIGHT", "Dimensions"), _snap_dim(m)),
                  (_field_label("ZDURATION", "Duration"), dur),
                  (_field_label("ZCAMERAFRONTFACING", "Camera"), m["camera"])])


def _url_grid(m):
    return "".join(
        f"<div class='k'>{html.escape(_field_label(c, URL_LABELS.get(c, c)))}</div>"
        f"<div class='v url'>{html.escape(u)}</div>"
        for c, u in m["urls"].items()) or "<div class='v muted'>none</div>"


def _snap_values_grid(m):
    """Per-snap ZGALLERYSNAP values: IDs (except the group's ZMEDIAID header) + curated extras."""
    pairs = [(_field_label(c, ID_LABELS.get(c, c)), v) for c, v in m["ids"].items() if c != "ZMEDIAID"]
    pairs += [(_field_label(c, SNAP_OTHER_LABELS.get(c, c)), v) for c, v in m["snap_other"].items()]
    return _grid(pairs) or "<div class='v muted'>none</div>"


def _entry_values_grid(m):
    """Per-snap ZGALLERYENTRY (entry/album) values."""
    pairs = [(_field_label(c, ENTRY_OTHER_LABELS.get(c, c)), v) for c, v in m["entry_other"].items()]
    return _grid(pairs) or "<div class='v muted'>none</div>"


def _shared_or_per(members, render_fn):
    """Render a block for every member; if they are all identical return (html, True) so it can be
    shown once, otherwise (per_member_list, False) so it is broken out inside each snap."""
    rendered = [render_fn(m) for m in members]
    if len(set(rendered)) <= 1:
        return rendered[0], True
    return rendered, False


def _shared_location(members, keychain_available):
    """Like _shared_or_per but for location: two snaps match when they share the same lat/long even
    if only one also resolved a city/address. When shared, the richest (address-bearing) is shown."""
    def key(m):
        if m["latitude"] is not None:
            return (round(m["latitude"], 6), round(m["longitude"], 6))
        return ("noloc", bool(m["has_location"]))                # located vs not is a real difference
    if len({key(m) for m in members}) == 1:
        best = max(members, key=lambda m: (m["latitude"] is not None, 1 if m.get("address") else 0))
        return _geo_html(best, keychain_available), True
    return [_geo_html(m, keychain_available) for m in members], False


def _map_html(members, media_prefix="../"):
    """The offline-tile-server map for a memory/group, or nothing when maps were not rendered.

    Labelled as a **derived** artifact: the imagery comes from the examiner's own tile server, only
    the marker position comes from the device.
    """
    info = next((m["map"] for m in members if m.get("map")), None)
    if not info:
        return ""
    lat, lon = info["center"]
    partial = ("" if info["fetched"] == info["expected"] else
               f" &middot; {info['expected'] - info['fetched']} tile(s) missing")
    note = _info(
        f"Derived artifact — not device data. The imagery is {info['fetched']} map tile(s) fetched "
        f"from the offline tile server you configured ({info['template']}) at zoom {info['zoom']}, "
        f"stitched together; only the marker position ({lat:.5f}, {lon:.5f}) comes from the "
        f"device (gallery.encrypteddb snap_location_table).")
    return (f"<div class='mapbox'><a href='{html.escape(media_prefix + info['path'])}' "
            f"target='_blank' title='open the full-size map'>"
            f"<img src='{html.escape(media_prefix + info['path'])}' loading='lazy'></a>"
            f"<div class='mapcap'>offline tile server &middot; zoom {info['zoom']}{partial}{note}"
            f"<br><a href='{html.escape(info['viewer'])}' target='_blank'>open on the tile "
            f"server</a></div></div>")


def _dedup_media(members):
    """Union of all members' media files, de-duplicated by content hash. Members of a group share
    the same ZMEDIAID, so the same media recovered under two snaps is the same bytes."""
    files, seen = [], set()
    for m in members:
        for f in m["media_files"]:
            hs = f.get("hashes") or [("", f.get("out", ""), "")]
            keyid = hs[0][1] or f.get("out")
            if keyid in seen:
                continue
            seen.add(keyid)
            files.append(f)
    return files


def _enc_html(members):
    """Encryption block. Normally one shared key/IV per ZMEDIAID group, but guard the rare case
    where grouped memories carry different keys by listing them per memory."""
    keyed = [m for m in members if m["key"] and m["iv"]]
    distinct = {(m["key"], m["iv"]) for m in keyed}
    if len(distinct) == 1:
        m = keyed[0]
        return (f"<div class='k'>Key (AES-256)</div><div class='v hex'>{m['key'].hex()}</div>"
                f"<div class='k'>IV</div><div class='v hex'>{m['iv'].hex()}</div>")
    if distinct:                                           # more than one key across the group
        rows = []
        for m in members:
            if m["key"] and m["iv"]:
                rows.append(f"<div class='k'>{html.escape(m['snap_id'][:8])}… key</div>"
                            f"<div class='v hex'>{m['key'].hex()}</div>"
                            f"<div class='k'>{html.escape(m['snap_id'][:8])}… IV</div>"
                            f"<div class='v hex'>{m['iv'].hex()}</div>")
        return "".join(rows)
    if any(m["is_meo"] for m in members):
        return "<div class='v muted'>My Eyes Only — key not unwrapped (persistedkey required)</div>"
    return "<div class='v muted'>key not available</div>"


# --------------------------------------------------------------------------- shared assets

# Interrogation-mark popover behaviour, shared by the index and every detail sub-page.
_HINT_JS = report_ui.HINT_JS

# The detail sub-page's selection bar (the index has the full toolbar).
_SUBSEL_CSS = """
 .subsel{display:flex;align-items:center;gap:10px;margin:10px 24px 0;font-size:12.5px}
 .subsel button{font-size:12.5px;padding:5px 9px;border:1px solid #bcbcd0;border-radius:5px;
   background:#fff;cursor:pointer;font-weight:600;color:#2d2d71}
 .subsel button:hover{background:#e7e7f4}
 .subsel .selhint{color:#999}
"""

# Offline map imagery on the detail sub-pages (only rendered when a tile server is configured).
_MAP_CSS = """
 .mapbox{margin-top:8px}
 .mapbox img{width:100%;max-width:330px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.25);display:block}
 .mapcap{font-size:10.5px;color:#777;margin-top:3px;max-width:330px}
 .mapcap a{color:#2d2d71}
"""

# Index-table geometry (the virtual table uses one fixed row height and one column track list for
# the header and every row; the thumbnail column sets the height).
MEM_COLS = "86px 78px 118px 236px 152px 288px 128px 144px 116px"
MEM_ROW_H = 130

# Styling shared by the detail sub-pages (single-braced: inserted as a value into the f-string).
_BASE_CSS = """
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f4f8;color:#1b1b1f}
 header{background:#2d2d71;color:#fff;padding:16px 24px}
 header h1{margin:0;font-size:19px} .sum{opacity:.85;font-size:13px;margin-top:4px}
 a.back{display:inline-block;margin:14px 24px 0;color:#2d2d71;font-weight:600;text-decoration:none;font-size:13px}
 a.back:hover{text-decoration:underline}
 .warn{background:#ffe8e8;border:1px solid #e0a0a0;color:#7a1f1f;padding:10px 24px;font-size:13px}
 .warn .sub{color:#9a5555} .warn code{font-family:ui-monospace,Consolas,monospace;font-size:12px}
 .detailwrap{display:grid;grid-template-columns:190px 1fr;gap:16px;padding:16px 24px;align-items:start}
 .detailwrap .thumbcol img{max-width:170px;max-height:300px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.25)}
 .noimg{width:150px;height:120px;display:flex;align-items:center;justify-content:center;background:#e6e6ee;color:#888;border-radius:6px;font-size:12px}
 .gencap{font-size:10.5px;color:#8a1f1f;margin-top:3px;max-width:170px}
 .kind{font-weight:600;font-size:16px} .mono{font-family:ui-monospace,Consolas,monospace}
 .idband{margin-top:8px}
 .idband .idrow{display:flex;align-items:baseline;gap:8px;margin:3px 0}
 .idband .idlab{color:#666;font-weight:700;text-transform:uppercase;font-size:10px;letter-spacing:.04em;min-width:74px}
 .idband .idval{font-family:ui-monospace,Consolas,monospace;font-size:14px;font-weight:700;color:#1b1b1f;overflow-wrap:anywhere}
 .sect{margin-top:12px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#2d2d71;font-weight:700;border-bottom:1px solid #e2e2ee;padding-bottom:2px}
 .grid{display:grid;grid-template-columns:auto 1fr;gap:2px 14px;font-size:12.5px;margin-top:4px}
 .grid .k{color:#666} .grid .v{color:#1b1b1f;word-break:break-word}
 .v.url{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#33367a}
 .v.hex{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#7a1f5a;overflow-wrap:anywhere}
 .grid .v .col{color:#aaa;font-family:ui-monospace,Consolas,monospace;font-size:10px;margin-left:8px}
 .geo{margin-top:4px;font-size:13px}
 table.files{border-collapse:collapse;margin-top:6px;font-size:12px;width:100%}
 table.files th{background:#2d2d71;color:#fff;text-align:left;padding:4px 8px;font-weight:600}
 table.files td{border:1px solid #e0e0e8;padding:4px 8px;vertical-align:top}
 table.files td.hash{font-family:ui-monospace,Consolas,monospace;font-size:10px;color:#555;white-space:nowrap}
 table.files td.hash .hl{color:#2d2d71;font-weight:700} table.files td.hash .pl{color:#8a1f5a}
 table.files td.hash .hgap{height:5px}
 table.files td.path{font-family:ui-monospace,Consolas,monospace;font-size:10.5px;color:#555;max-width:460px;overflow-wrap:anywhere}
 .muted{color:#999} .meo{background:#8a1f1f;color:#fff;padding:1px 6px;border-radius:4px;font-size:11px}
 a.cclink{color:#2d2d71;text-decoration:none;font-size:10.5px;white-space:nowrap} a.cclink:hover{text-decoration:underline}
 .xscope{background:#fff3d6;color:#8a5a00;border:1px solid #e6c983;border-radius:8px;padding:0 6px;font-size:10px;white-space:nowrap}
 /* partially cached media — the file is genuine but not the whole media */
 .partial{background:#fde3e3;color:#8a1f1f;border:1px solid #eeacac;border-radius:8px;padding:0 6px;
   font-size:10px;font-weight:700;white-space:nowrap}
 .unverified{background:#eef0f6;color:#666;border:1px solid #d3d7e4;border-radius:8px;padding:0 6px;
   font-size:10px;white-space:nowrap}
 .partialbar{background:#fdeeee;border:1px solid #edb8b8;border-left:4px solid #b03535;color:#6d1b1b;
   border-radius:5px;padding:8px 12px;margin:8px 0;font-size:12px;line-height:1.5}
 .partialcap{color:#b03535;font-weight:700}
 tr.partialrow td{background:#fff8f8}
 .hint{position:relative;display:inline-block}
 .qm{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;border-radius:50%;
   background:#c9cdf0;color:#25348a;font-size:10px;font-weight:700;cursor:pointer;margin:0 4px;user-select:none;vertical-align:middle}
 .qm:hover{background:#2d2d71;color:#fff}
 .tip{display:none;position:absolute;left:20px;top:-4px;z-index:30;background:#1f1f52;color:#fff;padding:8px 11px;
   border-radius:6px;font-size:11.5px;font-weight:400;width:340px;box-shadow:0 3px 10px rgba(0,0,0,.35);line-height:1.45;text-transform:none;letter-spacing:normal;white-space:normal;text-align:left}
 .hint.open .tip{display:block}
 .sharebar{background:#eef0ff;border:1px solid #c9cdf0;color:#2d2d71;padding:6px 10px;border-radius:5px;font-size:12.5px;font-weight:600;margin-bottom:10px}
 .mem{padding:8px 0;border-left:2px solid #e2e2ee;padding-left:10px;margin-top:8px}
 .mem + .mem{border-top:1px dashed #cfcfe0;margin-top:8px}
 .mem .snapid{font-family:ui-monospace,Consolas,monospace;font-size:13.5px;font-weight:700;color:#1b1b1f;overflow-wrap:anywhere}
 .mem .snaplab{color:#666;font-weight:700;text-transform:uppercase;font-size:10px;letter-spacing:.04em;margin-right:8px}
 .tswrap{overflow-x:auto;margin-top:4px}
 table.ts{border-collapse:collapse;font-size:11.5px;width:auto;min-width:100%}
 table.ts th{background:#efeff7;color:#2d2d71;text-align:left;padding:3px 8px;font-weight:600;white-space:nowrap;vertical-align:bottom}
 table.ts td{border:1px solid #e0e0e8;padding:3px 8px;white-space:nowrap}
 table.ts th.rid{font-family:ui-monospace,Consolas,monospace;font-weight:400;color:#555;position:sticky;left:0;background:#efeff7}
 table.ts .col{color:#aaa;font-family:ui-monospace,Consolas,monospace;font-size:9.5px;font-weight:400}
 table.ts td.tsmatch{background:#e7f6ea} table.ts td.tsuniq{background:#fdf0dc}
 .tslegend{font-size:10.5px;color:#777;margin-top:3px}
 .tslegend .sw{display:inline-block;width:10px;height:10px;border:1px solid #ccc;border-radius:2px;vertical-align:middle;margin-right:3px}
 .tslegend .sw.tsmatch{background:#e7f6ea} .tslegend .sw.tsuniq{background:#fdf0dc}
 .shared2,.cols2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 26px;align-items:start}
 .shared2 .c,.cols2 .c{min-width:0}
 @media(max-width:900px){.shared2,.cols2,.detailwrap{grid-template-columns:1fr}}
"""


# --------------------------------------------------------------------------- grouping

def assign_groups(memories):
    """Group memories by two merge relations, via union-find:

    1. shared ``ZMEDIAID`` (the same media object), and
    2. shared **non-zero** media MD5 (identical recovered bytes) — matched **across users**, so the
       same picture saved on two accounts lands on one page. Zero-byte files are excluded (they
       would all collapse together).

    Returns ``(groups, snap_to_key)``: ``groups`` is ``[(key, [members...])]`` ordered by earliest
    creation, ``snap_to_key`` maps each ``snap_id`` to its group key (a short, stable hash).
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:                               # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for m in memories.values():
        find(m["snap_id"])

    for attr, getter in (("mediaid", lambda m: m["ids"].get("ZMEDIAID")),):
        buckets = {}
        for m in memories.values():
            v = getter(m)
            if v:
                buckets.setdefault(v, []).append(m["snap_id"])
        for sids in buckets.values():
            for s in sids[1:]:
                union(sids[0], s)

    by_hash = {}
    for m in memories.values():
        for f in m["media_files"]:
            if f.get("bytes", 0) <= 0:
                continue
            hs = f.get("hashes") or []
            md5 = hs[0][1] if hs else None
            if md5:
                by_hash.setdefault(md5, set()).add(m["snap_id"])
    for sids in by_hash.values():
        sids = list(sids)
        for s in sids[1:]:
            union(sids[0], s)

    comps = {}
    for m in memories.values():
        comps.setdefault(find(m["snap_id"]), []).append(m)

    groups, snap_to_key = [], {}
    for members in comps.values():
        members.sort(key=lambda m: (m["created_sort"], m["snap_id"]))
        key = hashlib.md5("|".join(sorted(x["snap_id"] for x in members)).encode()).hexdigest()[:12]
        groups.append((key, members))
        for m in members:
            snap_to_key[m["snap_id"]] = key
    groups.sort(key=lambda kv: (min(m["created_sort"] for m in kv[1]), kv[0]))
    return groups, snap_to_key


def _primary_media(m):
    """The largest non-zero recovered media file for a memory (for hash/thumbnail columns)."""
    cands = [f for f in m["media_files"] if f.get("bytes", 0) > 0]
    return max(cands, key=lambda f: f["bytes"]) if cands else None


# --------------------------------------------------------------------------- detail sub-page

def _render_group_detail(members, keychain_available, snap_tcols, entry_tcols,
                         src_root, manifest, userids, media_prefix="../", cc_prefix="../../"):
    """Return the detail body HTML for one group (thumbnail + all blocks), for a sub-page.

    ``media_prefix`` prefixes links to ``media/`` and ``cc_prefix`` prefixes links to the sibling
    CacheController report, since sub-pages live one level deeper (``Memories/pages/``).
    """
    files = _dedup_media(members)
    still = _best_still(files)
    if still:
        cap = ("<div class='gencap'>▶ poster generated from video</div>"
               if still.get("generated") else "")
        if still.get("complete") is False:
            cap += "<div class='gencap partialcap'>⚠ from partially cached media</div>"
        thumb_html = (f'<a href="{media_prefix}{html.escape(still["path"])}" target="_blank">'
                      f'<img src="{media_prefix}{html.escape(still["path"])}" loading="lazy"></a>{cap}')
    else:
        thumb_html = '<div class="noimg">no cached media</div>'

    single = len(members) == 1
    lead = members[0]
    is_video = lead["media_type"] == 1
    kind = "🎬 Video" if is_video else "🖼️ Image"
    if is_video and not any(f["ext"] == "mp4" for f in files):
        kind += " <span class='muted'>(preview only — full video not cached)</span>"
    meo = ' <span class="meo">My Eyes Only</span>' if any(m["is_meo"] for m in members) else ""

    # An incomplete file is still evidence, but the examiner has to know the media in front of them
    # is not the whole media before they draw anything from what it does or does not show.
    n_partial = sum(1 for f in files if f.get("complete") is False)
    partial_banner = ("" if not n_partial else
                      f"<div class='partialbar'>⚠ <b>{n_partial} of {len(files)}</b> recovered "
                      "media file(s) below are <b>incomplete</b>: the device cached only part of "
                      "the original, so what plays or displays here stops short of — or breaks up "
                      "part way through — the real media. The bytes present are genuine and "
                      "correctly decrypted. Each file's <i>Source cache</i> cell says exactly what "
                      "is missing.</div>")

    # prominent MEDIA ID (shared) + snap count
    media_id = lead["ids"].get("ZMEDIAID")
    idrows = []
    if media_id:
        idrows.append(f"<div class='idrow'><span class='idlab'>Media ID</span> "
                      f"<span class='idval'>{html.escape(str(media_id))}</span></div>")
    idrows.append(f"<div class='idrow'><span class='idlab'>Memories</span> "
                  f"<span class='idval'>{len(members)}</span></div>")
    idband = f"<div class='idband'>{''.join(idrows)}</div>"
    sharebar = ("" if single else
                f"<div class='sharebar'>🔗 {len(members)} memories are grouped here (same ZMEDIAID "
                "and/or identical media bytes) — media-level details are shown once below</div>")

    meta, meta_shared = _shared_or_per(members, _meta_grid)
    loc, loc_shared = _shared_location(members, keychain_available)
    varies = "<div class='v muted'>varies per snap (see below)</div>"

    left = f"<div class='sect'>Metadata</div><div class='grid'>{meta if meta_shared else varies}</div>"
    loc_block = (f"<div class='sect'>Location (gallery.encrypteddb)</div>"
                 f"<div class='geo'>📍 {loc}</div>{_map_html(members, media_prefix)}"
                 if loc_shared else
                 f"<div class='sect'>Location (gallery.encrypteddb)</div>{varies}")
    enc_block = (f"<div class='sect'>Encryption (per-snap AES key)</div>"
                 f"<div class='grid'>{_enc_html(members)}</div>")
    shared_html = (f"<div class='shared2'><div class='c'>{left}</div>"
                   f"<div class='c'>{loc_block}{enc_block}</div></div>")

    mem_blocks = []
    for idx, m in enumerate(members):
        uid = userids.get(m["user_hash"]) or ("userHash " + m["user_hash"][:12] + "…")
        parts = [f"<div id='mem-{html.escape(m['snap_id'])}'>"
                 f"<span class='snaplab'>Snap ID</span> "
                 f"<span class='snapid'>{html.escape(m['snap_id'])}</span></div>"
                 f"<div class='mono' style='font-size:11px;color:#666;margin:2px 0 4px'>user {html.escape(str(uid))}</div>",
                 f"<div class='cols2'>"
                 f"<div class='c'><div class='sect'>ZGALLERYSNAP values</div>"
                 f"<div class='grid'>{_snap_values_grid(m)}</div></div>"
                 f"<div class='c'><div class='sect'>ZGALLERYENTRY values</div>"
                 f"<div class='grid'>{_entry_values_grid(m)}</div></div></div>",
                 f"<div class='sect'>CDN URLs (scdb-27)</div><div class='grid'>{_url_grid(m)}</div>"]
        if not meta_shared:
            parts.append(f"<div class='sect'>Metadata</div><div class='grid'>{meta[idx]}</div>")
        if not loc_shared:
            parts.append(f"<div class='sect'>Location (gallery.encrypteddb)</div>"
                         f"<div class='geo'>📍 {loc[idx]}</div>{_map_html([m], media_prefix)}")
        # the examiner's own selection — the same checkbox as the index row, same stored state
        parts.append(
            f"<label class='selrow' title='Mark this Memory as relevant. Shared with the Memories "
            f"index; saved in this browser, and exportable from the index toolbar.'>"
            f"<input type='checkbox' class='selbox' data-kind='mem' "
            f"data-id='mem-{html.escape(m['snap_id'])}'>Selected for the case</label>")
        mem_blocks.append("<div class='mem'>" + "".join(parts) + "</div>")

    frows = []
    for f in sorted(files, key=lambda f: (f["source"], -f["bytes"])):
        srcs = _render_src_paths(f, src_root, manifest)
        blocks = []
        for label, md5, sha256 in f.get("hashes", []):
            tag = f" <span class='pl'>({html.escape(label)})</span>" if label else ""
            blocks.append(f"<span class='hl'>MD5</span>{tag} {md5}<br>"
                          f"<span class='hl'>SHA-256</span>{tag} {sha256}")
        hashes = "<div class='hgap'></div>".join(blocks)
        dim = f.get("dim") or f.get("snap_dim") or ""
        source_cell = html.escape(f["source"]) + _info(f.get("how")) + _partial_badge(f)
        if f.get("in_cc") and f.get("cache_key"):
            source_cell += (f" <a class='cclink' target='scauto_cache' "
                            f"href=\"{cc_prefix}CacheController/CacheController_report.html#ck-"
                            f"{html.escape(f['cache_key'])}\">🗄 cache entry</a>")
        if f.get("cross_scope"):
            source_cell += (" <span class='xscope'>⚠ cross-scope copy</span>"
                            + _info(_cross_scope_note(f)))
        frows.append(
            f"<tr{' class=partialrow' if f.get('complete') is False else ''}>"
            f"<td>{html.escape(f['role'])}</td><td>{source_cell}</td>"
            f"<td>{f['ext']}</td><td>{html.escape(dim)}</td>"
            f"<td>{f['bytes']//1024} KB</td>"
            f"<td><a href=\"{media_prefix}{html.escape(f['path'])}\" target=\"_blank\">open</a></td>"
            f"<td class='hash'>{hashes}</td>"
            f"<td class='path'>{srcs}</td></tr>")
    files_table = ("<table class='files'><tr><th>Role</th><th>Source cache</th><th>Type</th>"
                   "<th>Dimensions</th><th>Size</th><th>File</th><th>Hashes (MD5 / SHA-256)</th>"
                   "<th>Source path(s) in extraction</th></tr>"
                   + "".join(frows) + "</table>") if frows else "<div class='muted'>no cached media recovered</div>"

    return f"""
      <div class="detailwrap">
        <div class="thumbcol">{thumb_html}</div>
        <div class="detailcol">
          {sharebar}
          <div class="kind">{kind}{meo}</div>
          {partial_banner}
          {idband}
          {shared_html}
          {''.join(mem_blocks)}
          <div class="sect">Timestamps — Snap (ZGALLERYSNAP)</div>{_ts_table(members, snap_tcols, "times", SNAP_TIME_LABELS, single)}
          <div class="sect">Timestamps — Entry / album (ZGALLERYENTRY)</div>{_ts_table(members, entry_tcols, "entry_times", ENTRY_TIME_LABELS, single)}
          <div class="sect">Media files</div>{files_table}
        </div>
      </div>"""


def render_subpage(key, members, pages_dir, keychain_available, snap_tcols, entry_tcols,
                   src_root, manifest, userids, tz_label, run_id="default"):
    """Write ``pages/<key>.html`` for one group and return its path relative to the Memories dir."""
    lead = members[0]
    body = _render_group_detail(members, keychain_available, snap_tcols, entry_tcols,
                                src_root, manifest, userids)
    back = (f'<a class="back" href="../Memories_report.html#mem-{html.escape(lead["snap_id"])}">'
            '← Back to Memories index</a>')
    # The selection controls: the same store as the index (both load ../selection.js), saved back
    # to a file the examiner keeps — see report_ui.SELECT_JS for why that is the durable route.
    selbar = ('<div class="subsel"><button onclick="scSelSave()" title="Download selection.js — '
              'put it next to the reports so every report of this run loads it">💾 Save selections'
              '</button><span class="selnote" id="selnote"></span>'
              '<span class="selhint">ticking a Memory below marks it for the case; it is shared '
              'with the Memories index</span></div>')
    doc = (f'<!doctype html><html><head><meta charset="utf-8">'
           f'<title>Memory {html.escape(lead["snap_id"][:8])}…</title>'
           f'<style>{_BASE_CSS}{report_ui.NAV_CSS}{report_ui.SELECT_CSS}{_MAP_CSS}{_SUBSEL_CSS}</style>'
           f'<script>window.SCAUTO_RUN={json.dumps(run_id)};window.SCAUTO_SELKIND="mem";</script>'
           f'<script>{report_ui.SELECT_JS}</script>'
           f'<script src="../../selection.js"></script></head><body>'
           f'<header><h1>Snapchat Memory detail</h1>'
           f'<div class="sum">Group of {len(members)} memory(ies) &middot; times in {html.escape(tz_label)}</div></header>'
           f'{back}{selbar}{body}<script>{_HINT_JS}{report_ui.NAV_JS}'
           f'{report_ui.SELECT_TOOLBAR_JS}'
           f'scSyncBoxes();scSelNote();SCSel.onChange(function(){{scSyncBoxes();scSelNote();}});'
           f'scConsumeHash();</script></body></html>')
    os.makedirs(pages_dir, exist_ok=True)
    with open(os.path.join(pages_dir, f"{key}.html"), "w", encoding="utf-8") as fh:
        fh.write(doc)
    return f"pages/{key}.html"


# --------------------------------------------------------------------------- index page

def _geo_compact(m):
    """Short geolocation cell for the index: coords + OpenStreetMap and Google Maps links.

    (A tile-server link can be added here later when an offline tile server is configured.)
    """
    if m["latitude"] is not None:
        lat, lon = m["latitude"], m["longitude"]
        return (f'{lat:.5f}, {lon:.5f}<br>'
                f'<a href="https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}" '
                f'target="_blank">OSM</a> &middot; '
                f'<a href="https://www.google.com/maps?q={lat},{lon}" target="_blank">Google</a>')
    if m["has_location"]:
        return '<span class="muted">on-device</span>'
    return '<span class="muted">—</span>'


def _cache_tokens(m):
    """Distinct cache-file tokens (CACHE_KEY / item hashes) for a memory's recovered media."""
    toks = []
    for f in m["media_files"]:
        for t in (f.get("cache_key"), f.get("item")):
            if t and t not in toks:
                toks.append(t)
    return toks


def render_maps(memories, outdir, tile_server):
    """Render a small static map for every geolocated Memory, from the examiner's tile server.

    Does nothing unless ``tile_server`` is set — a forensic report never reaches out on its own.
    Memories at the same coordinates share one image. Each memory gets ``m["map"]`` = the render
    details (path, zoom, how many tiles came back, a link to the server at those coordinates), which
    the detail page shows, clearly labelled as imagery from the examiner's server rather than
    device data.
    """
    for m in memories.values():
        m["map"] = None
    if not tile_server:
        return 0
    fetcher = offline_maps.TileFetcher(tile_server)
    if not fetcher.ok():
        logger.warning(f"Unusable map tile server '{tile_server}' — no maps will be rendered")
        return 0
    maps_dir = os.path.join(outdir, "maps")
    os.makedirs(maps_dir, exist_ok=True)
    by_coord, made = {}, 0
    for sid, m in sorted(memories.items()):
        if m["latitude"] is None:
            continue
        key = (round(m["latitude"], 5), round(m["longitude"], 5))
        if key in by_coord:
            m["map"] = by_coord[key]
            continue
        name = f"{key[0]:.5f}_{key[1]:.5f}.png".replace("-", "m")
        info = fetcher.static_map(m["latitude"], m["longitude"], os.path.join(maps_dir, name))
        if not info:
            continue
        info["path"] = "maps/" + name
        by_coord[key] = info
        m["map"] = info
        made += 1
    if made:
        logger.info(f"Rendered {made} offline map(s) from {fetcher.template} "
                    f"({len(fetcher.cache)} tiles fetched)")
    else:
        logger.warning(f"No map could be rendered from {fetcher.template}")
    return made


def write_media_manifest(memories, outdir):
    """Write ``media_by_cache_key.json``: CACHE_KEY -> the decrypted media file(s) recovered from it.

    Memory media is stored **encrypted** in the SCContent cache, so the cache_controller report
    cannot display those bytes; this manifest lets it link to the plaintext copy decrypted here
    instead of leaving the examiner with an unopenable blob. Only files that came from a cache key
    are listed (``caching-media`` packs are not indexed by ``cache_controller.db``).
    """
    out = {}
    for sid, m in memories.items():
        for f in m["media_files"]:
            key = f.get("cache_key")
            if not key or f.get("generated"):
                continue
            hashes = f.get("hashes") or [("", "", "")]
            out.setdefault(key.lower(), []).append({
                "path": f["path"], "role": f.get("role", ""), "ext": f.get("ext", ""),
                "bytes": f.get("bytes", 0), "snap_id": sid,
                "md5": hashes[0][1], "sha256": hashes[0][2],
                # so the cache_controller report can repeat the warning next to the same bytes
                "complete": f.get("complete"), "why_incomplete": f.get("why_incomplete", ""),
            })
    try:
        with open(os.path.join(outdir, "media_by_cache_key.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh)
    except Exception as error:
        logger.debug(f"Could not write media_by_cache_key.json: {error}")
    return out


def generate_report(memories, outdir, keychain_available, userids=None, tz_label="UTC",
                    src_root=None, manifest=None, run_id="default", keychain_note=""):
    """Write the lightweight index (``Memories_report.html``) plus one detail sub-page per group.

    Also writes ``memory_pages.json`` (snap_id -> sub-page path) so the cache_controller report can
    link straight to a memory's detail page. Returns (index_path, linked, located).
    """
    userids = userids or {}
    os.makedirs(outdir, exist_ok=True)
    total = len(memories)
    linked = sum(1 for m in memories.values() if m["media_files"])
    located = sum(1 for m in memories.values() if m["latitude"] is not None)
    n_partial = sum(1 for m in memories.values()
                    if any(f.get("complete") is False for f in m["media_files"]))

    snap_tcols = _union_cols(memories.values(), "times")
    entry_tcols = _union_cols(memories.values(), "entry_times")

    groups, snap_to_key = assign_groups(memories)
    pages_dir = os.path.join(outdir, "pages")
    for key, members in groups:
        render_subpage(key, members, pages_dir, keychain_available, snap_tcols, entry_tcols,
                       src_root, manifest, userids, tz_label, run_id)

    # manifest for the cache_controller report's direct-to-detail links
    page_manifest = {m["snap_id"]: f"pages/{key}.html" for key, members in groups for m in members}
    try:
        with open(os.path.join(outdir, "memory_pages.json"), "w", encoding="utf-8") as fh:
            json.dump(page_manifest, fh)
    except Exception as error:
        logger.debug(f"Could not write memory_pages.json: {error}")

    write_media_manifest(memories, outdir)

    # One row per memory, ordered by group then creation. Rows live in data/index.js and are drawn
    # by the virtual table (scripts/report_ui.py), so the index opens instantly whatever its size.
    rows = []
    n_users = len({m["user_hash"] for m in memories.values()})
    for key, members in groups:
        for m in members:
            uid = userids.get(m["user_hash"]) or ("userHash " + m["user_hash"][:10] + "…")
            own = m["media_files"] or _dedup_media(members)
            still = _best_still(m["media_files"]) or _best_still(_dedup_media(members))
            has_img = bool(still)
            n_part = sum(1 for f in own if f.get("complete") is False)
            page_href = f"pages/{key}.html#mem-{html.escape(m['snap_id'])}"
            if still:
                thumb = (f'<a href="{page_href}"><img src="{html.escape(still["path"])}" '
                         f'loading="lazy"></a>')
            else:
                thumb = '<span class="nothumb">—</span>'
            prim = _primary_media(m)
            md5 = prim["hashes"][0][1] if prim and prim.get("hashes") else ""
            sha = prim["hashes"][0][2] if prim and prim.get("hashes") else ""
            zsnap = m["snap_id"]
            zentry = m["entry_other"].get("ZENTRYID") or ""
            zmedia = m["ids"].get("ZMEDIAID") or ""
            is_meo = bool(m["is_meo"])
            tokens = _cache_tokens(m)
            # the index row is one fixed-height line-up: show the first two tokens and count the
            # rest (the detail page lists them all)
            toks = "<br>".join(html.escape(t) for t in tokens[:2])
            if len(tokens) > 2:
                toks += f"<br><span class='muted'>+{len(tokens) - 2} more</span>"
            is_video = m["media_type"] == 1
            kind = "🎬" if is_video else "🖼️"
            if is_meo:                                     # My Eyes Only — the private album
                kind += "<div class='meo' title='My Eyes Only'>MEO</div>"
            if n_part:                                     # only part of the media is on the device
                kind += (f"<div class='part' title='{n_part} recovered media file(s) are "
                         f"incomplete — the cache holds only part of the original'>PART</div>")
            # cells stay as markup-free as possible — per-column styling lives in the CSS (.vc.cN),
            # since every byte here is multiplied by the number of memories in data/index.js
            cells = [
                thumb,
                kind,
                html.escape(str(uid)),
                f"<div><i>ZMEDIAID</i> {html.escape(str(zmedia))}</div>"
                f"<div><i>ZSNAPID</i> {html.escape(zsnap)}</div>"
                f"<div><i>ZENTRYID</i> {html.escape(str(zentry))}</div>",
                toks,
                f"<i>MD5</i> {html.escape(md5)}<br><i>SHA-256</i> {html.escape(sha)}",
                html.escape(m["create_utc"]),
                _geo_compact(m),
                f"<a class='openbtn' target='scauto_memory_page' "
                f"title='open this memory in its own tab' href='{page_href}'>open ▸</a>"
                f" <span class='nsnaps'>{len(members)} snap{'s' if len(members) != 1 else ''}</span>",
            ]
            # `urls` holds every CDN URL of the memory (media / overlay / thumbnail, download and
            # redirect), so the index is searchable by a full or partial URL — the cache tokens
            # alone only match the last path segment.
            searchable = ([zsnap, str(zentry), str(zmedia), str(uid), md5, sha, m["create_utc"]]
                          + list(tokens) + list(dict.fromkeys(m["urls"].values())))
            if is_meo:
                searchable.append("meo my eyes only")
            if n_part:
                searchable.append("incomplete partial partially cached truncated")
            if m["latitude"] is not None:
                searchable.append(f"{m['latitude']:.5f}, {m['longitude']:.5f}")
            rows.append([
                f"mem-{zsnap}", cells,
                " ".join(s for s in searchable if s).lower(),
                {"1": ("video" if is_video else "image") + ("+meo" if is_meo else ""),
                 "2": str(uid), "3": f"{zmedia}|{zsnap}", "6": m["created_sort"]},
                None,
                {"user": str(uid), "img": "y" if has_img else "n",
                 "meo": "y" if is_meo else "n", "part": "y" if n_part else "n"},
            ])
    report_ui.write_rows(os.path.join(outdir, "data"), rows)

    user_opts = "".join(f"<option value='{html.escape(u)}'>{html.escape(u)}</option>"
                        for u in sorted({(userids.get(m['user_hash']) or ('userHash ' + m['user_hash'][:10] + '…'))
                                         for m in memories.values()}))

    # The banner names the cause (`keychain_note`, from read_keychain_status) and then its
    # consequences. The last sentence is there because the two are easily confused: My Eyes Only
    # memories on the new schema carry their key in scdb and show up with no keychain at all, so
    # seeing MEO media is not evidence that the keychain was read.
    banner = "" if keychain_available else (
        '<div class="warn">'
        + html.escape(keychain_note or "No usable keychain (egocipher) was available.")
        + ' Geolocation cannot be recovered, and on the old schema neither My Eyes Only nor '
          'regular memory imagery can be decrypted. <span class="sub">New-schema memories '
          '(including My Eyes Only) carry their key in <code>scdb</code> and are unaffected.'
          '</span></div>')

    index_css = """
 .toolbar{background:#ececf4;border-bottom:1px solid #d7d7e2;padding:10px 24px;
   display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:13px}
 .toolbar input,.toolbar select{font-size:13px;padding:5px 8px;border:1px solid #bcbcd0;border-radius:5px}
 .toolbar input[type=search]{min-width:280px} .toolbar label{color:#555;font-weight:600}
 .vcells>.vc{font-size:12px}
 .vc img{max-width:74px;max-height:118px;border-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,.25)}
 .nothumb{color:#bbb} .mono{font-family:ui-monospace,Consolas,monospace;font-size:11px}
 /* per-column styling for the index rows (keeps the row data in data/index.js markup-free) */
 .vcells>.vc.c1{font-size:16px;line-height:1.1}
 .vcells>.vc.c1 .meo{background:#8a1f1f;color:#fff;border-radius:3px;font-size:9px;font-weight:700;
   letter-spacing:.04em;padding:1px 4px;margin-top:3px;display:inline-block}
 .vcells>.vc.c1 .part{background:#fde3e3;color:#8a1f1f;border:1px solid #eeacac;border-radius:3px;
   font-size:9px;font-weight:700;letter-spacing:.04em;padding:0 4px;margin-top:3px;display:inline-block}
 .vcells>.vc.c2,.vcells>.vc.c3,.vcells>.vc.c4,.vcells>.vc.c5{
   font-family:ui-monospace,Consolas,monospace;font-size:11px;overflow-wrap:anywhere}
 .vcells>.vc.c3{color:#33367a} .vcells>.vc.c3 div{margin:1px 0}
 .vcells>.vc.c3 i{color:#8a8aa0;font-weight:700;font-size:9px;letter-spacing:.03em;
   margin-right:5px;font-style:normal}
 .vcells>.vc.c4,.vcells>.vc.c5{color:#555}
 .vcells>.vc.c5 i{color:#2d2d71;font-weight:700;font-style:normal}
 a.detail{color:#2d2d71;font-weight:600;text-decoration:none;white-space:nowrap} a.detail:hover{text-decoration:underline}
 a.openbtn{display:inline-flex;align-items:center;gap:4px;text-decoration:none;font-weight:700;
   font-size:11px;color:#25348a;background:#e7ecff;border:1px solid #b9c3f0;border-radius:10px;
   padding:3px 9px;white-space:nowrap}
 a.openbtn:hover{background:#d5deff;border-color:#8f9fe0}
 .nsnaps{color:#888;font-size:10.5px;white-space:nowrap} .muted{color:#999}
"""

    doc = (f'<!doctype html><html><head><meta charset="utf-8"><title>Snapchat Memories</title>'
           f'<style>{_BASE_CSS}{index_css}{report_ui.VTABLE_CSS}{report_ui.NAV_CSS}'
           f'{report_ui.SELECT_CSS}</style>'
           f'<script>window.SCAUTO_RUN={json.dumps(run_id)};window.SCAUTO_SELKIND="mem";</script>'
           f'<script>{report_ui.SELECT_JS}</script>'
           f'<script src="../selection.js"></script>'
           f'<script>{report_ui.VTABLE_JS}</script></head><body>'
           f'<header><h1>Snapchat Memories — index</h1>'
           f'<div class="sum">{n_users} user profile(s) &middot; {total} memories &middot; {linked} with '
           f'recovered media &middot; {located} geolocated &middot; {len(groups)} group(s) &middot; '
           + (f'<b>{n_partial}</b> with incomplete media &middot; ' if n_partial else '')
           + f'times in <b>{html.escape(tz_label)}</b></div></header>'
           f'{banner}'
           f'{report_ui.missing_data_banner("Memories_report.html")}'
           f'<div class="stickytop"><div class="toolbar">'
           f'<input type="search" id="q" placeholder="Search IDs, hashes, tokens, URLs, user…" oninput="flt()">'
           f'<label>User <select id="user" onchange="flt()"><option value="">all</option>{user_opts}</select></label>'
           f'<label>Thumbnail <select id="img" onchange="flt()"><option value="">any</option>'
           f'<option value="y">with</option><option value="n">without</option></select></label>'
           f'<label title="My Eyes Only — Snapchat&#39;s private, separately-encrypted album">'
           f'My Eyes Only <select id="meo" onchange="flt()"><option value="">any</option>'
           f'<option value="y">only MEO</option><option value="n">exclude MEO</option>'
           f'</select></label>'
           f'<label title="Memories whose recovered media the device only cached part of — the '
           f'file is genuine but stops short of the real media">'
           f'Media <select id="part" onchange="flt()"><option value="">any</option>'
           f'<option value="y">incomplete only</option><option value="n">complete only</option>'
           f'</select></label>'
           f'<span id="count" style="color:#555"></span></div>'
           f'<div class="toolbar">{report_ui.selection_toolbar("memory")}</div>'
           f'<div class="pager" id="pager"></div>'
           f'<div class="vhdr" id="vhdr" style="grid-template-columns:30px {MEM_COLS}">'
           f'<div class="vc sel"><input type="checkbox" class="selall"'
           f' title="Select / unselect every memory matching the current filters"'
           f' onclick="SCV.selectShown(this.checked)"></div>'
           f'<div class="vc nosort">Thumb</div>'
           f'<div class="vc" onclick="SCV.setSort(1)">Kind <span class="ar">↕</span></div>'
           f'<div class="vc" onclick="SCV.setSort(2)">User <span class="ar">↕</span></div>'
           f'<div class="vc" onclick="SCV.setSort(3)">IDs (ZMEDIAID / ZSNAPID / ZENTRYID) <span class="ar">↕</span></div>'
           f'<div class="vc nosort">Cache tokens</div>'
           f'<div class="vc nosort">Media MD5 / SHA-256</div>'
           f'<div class="vc" onclick="SCV.setSort(6)">Created <span class="ar">↕</span></div>'
           f'<div class="vc nosort">Geolocation</div><div class="vc nosort">Detail</div></div></div>'
           f'<div class="vwrap" id="vwrap"><div class="vpad" id="vpad"></div>'
           f'<div class="vwin" id="vwin"></div></div>'
           f'<div class="vempty" id="vempty" style="display:none">No memory matches the current filters.</div>'
           f'<script src="data/index.js"></script>'
           f'<script>{_HINT_JS}{report_ui.NAV_JS}{report_ui.SELECT_TOOLBAR_JS}'
           'var flt_t=0;'
           'function flt(){clearTimeout(flt_t);flt_t=setTimeout(function(){SCV.refilter();},120);}'
           'SCV.init({mount:"vwrap",win:"vwin",pad:"vpad",header:"#vhdr",missing:"vmiss",'
           f'empty:"vempty",pager:"pager",pageSize:500,selKind:"mem",'
           f'rowHeight:{MEM_ROW_H},cols:"{MEM_COLS}",detailBase:null,'
           'query:function(){return document.getElementById("q").value;},'
           'match:function(m,r){var u=document.getElementById("user").value,'
           'im=document.getElementById("img").value,mo=document.getElementById("meo").value,'
           'pa=document.getElementById("part").value;'
           'return (!u||m.user===u)&&(!im||m.img===im)&&(!mo||m.meo===mo)&&(!pa||m.part===pa)'
           '&&(!document.getElementById("selonly").checked||SCSel.get("mem",r[0]));},'
           'selectedOnly:function(){return document.getElementById("selonly").checked;},'
           'selCount:function(n){document.getElementById("selcount").textContent=n+" selected";'
           'scSelNote();},'
           'count:function(n,t){document.getElementById("count").textContent='
           'n===t?(n+" memories"):(n+" of "+t+" shown");},'
           'reset:function(){document.getElementById("q").value="";'
           'document.getElementById("user").value="";document.getElementById("img").value="";'
           'document.getElementById("meo").value="";document.getElementById("part").value="";'
           'document.getElementById("selonly").checked=false;}});'
           'scSelNote();scConsumeHash();'
           '</script></body></html>')

    report = os.path.join(outdir, "Memories_report.html")
    with open(report, "w", encoding="utf-8") as f:
        f.write(doc)
    return report, linked, located


# --------------------------------------------------------------------------- entry

def main(app_or_root, keychain="", outdir=None, padding="both", tz="local", src_root=None,
         tile_server=""):
    """
    Build a Memories media report.

    app_or_root : Snapchat app-container path, or any extraction root containing it.
    keychain    : path to a keychain plist (optional; enables geolocation / old-schema / MEO).
    outdir      : output directory (default: ./Snapchat_Memories_report_<timestamp>).
    padding     : SCContent media hashes to report — 'both' (default: with and without PKCS#7
                  padding), 'strip' (only without), or 'keep' (only with). The saved file is the
                  padded bytes only when padding=='keep', otherwise the byte-exact stripped media.
    tz          : timezone for displayed timestamps — 'local' (default), 'utc', an IANA name
                  ('America/Toronto', DST-aware), or a fixed offset ('-04:00').
    src_root    : the extraction root the files were unzipped under. Source paths in the report
                  are shown relative to it (as they appear inside the extraction archive). When
                  omitted, paths fall back to anchoring on a known device-tree root.
    tile_server : URL of an **offline** XYZ map tile server the examiner runs (server root or a
                  ``{z}/{x}/{y}`` template). Only when given, each geolocated Memory's detail page
                  gets a small map rendered from it. Nothing is fetched otherwise.
    """
    app = find_app_container(app_or_root)
    # When no src_root is given (e.g. the standalone CLI), device_path() falls back to anchoring
    # on a known device-tree root, which handles both logical ("/Application/…") and
    # full-filesystem ("/private/var/mobile/…") layouts without guessing an archive root.
    # The extraction manifest (written by extract_zip) restores the ZIP prefix that was truncated
    # off during extraction, so source paths can show the full on-device path.
    manifest = load_path_manifest(src_root, app_or_root, app)
    outdir = outdir or ("./Snapchat_Memories_report_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    workdir = os.path.join(outdir, "_work")
    media_dir = os.path.join(outdir, "media")
    os.makedirs(media_dir, exist_ok=True)
    timefmt, tz_label = make_time_formatter(tz)

    # read_keychain_status never raises and logs every outcome (missing path, unparseable dump,
    # parsed-but-no-egocipher), so the banner below can name the actual cause instead of the
    # catch-all "no usable keychain" that used to cover all three.
    kc = _memkeys.read_keychain_status(keychain)
    egocipher, persisted = kc["egocipher"], kc["persistedkey"]
    keychain_available = bool(egocipher)

    profiles = find_profiles(app)
    logger.info(f"Found {len(profiles)} Snapchat profile(s) in {app}; timestamps in {tz_label}")

    all_memories = {}
    for p in profiles:
        mems, stats = load_memories(p, egocipher, persisted, workdir, timefmt)
        logger.info(f"  profile {p['userHash'][:12]}: {len(mems)} memories "
                    f"(schema={stats['schema']}, gallery_keys={stats['gallery_keys']}, "
                    f"locations={stats['locations']})")
        all_memories.update(mems)

    collect_media(all_memories, app, media_dir, padding)
    # media link paths are relative to Memories.html (which sits in outdir)
    for m in all_memories.values():
        for f in m["media_files"]:
            f["path"] = "media/" + f["out"]

    render_maps(all_memories, outdir, tile_server)

    reports_root = os.path.dirname(os.path.abspath(outdir))
    run = report_ui.run_id(reports_root)
    report_ui.write_selection_stub(reports_root, run)       # shared by every report of the run
    report, linked, located = generate_report(all_memories, outdir, keychain_available,
                                              userids=map_userids(app), tz_label=tz_label,
                                              src_root=src_root, manifest=manifest, run_id=run,
                                              keychain_note=kc["detail"])
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)

    logger.info(f"Memories report: {os.path.abspath(report)}")
    logger.info(f"  {len(all_memories)} memories, {linked} with media, {located} geolocated")
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    padding, tz, args = "both", "local", []
    it = iter(sys.argv[1:])
    for a in it:
        if a == "--padding":
            padding = next(it, "both")
        elif a == "--tz":
            tz = next(it, "local")
        else:
            args.append(a)
    if not args:
        print("usage: python -m scripts.memories_media_report "
              "<extraction_root_or_app_container> [keychain.plist] [outdir] "
              "[--padding both|strip|keep] [--tz local|utc|<IANA name>|<±HH:MM>]")
        sys.exit(1)
    main(args[0], args[1] if len(args) > 1 else "",
         args[2] if len(args) > 2 else None, padding=padding, tz=tz)
