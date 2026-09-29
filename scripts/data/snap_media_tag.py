"""
The tag the Snapchat app writes into a media file it encodes: a base64 string in the file's
*description* field whose bytes are a protobuf naming the app version, the device, the operating
system and the lens used. The tag was found by Keban Bronsario (see docs/snapchat_media_tag.md).

Where it is found, as `media_meta` reads it (every text field a file carries is offered to
:func:`decode`, so a new carrier needs no change here):

* ``moov › udta › dscp`` — the 3GPP asset description box of an MP4;
* ``moov › meta``, key ``com.apple.quicktime.description`` — QuickTime metadata of a MOV;
* ``moov › udta › meta › ilst › desc`` — the iTunes-style description an ffmpeg muxer writes;
* EXIF ``UserComment`` of an image — met here as XMP ``exif:UserComment``, where an editing
  program copied a Snapchat-saved image's EXIF into the list of files an edit was made from.

The layout::

    1 (message) {
      1: string  Snapchat/<appVersion> (<deviceModel>; <operatingSystem>; gzip)
      2: int64   the lens id — a repeated field, written packed (wire type 2) or unpacked
                 (wire type 0); both are valid encodings of a repeated field and both occur
      4: varint  shown as stored, without a meaning attached
    }

**The one rule: the value is the tag only when all of it parses** — the whole string is base64,
every byte of both protobuf levels is accounted for, and field 1.1 is UTF-8 text of the form
``Snapchat/<version> (<…>)``, optionally followed by more text. Anything else is the text it is
and stays shown as stored.

Field 1.1 has the shape of an HTTP user agent. The parts inside its parentheses are kept exactly as
written: the device model identifier (``iPhone<n>,<m>`` on iOS, the manufacturer's model number on
Android) and the operating system with its version — which on Android carries further
``#``-separated values that are not interpreted here. Whatever follows the operating system,
inside the parentheses or after them (the Android app appends a ``V/<name>`` token), is kept as
written too.

Field 1.2 is the id of the lens used. Each value checked also appears in the app's own lens records
on the device that holds the file — a ``<prefix>_<lensId>_lens_central`` document key and a
``LENSES`` row in ``rtus.db`` for a Memory saved by the iOS app; an ``SCStoriesSnapLens`` archive
in ``content_feed_database`` for a story snap written by the Android app (verified on two devices).

What the tag names is the app instance that **wrote** the file: for a received snap or story that
is the sender's device; for a Memory it is the device that saved it — Memories follow the account,
so that can be a phone other than the one extracted. A file without the tag says nothing: older
versions of the app write none, and anything that re-encodes the file drops it.

Standard library only; :func:`decode` never raises.
"""

import re
import base64
import binascii

_UA = re.compile(r"^Snapchat/(\S+) \(([^()]*)\)(?: +(\S.*))?$")
_B64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_MIN_TEXT = 16                 # shorter than any tag: a UA alone is longer once encoded
_MAX_TEXT = 64 * 1024          # far beyond any tag; bounds the work on a hostile field
_MAX_FIELDS = 256              # fields per message level
_MAX_STORED_BYTES = 64         # bytes of an unknown binary field shown as hex
_WIRE_NAMES = {0: "varint", 1: "64-bit", 5: "32-bit"}


def _varint(buf, pos):
    """``(value, next position)``, or ``(None, pos)`` for a truncated or over-long varint."""
    value = shift = 0
    for _ in range(10):
        if pos >= len(buf):
            return None, pos
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    return None, pos


def fields(buf):
    """One protobuf message level read straight off the wire: ``[(field, wire type, value)]``, the
    value an int for wire types 0/1/5 and bytes for 2 — or ``None`` unless every byte is accounted
    for. Groups (wire types 3/4) and field number 0 are rejected, which is what keeps ordinary text
    from passing as a message."""
    out, pos, end = [], 0, len(buf)
    while pos < end:
        if len(out) >= _MAX_FIELDS:
            return None
        key, pos = _varint(buf, pos)
        if key is None:
            return None
        field, wire = key >> 3, key & 7
        if field == 0:
            return None
        if wire == 0:
            value, pos = _varint(buf, pos)
            if value is None:
                return None
        elif wire == 1:
            if pos + 8 > end:
                return None
            value, pos = int.from_bytes(buf[pos:pos + 8], "little"), pos + 8
        elif wire == 2:
            length, pos = _varint(buf, pos)
            if length is None or pos + length > end:
                return None
            value, pos = bytes(buf[pos:pos + length]), pos + length
        elif wire == 5:
            if pos + 4 > end:
                return None
            value, pos = int.from_bytes(buf[pos:pos + 4], "little"), pos + 4
        else:
            return None
        out.append((field, wire, value))
    return out


def _int64(value):
    return value - (1 << 64) if value >= (1 << 63) else value


def _packed(buf):
    """The varints a packed repeated field holds, or None unless they account for every byte."""
    values, pos = [], 0
    while pos < len(buf):
        value, pos = _varint(buf, pos)
        if value is None:
            return None
        values.append(value)
    return values or None


def _as_stored(wire, value):
    """A field this module attaches no meaning to, as display text."""
    if wire == 2:
        try:
            text = value.decode("utf-8")
            if text.isprintable():
                return text
        except UnicodeDecodeError:
            pass
        more = "…" if len(value) > _MAX_STORED_BYTES else ""
        return f"{len(value)} bytes: {value[:_MAX_STORED_BYTES].hex()}{more}"
    return f"{value} ({_WIRE_NAMES[wire]})"


def decode(text):
    """The tag in ``text``, or ``None`` when ``text`` is not one (see the module docstring's rule).

    Returns ``{user_agent, app_version, device, os, ua_extra, lens_ids, lens_encoding, stored,
    encoded}``: the user agent and its parts as written (``ua_extra`` is whatever follows the
    operating system, e.g. ``gzip``), the lens ids as signed 64-bit integers, how they were encoded
    (``packed`` / ``unpacked``), every other field as ``(path, text)`` shown as stored, and the
    base64 text the tag was decoded from.
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(text, str):
        return None
    encoded = text.strip().strip("\x00").strip()
    if not _MIN_TEXT <= len(encoded) <= _MAX_TEXT or not _B64.match(encoded):
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None

    top = fields(raw)
    outer = [(wire, value) for field, wire, value in top or () if field == 1]
    if len(outer) != 1 or outer[0][0] != 2:
        return None
    body = fields(outer[0][1])
    agent = [(wire, value) for field, wire, value in body or () if field == 1]
    if len(agent) != 1 or agent[0][0] != 2:
        return None
    try:
        user_agent = agent[0][1].decode("utf-8")
    except UnicodeDecodeError:
        return None
    match = _UA.match(user_agent)
    if not match:
        return None

    lens_ids, encodings, stored = [], set(), []
    for field, wire, value in body:
        if field == 1:
            continue
        if field == 2 and wire == 0:
            lens_ids.append(_int64(value))
            encodings.add("unpacked")
            continue
        if field == 2 and wire == 2:
            values = _packed(value)
            if values:
                lens_ids += [_int64(v) for v in values]
                encodings.add("packed")
                continue
        stored.append((f"1.{field}", _as_stored(wire, value)))
    for field, wire, value in top:
        if field != 1:
            stored.append((str(field), _as_stored(wire, value)))

    parts = [part.strip() for part in match.group(2).split(";")]
    extra = [p for p in parts[2:] if p] + ([match.group(3).strip()] if match.group(3) else [])
    return {"user_agent": user_agent, "app_version": match.group(1),
            "device": parts[0], "os": parts[1] if len(parts) > 1 else "",
            "ua_extra": extra, "lens_ids": lens_ids,
            "lens_encoding": " + ".join(sorted(encodings)), "stored": stored,
            "encoded": encoded}
