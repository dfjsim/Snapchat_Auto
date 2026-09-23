"""
Snapchat for Android — the Memories report, read from ``databases/memories.db``.

``memories.db`` is plain SQLite (no key), and three of its tables describe a Memory:

* ``memories_snap`` — one row per snap: its ids (``_id``, ``media_id``), when it was made
  (``create_time``, ``snap_capture_time``, Unix **milliseconds**, and the device's ``time_zone_id``),
  its size and duration, whether it carries an overlay, its **location** (``latitude`` /
  ``longitude``, in degrees, when ``has_location`` is set), and the snap's own media key and IV
  (``media_key`` / ``media_iv``). A My Eyes Only snap carries its key wrapped instead
  (``encrypted_media_key`` / ``encrypted_media_iv``).
* ``memories_entry`` — the Memory a snap belongs to (``memories_snap.memories_entry_id``): its
  title, its creation time and ``is_private`` — set for a Memory in **My Eyes Only**.
* ``memories_media`` — the media object (``memories_snap.media_id``): size, format, download URL.

The media is **not** in ``memories.db``. What the device kept of it is found three ways, each of
which names the snap by something of its own:

* the app's own cache folders, ``files/file_manager/<type>/`` (``memories_media``,
  ``memories_thumbnail``, ``memories_overlay``, …), whose file names begin with the MD5 (upper-case
  hex) of a request string built from the snap's ids — ``<media_id>.media``,
  ``<snap_id>.thumbnail``, ``<snap_id>.overlay`` — so a name is tied to a snap by recomputing that
  hash, which anyone can do; and any file there whose name carries the snap's id or media id;
* the native content cache indexed by ``cache_controller.db`` (as on iOS): a claim whose
  ``EXTERNAL_KEY`` names the snap's id or media id;
* a ``CACHE_KEY`` equal to the SHA-256 of a download URL's token (the content-addressed cache).

Every file is tried as it is and then with the snap's keys — ``memories_snap.media_key`` /
``media_iv`` (base64; AES-256-CBC, PKCS#7), the key a My Eyes Only snap stores wrapped
(``encrypted_media_key`` / ``encrypted_media_iv``, unwrapped with the master key
``memories_meo_confidential`` holds), and any key / IV pair inside the snap's ``snapdoc``. Only bytes
that ARE media, by their magic bytes, are accepted: a wrong key never produces a JPEG or an MP4, so
decrypting a file is also what confirms it belongs to the snap. A file that is already plaintext is
published as it is.

What is reported is what is there: a snap whose media the device did not keep is listed with its
metadata and says so; a My Eyes Only snap whose key could not be unwrapped says that instead.
"""

import os
import re
import json
import html
import base64
import hashlib
import logging

from Crypto.Cipher import AES
from urllib.parse import urlparse

from scripts import report_ui
from scripts import app_version
from scripts import android_layout
from scripts.data import sqlite_open
from scripts.data import sniff
from scripts.data import media_meta
from scripts.data import flatbuffers_doc
from scripts.memories_media_report import (
    make_time_formatter, index_sccontent, _resolve_sccontent, decrypt_sccontent,
    classify_snap_claim, cache_controller_paths, render_maps, load_fs_records, manifest_key,
    device_path,
)

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_COCOA_EPOCH = 978307200

#: memories_snap columns shown on a row's detail, as stored (anything else the table has is shown
#: after them, so an app version that adds a column does not lose it).
_SNAP_FIRST = ("_id", "media_id", "memories_entry_id", "media_type", "create_time",
               "snap_capture_time", "time_zone_id", "width", "height", "duration", "has_location",
               "latitude", "longitude", "has_overlay_image", "front_facing",
               "camera_orientation_degrees", "snap_source_type", "snap_status", "has_deleted",
               "is_favorite", "multi_snap_group_id", "copy_from_snap_id", "external_id")
#: columns holding keys, URLs or blobs: shown, but in their own block
_KEY_COLS = ("media_key", "media_iv", "encrypted_media_key", "encrypted_media_iv")
_URL_COLS = ("thumbnail_download_url", "overlay_download_url")
_MS_COLS = ("create_time", "snap_capture_time", "framing_create_time", "place_holder_create_time",
            "latest_snap_create_time", "earliest_snap_create_time", "last_auto_save_time",
            "last_accessed")

MEO_NOTE = (
    "memories_entry.is_private = 1: this Memory is in My Eyes Only. Its snaps carry their media key "
    "wrapped (memories_snap.encrypted_media_key / encrypted_media_iv): each is AES-256-CBC under the "
    "master key and IV that memories_meo_confidential stores (base64, with the account's bcrypt "
    "passcode hash beside them). The report unwraps them with that master key — no passcode is "
    "needed while that row exists — and uses the result only if it then decrypts a cached file to "
    "media. The table is shown as stored at the top of the report.")
KEY_NOTE = (
    "memories_snap.media_key / media_iv as stored, and what they decode to. The key is tried against "
    "every cached file linked to this snap: AES-256-CBC, and only a result that IS media (by its "
    "magic bytes) is accepted, so a wrong key never produces a file. A snap row with no key cannot "
    "have its encrypted media decrypted; a cached file that is already plaintext needs none.")
LINK_NOTE = (
    "How each cached file was tied to this snap:\n\n"
    "• MD5 of a request string — a file in the app's cache folders (files/file_manager/<type>/) "
    "whose name begins with the MD5, in upper-case hex, of '<media_id>.media', "
    "'<snap_id>.thumbnail' or '<snap_id>.overlay' built from this snap's own ids. Recompute the MD5 "
    "to check it.\n\n"
    "• named after this snap — a file in those folders whose name carries the snap's id or media "
    "id.\n\n"
    "• claim — a cache_controller.db CACHE_FILE_CLAIM row whose EXTERNAL_KEY names this snap's "
    "memories_snap._id or its media_id; the file on disk is named after that row's CACHE_KEY.\n\n"
    "• url token — the file's CACHE_KEY equals the first 16 bytes of the SHA-256 of the last path "
    "segment of one of this snap's download URLs (the cache is content-addressed by that token).\n\n"
    "Decrypting a file with one of this snap's keys is the confirmation: a wrong key does not "
    "produce media. The State column names the key that opened it.")
LOCATION_NOTE = (
    "memories_snap.latitude / longitude, as stored (decimal degrees), when has_location is set. The "
    "map links go to OpenStreetMap / Google Maps in a new tab — nothing is fetched unless you click. "
    "A map image, when one is shown, comes from the examiner's own tile server and is derived, not "
    "device data.")
TIME_NOTE = (
    "memories_snap.create_time and snap_capture_time are Unix milliseconds (UTC), shown in the "
    "report's timezone; time_zone_id is the zone the device recorded for the snap, shown as stored.")


def _esc(value):
    return html.escape("" if value is None else str(value))


def _int(value):
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- keys

def decode_key(value, size):
    """A stored key or IV as ``size`` raw bytes, and how it was read — or ``(None, reason)``.

    memories.db stores them as text; base64 (standard or URL-safe) and hex are both accepted, and a
    BLOB of exactly ``size`` bytes is taken as it is. Anything that does not come out at the size an
    AES-256 key (32) or IV (16) has is reported rather than guessed at.
    """
    if value is None or value == "":
        return None, "not stored"
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) == size:
            return raw, "raw bytes"
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            return None, f"{len(raw)} raw bytes (a {size}-byte value was expected)"
    text = str(value).strip()
    if re.fullmatch(r"[0-9a-fA-F]+", text) and len(text) == size * 2:
        return bytes.fromhex(text), "hex"
    for name, decoder in (("base64", base64.b64decode), ("base64url", base64.urlsafe_b64decode)):
        try:
            raw = decoder(text + "=" * (-len(text) % 4))
        except (ValueError, TypeError):
            continue
        if len(raw) == size:
            return raw, name
    return None, f"{len(text)} characters that decode to no {size}-byte value"


def _b64(value):
    """Base64 text (standard or URL-safe, whitespace and missing padding tolerated) as bytes."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("ascii")
        except UnicodeDecodeError:
            return None
    text = re.sub(r"\s+", "", str(value))
    if not text:
        return None
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(text + "=" * (-len(text) % 4))
        except (ValueError, TypeError):
            continue
    return None


def _strip_pkcs7(data):
    n = data[-1] if data else 0
    return data[:-n] if 1 <= n <= 16 and data[-n:] == bytes([n]) * n else None


def unwrap_meo_key(meo_rows, enc_key, enc_iv):
    """A My Eyes Only snap's media key and IV, unwrapped — ``(key, iv, how)`` or ``None``.

    ``encrypted_media_key`` / ``encrypted_media_iv`` are each AES-256-CBC (PKCS#7) under the account's
    master key and IV, which ``memories_meo_confidential`` stores as base64. The result is only
    accepted when both unwrap to valid padding and to exactly 32 and 16 bytes — and it is only
    *used* when it then decrypts a cached file to media, which is the real test.
    """
    ek, ei = _b64(enc_key), _b64(enc_iv)
    if not ek or not ei or len(ek) % 16 or len(ei) % 16:
        return None
    for row in meo_rows or ():
        mk, _how = decode_key(row.get("master_key"), 32)
        miv, _how = decode_key(row.get("master_key_iv"), 16)
        if not mk or not miv:
            continue
        key = _strip_pkcs7(AES.new(mk, AES.MODE_CBC, miv).decrypt(ek))
        iv = _strip_pkcs7(AES.new(mk, AES.MODE_CBC, miv).decrypt(ei))
        if key is not None and iv is not None and len(key) == 32 and len(iv) == 16:
            return key, iv, (f"encrypted_media_key / encrypted_media_iv unwrapped with "
                             f"memories_meo_confidential.master_key (user_id {row.get('user_id')})")
    return None


def _proto_fields(data):
    """``[(field, wire, value)]`` of one protobuf message, or ``None`` if it is not one."""
    out, pos = [], 0
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
            field, wire = key >> 3, key & 7
            if field == 0:
                return None
            if wire == 0:
                while data[pos] & 0x80:
                    pos += 1
                pos += 1
                out.append((field, 0, None))
            elif wire == 2:
                length, shift = 0, 0
                while True:
                    byte = data[pos]
                    pos += 1
                    length |= (byte & 0x7F) << shift
                    shift += 7
                    if not byte & 0x80:
                        break
                if pos + length > len(data):
                    return None
                out.append((field, 2, data[pos:pos + length]))
                pos += length
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                return None
    except IndexError:
        return None
    return out if pos == len(data) else None


def snapdoc_keys(blob, depth=0):
    """Every (32-byte key, 16-byte IV) pair a protobuf blob carries as fields 1 and 2 of one
    message, at any depth. Tried against a snap's cached files only after its own key; a pair that
    is not the file's key cannot turn it into media, so a wrong candidate costs nothing."""
    if blob is None or depth > 8:
        return []
    fields = _proto_fields(bytes(blob))
    if not fields:
        return []
    found = []
    ones = [v for f, w, v in fields if f == 1 and w == 2 and v is not None and len(v) == 32]
    twos = [v for f, w, v in fields if f == 2 and w == 2 and v is not None and len(v) == 16]
    for k in ones:
        for iv in twos:
            found.append((k, iv))
    for _f, w, v in fields:
        if w == 2 and v and len(v) > 2:
            found += snapdoc_keys(v, depth + 1)
    return found


def split_package(data):
    """A downloaded thumbnail package as its images, or ``None``: a little-endian int32 count (1 to
    1024), that many int32 sizes, then the images back to back. Accepted only when the sizes account
    for every byte and every part is media, so a file that merely starts with a small number is not
    cut up."""
    if len(data) < 8:
        return None
    n = int.from_bytes(data[:4], "little")
    if not 1 <= n <= 1024 or 4 + 4 * n > len(data):
        return None
    sizes = [int.from_bytes(data[4 + 4 * i:8 + 4 * i], "little") for i in range(n)]
    pos = 4 + 4 * n
    if pos + sum(sizes) != len(data):
        return None
    parts = []
    for size in sizes:
        parts.append(data[pos:pos + size])
        pos += size
    return parts if all(sniff.guess_media(part[:32]) for part in parts) else None


# --------------------------------------------------------------------------- the model

def _url_token(url):
    if not url:
        return None
    seg = urlparse(str(url)).path.rstrip("/").split("/")[-1]
    return seg or None


def load_memories(memories_db):
    """``{snap_id: memory}`` from memories.db, both readings (see ``sqlite_open``)."""
    out = {}
    views = sqlite_open.open_views(memories_db)
    try:
        tables = {r[0] for r in views.merged.execute(
            "select name from sqlite_master where type = 'table'")}
        if "memories_snap" not in tables:
            logger.warning("memories.db has no memories_snap table — no Memories to report")
            return out, dict(views.info), {}
        snaps, snap_marks = sqlite_open.read_table(views, "memories_snap")
        entries, _m = (sqlite_open.read_table(views, "memories_entry")
                       if "memories_entry" in tables else ([], []))
        medias, _m = (sqlite_open.read_table(views, "memories_media")
                      if "memories_media" in tables else ([], []))
        meo, _m = (sqlite_open.read_table(views, "memories_meo_confidential")
                   if "memories_meo_confidential" in tables else ([], []))
        info = dict(views.info)
    finally:
        views.close()
    entry_by_id = {}
    for e in entries:
        entry_by_id.setdefault(str(e.get("_id")), e)
    media_by_id = {}
    for m in medias:
        media_by_id.setdefault(str(m.get("_id")), m)
    for row, mark in zip(snaps, snap_marks):
        sid = str(row.get("_id") or "")
        if not sid:
            continue
        if sid in out and mark == sqlite_open.MAIN_ONLY:
            out[sid].setdefault("prior", []).append(row)     # an older version of the same row
            continue
        entry = entry_by_id.get(str(row.get("memories_entry_id") or ""), {})
        media = media_by_id.get(str(row.get("media_id") or ""), {})
        lat, lon = _float(row.get("latitude")), _float(row.get("longitude"))
        located = bool(_int(row.get("has_location"))) or (lat not in (None, 0.0)
                                                         and lon not in (None, 0.0))
        out[sid] = {
            "snap_id": sid, "row": row, "entry": entry, "media": media, "wal": mark,
            "media_id": str(row.get("media_id") or ""),
            "meo": bool(_int(entry.get("is_private"))),
            "latitude": lat if located and lat is not None else None,
            "longitude": lon if located and lon is not None else None,
            "files": [], "key_state": "", "map": None,
        }
    meo_rows = [dict(r) for r in meo]
    return out, info, {"meo": meo_rows}


def memory_index(app):
    """The shape ``cache_controller_report.load_memory_index`` returns, from an Android app folder.

    ``snap_ids`` / ``media_ids`` map an upper-cased id to ``(snap_id, "")`` (there is no per-account
    hash folder on Android); ``url_keys`` maps a CACHE_KEY to ``(snap_id, "", column)`` for the
    content-addressed cache; ``snap_urls`` lists each snap's URLs; ``labels`` names the columns in
    the words the link's explanation uses.
    """
    out = {"snap_ids": {}, "url_keys": {}, "media_ids": {}, "snap_urls": {},
           "labels": {"snap": "memories_snap._id", "media": "memories_snap.media_id",
                      "db": "memories.db"}}
    layouts = android_layout.discover(app)
    db = layouts[0].db("memories") if layouts else ""
    if not db:
        return out
    try:
        memories, _info, _extra = load_memories(db)
    except Exception as error:                                 # noqa: BLE001
        logger.debug(f"Could not read {db}: {error}")
        return out
    for sid, m in memories.items():
        out["snap_ids"][sid.upper()] = (sid, "")
        if m["media_id"]:
            out["media_ids"].setdefault(m["media_id"].upper(), (sid, ""))
        for column, url in _urls(m):
            out["snap_urls"].setdefault(sid, []).append(url)
            token = _url_token(url)
            if token:
                key = hashlib.sha256(token.encode()).hexdigest()[:32]
                out["url_keys"].setdefault(key, (sid, "", column))
    return out


def _urls(m):
    """``[(column, url)]`` — every download URL memories.db records for this snap."""
    out = []
    for col in _URL_COLS:
        if m["row"].get(col):
            out.append((f"memories_snap.{col}", str(m["row"][col])))
    if m["media"].get("download_url"):
        out.append(("memories_media.download_url", str(m["media"]["download_url"])))
    return out


def index_claims(app):
    """``{uuid (lower): [(CACHE_KEY, EXTERNAL_KEY, role)]}`` over every cache_controller.db claim.

    Every UUID an EXTERNAL_KEY carries is indexed, not only the shapes the iOS Memories report
    recognises: the Android app may spell its Memory claims differently, and a claim that names the
    snap's own id is a link whatever its prefix. ``role`` comes from ``classify_snap_claim`` when the
    shape is a known one, and is "named in a claim" otherwise.
    """
    out = {}
    for db in cache_controller_paths(app):
        claims, _marks, _info = sqlite_open.read_all(db, "CACHE_FILE_CLAIM")
        for c in claims:
            ek, ck = c.get("EXTERNAL_KEY") or "", c.get("CACHE_KEY") or ""
            if not ek or not ck:
                continue
            _uuid, _category, role = classify_snap_claim(ek)
            for mo in _UUID_RE.finditer(ek):
                out.setdefault(mo.group(0).lower(), []).append(
                    (ck, ek, role or "named in a claim"))
    return out


# --------------------------------------------------------------------------- media

#: The request strings the app's own cache names a Memory's files after (the MD5, upper-case hex, of
#: the string begins the file name), with the role each gives the file.
_REQUESTS = (("{media_id}.media", "full media"), ("{snap_id}.thumbnail", "thumbnail"),
             ("{snap_id}.overlay", "overlay"))
_HEX32 = re.compile(r"^([0-9A-Fa-f]{32})")


def index_file_manager(file_manager):
    """``(by_md5, by_uuid)`` over every file in the app's ``files/file_manager/<type>/`` folders:
    by the 32-hex prefix of the name (upper-cased), and by every UUID the name carries (lower)."""
    by_md5, by_uuid = {}, {}
    for kind, folder in sorted((file_manager or {}).items()):
        for dirpath, _dirs, files in os.walk(folder):
            for name in files:
                path = os.path.join(dirpath, name).replace("\\", "/")
                mo = _HEX32.match(name)
                if mo:
                    by_md5.setdefault(mo.group(1).upper(), []).append((kind, path))
                for u in _UUID_RE.findall(name):
                    by_uuid.setdefault(u.lower(), []).append((kind, path))
    return by_md5, by_uuid

def _keys_for(m, meo_rows):
    """The keys a snap's cached files are tried with, in order: ``[(label, key, iv)]``."""
    out = []
    key, key_how = decode_key(m["row"].get("media_key"), 32)
    iv, iv_how = decode_key(m["row"].get("media_iv"), 16)
    m["key"] = {"key": key, "iv": iv, "key_how": key_how, "iv_how": iv_how, "unwrapped": ""}
    if key and iv:
        out.append(("memories_snap.media_key / media_iv", key, iv))
    wrapped = unwrap_meo_key(meo_rows, m["row"].get("encrypted_media_key"),
                             m["row"].get("encrypted_media_iv"))
    if wrapped:
        m["key"]["unwrapped"] = wrapped[2]
        out.append((wrapped[2], wrapped[0], wrapped[1]))
    for n, (k, v) in enumerate(snapdoc_keys(m["row"].get("snapdoc")), 1):
        out.append((f"key / IV pair #{n} inside memories_snap.snapdoc", k, v))
    return out


def _open(raw, keys):
    """``(data, padded, ext, tail_ok, how)`` for the first reading of ``raw`` that is media."""
    ext = sniff.guess_media(raw[:32])
    if ext:
        return raw, raw, ext, None, "plaintext"
    for label, key, iv in keys:
        padded, stripped, ext, tail_ok = decrypt_sccontent(raw, key, iv)
        if ext:
            return stripped, padded, ext, tail_ok, label
    return None, None, None, None, ""


def collect_media(memories, app, outdir, padding="both", file_manager=None, meo_rows=None):
    """Find, decrypt and publish every cached file of every snap. Returns the number published."""
    scfull, scparts = index_sccontent(app)
    claims = index_claims(app)
    fm_md5, fm_uuid = index_file_manager(file_manager)
    url_keys = {}                                  # snap id -> [(CACHE_KEY, column)]
    for sid, m in memories.items():
        for column, url in _urls(m):
            token = _url_token(url)
            if token:
                url_keys.setdefault(sid, []).append(
                    (hashlib.sha256(token.encode()).hexdigest()[:32], column))
    media_dir = os.path.join(outdir, "media")
    published, count = {}, 0

    def publish(m, f, data, padded, ext, tail_ok, how, suffix):
        nonlocal count
        plain = how == "plaintext"
        body = data if plain or padding != "keep" else padded
        f.update({"state": "plaintext in the cache" if plain else f"decrypted ({how})",
                  "ext": ext, "tail_ok": tail_ok,
                  "hashes": [("as published", hashlib.md5(body).hexdigest(),
                              hashlib.sha256(body).hexdigest())]})
        if not plain and padding == "both" and padded != data:
            f["hashes"].append(("with PKCS#7 padding", hashlib.md5(padded).hexdigest(),
                                hashlib.sha256(padded).hexdigest()))
        md5 = f["hashes"][0][1]
        if md5 in published:
            f["path"] = published[md5]
        else:
            os.makedirs(media_dir, exist_ok=True)
            role = re.sub(r"[^A-Za-z0-9]+", "-", f["role"]).strip("-")
            name = f"{m['snap_id']}_{role}_{suffix}.{ext}"
            with open(os.path.join(media_dir, name), "wb") as fh:
                fh.write(body)
            f["path"] = published[md5] = "media/" + name
            count += 1
            try:
                f["meta"] = media_meta.extract(os.path.join(media_dir, name))
            except Exception:                                  # noqa: BLE001 — never costs a file
                f["meta"] = None
        f["bytes"] = len(body)

    def examine(m, keys, f, raw, suffix):
        data, padded, ext, tail_ok, how = _open(raw, keys)
        if ext:
            publish(m, f, data, padded, ext, tail_ok, how, suffix)
            m["files"].append(f)
            return
        parts = split_package(raw)
        if parts:
            for n, part in enumerate(parts):
                piece = dict(f, role=f"{f['role']} {n + 1} of {len(parts)}",
                             basis=f["basis"] + f" — image {n + 1} of the {len(parts)} a "
                                                "thumbnail package holds (a count, the sizes, then "
                                                "the images; every byte accounted for)")
                publish(m, piece, part, part, sniff.guess_media(part[:32]), None, "plaintext",
                        f"{suffix}-{n}")
                m["files"].append(piece)
            return
        _kind, _ext, label, encrypted = sniff.classify(raw[:65536], len(raw))
        if encrypted and keys:
            f["state"] = "encrypted — none of the snap's keys opens it"
        elif encrypted:
            f["state"] = "encrypted — no key for this snap"
        else:
            f["state"] = f"not media ({label})"
        f["bytes_on_disk"] = len(raw)
        m["files"].append(f)

    for sid, m in sorted(memories.items()):
        keys = _keys_for(m, meo_rows)
        seen = set()
        # 1. the app's own cache folders, by the MD5 of the request string, then by id in the name
        for template, role in _REQUESTS:
            request = template.format(media_id=m["media_id"], snap_id=sid)
            if "{" in request or request.startswith("."):
                continue
            digest = hashlib.md5(request.encode("utf-8")).hexdigest().upper()
            for kind, path in fm_md5.get(digest, []):
                if path in seen:
                    continue
                seen.add(path)
                f = {"cache_key": None, "source": f"files/file_manager/{kind}", "role": role,
                     "basis": f'file name begins with MD5("{request}") = {digest}',
                     "paths": [path]}
                try:
                    with open(path, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                examine(m, keys, f, raw, digest[:8] + "-" + os.path.basename(path).split(".")[-1])
        for ident, what in ((sid, "memories_snap._id"), (m["media_id"], "memories_snap.media_id")):
            for kind, path in fm_uuid.get(ident.lower(), []) if ident else []:
                if path in seen:
                    continue
                seen.add(path)
                f = {"cache_key": None, "source": f"files/file_manager/{kind}",
                     "role": "named after this snap",
                     "basis": f"file name carries this snap's {what}", "paths": [path]}
                try:
                    with open(path, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                examine(m, keys, f, raw, os.path.basename(path)[:12])
        # 2. the native content cache, by claim, then by URL token
        cands, seen_keys = [], set()
        for ident, what in ((sid, "memories_snap._id"), (m["media_id"], "memories_snap.media_id")):
            for ck, ek, role in claims.get(ident.lower(), []) if ident else []:
                if ck.lower() not in seen_keys:
                    seen_keys.add(ck.lower())
                    cands.append((ck, role, f'claim EXTERNAL_KEY "{ek}" names this snap\'s {what}'))
        for ck, column in url_keys.get(sid, []):
            if ck not in seen_keys:
                seen_keys.add(ck)
                cands.append((ck, "downloaded", f"CACHE_KEY = SHA-256(token of {column})[:16]"))
        for ck, role, basis in cands:
            raw, fulls, parts, coverage = _resolve_sccontent(ck, scfull, scparts)
            f = {"cache_key": ck, "source": "cache_controller.db", "role": role, "basis": basis,
                 "coverage": coverage,
                 "paths": [p.replace("\\", "/") for p in (fulls or []) + (parts or [])]}
            if raw is None:
                f["state"] = "not on disk"
                m["files"].append(f)
                continue
            examine(m, keys, f, raw, ck[:8])
        if m["key"]["unwrapped"]:
            m["key_state"] = "My Eyes Only — key unwrapped"
        elif m["meo"] and not m["key"]["key"]:
            m["key_state"] = "My Eyes Only — key wrapped"
        elif m["key"]["key"] and m["key"]["iv"]:
            m["key_state"] = "key stored"
        else:
            m["key_state"] = "no usable key"
    return count


def write_manifests(memories, outdir):
    """``media_by_cache_key.json`` and ``memory_pages.json`` — what the cache_controller report
    reads to link a cache entry to the decrypted copy and to the Memory."""
    by_key = {}
    for sid, m in memories.items():
        for f in m["files"]:
            if not f.get("path"):
                continue
            by_key.setdefault(f["cache_key"].lower(), []).append({
                "path": f["path"], "role": f.get("role", ""), "ext": f.get("ext", ""),
                "bytes": f.get("bytes", 0), "snap_id": sid,
                "md5": f["hashes"][0][1], "sha256": f["hashes"][0][2],
                "complete": f.get("tail_ok"), "why_incomplete": ""})
    for name, data in (("media_by_cache_key.json", by_key), ("memory_pages.json", {})):
        try:
            with open(os.path.join(outdir, name), "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError as error:
            logger.debug(f"Could not write {name}: {error}")


# --------------------------------------------------------------------------- HTML

MEM_COLS = "24px 96px 160px 160px 70px minmax(150px,1fr) minmax(170px,1.1fr) 150px 250px"
MEM_ROW_H = 74


def _ms_fmt(value, timefmt):
    ms = _int(value)
    if not ms:
        return ""
    return timefmt(ms / 1000.0 - _COCOA_EPOCH)


def _kind(m):
    exts = {f.get("ext") for f in m["files"] if f.get("ext")}
    if exts & {"mp4", "mov", "m4v", "webm"}:
        return "video"
    if exts:
        return "image"
    return {0: "image?", 1: "video?"}.get(_int(m["row"].get("media_type")), "")


def _media_cell(m, prefix=""):
    shown = [f for f in m["files"] if f.get("path")]
    if not shown:
        return f'<span class="none">{_esc(_state(m))}</span>'
    best = max(shown, key=lambda f: (f.get("ext") in ("jpg", "png", "webp"), f.get("bytes") or 0))
    url = prefix + best["path"]
    if best.get("ext") in ("jpg", "png", "webp", "gif"):
        return (f'<a class="filebtn img" href="{_esc(url)}" target="_blank">'
                f'<img src="{_esc(url)}" loading="lazy"><span class="lbl">{_esc(best["ext"])}'
                f'</span></a>')
    return (f'<a class="filebtn play" href="{_esc(url)}" target="_blank">&#9654; '
            f'<span class="lbl">{_esc(best.get("ext"))}</span></a>')


def _state(m):
    if any(f.get("path") for f in m["files"]):
        return ("decrypted" if any(f.get("state", "").startswith("decrypted") for f in m["files"])
                else "plaintext")
    if m["meo"] and m["key_state"] == "My Eyes Only — key wrapped":
        return "My Eyes Only — locked"
    if any(f.get("state", "").startswith("encrypted") for f in m["files"]):
        return "encrypted"
    if m["files"]:
        return "not on disk"
    return "not cached"


def _grid(pairs):
    return '<div class="grid">' + "".join(
        f'<div class="k">{k}</div><div class="v">{v}</div>' for k, v in pairs if v not in (None, "")
    ) + "</div>"


def _value_html(col, value, timefmt):
    if value is None or value == "":
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        text = raw.decode("utf-8", "replace") if raw.isascii() else raw[:48].hex()
        return (f'<span class="mono">{_esc(text)}</span>'
                f' <span class="muted">({len(raw)} bytes)</span>')
    if col in _MS_COLS and _int(value):
        return f'{_esc(_ms_fmt(value, timefmt))} <span class="muted">raw {_esc(value)}</span>'
    return _esc(value)


def _files_html(m, src_root, fs_records, epochfmt):
    if not m["files"]:
        return ('<div class="muted">No cached file names this snap: no cache_controller.db claim '
                'carries its id or media id, and no cached file is the content-addressed copy of '
                'one of its URLs. The device did not keep this media, or kept it where this report '
                'does not look.</div>')
    rows = []
    for f in m["files"]:
        if f.get("cache_key"):
            link = (f'<a class="chip" target="scauto_cache" href="../CacheController/'
                    f'CacheController_report.html#ck-{_esc(f["cache_key"])}">cache_controller entry'
                    f'</a><div class="mono small">{_esc(f["cache_key"])}</div>')
        else:
            link = f'<span class="mono small">{_esc(f.get("source"))}</span>'

        view = ""
        if f.get("path"):
            view = (f'<a href="{_esc(f["path"])}" target="_blank">{_esc(os.path.basename(f["path"]))}'
                    f'</a>')
            if f.get("ext") in ("jpg", "png", "webp", "gif"):
                view = (f'<a href="{_esc(f["path"])}" target="_blank"><img class="prev" '
                        f'src="{_esc(f["path"])}" loading="lazy"></a><br>') + view
        hashes = "".join(f'<div class="mono small">{_esc(label)}: MD5 {md5}<br>SHA-256 {sha}</div>'
                         for label, md5, sha in f.get("hashes") or [])
        paths = "".join(
            f'<div class="mono small">{_esc(device_path(p, src_root))}</div>'
            + report_ui.device_fs_html([fs_records.get(manifest_key(p))], epochfmt)
            for p in f["paths"][:6])
        if len(f["paths"]) > 6:
            paths += f'<div class="muted">… and {len(f["paths"]) - 6} more part(s)</div>'
        extra = ""
        if f.get("tail_ok") is False:
            extra = ('<div class="warn small">&#9888; incomplete — the decrypted bytes end without '
                     'valid padding: the cache holds only part of the file</div>')
        rows.append(f'<tr><td>{view}</td><td>{_esc(f.get("state"))}{extra}</td>'
                    f'<td>{_esc(f.get("role"))}<div class="muted small">{_esc(f.get("basis"))}</div>'
                    f'</td><td>{link}</td><td>{hashes}{paths}</td></tr>')
    return ('<table class="sub"><tr><th>File</th><th>State</th><th>Role / how it was linked'
            f'{report_ui.info_icon(LINK_NOTE)}</th><th>Found through</th>'
            '<th>Hashes / where it is on the device</th></tr>' + "".join(rows) + "</table>")


def _detail(m, timefmt, epochfmt, src_root, fs_records):
    row = m["row"]
    ordered = [c for c in _SNAP_FIRST if c in row] + sorted(
        c for c in row if c not in _SNAP_FIRST and c not in _KEY_COLS and c not in _URL_COLS)
    snap_grid = _grid([(f'<span class="mono">{_esc(c)}</span>', _value_html(c, row.get(c), timefmt))
                       for c in ordered])
    entry_pairs = [(f'<span class="mono">{_esc(c)}</span>', _value_html(c, v, timefmt))
                   for c, v in sorted(m["entry"].items())]
    for col in ("snap_ids", "highlighted_snap_ids"):
        ids = flatbuffers_doc.string_vector_field(m["entry"].get(col)) if m["entry"] else None
        if ids:
            entry_pairs.append((f'<span class="mono">{col}</span> <span class="muted">(read)</span>',
                                "<br>".join(_esc(i) for i in ids)))
    entry_grid = _grid(entry_pairs) if m["entry"] else (
        '<div class="muted">No memories_entry row for this snap\'s memories_entry_id.</div>')
    media_grid = _grid([(f'<span class="mono">{_esc(c)}</span>', _value_html(c, v, timefmt))
                        for c, v in sorted(m["media"].items())]) if m["media"] else (
        '<div class="muted">No memories_media row for this snap\'s media_id.</div>')
    k = m.get("key") or {}
    key_rows = [
        ('<span class="mono">media_key</span>',
         (_esc(row.get("media_key")) + f' <span class="muted">→ {_esc(k.get("key_how"))}'
          + (f', {len(k["key"])} bytes' if k.get("key") else "") + "</span>")
         if row.get("media_key") not in (None, "") else '<span class="muted">not stored</span>'),
        ('<span class="mono">media_iv</span>',
         (_esc(row.get("media_iv")) + f' <span class="muted">→ {_esc(k.get("iv_how"))}</span>')
         if row.get("media_iv") not in (None, "") else '<span class="muted">not stored</span>'),
        ('<span class="mono">encrypted_media_key</span>', _esc(row.get("encrypted_media_key"))),
        ('<span class="mono">encrypted_media_iv</span>', _esc(row.get("encrypted_media_iv"))),
    ]
    if k.get("unwrapped"):
        key_rows.append(("My Eyes Only key", _esc(k["unwrapped"])))
    key_grid = _grid(key_rows)
    urls = "".join(f'<div><span class="mono small">{_esc(col)}</span><br>'
                   f'<span class="mono small url">{_esc(url)}</span></div>' for col, url in _urls(m))
    geo = ""
    if m["latitude"] is not None:
        lat, lon = m["latitude"], m["longitude"]
        geo = (f'{lat:.6f}, {lon:.6f} &middot; '
               f'<a target="_blank" rel="noopener" href="https://www.openstreetmap.org/?mlat={lat}'
               f'&mlon={lon}#map=16/{lat}/{lon}">OpenStreetMap</a> &middot; '
               f'<a target="_blank" rel="noopener" href="https://www.google.com/maps?q={lat},{lon}">'
               f'Google Maps</a>')
        if m.get("map"):
            geo += (f'<div><img class="map" src="{_esc(m["map"]["path"])}"><div class="muted small">'
                    f'Map from the examiner\'s tile server ({_esc(m["map"]["template"])}) — '
                    f'derived imagery, not device data.</div></div>')
    prior = ""
    if m.get("prior"):
        prior = ('<div class="sect">Earlier version of this row (memories.db without its -wal)'
                 + report_ui.info_icon(sqlite_open.MARKER_HELP[sqlite_open.MAIN_ONLY]) + "</div>"
                 + "".join(_grid([(f'<span class="mono">{_esc(c)}</span>',
                                   _value_html(c, v, timefmt)) for c, v in sorted(p.items())])
                           for p in m["prior"]))
    meo = (f'<div class="warn">My Eyes Only{report_ui.info_icon(MEO_NOTE)}</div>'
           if m["meo"] else "")
    return (meo
            + f'<div class="sect">Cached media{report_ui.info_icon(LINK_NOTE)}</div>'
            + _files_html(m, src_root, fs_records, epochfmt)
            + (f'<div class="sect">Location{report_ui.info_icon(LOCATION_NOTE)}</div>{geo}'
               if geo else "")
            + f'<div class="sect">memories_snap{report_ui.info_icon(TIME_NOTE)}</div>{snap_grid}'
            + f'<div class="sect">Key{report_ui.info_icon(KEY_NOTE)}</div>{key_grid}'
            + (f'<div class="sect">Download URLs</div>{urls}' if urls else "")
            + f'<div class="sect">memories_entry</div>{entry_grid}'
            + f'<div class="sect">memories_media</div>{media_grid}'
            + prior)


_CSS = """
 .vcells>.vc{font-size:12.5px}
 .vcells>.vc.c0{color:#2d2d71;font-weight:700;text-align:center}
 .vr.open .vc.c0{color:#8a1f5a}
 .vcells>.vc.c8{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#33367a}
 .filebtn{display:inline-flex;align-items:center;gap:4px;text-decoration:none;color:#2d2d71;
   border:1px solid #d6d6e6;border-radius:5px;padding:1px 5px;background:#fff;max-height:66px}
 .filebtn.img img{height:60px;max-width:84px;object-fit:cover;border-radius:3px}
 .filebtn .lbl{font-size:10px;color:#555}
 .none{color:#999;font-size:11px}
 .meo{background:#3a1f5a;color:#fff;border-radius:3px;font-size:9px;font-weight:700;padding:1px 4px}
 .grid{display:grid;grid-template-columns:230px 1fr;gap:3px 12px;font-size:12px;margin:4px 0 8px}
 .grid .k{color:#555} .mono{font-family:ui-monospace,Consolas,monospace;color:#33367a}
 .small{font-size:10.5px} .muted{color:#888} .url{word-break:break-all}
 .warn{background:#fff4e5;border:1px solid #f0c080;border-radius:5px;padding:4px 8px;margin:4px 0}
 .sect{font-weight:700;color:#2d2d71;margin:10px 0 3px;font-size:12.5px}
 table.sub{border-collapse:collapse;font-size:11.5px;margin:4px 0 8px}
 table.sub td,table.sub th{border:1px solid #e0e0ea;padding:3px 6px;vertical-align:top;text-align:left}
 img.prev{max-height:150px;max-width:220px} img.map{max-width:420px;margin-top:6px}
 .chip{font-size:11px}
"""


def generate_report(memories, outdir, tz_label, run_id, timefmt, epochfmt, src_root, fs_records,
                    meo_rows, db_info):
    os.makedirs(outdir, exist_ok=True)
    data_dir = os.path.join(outdir, "data")
    ordered = sorted(memories.values(),
                     key=lambda m: (-(_int(m["row"].get("create_time")) or 0), m["snap_id"]))
    details = [(f'mem-{m["snap_id"]}', _detail(m, timefmt, epochfmt, src_root, fs_records))
               for m in ordered]
    chunk_of = report_ui.write_details(data_dir, details)
    rows, states = [], {}
    for m in ordered:
        row = m["row"]
        state = _state(m)
        states[state] = states.get(state, 0) + 1
        created, captured = _ms_fmt(row.get("create_time"), timefmt), _ms_fmt(
            row.get("snap_capture_time"), timefmt)
        geo = (f'{m["latitude"]:.5f}, {m["longitude"]:.5f}' if m["latitude"] is not None else "")
        title = m["entry"].get("title") or ""
        entry_cell = (('<span class="meo">My Eyes Only</span> ' if m["meo"] else "")
                      + _esc(title))
        cells = ["&#9656;", _media_cell(m), _esc(created), _esc(captured), _esc(_kind(m)),
                 _esc(geo) or '<span class="none">&mdash;</span>', entry_cell or "",
                 _esc(state), _esc(m["snap_id"])]
        search = " ".join(str(x) for x in (
            m["snap_id"], m["media_id"], title, created, captured, geo, state,
            row.get("time_zone_id") or "", " ".join(f["cache_key"] for f in m["files"]),
            " ".join(u for _c, u in _urls(m)), "my eyes only" if m["meo"] else "")).lower()
        rows.append([f'mem-{m["snap_id"]}', cells, search,
                     {"2": _int(row.get("create_time")) or 0,
                      "3": _int(row.get("snap_capture_time")) or 0, "4": _kind(m),
                      "7": state, "8": m["snap_id"]},
                     chunk_of.get(f'mem-{m["snap_id"]}'),
                     {"state": state, "geo": "y" if geo else "n", "meo": "y" if m["meo"] else "n"}])
    report_ui.write_rows(data_dir, rows)

    total = len(ordered)
    located = sum(1 for m in ordered if m["latitude"] is not None)
    with_media = sum(1 for m in ordered if any(f.get("path") for f in m["files"]))
    n_meo = sum(1 for m in ordered if m["meo"])
    state_opts = "".join(f'<option value="{_esc(s)}">{_esc(s)} ({n})</option>'
                         for s, n in sorted(states.items()))
    meo_block = ""
    if meo_rows:
        cells = "".join(
            "<tr>" + "".join(f'<td class="mono small">{_esc(r.get(c))}</td>'
                             for c in ("user_id", "hashed_passcode", "master_key", "master_key_iv"))
            + "</tr>" for r in meo_rows)
        meo_block = ('<details class="meoconf"><summary>memories_meo_confidential — '
                     f'{len(meo_rows)} row(s){report_ui.info_icon(MEO_NOTE)}</summary>'
                     '<table class="sub"><tr><th>user_id</th><th>hashed_passcode</th>'
                     f'<th>master_key</th><th>master_key_iv</th></tr>{cells}</table></details>')
    doc = (f'<!doctype html><html><head><meta charset="utf-8"><title>Snapchat Memories</title>'
           f'<style>{report_ui.PAGE_CSS}{_CSS}{report_ui.VTABLE_CSS}{report_ui.NAV_CSS}'
           f'{report_ui.SELECT_CSS}{report_ui.HINT_CSS}{report_ui.DEVICE_FS_CSS}</style>'
           f'<script>window.SCAUTO_RUN={json.dumps(run_id)};'
           f'window.SCAUTO_VERSION={json.dumps(app_version.get_version())};'
           f'window.SCAUTO_SELKIND="mem";</script>'
           f'<script>{report_ui.SELECT_JS}</script><script src="../selection.js"></script>'
           f'<script>{report_ui.VTABLE_JS}</script></head><body>'
           f'<header><h1>Snapchat Memories (Android)</h1>'
           f'<div class="sum"><b>{total}</b> snap(s) in memories.db &middot; <b>{with_media}</b> with '
           f'cached media recovered &middot; <b>{located}</b> with a location &middot; '
           f'<b>{n_meo}</b> in My Eyes Only &middot; times in <b>{_esc(tz_label)}</b></div>'
           f'<div class="sum muted">memories.db: {_esc(sqlite_open.describe(db_info))}</div>'
           f'</header>'
           + (report_ui.missing_data_banner("Memories_report.html") if total else "")
           + meo_block +
           '<div class="stickytop"><div class="toolbar">'
           '<input type="search" id="q" placeholder="Search snap id, media id, title, date, '
           'cache key, URL…" oninput="flt()">'
           f'<label>Media <select id="state" onchange="flt()"><option value="">any</option>'
           f'{state_opts}</select></label>'
           '<label>Location <select id="geo" onchange="flt()"><option value="">any</option>'
           '<option value="y">with a location</option><option value="n">without</option>'
           '</select></label>'
           '<label>My Eyes Only <select id="meo" onchange="flt()"><option value="">any</option>'
           '<option value="y">yes</option><option value="n">no</option></select></label>'
           f'{report_ui.clear_filters_button("memory")}'
           '<span id="count" style="color:#555"></span></div>'
           f'<div class="toolbar">{report_ui.selection_toolbar("memory")}</div>'
           '<div class="pager" id="pager"></div>'
           f'<div class="vhdr" id="vhdr" style="grid-template-columns:30px {MEM_COLS}">'
           '<div class="vc sel"><input type="checkbox" class="selall" '
           'onclick="SCV.selectShown(this.checked)"></div>'
           '<div class="vc nosort"></div><div class="vc nosort">Media</div>'
           f'<div class="vc" onclick="SCV.setSort(2)">Created{report_ui.info_icon(TIME_NOTE)} '
           '<span class="ar">&#8597;</span></div>'
           '<div class="vc" onclick="SCV.setSort(3)">Captured <span class="ar">&#8597;</span></div>'
           '<div class="vc" onclick="SCV.setSort(4)">Type <span class="ar">&#8597;</span></div>'
           f'<div class="vc nosort">Location{report_ui.info_icon(LOCATION_NOTE)}</div>'
           '<div class="vc nosort">Memory</div>'
           '<div class="vc" onclick="SCV.setSort(7)">Media state <span class="ar">&#8597;</span></div>'
           '<div class="vc" onclick="SCV.setSort(8)">Snap ID <span class="ar">&#8597;</span></div>'
           '</div></div>'
           '<div class="vwrap" id="vwrap"><div class="vpad" id="vpad"></div>'
           '<div class="vwin" id="vwin"></div></div>'
           '<div class="vempty" id="vempty" style="display:none">No Memory matches the current '
           'filters.</div>'
           '<script src="data/index.js"></script>'
           f'<script>{report_ui.HINT_JS}{report_ui.NAV_JS}{report_ui.SELECT_TOOLBAR_JS}'
           'var flt_t=0;'
           'function flt(){clearTimeout(flt_t);flt_t=setTimeout(function(){SCV.refilter();},120);}'
           'SCV.init({mount:"vwrap",win:"vwin",pad:"vpad",header:"#vhdr",missing:"vmiss",'
           'empty:"vempty",pager:"pager",pageSize:500,selKind:"mem",sort:2,sortDir:-1,'
           'emptyAll:"memories.db holds no snap.",'
           f'rowHeight:{MEM_ROW_H},estDetail:320,cols:"{MEM_COLS}",detailBase:"data/detail-",'
           'query:function(){return document.getElementById("q").value;},'
           'match:function(m,r){var s=document.getElementById("state").value,'
           'g=document.getElementById("geo").value,e=document.getElementById("meo").value;'
           'return (!s||m.state===s)&&(!g||m.geo===g)&&(!e||m.meo===e)'
           '&&scSelPass("mem",SCV.selId(r[0]));},'
           'selectedOnly:scSelOnly,selCount:scSelCount,'
           'count:function(n,t){document.getElementById("count").textContent='
           'n===t?(n+" snaps"):(n+" of "+t+" shown");},'
           'reset:function(){document.getElementById("q").value="";'
           'document.getElementById("state").value="";document.getElementById("geo").value="";'
           'document.getElementById("meo").value="";document.getElementById("selonly").value="";}});'
           'scSelNote();scConsumeHash();</script></body></html>')
    report = os.path.join(outdir, "Memories_report.html")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return report


# --------------------------------------------------------------------------- entry

def main(layout, outdir, tz="local", padding="both", tile_server="", report_dir=None):
    """Build the Android Memories report for one :class:`android_layout.AndroidLayout`."""
    db = layout.db("memories")
    if not db:
        logger.info("Memories: memories.db not present — no Memories report")
        return None
    memories, info, extra = load_memories(db)
    timefmt, tz_label = make_time_formatter(tz)
    epochfmt = lambda seconds: timefmt(float(seconds) - _COCOA_EPOCH) if seconds else ""  # noqa: E731
    rdir = report_dir or os.path.dirname(os.path.abspath(outdir))
    run_id = report_ui.run_id(rdir)
    report_ui.write_selection_stub(rdir, run_id)
    os.makedirs(outdir, exist_ok=True)
    published = collect_media(memories, layout.app, outdir, padding=padding,
                              file_manager=layout.file_manager, meo_rows=extra.get("meo") or [])
    render_maps(memories, outdir, tile_server)
    write_manifests(memories, outdir)
    fs_records = load_fs_records(layout.root)
    report = generate_report(memories, outdir, tz_label, run_id, timefmt, epochfmt, layout.root,
                             fs_records, extra.get("meo") or [], info)
    total = len(memories)
    logger.info(f"Memories report: {os.path.abspath(report)}")
    logger.info(f"  {total} snap(s), {sum(1 for m in memories.values() if m['files'])} with a cached "
                f"file linked, {published} media file(s) published, "
                f"{sum(1 for m in memories.values() if m['latitude'] is not None)} with a location, "
                f"{sum(1 for m in memories.values() if m['meo'])} in My Eyes Only")
    return report
