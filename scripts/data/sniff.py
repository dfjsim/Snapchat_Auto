"""
Shared content sniffing — what a file's bytes actually are, independent of its name.

Every cache in this app is content-addressed: the filename is a hash or a cache key and says
nothing about the format. So identification is by **magic bytes only**, never by extension.

Why this module exists
----------------------
The cache_controller report used to identify files with :func:`guess_media` alone, which knows
four formats (JPEG/MP4/PNG/WebP), and labelled everything it did not recognise
"🔒 encrypted". Measured across four test devices, 600 files carried that padlock and only **19**
were genuinely encrypted: the rest were 480 LZC lens bundles, 27 protobuf blobs, 10 WEBVTT
subtitle tracks, 9 ZIP archives, 9 JSON/text files, 4 HTML pages, 4 TrueType fonts and 2 binary
plists. Calling all of that "encrypted" tells an examiner that evidence is locked away when it is
in fact readable, and buries the handful of files that really are encrypted.

:func:`classify` is therefore deliberately conservative about the word "encrypted": it is used
only for bytes that look like a block cipher's output — high Shannon entropy *and* a length that
is a multiple of the AES block size. Anything merely unrecognised is reported as unrecognised.

The same principle applies to the type a file is *given*. An ISO base media file's magic bytes say
only "this is an ISO base media file"; its **major brand** says whether it holds a video, an audio
recording or a still photo. Stopping at the magic bytes and calling all of them ".mp4" is not a
neutral simplification — it tells the examiner a voice note is a video. :func:`guess_media` reads
the brand.
"""

import math
import collections

# --------------------------------------------------------------------------- media magic bytes


# An ISO base media file (".....ftyp....") is a *container*, and its major brand — the four bytes
# right after `ftyp` — says what is actually in it. They all start with the same magic bytes, so a
# check that stops at `ftyp` calls an audio recording, a still photo and a video the same thing:
# ".mp4". That is a statement about content, and on the test corpus it was a false one — a cached
# 1,859-byte M4A audio file was reported as a video (and then handed to a video decoder, which hung
# on it). Only brands whose meaning is unambiguous are mapped; anything else stays "mp4", which is
# what the generic brands (isom, mp42, iso2/4/5/6, avc1, dash) do mean.
_FTYP_BRANDS = {
    # audio-only containers
    b"M4A ": "m4a", b"M4B ": "m4a", b"M4P ": "m4a", b"M4R ": "m4a",
    b"F4A ": "m4a", b"F4B ": "m4a",
    # QuickTime
    b"qt  ": "mov",
    # iTunes video
    b"M4V ": "m4v", b"M4VH": "m4v", b"M4VP": "m4v",
    # HEIF still images / sequences
    b"heic": "heic", b"heix": "heic", b"heim": "heic", b"hesp": "heic",
    b"hevc": "heic", b"hevx": "heic", b"mif1": "heic", b"msf1": "heic",
    # AV1 still images
    b"avif": "avif", b"avis": "avif",
    # 3GPP
    b"3gp4": "3gp", b"3gp5": "3gp", b"3g2a": "3gp",
}


def guess_media(data):
    """Return a file extension for known media magic bytes, else None.

    Deliberately narrow about *what counts as media*: this is the predicate the Memories
    decrypt-and-match linker uses to decide whether a candidate key produced real media, so anything
    it accepts becomes an evidential claim. Broaden :func:`sniff_content` instead.

    It is not narrow about *which* media: an ISO base media file's major brand is read
    (:data:`_FTYP_BRANDS`) so an audio container or a still image is not reported as a video. The
    set of byte patterns accepted is unchanged by that — only the extension returned for them — so
    the linker's acceptance is exactly what it always was.
    """
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(bytes(data[8:12]), "mp4")
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def sniff_content(head):
    """Return a ``(kind, extension)`` for a buffer's magic bytes. ``kind`` groups it for a report.

    Sniffing is by **magic bytes, never by name or extension**: the story thumbnails are images
    wrapped in an NSKeyedArchiver plist, so a naive ftyp/JPEG check misses every one of them, and
    ``sccache.dynamic-caption.data`` looks encrypted but is a font cache.
    """
    if not head:
        return ("empty", "")
    # jpg / png / webp, or the ftyp brand's real type. Audio stays kind "media", as mp3 and ogg
    # below already are — "media" groups a row for a report, and the extension is what says whether
    # it is a video, a photo or a recording.
    ext = guess_media(head)
    if ext:
        return ("media", ext)
    if head[:8] == b"bplist00":
        return ("bplist", "plist")
    if head[:4] == b"TSAF":
        return ("tsaf", "tsaf")
    if head[:4] == b"\x00\x01\x00\x00" or head[:4] == b"OTTO" or head[:4] == b"true":
        return ("font", "ttf")
    if head[:4] == b"LZC\x00":
        return ("lzc", "lzc")
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return ("zstd", "zst")
    if head[:2] == b"\x1f\x8b":
        return ("gzip", "gz")
    if head[:4] == b"PK\x03\x04":
        return ("zip", "zip")
    if head[:3] == b"GIF":
        return ("media", "gif")
    if head[:6] == b"SQLite":
        return ("sqlite", "db")
    if head[:5] == b"%PDF-":
        return ("media", "pdf")
    if head[:6] == b"WEBVTT":
        return ("webvtt", "vtt")
    if head[:2] == b"\xff\xfb" or head[:3] == b"ID3":
        return ("media", "mp3")
    if head[:4] == b"OggS":
        return ("media", "ogg")
    stripped = head.lstrip(b" \t\r\n\xef\xbb\xbf")
    if stripped[:1] in (b"{", b"[") or stripped[:5] == b"<?xml":
        return ("text", "json")
    if stripped[:9].lower() == b"<!doctype" or stripped[:5].lower() == b"<html":
        return ("html", "html")
    return ("unknown", "")


# --------------------------------------------------------------------------- encryption test

# Shannon entropy at or above this (bits per byte) means the bytes carry no exploitable structure.
# Compressed data reaches ~7.9 too, which is why a magic-byte check runs first and why block
# alignment has to agree before anything is called encrypted.
_ENTROPY_ENCRYPTED = 7.5
# Below this many bytes entropy is not meaningful — a 64-byte sample maxes out at 6 bits/byte.
_ENTROPY_MIN_BYTES = 512
_AES_BLOCK = 16


def entropy(data):
    """Shannon entropy of ``data`` in bits per byte (0.0 for an empty buffer)."""
    if not data:
        return 0.0
    counts = collections.Counter(data)
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def classify(head, size):
    """Describe a file's bytes for a report.

    ``head`` is the first few KB, ``size`` the file's full length on disk (needed for the block
    alignment test, which the head alone cannot answer).

    Returns ``(kind, ext, label, encrypted)``:
      * ``kind``/``ext``  — as :func:`sniff_content`
      * ``label``         — a short human phrase for the report's File column
      * ``encrypted``     — True only for bytes that behave like block-cipher output

    The bar for ``encrypted`` is deliberately high. Being unable to identify something is not
    evidence that it is encrypted, and reporting it as such sends an examiner looking for a key
    that does not exist.
    """
    kind, ext = sniff_content(head)
    if kind == "empty":
        return kind, ext, "0 bytes", False
    if kind != "unknown":
        return kind, ext, _KIND_LABELS.get(kind, ext or kind), False
    aligned = size > 0 and size % _AES_BLOCK == 0
    ent = entropy(head[:65536])
    if len(head) >= _ENTROPY_MIN_BYTES and ent >= _ENTROPY_ENCRYPTED:
        if aligned:
            return kind, ext, "encrypted", True
        return kind, ext, "high entropy, not block-aligned", False
    if looks_protobuf(head):
        return kind, ext, "protobuf", False
    if aligned and len(head) < _ENTROPY_MIN_BYTES:
        # too short to judge by entropy; alignment alone is weak evidence, so say so
        return kind, ext, "unrecognized, block-aligned", False
    return kind, ext, "unrecognized", False


_KIND_LABELS = {
    "bplist": "binary plist",
    "tsaf": "TSAF container",
    "font": "font",
    "lzc": "lens bundle (LZC)",
    "zstd": "zstd archive",
    "gzip": "gzip",
    "zip": "zip archive",
    "sqlite": "SQLite database",
    "text": "text / JSON",
    "html": "HTML",
    "webvtt": "WEBVTT subtitles",
}


def looks_protobuf(data, want_fields=2, scan_bytes=64):
    """Heuristic: do the first bytes parse as a protobuf message?

    Snapchat stores a lot of small unframed protobuf blobs in its caches. They have no magic bytes,
    so the only way to tell one from random data is to walk its tag/wire-type structure. This is a
    heuristic and is labelled as such wherever it is shown — it decides how a file is *described*,
    never what is claimed about its content.
    """
    i = 0
    fields = 0
    limit = min(len(data), scan_bytes)
    try:
        while i < limit and fields < want_fields + 1:
            tag = data[i]
            i += 1
            wire, field_no = tag & 7, tag >> 3
            if field_no == 0 or wire in (3, 4, 6, 7):
                return False
            if wire == 0:                                      # varint
                while i < len(data) and data[i] & 0x80:
                    i += 1
                i += 1
            elif wire == 2:                                    # length-delimited
                length, shift = 0, 0
                while i < len(data):
                    byte = data[i]
                    i += 1
                    length |= (byte & 0x7F) << shift
                    shift += 7
                    if not byte & 0x80:
                        break
                    if shift > 28:
                        return False
                i += length
            elif wire == 5:                                    # 32-bit
                i += 4
            elif wire == 1:                                    # 64-bit
                i += 8
            fields += 1
        return fields >= want_fields
    except Exception:
        return False
