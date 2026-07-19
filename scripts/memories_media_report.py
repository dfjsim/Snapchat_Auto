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

    # ZGALLERYENTRY (the album/entry a snap belongs to) carries more timestamps
    entry_times = {}
    try:
        ecols = [r[1] for r in cur.execute("PRAGMA table_info(ZGALLERYENTRY)")]
        etcols = _timecols(ecols)
        if etcols:
            for er in cur.execute(f"SELECT Z_PK,{','.join(etcols)} FROM ZGALLERYENTRY"):
                er = dict(er)
                entry_times[er["Z_PK"]] = {c: timefmt(er[c]) for c in etcols
                                           if isinstance(er[c], (int, float)) and er[c]}
    except sqlite3.DatabaseError as error:
        logger.debug(f"ZGALLERYENTRY read failed: {error}")

    url_cols = [c for c in ("ZMEDIADOWNLOADURL", "ZMEDIAREDIRECTURI", "ZOVERLAYDOWNLOADURL",
                            "ZOVERLAYREDIRECTURI", "ZTHUMBNAILDOWNLOADURL", "ZTHUMBNAILREDIRECTURI")
                if c in colset]
    id_cols = [c for c in ("ZMEDIAID", "ZEXTERNALID", "ZSAVERUSERID", "ZDEVICEID",
                           "ZTIMEZONENAME", "ZMULTISNAPGROUPID", "ZCAMERAROLLID") if c in colset]

    for r in cur.execute("SELECT * FROM ZGALLERYSNAP WHERE ZSNAPID IS NOT NULL"):
        r = dict(r)
        snap = r["ZSNAPID"]
        times = {}
        for c in time_cols:
            v = r.get(c)
            if isinstance(v, (int, float)) and v:
                times[c] = timefmt(v)
        m = {
            "snap_id": snap,
            "user_hash": profile["userHash"],
            "media_type": r.get("ZMEDIATYPE"),
            "format": r.get("ZSERVLETMEDIAFORMAT") or "",
            "media_format": r.get("ZMEDIAFORMAT"),
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
            "entry_times": entry_times.get(r.get("ZENTRY"), {}),
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


def index_sccontent(app):
    """Map every SCContent file basename -> full path (all per-user folders)."""
    index = {}
    for pat in ("Documents/com.snap.file_manager_*_SCContent_*",
                "Library/Caches/com.snap.file_manager_*_SCContent_*"):
        for d in glob.glob(os.path.join(app, pat)):
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                fp = os.path.join(d, name)
                if os.path.isfile(fp):
                    index.setdefault(name, fp)
    return index


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
    (locally-captured media that has no CDN URL, e.g. videos recorded on the device). Each
    output name embeds the cache key / item hash so a Memory whose media spans multiple caches
    never overwrites itself.
    """
    os.makedirs(outdir, exist_ok=True)
    scindex = index_sccontent(app)
    ccindex = index_cache_controller(app)
    keyed = [(sid, m) for sid, m in memories.items() if m["key"] and m["iv"]]

    # --- SCContent (URL-addressed + cache_controller-addressed) ---
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
            fp = scindex.get(cache_key)
            if not fp:
                continue
            padded, stripped, ext = decrypt_sccontent(open(fp, "rb").read(), m["key"], m["iv"])
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
            entry.update({"role": role, "source": "SCContent", "ext": ext, "src": [fp],
                          "hashes": hashes})
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
                          "hashes": [("", *_hashes(payload))]})
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
                      "hashes": [("", *_hashes(data))], "generated": True})
        m["media_files"].append(entry)


# --------------------------------------------------------------------------- report

def _best_still(files):
    imgs = [f for f in files if f["ext"] in ("jpg", "png", "webp")]
    return max(imgs, key=lambda f: f["bytes"]) if imgs else None


def device_path(fp):
    """Render a source path as its on-device path when recognisable, else the full path."""
    p = fp.replace("\\", "/")
    i = p.find("private/var/mobile/")
    return "/" + p[i:] if i != -1 else p


# friendlier labels for scdb columns
TIME_LABELS = {
    # ZGALLERYSNAP
    "ZCREATETIMEUTC": "Created (added to Memories)",
    "ZCAPTURETIMEUTC": "Captured",
    "ZPLACEHOLDERCREATETIME": "Placeholder created",
    # ZGALLERYENTRY (album/entry the snap belongs to)
    "ZEARLIESTSNAPCREATETIMEUTC": "Earliest snap created",
    "ZLATESTSNAPCAPTURETIMEUTC": "Latest snap captured",
    "ZAUTOSAVETIMEUTC": "Auto-saved",
    "ZSYNCEDAUTOSAVETIMEUTC": "Auto-save synced",
    "ZDUPLICATETIMEUTC": "Duplicated",
    "ZFEATUREDEXPIRATIONTIMEUTC": "Featured expiration",
    "ZFEATUREDSTORYACTIVATIONDATEUTC": "Featured story activation",
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


def _render_memory(m, keychain_available):
    files = m["media_files"]
    still = _best_still(files)
    if still:
        cap = "<div class='gencap'>▶ poster generated from video</div>" if still.get("generated") else ""
        thumb_html = (f'<a href="{html.escape(still["path"])}" target="_blank">'
                      f'<img src="{html.escape(still["path"])}" loading="lazy"></a>{cap}')
    else:
        thumb_html = '<div class="noimg">no cached media</div>'

    is_video = m["media_type"] == 1
    kind = "🎬 Video" if is_video else "🖼️ Image"
    if is_video and not any(f["ext"] == "mp4" for f in files):
        kind += " <span class='muted'>(preview only — full video not cached)</span>"
    meo = ' <span class="meo">My Eyes Only</span>' if m["is_meo"] else ""

    dur = f"{m['duration']:.1f}s" if isinstance(m["duration"], (int, float)) and m["duration"] else ""
    meta = [("Media type", "Video" if is_video else "Image"),
            ("Servlet format", m["format"]),
            ("Dimensions", f"{m['width']}×{m['height']}" if m["width"] else ""),
            ("Duration", dur),
            ("Camera", m["camera"])]
    for c, v in m["ids"].items():
        meta.append((ID_LABELS.get(c, c), v))
    meta_html = "".join(f"<div class='k'>{html.escape(str(k))}</div>"
                        f"<div class='v'>{html.escape(str(v))}</div>"
                        for k, v in meta if v not in (None, "", 0))

    def _ts_grid(d):
        return "".join(
            f"<div class='k'>{html.escape(TIME_LABELS.get(c, c))}</div>"
            f"<div class='v'>{html.escape(v)} <span class='col'>{html.escape(c)}</span></div>"
            for c, v in d.items() if v)
    ts_snap = _ts_grid(m["times"])
    ts_entry = _ts_grid(m.get("entry_times", {}))

    # encryption (per-snap AES key / IV)
    if m["key"] and m["iv"]:
        enc_html = (f"<div class='k'>Key (AES-256)</div><div class='v hex'>{m['key'].hex()}</div>"
                    f"<div class='k'>IV</div><div class='v hex'>{m['iv'].hex()}</div>")
    elif m["is_meo"]:
        enc_html = "<div class='v muted'>My Eyes Only — key not unwrapped (persistedkey required)</div>"
    else:
        enc_html = "<div class='v muted'>key not available</div>"

    if m["latitude"] is not None:
        lat, lon = m["latitude"], m["longitude"]
        addr = f" — {html.escape(m['address'])}" if m.get("address") else ""
        geo = (f'<a href="https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}" '
               f'target="_blank">{lat:.6f}, {lon:.6f}</a>{addr}')
    elif m["has_location"]:
        geo = ('<span class="muted">recorded on device — full-filesystem keychain required</span>'
               if not keychain_available else '<span class="muted">flagged but not found</span>')
    else:
        geo = "&mdash;"

    url_html = "".join(
        f"<div class='k'>{html.escape(URL_LABELS.get(c, c))}</div>"
        f"<div class='v url'>{html.escape(u)}</div>"
        for c, u in m["urls"].items()) or "<div class='v muted'>none</div>"

    frows = []
    for f in sorted(files, key=lambda f: (f["source"], -f["bytes"])):
        srcs = "<br>".join(html.escape(device_path(s)) for s in f["src"])
        blocks = []
        for label, md5, sha256 in f.get("hashes", []):
            tag = f" <span class='pl'>({html.escape(label)})</span>" if label else ""
            blocks.append(f"<span class='hl'>MD5</span>{tag} {md5}<br>"
                          f"<span class='hl'>SHA-256</span>{tag} {sha256}")
        hashes = "<div class='hgap'></div>".join(blocks)
        frows.append(
            f"<tr><td>{html.escape(f['role'])}</td><td>{html.escape(f['source'])}</td>"
            f"<td>{f['ext']}</td><td>{html.escape(f.get('dim') or '')}</td>"
            f"<td>{f['bytes']//1024} KB</td>"
            f"<td><a href=\"{html.escape(f['path'])}\" target=\"_blank\">open</a></td>"
            f"<td class='hash'>{hashes}</td>"
            f"<td class='path'>{srcs}</td></tr>")
    files_table = ("<table class='files'><tr><th>Role</th><th>Source cache</th><th>Type</th>"
                   "<th>Dimensions</th><th>Size</th><th>File</th><th>Hashes (MD5 / SHA-256)</th>"
                   "<th>Source path(s) on device</th></tr>"
                   + "".join(frows) + "</table>") if frows else "<div class='muted'>no cached media recovered</div>"

    entry_ts_block = (f"<div class='sect'>Timestamps — Entry / album (ZGALLERYENTRY)</div>"
                      f"<div class='grid'>{ts_entry}</div>") if ts_entry else ""

    return f"""
        <tr>
          <td class="media">{thumb_html}</td>
          <td>
            <div class="kind">{kind}{meo}</div>
            <div class="mono">{html.escape(m['snap_id'])}</div>
            <div class="sect">Metadata</div><div class="grid">{meta_html}</div>
            <div class="sect">Timestamps — Snap (ZGALLERYSNAP)</div><div class="grid">{ts_snap or "<div class='v muted'>none</div>"}</div>
            {entry_ts_block}
            <div class="sect">Encryption (per-snap AES key)</div><div class="grid">{enc_html}</div>
            <div class="sect">Location (gallery.encrypteddb)</div><div class="geo">📍 {geo}</div>
            <div class="sect">CDN URLs (scdb-27)</div><div class="grid">{url_html}</div>
            <div class="sect">Media files</div>{files_table}
          </td>
        </tr>"""


def generate_report(memories, outdir, keychain_available, userids=None, tz_label="UTC"):
    userids = userids or {}
    groups = {}
    for m in memories.values():
        groups.setdefault(m["user_hash"], []).append(m)
    # most-populated profile first
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    total = len(memories)
    linked = sum(1 for m in memories.values() if m["media_files"])
    located = sum(1 for m in memories.values() if m["latitude"] is not None)

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

    sections = []
    for uh, mems in ordered:
        uid = userids.get(uh)
        mems_sorted = sorted(mems, key=lambda m: (m["created_sort"], m["snap_id"]))
        rows = "".join(_render_memory(m, keychain_available) for m in mems_sorted)
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
</style></head><body>
<header><h1>Snapchat Memories</h1>
 <div class="sum">{len(ordered)} user profile(s) &middot; {total} memories &middot; {linked} with recovered media &middot; {located} geolocated &middot; times shown in <b>{html.escape(tz_label)}</b></div></header>
{banner}
{nav}
{''.join(sections)}
</body></html>"""

    os.makedirs(outdir, exist_ok=True)
    report = os.path.join(outdir, "Memories.html")
    with open(report, "w", encoding="utf-8") as f:
        f.write(doc)
    return report, linked, located


# --------------------------------------------------------------------------- entry

def main(app_or_root, keychain="", outdir=None, padding="both", tz="local"):
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
    """
    app = find_app_container(app_or_root)
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
                                              userids=map_userids(app), tz_label=tz_label)
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
