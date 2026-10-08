"""Chat media the cache holds only in pieces, rebuilt so the chat reports can show it.

``ParseSnapchat_iOS.mergeCache`` — the join both platforms share — copies a claim's file for a chat
message only when a **whole** file named after its CACHE_KEY is media. The same cache stores chat media
two other ways (docs/report_cache_controller.md): as byte-range shards, and as a bundle whose
CACHE_KEY file is a small descriptor while the content sits in child files. :func:`materialize_chat_media`
rebuilds those (and tries the key / IV pairs a message carries on a file that is not plaintext) into a
folder the join searches first, and :func:`chat_cache_key` tells the Conversations report how each
attachment it shows was put together. Used by the iOS and the Android run alike.
"""
import logging
import os
import re

from scripts.data import sqlite_open

logger = logging.getLogger(__name__)


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
    largest media child is taken. A chat video is regularly a bundle, so without this every chat video
    stored that way would be listed as having no cached file — on Android always (it has no
    ``SCPersistentMedia``), on iOS whenever no saved copy named after its conversation, message and part
    stood in for it.

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
        if whole:
            try:
                with open(whole[0], "rb") as fh:
                    head = fh.read(16)
            except OSError:
                continue
            if guess_media(head):
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
            if whole and pairs:
                try:
                    with open(whole[0], "rb") as fh:
                        candidates.append((key, fh.read()))
                except OSError:
                    pass
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
    from scripts import ParseSnapchat_iOS
    return ParseSnapchat_iOS.cacheControllerKey(basename)


def message_blobs(arroyo):
    """``{(conversation, server message id): message_content}`` — the key / IV pairs a cached chat
    file is tried with come from the content of the message its claim names."""
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
    return blobs
