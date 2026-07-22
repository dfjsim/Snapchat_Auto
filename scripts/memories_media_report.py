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
import shutil
import hashlib
import sqlite3
import logging
import subprocess
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


def _strip_pkcs7(data):
    """Remove PKCS#7 padding if present (SCContent media is CBC + PKCS#7)."""
    if data:
        n = data[-1]
        if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
            return data[:-n]
    return data


def decrypt_sccontent(raw, key, iv):
    """Decrypt an SCContent file. Returns (padded, stripped, ext) or (None, None, None).

    SCContent media is AES-256-CBC with PKCS#7 padding. ``padded`` is the raw CBC output (as some
    older decryptors emit it); ``stripped`` has the padding removed so it is byte-exact and its
    MD5/SHA-256 match current tools. They are equal when the file was already plaintext.
    """
    ext = guess_media(raw[:16])
    if ext:
        return raw, raw, ext                              # already plaintext, no padding
    if not key or len(key) != 32 or len(iv) != 16 or len(raw) % 16:
        return None, None, None
    plain = _aes_cbc(key, iv, raw)
    if guess_media(plain[:16]):
        return plain, _strip_pkcs7(plain), guess_media(plain[:16])
    if guess_media(plain[8:24]):                           # some have an 8-byte prefix
        body = plain[8:]
        return body, _strip_pkcs7(body), guess_media(plain[8:24])
    return None, None, None


def decrypt_pack(cipher, key, iv):
    """Decrypt a concatenated caching-media pack. Returns (bytes, ext) or (None, None)."""
    if not key or len(key) != 32 or len(iv) != 16:
        return None, None
    plain = _aes_cbc(key, iv, cipher)
    if plain[:4] == PACK_HEADER_MARKER:
        length = int.from_bytes(plain[4:8], "little")
        payload = plain[8:8 + length]
        ext = guess_media(payload[:16])
        if ext:
            return payload, ext
    # fallbacks (older/variant containers)
    ext = guess_media(plain[:16])
    if ext:
        return plain, ext
    ext = guess_media(plain[8:24])
    if ext:
        return plain[8:], ext
    return None, None


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


def _resolve_sccontent(cache_key, full, parts):
    """Resolve a cache key to (ciphertext_bytes, [full paths], [ordered part paths]) or (None,...).

    Prefers a full copy for the bytes; otherwise concatenates the byte-range parts (deduped by
    start offset). Full copies and parts are all returned so every on-disk copy shows as a source.
    """
    fulls = full.get(cache_key, [])
    ordered, seen_off = [], set()
    for off, p in sorted(parts.get(cache_key.lower(), [])):
        if off in seen_off:                                # e.g. a PREFETCH and a _0-1 both at 0
            continue
        seen_off.add(off)
        ordered.append(p)
    if fulls:
        cipher = open(fulls[0], "rb").read()
    elif ordered:
        cipher = _read_concat(ordered)
    else:
        return None, [], []
    return cipher, fulls, ordered


_UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


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


def _dims(path):
    try:
        with Image.open(path) as im:
            return f"{im.size[0]}×{im.size[1]}"
    except Exception:
        return ""


def _snap_dim(m):
    """ZGALLERYSNAP dimensions (ZWIDTH×ZHEIGHT) — used for videos, whose container PIL can't read."""
    return f"{m['width']}×{m['height']}" if m.get("width") and m.get("height") else ""


def generate_poster(video_path, out_path, at_seconds=1.0):
    """Extract a single poster frame from a video into out_path (JPEG). Returns True on success.

    The result is a DERIVED artifact (not original device data) — callers must label it as such.
    """
    for var in ("OPENCV_LOG_LEVEL", "OPENCV_FFMPEG_LOGLEVEL", "OPENCV_VIDEOIO_DEBUG"):
        os.environ.setdefault(var, "OFF" if "LOG_LEVEL" in var else "0")
    try:
        import cv2
    except Exception as error:
        logger.debug(f"cv2 unavailable, cannot generate poster: {error}")
        return False
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        if fps and frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(int(fps * at_seconds), max(int(frames) - 1, 0)))
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
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
    keyed = [(sid, m) for sid, m in memories.items() if m["key"] and m["iv"]]

    # --- SCContent (URL-addressed + cache_controller-addressed, whole or split into parts) ---
    for sid, m in keyed:
        targets = []                                       # (role, cache_key)
        for role, url in (("full", m["media_url"]), ("overlay", m["overlay_url"]),
                          ("thumbnail", m["thumb_url"])):
            tok = url_token(url)
            if tok:
                targets.append((role, hashlib.sha256(tok.encode()).hexdigest()[:32]))
        targets += [(role, ck) for ck, role in ccindex.get(sid.lower(), [])]

        seen = set()
        for role, cache_key in targets:
            if cache_key in seen:
                continue
            seen.add(cache_key)
            cipher, fulls, pparts = _resolve_sccontent(cache_key, scfull, scparts)
            if cipher is None:
                continue
            padded, stripped, ext = decrypt_sccontent(cipher, m["key"], m["iv"])
            if padded is None:
                continue
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
            entry.update({"role": role, "source": source, "ext": ext, "src": fulls + pparts,
                          "hashes": hashes, "snap_dim": _snap_dim(m)})
            m["media_files"].append(entry)

    # --- caching-media (link by decrypt-and-match; unique names by item hash) ---
    for folder, by_item in index_caching_media(app):
        first = _read_concat(next(iter(by_item.values())))
        match = None
        for sid, m in keyed:
            payload, ext = decrypt_pack(first, m["key"], m["iv"])
            if payload:
                match = (sid, m)
                break
        if not match:
            continue
        sid, m = match
        for item_hash, chunks in by_item.items():
            payload, ext = decrypt_pack(_read_concat(chunks), m["key"], m["iv"])
            if not payload:
                continue
            entry = _save_media(outdir, f"{sid}_pack_{item_hash[:12]}.{ext}", payload)
            entry.update({"role": "cached", "source": "caching-media", "ext": ext,
                          "src": chunks, "folder": folder, "item": item_hash,
                          "hashes": [("", *_hashes(payload))], "snap_dim": _snap_dim(m)})
            m["media_files"].append(entry)

    # label the smallest caching-media still per memory as the "preview"
    for m in memories.values():
        packs = sorted((f for f in m["media_files"] if f["source"] == "caching-media"),
                       key=lambda f: f["bytes"])
        if packs:
            packs[0]["role"] = "preview"

    # for video memories with a recovered .mp4 but no still, derive a poster frame from the video
    for sid, m in memories.items():
        if _best_still(m["media_files"]):
            continue
        vids = [f for f in m["media_files"] if f["ext"] == "mp4"]
        if not vids:
            continue
        video_out = os.path.join(outdir, vids[0]["out"])
        poster_name = f"{sid}_poster.jpg"
        if not generate_poster(video_out, os.path.join(outdir, poster_name)):
            continue
        data = open(os.path.join(outdir, poster_name), "rb").read()
        entry = _save_media(outdir, poster_name, data)      # already written; recompute size/dim
        entry.update({"role": "poster (generated)", "source": "generated", "ext": "jpg",
                      "src": ["(generated from the decrypted video — not original device data)"]
                             + vids[0]["src"],
                      "hashes": [("", *_hashes(data))], "generated": True, "snap_dim": ""})
        m["media_files"].append(entry)


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


def _render_group(members, keychain_available, snap_tcols, entry_tcols, src_root=None, manifest=None):
    """Render one `<tr>` for a set of memories that share the same media (same ZMEDIAID).

    Media-intrinsic blocks (metadata, location, CDN URLs, encryption) are shown once for the whole
    group when identical across its snaps, and only broken out per-snap when they actually differ.
    Per-snap identity (Snap ID + ZGALLERYSNAP / ZGALLERYENTRY values) and the timestamps are always
    listed per snap.
    """
    files = _dedup_media(members)
    still = _best_still(files)
    if still:
        cap = "<div class='gencap'>▶ poster generated from video</div>" if still.get("generated") else ""
        thumb_html = (f'<a href="{html.escape(still["path"])}" target="_blank">'
                      f'<img src="{html.escape(still["path"])}" loading="lazy"></a>{cap}')
    else:
        thumb_html = '<div class="noimg">no cached media</div>'

    single = len(members) == 1
    lead = members[0]
    is_video = lead["media_type"] == 1
    kind = "🎬 Video" if is_video else "🖼️ Image"
    if is_video and not any(f["ext"] == "mp4" for f in files):
        kind += " <span class='muted'>(preview only — full video not cached)</span>"
    meo = ' <span class="meo">My Eyes Only</span>' if any(m["is_meo"] for m in members) else ""

    media_id = lead["ids"].get("ZMEDIAID")
    header = (f"<div class='ghdr'><span class='glab'>Media ID</span>"
              f"<span class='mono'>{html.escape(str(media_id))}</span></div>") if media_id else ""
    sharebar = ("" if single else
                f"<div class='sharebar'>🔗 {len(members)} memories share the same media "
                f"(ZMEDIAID) — media-level details are shown once below</div>")

    # blocks that are usually identical across the group: show once, else break out per snap.
    # (CDN URLs almost always differ between snaps of the same media, so they live per-snap only.)
    meta, meta_shared = _shared_or_per(members, _meta_grid)
    loc, loc_shared = _shared_location(members, keychain_available)
    varies = "<div class='v muted'>varies per snap (see below)</div>"

    left = f"<div class='sect'>Metadata</div><div class='grid'>{meta if meta_shared else varies}</div>"
    loc_block = (f"<div class='sect'>Location (gallery.encrypteddb)</div>"
                 f"<div class='geo'>📍 {loc}</div>" if loc_shared else
                 f"<div class='sect'>Location (gallery.encrypteddb)</div>{varies}")
    enc_block = (f"<div class='sect'>Encryption (per-snap AES key)</div>"
                 f"<div class='grid'>{_enc_html(members)}</div>")
    shared_html = (f"<div class='shared2'><div class='c'>{left}</div>"
                   f"<div class='c'>{loc_block}{enc_block}</div></div>")

    # per-snap blocks: Snap ID + ZGALLERYSNAP / ZGALLERYENTRY values + CDN URLs, plus any shared
    # block (metadata / location) that turned out to differ across the group.
    mem_blocks = []
    for idx, m in enumerate(members):
        parts = [f"<div class='ghdr'><span class='glab'>Snap ID</span>"
                 f"<span class='mono'>{html.escape(m['snap_id'])}</span></div>",
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
                         f"<div class='geo'>📍 {loc[idx]}</div>")
        mem_blocks.append("<div class='mem'>" + "".join(parts) + "</div>")

    frows = []
    for f in sorted(files, key=lambda f: (f["source"], -f["bytes"])):
        srcs = "<br>".join(html.escape(s) for s in
                           _collapse_part_paths(device_path(s, src_root, manifest) for s in f["src"]))
        blocks = []
        for label, md5, sha256 in f.get("hashes", []):
            tag = f" <span class='pl'>({html.escape(label)})</span>" if label else ""
            blocks.append(f"<span class='hl'>MD5</span>{tag} {md5}<br>"
                          f"<span class='hl'>SHA-256</span>{tag} {sha256}")
        hashes = "<div class='hgap'></div>".join(blocks)
        # videos: fall back to the ZGALLERYSNAP dimensions PIL can't read off an mp4 container
        dim = f.get("dim") or f.get("snap_dim") or ""
        frows.append(
            f"<tr><td>{html.escape(f['role'])}</td><td>{html.escape(f['source'])}</td>"
            f"<td>{f['ext']}</td><td>{html.escape(dim)}</td>"
            f"<td>{f['bytes']//1024} KB</td>"
            f"<td><a href=\"{html.escape(f['path'])}\" target=\"_blank\">open</a></td>"
            f"<td class='hash'>{hashes}</td>"
            f"<td class='path'>{srcs}</td></tr>")
    files_table = ("<table class='files'><tr><th>Role</th><th>Source cache</th><th>Type</th>"
                   "<th>Dimensions</th><th>Size</th><th>File</th><th>Hashes (MD5 / SHA-256)</th>"
                   "<th>Source path(s) in extraction</th></tr>"
                   + "".join(frows) + "</table>") if frows else "<div class='muted'>no cached media recovered</div>"

    return f"""
        <tr>
          <td class="media">{thumb_html}</td>
          <td>
            {sharebar}
            <div class="kind">{kind}{meo}</div>
            {header}
            {shared_html}
            {''.join(mem_blocks)}
            <div class="sect">Timestamps — Snap (ZGALLERYSNAP)</div>{_ts_table(members, snap_tcols, "times", SNAP_TIME_LABELS, single)}
            <div class="sect">Timestamps — Entry / album (ZGALLERYENTRY)</div>{_ts_table(members, entry_tcols, "entry_times", ENTRY_TIME_LABELS, single)}
            <div class="sect">Media files</div>{files_table}
          </td>
        </tr>"""


def generate_report(memories, outdir, keychain_available, userids=None, tz_label="UTC",
                    src_root=None, manifest=None):
    userids = userids or {}
    groups = {}
    for m in memories.values():
        groups.setdefault(m["user_hash"], []).append(m)
    # most-populated profile first
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    total = len(memories)
    linked = sum(1 for m in memories.values() if m["media_files"])
    located = sum(1 for m in memories.values() if m["latitude"] is not None)

    # one column set for every timestamp table in the report (union across all memories, NULL-filled)
    snap_tcols = _union_cols(memories.values(), "times")
    entry_tcols = _union_cols(memories.values(), "entry_times")

    # top navigation (only when more than one profile)
    nav = ""
    if len(ordered) > 1:
        items = []
        for uh, mems in ordered:
            label = userids.get(uh) or ("userHash " + uh[:12] + "…")
            withmedia = sum(1 for x in mems if x["media_files"])
            items.append(f'<a href="#u_{uh}">{html.escape(label)}'
                         f'<span class="navcount">{len(mems)} memories &middot; {withmedia} with media</span></a>')
        nav = '<nav><span class="navlabel">Users:</span>' + "".join(items) + "</nav>"

    # memories that reference the same media (same ZMEDIAID) are grouped so the media, encryption
    # and matching timestamps are shown once. We key on ZMEDIAID rather than the AES key or media
    # hash: the same picture can legitimately appear in several distinct memories with its own
    # key, and only a shared ZMEDIAID means it is truly the same media object. Memories with no
    # ZMEDIAID each form their own group.
    def _grp_key(m):
        mid = m["ids"].get("ZMEDIAID")
        return ("m", mid) if mid else ("s", m["snap_id"])

    sections = []
    for uh, mems in ordered:
        uid = userids.get(uh)
        mems_sorted = sorted(mems, key=lambda m: (m["created_sort"], m["snap_id"]))
        grouped = {}
        for m in mems_sorted:
            grouped.setdefault(_grp_key(m), []).append(m)
        rows = "".join(_render_group(g, keychain_available, snap_tcols, entry_tcols, src_root, manifest)
                       for g in grouped.values())
        withmedia = sum(1 for x in mems if x["media_files"])
        loc = sum(1 for x in mems if x["latitude"] is not None)
        head = (f'<h2 id="u_{uh}">User: {html.escape(uid) if uid else "(user id unknown)"}'
                f'<span class="uh">userHash {uh}</span>'
                f'<span class="hcount">{len(mems)} memories &middot; {withmedia} with media &middot; {loc} geolocated</span></h2>')
        sections.append(head + f"<table>{rows}</table>")

    banner = "" if keychain_available else (
        '<div class="warn">No usable keychain (egocipher/persistedkey) was supplied — '
        'geolocation and My Eyes Only memories cannot be recovered, and old-schema imagery '
        'cannot be decrypted. Provide a full-filesystem keychain to complete these.</div>')

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Snapchat Memories</title><style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f4f8;color:#1b1b1f}}
 header{{background:#2d2d71;color:#fff;padding:16px 24px}}
 header h1{{margin:0;font-size:20px}} .sum{{opacity:.85;font-size:13px;margin-top:4px}}
 .warn{{background:#ffe8e8;border:1px solid #e0a0a0;color:#7a1f1f;padding:10px 24px;font-size:13px}}
 nav{{padding:12px 24px;background:#ececf4;border-bottom:1px solid #d7d7e2;font-size:13px;position:sticky;top:0}}
 nav .navlabel{{color:#555;font-weight:700;margin-right:8px}}
 nav a{{display:inline-block;margin:2px 18px 2px 0;color:#2d2d71;text-decoration:none;font-weight:600}}
 nav a:hover{{text-decoration:underline}} .navcount{{color:#888;font-weight:400;margin-left:8px}}
 h2{{margin:0;padding:12px 24px;background:#1f1f52;color:#fff;font-size:15px}}
 h2 .uh{{font-weight:400;opacity:.65;font-size:11.5px;font-family:ui-monospace,Consolas,monospace;margin-left:12px}}
 h2 .hcount{{font-weight:400;opacity:.8;font-size:12px;margin-left:12px}}
 table{{border-collapse:collapse;width:100%}}
 body table td{{border-bottom:1px solid #ddd;padding:14px;vertical-align:top}}
 td.media{{width:170px}} td.media img{{max-width:150px;max-height:280px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.25)}}
 .noimg{{width:150px;height:120px;display:flex;align-items:center;justify-content:center;background:#e6e6ee;color:#888;border-radius:6px;font-size:12px}}
 .gencap{{font-size:10.5px;color:#8a1f1f;margin-top:3px;max-width:150px}}
 .kind{{font-weight:600;font-size:15px}} .mono{{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#555;margin-bottom:4px}}
 .sect{{margin-top:10px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#2d2d71;font-weight:700;border-bottom:1px solid #e2e2ee;padding-bottom:2px}}
 .grid{{display:grid;grid-template-columns:auto 1fr;gap:2px 14px;font-size:12.5px;margin-top:4px}}
 .grid .k{{color:#666}} .grid .v{{color:#1b1b1f;word-break:break-word}}
 .v.url{{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#33367a}}
 .v.hex{{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#7a1f5a;overflow-wrap:anywhere}}
 .grid .v .col{{color:#aaa;font-family:ui-monospace,Consolas,monospace;font-size:10px;margin-left:8px}}
 .geo{{margin-top:4px;font-size:13px}}
 table.files{{border-collapse:collapse;margin-top:6px;font-size:12px;width:100%}}
 table.files th{{background:#2d2d71;color:#fff;text-align:left;padding:4px 8px;font-weight:600}}
 table.files td{{border:1px solid #e0e0e8;padding:4px 8px;vertical-align:top}}
 table.files td.hash{{font-family:ui-monospace,Consolas,monospace;font-size:10px;color:#555;white-space:nowrap}}
 table.files td.hash .hl{{color:#2d2d71;font-weight:700}} table.files td.hash .pl{{color:#8a1f5a}}
 table.files td.hash .hgap{{height:5px}}
 table.files td.path{{font-family:ui-monospace,Consolas,monospace;font-size:10.5px;color:#555;max-width:460px;overflow-wrap:anywhere}}
 .muted{{color:#999}} .meo{{background:#8a1f1f;color:#fff;padding:1px 6px;border-radius:4px;font-size:11px}}
 .sharebar{{background:#eef0ff;border:1px solid #c9cdf0;color:#2d2d71;padding:6px 10px;border-radius:5px;font-size:12.5px;font-weight:600;margin-bottom:10px}}
 .ghdr{{margin-top:8px;font-size:12.5px}} .ghdr .glab{{color:#666;font-weight:700;text-transform:uppercase;font-size:10.5px;letter-spacing:.04em;margin-right:8px}}
 .ghdr .mono{{margin:0}}
 .mem{{padding:8px 0}} .mem + .mem{{border-top:1px dashed #cfcfe0;margin-top:8px}}
 .mem{{border-left:2px solid #e2e2ee;padding-left:10px;margin-top:8px}}
 .tswrap{{overflow-x:auto;margin-top:4px}}
 table.ts{{border-collapse:collapse;font-size:11.5px;width:auto;min-width:100%}}
 table.ts th{{background:#efeff7;color:#2d2d71;text-align:left;padding:3px 8px;font-weight:600;white-space:nowrap;vertical-align:bottom}}
 table.ts td{{border:1px solid #e0e0e8;padding:3px 8px;white-space:nowrap}}
 table.ts th.rid{{font-family:ui-monospace,Consolas,monospace;font-weight:400;color:#555;position:sticky;left:0;background:#efeff7}}
 table.ts .col{{color:#aaa;font-family:ui-monospace,Consolas,monospace;font-size:9.5px;font-weight:400}}
 table.ts td.tsmatch{{background:#e7f6ea}} table.ts td.tsuniq{{background:#fdf0dc}}
 .tslegend{{font-size:10.5px;color:#777;margin-top:3px}}
 .tslegend .sw{{display:inline-block;width:10px;height:10px;border:1px solid #ccc;border-radius:2px;vertical-align:middle;margin-right:3px}}
 .tslegend .sw.tsmatch{{background:#e7f6ea}} .tslegend .sw.tsuniq{{background:#fdf0dc}}
 .shared2,.cols2{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 26px;align-items:start}}
 .shared2 .c,.cols2 .c{{min-width:0}}
 @media(max-width:900px){{.shared2,.cols2{{grid-template-columns:1fr}}}}
</style></head><body>
<header><h1>Snapchat Memories</h1>
 <div class="sum">{len(ordered)} user profile(s) &middot; {total} memories &middot; {linked} with recovered media &middot; {located} geolocated &middot; times shown in <b>{html.escape(tz_label)}</b></div></header>
{banner}
{nav}
{''.join(sections)}
</body></html>"""

    os.makedirs(outdir, exist_ok=True)
    report = os.path.join(outdir, "Memories_report.html")
    with open(report, "w", encoding="utf-8") as f:
        f.write(doc)
    return report, linked, located


# --------------------------------------------------------------------------- entry

def main(app_or_root, keychain="", outdir=None, padding="both", tz="local", src_root=None):
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

    egocipher = persisted = ""
    if keychain and os.path.exists(keychain):
        try:
            egocipher, persisted = _memkeys.readKeychain(keychain)
        except Exception as error:
            logger.warning(f"Could not read keychain: {error}")
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

    report, linked, located = generate_report(all_memories, outdir, keychain_available,
                                              userids=map_userids(app), tz_label=tz_label,
                                              src_root=src_root, manifest=manifest)
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
