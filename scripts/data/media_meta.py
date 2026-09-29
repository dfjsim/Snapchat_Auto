"""
Metadata carried **inside** a media file — EXIF, XMP and PNG text in images; in ISO base media
files the ``moov`` and track headers, QuickTime user data (``udta``, including the 3GPP asset boxes
such as ``dscp``), QuickTime metadata (``moov › meta``) and XMP — read so a report can set it beside
the timestamps the app's databases hold. Two things inside those fields get a reading of their own:
the Snapchat app's tag (`snap_media_tag`) and the source files an editing program lists in XMP.

Why this module exists
----------------------
Every timestamp the Memories report shows comes from a database row or from the extraction
archive. The media file itself carries its own: an EXIF ``DateTimeOriginal`` written by the camera,
an ``mvhd`` creation time written by the encoder, an XMP ``CreateDate``. Those are independent
witnesses — a different program wrote them, at a different moment, from a different clock — which is
exactly why an examiner wants them next to the database's, and why they must be labelled with
where they came from and what clock they are on rather than folded into one column.

Three rules the readers here follow:

* **Every value says what clock it is on, and how that is known.** An EXIF ``DateTime*`` is a wall
  clock with no zone unless the file also carries ``OffsetTime*`` (EXIF 2.31); a QuickTime
  ``com.apple.quicktime.creationdate`` is ISO 8601 with its offset — those zones are *stated* by the
  file. An ``mvhd`` time and the EXIF GPS stamp are UTC only because the *format* defines them so;
  the file records no zone, and encoders have written local time there, so a conversion made on
  that basis is marked as an assumption. Where nothing is known, a naive wall clock is handed back
  as the string it is, never guessed into an instant.
* **Nothing is inferred from a missing field.** No EXIF means the file carries none (or was
  re-encoded without it, which is what a CDN does); it says nothing about the capture.
* **The reader never raises.** A truncated or hostile file yields ``None`` or a partial result,
  because this runs on every recovered file and one bad header must not cost the report.
* **A value is attributed to the file only when it describes the file.** An editing program writes
  into the XMP of what it exports every file that went into the edit — ``xmpMM:Ingredients`` (their
  paths) and ``xmpMM:Pantry`` (their own dates, tools, history, sometimes a GPS fix). Those values
  are stored *in* this file but describe *those* files, so they are returned apart
  (``xmp_sources``) and never reach this file's ``times`` or ``gps``.

The Snapchat app's tag is recognised in any text field by `snap_media_tag.decode`, which accepts a
value only when all of it parses; the field it was found in stays in ``other``, as stored.

Only the standard library and Pillow are used; Pillow is already a dependency for thumbnails.
HEIF/HEIC is not read (Pillow has no HEIF codec here), and the result says so rather than
reporting "no metadata". An ISO base media file is walked by seeking from box to box, so its media
data is never read.
"""

import io
import re
import struct
import logging
import calendar
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

from PIL import Image, ExifTags

from scripts.data import snap_media_tag

logger = logging.getLogger("snapchat_auto")

#: The EXIF fields shown first, in this order; everything else goes behind "all fields".
KEY_TAGS = ("Make", "Model", "Software", "LensMake", "LensModel", "Orientation",
            "ExifImageWidth", "ExifImageHeight", "ImageDescription", "Artist", "Copyright",
            "UserComment", "HostComputer", "BodySerialNumber", "LensSerialNumber", "ImageUniqueID")

#: QuickTime / ISO BMFF metadata keys shown first (``ilst`` atoms and ``keys``-namespaced names).
KEY_QT = ("com.apple.quicktime.make", "com.apple.quicktime.model",
          "com.apple.quicktime.software", "com.apple.quicktime.location.ISO6709",
          "com.apple.quicktime.description", "©mak", "©mod", "©swr", "©xyz", "©nam", "©cmt",
          "©des", "©ART", "©too", "titl", "dscp", "desc")

#: XMP properties shown first, after the container's own key fields.
KEY_XMP = ("XMP dc:title", "XMP dc:description", "XMP xmp:CreatorTool")

#: Fields every encoder writes and that say nothing about a device, a place or a moment: pixel size,
#: orientation, colour space, the EXIF version. A file carrying only these is not flagged as having
#: metadata worth a look — on a real device three quarters of the cached JPEGs carry exactly this set.
STRUCTURAL = frozenset((
    "ExifImageWidth", "ExifImageHeight", "Orientation", "ColorSpace", "ResolutionUnit",
    "XResolution", "YResolution", "YCbCrPositioning", "ExifVersion", "FlashPixVersion",
    "ComponentsConfiguration", "CompressedBitsPerPixel", "InteroperabilityIndex",
    "InteroperabilityVersion", "SceneCaptureType", "Duration (mvhd)", "Compression",
    "JpegIFOffset", "JpegIFByteCount", "PhotometricInterpretation", "SamplesPerPixel",
    "BitsPerSample", "PlanarConfiguration")) | frozenset(
    # the same facts as XMP spells them, and the stream layout an editor's XMP records
    f"XMP {name}" for name in (
        "tiff:Orientation", "tiff:XResolution", "tiff:YResolution", "tiff:ResolutionUnit",
        "tiff:YCbCrPositioning", "tiff:Compression", "tiff:PhotometricInterpretation",
        "tiff:SamplesPerPixel", "tiff:BitsPerSample", "tiff:PlanarConfiguration",
        "tiff:ImageWidth", "tiff:ImageLength", "exif:ColorSpace", "exif:PixelXDimension",
        "exif:PixelYDimension", "exif:ExifVersion", "exif:FlashpixVersion",
        "exif:ComponentsConfiguration", "exif:CompressedBitsPerPixel", "exif:SceneCaptureType",
        "xmpDM:videoFrameRate", "xmpDM:videoFieldOrder", "xmpDM:videoPixelAspectRatio",
        "xmpDM:videoFrameSize", "xmpDM:audioSampleRate", "xmpDM:audioSampleType",
        "xmpDM:audioChannelType", "xmpDM:startTimeScale", "xmpDM:startTimeSampleSize"))

_EXIF_TIME_TAGS = (("DateTimeOriginal", "OffsetTimeOriginal", "SubsecTimeOriginal"),
                   ("DateTimeDigitized", "OffsetTimeDigitized", "SubsecTimeDigitized"),
                   ("DateTime", "OffsetTime", "SubsecTime"))

_EXIF_DT = re.compile(r"^\s*(\d{4})[:\-](\d\d)[:\-](\d\d)[ T](\d\d):(\d\d):(\d\d)")
_OFFSET = re.compile(r"^\s*([+-])(\d\d):?(\d\d)\s*$")
_ISO_DT = re.compile(r"^\s*(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)(?::(\d\d))?(?:\.\d+)?\s*"
                     r"(Z|[+-]\d\d:?\d\d)?\s*$")
_ISO6709 = re.compile(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:([+-]\d+(?:\.\d+)?))?")
_XMP_DATES = ("xmp:CreateDate", "xmp:ModifyDate", "xmp:MetadataDate", "exif:DateTimeOriginal",
              "exif:DateTimeDigitized", "photoshop:DateCreated", "tiff:DateTime",
              "exif:GPSTimeStamp")
_XMP_COORD = re.compile(r"^\s*(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?))?\s*([NSEW])\s*$")
#: The zero value of a QuickTime time, as an editing program writes it into XMP: «not set».
_QT_ZERO = "1904-01-01T00:00:00Z"

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
#: The conventional prefix of each namespace, so a property reads the same whatever prefix a file
#: declared for it; any other namespace keeps the prefix the file gave it.
_XMP_PREFIX = {
    "http://ns.adobe.com/xap/1.0/": "xmp",
    "http://ns.adobe.com/xap/1.0/mm/": "xmpMM",
    "http://ns.adobe.com/xap/1.0/sType/ResourceEvent#": "stEvt",
    "http://ns.adobe.com/xap/1.0/sType/ResourceRef#": "stRef",
    "http://purl.org/dc/elements/1.1/": "dc",
    "http://ns.adobe.com/exif/1.0/": "exif",
    "http://ns.adobe.com/tiff/1.0/": "tiff",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop",
    "http://ns.adobe.com/xmp/1.0/DynamicMedia/": "xmpDM",
    _RDF: "rdf",
}
_XMP_MAX_HISTORY = 20       # history events of the file itself turned into times
_XMP_MAX_SOURCES = 50       # source files kept per file
_XMP_MAX_DEPTH = 8          # nesting followed inside one XMP property

_MAX_VALUE = 200            # characters of a value shown before it is cut
_MAX_OTHER = 80             # fields kept in "other" per file, so a hostile file cannot flood a page
_ISOBMFF_1904 = calendar.timegm((1904, 1, 1, 0, 0, 0, 0, 0, 0))
_MAX_BOX_WALK = 4 * 1024 * 1024        # bytes of any one metadata box (udta, meta, XMP) read
_MAX_BOXES = 10_000                    # boxes walked per level, so a hostile file cannot spin
_XMP_UUID = bytes.fromhex("BE7ACFCB97A942E89C71999491E3AFAC")   # the uuid box that holds XMP
#: 3GPP asset boxes in ``udta``: a FullBox, a packed ISO-639 language, then NUL-terminated text.
_ASSET_3GPP = frozenset((b"titl", b"dscp", b"cprt", b"perf", b"auth", b"gnre", b"albm"))


# ------------------------------------------------------------------------------ helpers

def _time(label, wall, *, zone=None, epoch=None, note="", basis=None):
    """One timestamp record.

    ``wall`` is ``YYYY-MM-DD HH:MM:SS`` **as the file states it** (its own clock); ``zone`` is the
    offset that applies to it (``"+02:00"``, ``"UTC"``) or ``None`` when none is known; ``epoch`` is
    the instant, present only when a zone is known so the report can convert it — a naive wall
    clock is never turned into an instant here.

    ``basis`` says **how** the zone is known, because the two ways are not equally strong:

    * ``"stated"`` — the file records it (EXIF ``OffsetTime*``, an ISO 8601 offset, a PNG
      ``Creation Time`` with a zone);
    * ``"format"`` — the file records no zone, but the format defines the field as UTC (an
      ``mvhd`` time in ISO base media files; the EXIF GPS stamp). Encoders have been known to write
      local time into such a field regardless, so a conversion made on this basis is an
      assumption and the report has to say so;
    * ``None`` — nothing is known; the value is a wall clock on the writing device's clock.
    """
    return {"label": label, "wall": wall, "zone": zone, "epoch": epoch, "note": note,
            "basis": basis if zone else None}


def _exif_wall(text):
    """``2024:05:01 12:00:00`` -> ``2024-05-01 12:00:00`` and its fields, or (None, None)."""
    match = _EXIF_DT.match(str(text or ""))
    if not match:
        return None, None
    parts = tuple(int(g) for g in match.groups())
    year, month, day, hour, minute, second = parts
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second < 61):
        return None, None
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}", parts


def _offset_seconds(text):
    match = _OFFSET.match(str(text or ""))
    if not match:
        return None
    sign = 1 if match.group(1) == "+" else -1
    return sign * (int(match.group(2)) * 3600 + int(match.group(3)) * 60)


def _epoch(parts, offset_seconds):
    try:
        return int(calendar.timegm((*parts, 0, 0, 0))) - offset_seconds
    except (OverflowError, ValueError):
        return None


def _cut(value):
    """A field value as display text: bytes by their size, long text truncated."""
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, tuple) and value and all(isinstance(v, (bytes, bytearray)) for v in value):
        return f"<{sum(len(v) for v in value)} bytes>"
    try:
        text = str(value)
    except Exception:                                      # a value whose repr itself fails
        return "<unreadable>"
    text = text.replace("\x00", "").strip()
    return text if len(text) <= _MAX_VALUE else text[:_MAX_VALUE] + "…"


def _rational(value):
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _gps_coord(dms, ref):
    """EXIF (deg, min, sec) rationals + hemisphere -> signed decimal degrees, or None."""
    try:
        deg, minute, sec = (_rational(v) for v in dms)
        if None in (deg, minute, sec):
            return None
        value = deg + minute / 60 + sec / 3600
        return -value if str(ref).upper() in ("S", "W") else value
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------ EXIF / XMP / PNG

def _exif_text(value):
    """An EXIF field's full text, or None when it is not text. ``UserComment`` carries an 8-byte
    character-code prefix (``ASCII``, ``UNICODE``, ``JIS`` or undefined) before its text."""
    if isinstance(value, str):
        return value.replace("\x00", "").strip() or None
    if isinstance(value, (bytes, bytearray)) and len(value) > 8:
        code, body = bytes(value[:8]), bytes(value[8:])
        codec = "utf-16" if code.startswith(b"UNICODE") else "latin-1"
        return body.decode(codec, "replace").replace("\x00", "").strip() or None
    return None


def _from_exif(exif):
    """(times, key fields, other fields, gps, texts) out of a Pillow ``Image.Exif`` — ``texts`` is
    ``[(name, full text)]`` of every text field before any value is cut for display."""
    times, key, other, gps = [], [], [], {}
    base = dict(exif)
    try:
        ifd = dict(exif.get_ifd(ExifTags.IFD.Exif))
    except Exception:
        ifd = {}
    try:
        gps_ifd = dict(exif.get_ifd(ExifTags.IFD.GPSInfo))
    except Exception:
        gps_ifd = {}
    named = {}
    for tag, value in list(base.items()) + list(ifd.items()):
        name = ExifTags.TAGS.get(tag, f"tag {tag}")
        if name in ("ExifOffset", "GPSInfo", "InteropOffset"):
            continue
        named.setdefault(name, value)
    texts = [(name, text) for name, value in named.items()
             if name != "MakerNote" and (text := _exif_text(value))]

    for tag, offset_tag, subsec_tag in _EXIF_TIME_TAGS:
        raw = named.pop(tag, None)
        offset = named.pop(offset_tag, None)
        subsec = named.pop(subsec_tag, None)
        if raw is None:
            continue
        wall, parts = _exif_wall(raw)
        if wall is None:
            other.append((tag, _cut(raw)))
            continue
        offset_seconds = _offset_seconds(offset)
        if offset_seconds is not None:
            zone = f"{'+' if offset_seconds >= 0 else '-'}{abs(offset_seconds) // 3600:02d}:" \
                   f"{abs(offset_seconds) % 3600 // 60:02d}"
            note = f"the file records the offset {zone} ({offset_tag})"
            epoch = _epoch(parts, offset_seconds)
            basis = "stated"
        else:
            zone, epoch, basis = None, None, None
            note = ("no timezone recorded in the file — the clock of the device that wrote it, "
                    "as it was set")
        if subsec not in (None, ""):
            note += f"; sub-second .{_cut(subsec)}"
        times.append(_time(f"EXIF {tag}", wall, zone=zone, epoch=epoch, note=note, basis=basis))

    if gps_ifd:
        g = {ExifTags.GPSTAGS.get(t, f"gps {t}"): v for t, v in gps_ifd.items()}
        lat = _gps_coord(g.get("GPSLatitude"), g.get("GPSLatitudeRef"))
        lon = _gps_coord(g.get("GPSLongitude"), g.get("GPSLongitudeRef"))
        if lat is not None and lon is not None:
            gps["lat"], gps["lon"] = lat, lon
            alt = _rational(g.get("GPSAltitude"))
            if alt is not None:
                if g.get("GPSAltitudeRef") in (1, b"\x01"):
                    alt = -alt
                gps["alt"] = alt
        stamp, clock = g.get("GPSDateStamp"), g.get("GPSTimeStamp")
        if stamp and clock:
            try:
                h, mi, s = (int(round(_rational(v) or 0)) for v in clock)
                wall, parts = _exif_wall(f"{stamp} {h:02d}:{mi:02d}:{s:02d}")
                if wall:
                    times.append(_time("EXIF GPSDateStamp + GPSTimeStamp", wall, zone="UTC",
                                       epoch=_epoch(parts, 0), basis="format",
                                       note="the EXIF specification defines the GPS stamp as UTC "
                                            "from the receiver's fix; the file itself records no "
                                            "zone"))
            except (TypeError, ValueError):
                pass
        for name in sorted(g):
            if name not in ("GPSLatitude", "GPSLongitude", "GPSLatitudeRef", "GPSLongitudeRef",
                            "GPSAltitude", "GPSAltitudeRef", "GPSDateStamp", "GPSTimeStamp"):
                other.append((name, _cut(g[name])))

    for name in KEY_TAGS:
        if name in named:
            key.append((name, _cut(named.pop(name))))
    for name in sorted(named):                             # MakerNote is bytes: its size only
        other.append((name, _cut(named[name])))
    return times, key, other, gps, texts


def _from_xmp(blob):
    """Dated XMP properties (``xmp:CreateDate`` …) as time records; attribute or element form.

    The fallback for an XMP packet that is not well-formed XML (see :func:`_xmp_read`): it matches
    text, so it stops where an editing program's list of source files begins — a date after that
    point belongs to one of those files, not to this one."""
    times = []
    if not blob:
        return times
    text = _xmp_decode(blob)
    for marker in ("<xmpMM:Pantry", "<xmpMM:Ingredients"):
        if marker in text:
            text = text[:text.index(marker)]
    for prop in _XMP_DATES:
        match = (re.search(rf'{re.escape(prop)}="([^"]+)"', text)
                 or re.search(rf"<{re.escape(prop)}>([^<]+)</{re.escape(prop)}>", text))
        if not match or match.group(1).strip() == _QT_ZERO:
            continue
        record = _iso_time(f"XMP {prop}", match.group(1))
        if record:
            times.append(record)
    return times


def _xmp_decode(blob):
    if isinstance(blob, (bytes, bytearray)):
        raw = bytes(blob)
        if raw[:2] in (b"\xfe\xff", b"\xff\xfe"):
            return raw.decode("utf-16", "replace")
        return raw.decode("utf-8", "replace")
    return str(blob)


def _xmp_rdf(text):
    """The ``rdf:RDF`` element of an XMP packet and the namespace prefixes the packet declares, or
    ``(None, {})``. A packet with a document type or an entity declaration is refused before it is
    parsed — nothing is expanded out of a hostile file."""
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return None, {}
    for opening, closing in (("<x:xmpmeta", "</x:xmpmeta>"), ("<rdf:RDF", "</rdf:RDF>")):
        start, end = text.find(opening), text.rfind(closing)
        if start >= 0 and end > start:
            break
    else:
        return None, {}
    try:
        root = ET.fromstring(text[start:end + len(closing)])
    except Exception:                                      # noqa: BLE001 - malformed XML: fallback
        return None, {}
    rdf = root if root.tag == f"{{{_RDF}}}RDF" else root.find(f".//{{{_RDF}}}RDF")
    prefixes = {uri: prefix for prefix, uri
                in re.findall(r"""xmlns:([\w.-]+)\s*=\s*["']([^"']+)["']""", text)}
    return rdf, prefixes


def _xmp_name(tag, prefixes):
    if not tag.startswith("{"):
        return tag
    uri, local = tag[1:].split("}", 1)
    return f"{_XMP_PREFIX.get(uri) or prefixes.get(uri) or uri}:{local}"


def _xmp_value(el, prefixes, depth=0):
    """One XMP property: text, a list (``rdf:Seq`` / ``rdf:Bag``), the x-default (or first) item of
    an ``rdf:Alt``, or a structure as a dict — in attribute form, element form or a nested
    ``rdf:Description``."""
    if depth > _XMP_MAX_DEPTH:
        return ""
    resource = el.get(f"{{{_RDF}}}resource")
    if resource is not None:
        return resource
    for kind in ("Alt", "Seq", "Bag"):
        box = el.find(f"{{{_RDF}}}{kind}")
        if box is not None:
            items = box.findall(f"{{{_RDF}}}li")
            if kind == "Alt":
                pick = next((li for li in items if li.get(_XML_LANG) == "x-default"),
                            items[0] if items else None)
                return "" if pick is None else _xmp_value(pick, prefixes, depth + 1)
            return [_xmp_value(li, prefixes, depth + 1) for li in items]
    attributes = [k for k in el.attrib if not k.startswith(f"{{{_RDF}}}") and k != _XML_LANG]
    if attributes or len(el):
        return _xmp_struct(el, prefixes, depth + 1)
    return (el.text or "").strip()


def _xmp_struct(el, prefixes, depth=0):
    """The properties of a description or structure element, attribute and element form alike."""
    out = {}
    for target in [el] + el.findall(f"{{{_RDF}}}Description"):
        for name, value in target.attrib.items():
            if not name.startswith(f"{{{_RDF}}}") and name != _XML_LANG:
                out.setdefault(_xmp_name(name, prefixes), value)
        for child in target:
            if child.tag != f"{{{_RDF}}}Description":
                out.setdefault(_xmp_name(child.tag, prefixes), _xmp_value(child, prefixes, depth))
    return out


def _xmp_flat(value):
    """A property as display text: a list or a structure spelled out, so nothing is hidden (its
    nesting is already bounded by :data:`_XMP_MAX_DEPTH`)."""
    if isinstance(value, dict):
        return "; ".join(f"{k}={_xmp_flat(v)}" for k, v in list(value.items())[:20])
    if isinstance(value, list):
        items = [_xmp_flat(v) for v in value[:20]]
        return "; ".join(items) + (f"; … ({len(value)} in all)" if len(value) > 20 else "")
    return str(value)


def _xmp_coord(text):
    """XMP's ``DDD,MM.mmk`` / ``DDD,MM,SSk`` GPS coordinate as signed decimal degrees, or None."""
    match = _XMP_COORD.match(text) if isinstance(text, str) else None
    if not match:
        return None
    deg, minutes, seconds, ref = match.groups()
    value = float(deg) + float(minutes) / 60 + float(seconds or 0) / 3600
    return -value if ref in "SW" else value


def _xmp_gps(props):
    lat, lon = _xmp_coord(props.get("exif:GPSLatitude")), _xmp_coord(props.get("exif:GPSLongitude"))
    return {"lat": lat, "lon": lon} if lat is not None and lon is not None else {}


def _xmp_duration(value):
    """``xmpDM:duration`` (a value and a scale such as ``1/1000``) as seconds, else as stored."""
    if not isinstance(value, dict):
        return _xmp_flat(value) if value else ""
    try:
        num, _slash, den = str(value.get("xmpDM:scale", "1")).partition("/")
        seconds = float(value["xmpDM:value"]) * float(num) / float(den or 1)
        return f"{seconds:.2f} s"
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return _xmp_flat(value)


def _xmp_events(value):
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _xmp_source(props, paths, where, raw, base):
    """One file an editing program lists as having gone into this one (an ``xmpMM:Pantry`` item),
    joined to its ``xmpMM:Ingredients`` entry — its path — by instance id. A Snapchat tag among its
    fields (a Snapchat-saved image carries one in its EXIF ``UserComment``, which the program copies
    into the item) is that source file's, and is returned with it."""
    instance = _xmp_flat(props.get("xmpMM:InstanceID", ""))
    candidates = []
    for name, value in props.items():
        if isinstance(value, str) and len(value) >= 16:
            found = raw.find(value.strip().encode("utf-8", "replace"))
            offset = base + found if base is not None and found >= 0 else None
            candidates.append((f"{where} › xmpMM:Pantry › {name}", name, value, offset))
    tags = [{k: v for k, v in tag.items() if k != "names"} for tag in _snap_tags(candidates)]
    ingredient = paths.get(instance) or {}
    times, not_set = [], []
    for name in _XMP_DATES:
        value = props.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        if value.strip() == _QT_ZERO:
            not_set.append(name)
            continue
        times.append(_iso_time(f"XMP {name}", value) or _time(f"XMP {name}", value.strip(),
                                                                note="shown as stored"))
    history = _xmp_events(props.get("xmpMM:History"))
    last = history[-1] if history else {}
    if isinstance(last.get("stEvt:when"), str):
        record = _iso_time("XMP history · last event", last["stEvt:when"])
        if record:
            times.append(record)
    return {"file_path": ingredient.get("path", ""), "uses": ingredient.get("uses", 0),
            "title": _xmp_flat(props.get("dc:title", "")),
            "format": _xmp_flat(props.get("dc:format", "")),
            "creator_tool": _xmp_flat(props.get("xmp:CreatorTool", "")),
            "saved_by": _xmp_flat(last.get("stEvt:softwareAgent", "")),
            "times": times, "not_set": not_set,
            "duration": _xmp_duration(props.get("xmpDM:duration")), "gps": _xmp_gps(props),
            "instance_id": instance, "document_id": _xmp_flat(props.get("xmpMM:DocumentID", "")),
            "snapchat": tags}


def _xmp_read(blob, where="XMP", base=None):
    """What an XMP packet says about the file that holds it, and — apart — about the files an
    editing program lists as having gone into it.

    Returns ``{times, key, other, sources, candidates, read}``: ``candidates`` are the text fields a
    Snapchat tag may sit in, as ``(field, name, text, offset)`` (``offset`` is absolute when
    ``base``, the packet's offset in the file, is known). A packet that is not well-formed XML falls
    back to :func:`_from_xmp`, dates only."""
    out = {"times": [], "key": [], "other": [], "sources": [], "candidates": [], "read": False}
    if not blob:
        return out
    text = _xmp_decode(blob)
    rdf, prefixes = _xmp_rdf(text)
    if rdf is None:
        out["times"] = _from_xmp(text)
        out["read"] = bool(out["times"])
        return out
    own = {}
    for description in rdf.findall(f"{{{_RDF}}}Description"):
        for name, value in _xmp_struct(description, prefixes).items():
            own.setdefault(name, value)
    out["read"] = bool(own)
    raw = bytes(blob) if isinstance(blob, (bytes, bytearray)) else text.encode("utf-8", "replace")

    # the files that went into this one: every pantry item (nested ones flattened), each joined to
    # the ingredient entries that name its path
    paths, pantry = {}, []

    def ingredients(value):
        for entry in _xmp_events(value):
            instance = _xmp_flat(entry.get("stRef:instanceID", ""))
            path = _xmp_flat(entry.get("stRef:filePath", ""))
            record = paths.setdefault(instance or path, {"path": path, "uses": 0,
                                                         "instance": instance})
            record["uses"] += 1
            record["path"] = record["path"] or path

    def walk(items, depth):
        for item in _xmp_events(items):
            if len(pantry) >= _XMP_MAX_SOURCES:
                return
            nested = item.pop("xmpMM:Pantry", None)
            ingredients(item.pop("xmpMM:Ingredients", None))
            pantry.append(item)
            if depth < _XMP_MAX_DEPTH:
                walk(nested, depth + 1)

    ingredients(own.pop("xmpMM:Ingredients", None))
    walk(own.pop("xmpMM:Pantry", None), 0)
    sources = [_xmp_source(item, paths, where, raw, base) for item in pantry]
    joined = {s["instance_id"] for s in sources if s["instance_id"]}
    for record in paths.values():                    # named in the edit, described by no pantry item
        if len(sources) >= _XMP_MAX_SOURCES:
            break
        if not (record["instance"] and record["instance"] in joined):
            sources.append({"file_path": record["path"], "uses": record["uses"], "title": "",
                            "format": "", "creator_tool": "", "saved_by": "", "times": [],
                            "not_set": [], "duration": "", "gps": {},
                            "instance_id": record["instance"], "document_id": "",
                            "snapchat": []})
    out["sources"] = sources

    history = _xmp_events(own.pop("xmpMM:History", None))
    key, dated = {}, {}
    for name, value in own.items():
        label = f"XMP {name}"
        if name in _XMP_DATES and isinstance(value, str) and value.strip():
            if value.strip() == _QT_ZERO:
                out["other"].append((label, f"{value.strip()} — the zero QuickTime time: not set"))
                continue
            record = _iso_time(label, value)
            if record:
                dated[name] = record
                continue
        shown = _xmp_flat(value)
        if isinstance(value, str) and value.strip():
            found = raw.find(value.strip().encode("utf-8", "replace"))
            offset = base + found if base is not None and found >= 0 else None
            out["candidates"].append((f"{where} › {name}", label, value, offset))
        if label in KEY_XMP:
            key[label] = shown
        elif shown:
            out["other"].append((label, shown))
    out["key"] = [(label, key[label]) for label in KEY_XMP if key.get(label)]
    # the file's own dates in a fixed order, then its history: each event a time, labelled with what
    # happened and which program did it
    out["times"] = [dated[name] for name in _XMP_DATES if name in dated]
    for event in history[:_XMP_MAX_HISTORY]:
        when = event.get("stEvt:when")
        agent = _xmp_flat(event.get("stEvt:softwareAgent", ""))
        label = (f"XMP history · {_xmp_flat(event.get('stEvt:action', '')) or 'event'}"
                 + (f" ({agent})" if agent else ""))
        record = _iso_time(label, when) if isinstance(when, str) else None
        if record:
            out["times"].append(record)
    if len(history) > _XMP_MAX_HISTORY:
        out["other"].append(("XMP xmpMM:History", f"{len(history)} events; the first "
                                                  f"{_XMP_MAX_HISTORY} are listed as times"))
    return out


def _iso_time(label, text):
    """An ISO 8601 date-time (XMP, QuickTime ``creationdate``) as a time record, or None."""
    match = _ISO_DT.match(str(text or ""))
    if not match:
        wall, parts = _exif_wall(text)                     # some writers use the EXIF spelling
        if wall is None:
            return None
        return _time(label, wall, note="no timezone recorded in the file")
    year, month, day, hour, minute, second, zone = match.groups()
    parts = (int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
    wall = f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d} {parts[3]:02d}:{parts[4]:02d}:{parts[5]:02d}"
    if not zone:
        return _time(label, wall, note="no timezone recorded in the file")
    if zone == "Z":
        return _time(label, wall, zone="UTC", epoch=_epoch(parts, 0), basis="stated",
                     note="stated in UTC by the file")
    offset = _offset_seconds(zone)
    zone_text = f"{zone[0]}{zone[1:3]}:{zone[-2:]}"
    return _time(label, wall, zone=zone_text, epoch=_epoch(parts, offset), basis="stated",
                 note=f"the file records the offset {zone_text}")


def _png_text(info):
    """PNG text chunks: the dated ones as times, the rest as fields."""
    times, other = [], []
    for name, value in sorted(info.items()):
        if name in ("exif", "xmp", "icc_profile", "transparency", "gamma", "dpi", "interlace",
                    "srgb", "chromaticity", "aspect"):
            continue
        text = _cut(value)
        if name.lower() in ("creation time", "create-date", "date:create", "date:modify",
                            "date:timestamp"):
            record = _iso_time(f"PNG {name}", value) or _rfc1123(f"PNG {name}", value)
            if record:
                times.append(record)
                continue
        other.append((name, text))
    return times, other


def _rfc1123(label, text):
    """``Tue, 01 May 2024 12:00:00 +0200`` (the form PNG «Creation Time» recommends)."""
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%d %b %Y %H:%M:%S"):
        try:
            dt = datetime.strptime(str(text).strip(), fmt)
        except (TypeError, ValueError):
            continue
        wall = dt.strftime("%Y-%m-%d %H:%M:%S")
        if dt.tzinfo is None:
            return _time(label, wall, note="no timezone recorded in the file")
        off = dt.utcoffset() or timedelta(0)
        secs = int(off.total_seconds())
        zone = f"{'+' if secs >= 0 else '-'}{abs(secs) // 3600:02d}:{abs(secs) % 3600 // 60:02d}"
        return _time(label, wall, zone=zone, epoch=int(dt.timestamp()), basis="stated",
                     note=f"the file records the offset {zone}")
    return None


def _read_image(fh):
    with Image.open(fh) as im:
        fmt = im.format or ""
        info = dict(im.info)
        try:
            exif = im.getexif()
        except Exception:
            exif = None
        size = im.size
    times, key, other, gps, texts = ([], [], [], {}, [])
    if exif:
        times, key, other, gps, texts = _from_exif(exif)
    candidates = [(f"EXIF {name}", name, text, None) for name, text in texts]
    xmp_key, xmp_other, xmp_sources = [], [], []
    for blob in (info.get("xmp"), info.get("XML:com.adobe.xmp") if fmt == "PNG" else None):
        read = _xmp_read(blob)
        times += read["times"]
        xmp_key += read["key"]
        xmp_other += read["other"]
        xmp_sources += read["sources"]
        candidates += read["candidates"]
    if fmt == "PNG":
        chunks = {k: v for k, v in info.items() if k != "xmp"}
        png_times, png_other = _png_text(chunks)
        times += png_times
        other += png_other
        candidates += [(f"PNG {k}", k, v, None) for k, v in chunks.items() if isinstance(v, str)]
    sources = []
    if exif:
        sources.append("EXIF")
    if info.get("xmp") or info.get("XML:com.adobe.xmp"):
        sources.append("XMP")
    if fmt == "PNG" and other:
        sources.append("PNG text")
    tags = _snap_tags(candidates)
    key, other = _demote(key + xmp_key, other + xmp_other, tags)
    return _result(container=fmt, pixels=f"{size[0]}×{size[1]}", times=times, key=key,
                   other=other[:_MAX_OTHER], gps=gps, sources=sources, snapchat=tags,
                   xmp_sources=xmp_sources)


def _snap_tags(candidates):
    """The Snapchat app tags among ``candidates`` — ``(field, name, full text, offset)`` — each
    decoded once, with the field (and file offset, when known) it was read from; a tag repeated in
    several fields is one tag that names all of them. ``names`` are the display names of those
    fields in ``key`` / ``other``, which :func:`_demote` moves."""
    tags, seen = [], {}
    for field, name, text, offset in candidates:
        if not isinstance(text, str) or len(text) < 16:
            continue
        tag = snap_media_tag.decode(text)
        if tag is None:
            continue
        if tag["encoded"] in seen:
            seen[tag["encoded"]]["also"].append(field)
            seen[tag["encoded"]]["names"].append(name)
            continue
        record = dict(tag, field=field, offset=offset, also=[], names=[name])
        seen[tag["encoded"]] = record
        tags.append(record)
    return tags


def _demote(key, other, tags):
    """A field that holds a Snapchat tag is shown as stored, at the front of ``other`` (so the
    ``_MAX_OTHER`` cap cannot drop it) — never as a key field or a search term; the decoded tag is
    what the report shows first."""
    names = {name for tag in tags for name in tag["names"]}
    if not names:
        return key, other
    moved = [pair for pair in key + other if pair[0] in names]
    return ([pair for pair in key if pair[0] not in names],
            moved + [pair for pair in other if pair[0] not in names])


# ------------------------------------------------------------------------------ ISO BMFF (MP4/MOV)

def _boxes(buf, start, end):
    """Yield ``(type, payload_start, payload_end)`` for the boxes laid end to end in buf[start:end]."""
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", buf, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos + header, pos + size
        pos += size


def _mvhd(payload):
    """(creation, modification, duration_seconds) out of an ``mvhd`` payload; epochs are 1904-based."""
    version = payload[0]
    try:
        if version == 1:
            created, modified, timescale, duration = struct.unpack_from(">QQIQ", payload, 4)
        else:
            created, modified, timescale, duration = struct.unpack_from(">IIII", payload, 4)
    except struct.error:
        return None, None, None
    seconds = (duration / timescale) if timescale else None
    return created, modified, seconds


def _bmff_time(label, stamp):
    """A 1904-epoch UTC stamp as a time record; zero means «not set» and yields None."""
    if not stamp:
        return None
    epoch = stamp + _ISOBMFF_1904
    try:
        wall = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None
    return _time(label, wall, zone="UTC", epoch=epoch, basis="format",
                 note="seconds since 1904-01-01, written by the encoder. The ISO base media format "
                      "defines the field as UTC; the file records no zone of its own, and some "
                      "encoders are known to write local time here")


def _header_times(payload, with_track=False):
    """``(creation, modification, track_ID)`` out of a ``tkhd`` / ``mdhd`` payload (1904-based
    stamps; the track id only from a ``tkhd``), or None when the payload is too short."""
    try:
        if payload[0] == 1:
            created, modified, track = struct.unpack_from(">QQI", payload, 4)
        else:
            created, modified, track = struct.unpack_from(">III", payload, 4)
    except (IndexError, struct.error):
        return None
    return created, modified, (track if with_track else None)


def _data_value(payload):
    """The value of a ``data`` atom by its type: text (1, 4 UTF-8; 2, 5 UTF-16), a big-endian
    integer (21, 65-67, 74 signed; 22, 75-78 unsigned) or float (23, 24) as text — anything else
    (images, binary) as the bytes, which are shown by their size."""
    if len(payload) < 8:
        return None
    kind = int.from_bytes(payload[:4], "big") & 0xFFFFFF
    value = payload[8:]
    if kind in (1, 4):
        return value.decode("utf-8", "replace")
    if kind in (2, 5):
        return value.decode("utf-16-be", "replace")
    if kind in (21, 65, 66, 67, 74) and len(value) in (1, 2, 3, 4, 8):
        return str(int.from_bytes(value, "big", signed=True))
    if kind in (22, 75, 76, 77, 78) and len(value) in (1, 2, 3, 4, 8):
        return str(int.from_bytes(value, "big"))
    if kind == 23 and len(value) == 4:
        return f"{struct.unpack('>f', value)[0]:g}"
    if kind == 24 and len(value) == 8:
        return f"{struct.unpack('>d', value)[0]:g}"
    return bytes(value)


def _qt_text(payload):
    """Text out of a QuickTime international-text udta atom, or a plain ``data`` atom child."""
    for kind, s, e in _boxes(payload, 0, len(payload)):
        if kind == b"data" and e - s > 8:
            value = _data_value(payload[s:e])
            return value if isinstance(value, str) else ""
    if len(payload) >= 4:
        size, _lang = struct.unpack_from(">HH", payload, 0)
        return payload[4:4 + size].decode("utf-8", "replace")
    return payload.decode("utf-8", "replace")


def _asset_text(payload):
    """The text of a 3GPP asset box (``dscp``, ``titl`` …): version/flags (zero), a packed ISO-639
    language, then NUL-terminated UTF-8 — or UTF-16 when it opens with a byte-order mark. None when
    the payload is not in that form, so the caller can read it the QuickTime way instead."""
    if len(payload) <= 6 or payload[:4] != b"\x00\x00\x00\x00":
        return None
    body = payload[6:]
    if body[:2] in (b"\xfe\xff", b"\xff\xfe"):
        end = len(body) - len(body) % 2
        for pos in range(2, end, 2):                       # a two-byte NUL, on a character boundary
            if body[pos:pos + 2] == b"\x00\x00":
                end = pos
                break
        text = body[:end].decode("utf-16", "replace")
    else:
        text = body.split(b"\x00", 1)[0].decode("utf-8", "replace")
    return text.strip()


def _ilst(payload, names):
    """``[(name, value, offset of the value in payload)]`` out of an ``ilst``; ``names`` maps a
    1-based index to a ``keys`` name. An item whose index has no name is called ``key #<n>``."""
    out = []
    for kind, s, e in _boxes(payload, 0, len(payload)):
        idx = struct.unpack(">I", kind)[0]
        if idx in names:
            name = names[idx]
        elif kind[:1] == b"\xa9" or all(0x20 <= b < 0x7F for b in kind):
            name = kind.decode("latin-1")
        else:
            name = f"key #{idx}"
        value, offset = None, s
        for k2, s2, e2 in _boxes(payload, s, e):
            if k2 == b"data":
                value, offset = _data_value(payload[s2:e2]), s2 + 8
                break
        if value is None:
            value = _qt_text(payload[s:e])
        if name and value not in (None, ""):
            out.append((name, value, offset))
    return out


def _keys(payload):
    names = {}
    try:
        count = struct.unpack_from(">I", payload, 4)[0]
    except struct.error:
        return names
    pos = 8
    for idx in range(1, min(count, 512) + 1):
        if pos + 8 > len(payload):
            break
        size, _ns = struct.unpack_from(">I4s", payload, pos)
        if size < 8 or pos + size > len(payload):
            break
        names[idx] = payload[pos + 8:pos + size].decode("utf-8", "replace")
        pos += size
    return names


def _meta_items(payload):
    """``[(name, value, offset in payload)]`` out of a ``meta`` box: its ``keys`` and ``ilst``.

    Two kinds of ``meta`` exist and are told apart by where their ``hdlr`` sits: the ISO one (in
    ``udta``, what an ffmpeg muxer writes) is a FullBox, its children after 4 bytes of
    version/flags; QuickTime's (directly in ``moov``, what iOS writes) is not, its children start
    at once."""
    if payload[4:8] == b"hdlr":
        start = 0
    elif payload[8:12] == b"hdlr":
        start = 4
    else:
        start = 4 if payload[:4] == b"\x00\x00\x00\x00" else 0
    names = {}
    for kind, s, e in _boxes(payload, start, len(payload)):
        if kind == b"keys":
            names = _keys(payload[s:e])
    out = []
    for kind, s, e in _boxes(payload, start, len(payload)):
        if kind == b"ilst":
            out += [(name, value, s + offset) for name, value, offset in _ilst(payload[s:e], names)]
    return out


def _file_boxes(fh, start, end):
    """Yield ``(type, payload_start, payload_end)`` for the boxes laid end to end in the file between
    ``start`` and ``end``, by seeking: a box is never read to be skipped, so a video's media data
    costs nothing. A box claiming more than the file holds is cut at ``end`` (a file cached only in
    part); the walk stops at a malformed header or after :data:`_MAX_BOXES` boxes."""
    pos = start
    for _ in range(_MAX_BOXES):
        if pos + 8 > end:
            return
        fh.seek(pos)
        head = fh.read(16)
        if len(head) < 8:
            return
        size, kind = struct.unpack_from(">I4s", head, 0)
        header = 8
        if size == 1:
            if len(head) < 16:
                return
            size = struct.unpack_from(">Q", head, 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        yield kind, pos + header, min(pos + size, end)
        pos += size


def _read_at(fh, start, end, limit=_MAX_BOX_WALK):
    fh.seek(start)
    return fh.read(max(0, min(end - start, limit)))


def _udta_items(buf, base, items, xmp):
    """The fields of a ``udta`` payload into ``items`` as ``(where, name, value, offset)``, and any
    ``XMP_`` packet into ``xmp`` as ``(where, blob, offset)``; ``base`` is the payload's offset."""
    where = "moov › udta"
    for kind, s, e in _boxes(buf, 0, len(buf)):
        payload = buf[s:e]
        name = kind.decode("latin-1", "replace")
        if kind == b"meta":
            for key, value, offset in _meta_items(payload):
                items.append((f"{where} › meta › ilst", key, value, base + s + offset))
        elif kind == b"XMP_":
            xmp.append((f"{where} › XMP_", payload, base + s))
        elif kind in _ASSET_3GPP and (text := _asset_text(payload)) is not None:
            if text:
                items.append((where, name, text, base + s + 6))
        elif (text := _qt_text(payload)) and text.isprintable():
            items.append((where, name, text, base + s + 4))


def _track_rows(mvhd, tracks):
    """What the track headers (``tkhd``, ``mdhd``) say beside the movie header: a note on each
    ``mvhd`` time they all agree with, and a time of its own for every value that differs.
    ``tracks`` is ``[(box, track id, creation, modification)]``; a zero stamp is «not set»."""
    notes, rows = {}, []
    for index, kind in ((0, "creation"), (1, "modification")):
        movie = mvhd[index] if mvhd else None
        stated = [(f"{box} track {track}" if track else box, stamps[index])
                  for box, track, *stamps in tracks if stamps[index]]
        if not stated:
            continue
        differ = {}
        for where, stamp in stated:
            if stamp != movie:
                differ.setdefault(stamp, []).append(where)
        if not differ:
            notes[kind] = "every track header (tkhd, mdhd) states the same time"
        for stamp, where in differ.items():
            record = _bmff_time(f"track header {kind}_time ({', '.join(where)})", stamp)
            if record:
                rows.append(record)
    return notes, rows


def _read_bmff(fh):
    fh.seek(0)
    head = fh.read(16)
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    total = fh.seek(0, io.SEEK_END)
    brand = head[8:12].decode("latin-1", "replace").strip()
    # the moov box is often at the END of a large file, and an XMP uuid box can follow it: walk the
    # whole top level by seeking
    found, mvhd, tracks, items, xmp = False, None, [], [], []
    for kind, start, end in _file_boxes(fh, 0, total):
        if kind == b"moov":
            found = True
            for k2, s2, e2 in _file_boxes(fh, start, end):
                if k2 == b"mvhd":
                    mvhd = _mvhd(_read_at(fh, s2, e2, 128))
                elif k2 == b"trak":
                    track = None
                    for k3, s3, e3 in _file_boxes(fh, s2, e2):
                        if k3 == b"tkhd":
                            stamps = _header_times(_read_at(fh, s3, e3, 64), with_track=True)
                            if stamps:
                                track = stamps[2]
                                tracks.append(("tkhd", track, stamps[0], stamps[1]))
                        elif k3 == b"mdia":
                            for k4, s4, e4 in _file_boxes(fh, s3, e3):
                                if k4 == b"mdhd":
                                    stamps = _header_times(_read_at(fh, s4, e4, 64))
                                    if stamps:
                                        tracks.append(("mdhd", track, stamps[0], stamps[1]))
                elif k2 == b"udta":
                    _udta_items(_read_at(fh, s2, e2), s2, items, xmp)
                elif k2 == b"meta":
                    for name, value, offset in _meta_items(_read_at(fh, s2, e2)):
                        items.append(("moov › meta › ilst", name, value, s2 + offset))
        elif kind == b"uuid" and end - start > 16:
            fh.seek(start)
            if fh.read(16) == _XMP_UUID:
                xmp.append(("uuid XMP", _read_at(fh, start + 16, end), start + 16))

    times, key, other, candidates, xmp_key, xmp_other, xmp_sources = [], [], [], [], [], [], []
    duration = None
    if mvhd:
        created, modified, duration = mvhd
        notes, track_times = _track_rows(mvhd, tracks)
        for label, stamp, kind in (("mvhd creation_time", created, "creation"),
                                   ("mvhd modification_time", modified, "modification")):
            record = _bmff_time(label, stamp)
            if record:
                if kind in notes:
                    record["note"] += f"; {notes[kind]}"
                times.append(record)
        times += track_times
    for where, blob, offset in xmp:
        read = _xmp_read(blob, where, offset)
        times += read["times"]
        xmp_key += read["key"]
        xmp_other += read["other"]
        xmp_sources += read["sources"]
        candidates += read["candidates"]
    if not found and not xmp:
        return _result(container=brand, note="no moov box found — the file is truncated before "
                                             "its header, or the header was never cached")

    # one value per name, the first met in file order; a different value under a name already
    # seen is kept too, labelled with where it sits
    named, origin = {}, {}
    for where, name, value, offset in items:
        label = f"{name} [{where}]" if name in named and named[name] != value else name
        named.setdefault(label, value)
        origin.setdefault(label, where)
        if isinstance(value, str):
            candidates.append((f"{where} › {name}", label, value, offset))
    gps = {}
    for name, text in list(named.items()):
        if not isinstance(text, str):
            continue
        low = name.lower()
        if low in ("com.apple.quicktime.creationdate", "©day", "creationdate"):
            record = _iso_time(f"QuickTime {name}", text)
            if record:
                times.append(record)
                named.pop(name)
        elif low in ("com.apple.quicktime.location.iso6709", "©xyz"):
            match = _ISO6709.match(text)
            if match:
                try:
                    gps = {"lat": float(match.group(1)), "lon": float(match.group(2))}
                    if match.group(3):
                        gps["alt"] = float(match.group(3))
                except ValueError:
                    pass
    for name in KEY_QT:
        if name in named:
            key.append((name, _cut(named.pop(name))))
    if duration:
        key.insert(0, ("Duration (mvhd)", f"{duration:.1f} s"))
    for name in sorted(named):
        other.append((name, _cut(named[name])))
    sources = []
    if any(t["label"].startswith("mvhd") for t in times):
        sources.append("mvhd")
    if any(t["label"].startswith("track header") for t in times):
        sources.append("tkhd/mdhd")
    wheres = set(origin.values())
    if any(w.startswith("moov › udta") for w in wheres):
        sources.append("QuickTime user data")
    if "moov › meta › ilst" in wheres:
        sources.append("QuickTime metadata (moov › meta)")
    if xmp:
        sources.append("XMP")
    tags = _snap_tags(candidates)
    key, other = _demote(key + xmp_key, other + xmp_other, tags)
    return _result(container=brand, times=times, key=key, other=other[:_MAX_OTHER], gps=gps,
                   sources=sources, snapchat=tags, xmp_sources=xmp_sources,
                   note="" if found else "no moov box found — the file is truncated before its "
                                         "header, or the header was never cached")


# ------------------------------------------------------------------------------ entry point

def _result(container="", pixels="", times=(), key=(), other=(), gps=None, sources=(), note="",
            snapchat=(), xmp_sources=()):
    """The one shape every reader returns.

    ``present`` is whether the file carries anything at all; ``notable`` whether it carries anything
    worth flagging — a timestamp, a fix, a Snapchat tag, or a field that is not one every encoder
    writes (:data:`STRUCTURAL`). The distinction matters on real data: most cached JPEGs hold only
    pixel size and orientation, and a chip on three quarters of the rows would say nothing. The
    source files an editing program lists (``xmp_sources``) make a file present, never notable on
    their own: their dates and places are theirs.
    """
    times, key, other = list(times), list(key), list(other)
    gps = dict(gps or {})
    notable = bool(times or gps or snapchat
                   or any(name not in STRUCTURAL for name, _v in key + other))
    out = {"container": container, "times": times, "key": key, "other": other, "gps": gps,
           "present": bool(times or key or other or gps or snapchat or xmp_sources),
           "notable": notable, "sources": list(sources)}
    if pixels:
        out["pixels"] = pixels
    if note:
        out["note"] = note
    if snapchat:
        out["snapchat"] = list(snapchat)
    if xmp_sources:
        out["xmp_sources"] = list(xmp_sources)
    return out


def extract(path):
    """Embedded metadata of one media file on disk, or ``None`` when it is not a format read here.

    The result is a dict: ``container`` (format / brand), ``pixels`` (images), ``times`` (records
    from :func:`_time`), ``key`` and ``other`` (``[(field, value)]``), ``gps`` (``lat``/``lon``/
    ``alt`` when the file carries a fix), ``present`` (whether anything at all was found),
    ``notable`` (whether any of it is worth flagging — see :func:`_result`) and ``sources`` (which
    metadata stores were present). ``note`` says why a file could not be read when that is worth
    telling the examiner (HEIF; a video with no header).

    Two keys appear only when there is something in them. ``snapchat`` is the Snapchat app's tags
    (`snap_media_tag.decode`, plus ``field`` — where it was read — ``offset`` in the file when known,
    and ``also``, any further field holding the same tag). ``xmp_sources`` is the files an editing
    program lists in XMP as having gone into this one — ``file_path``, ``uses``, ``title``,
    ``format``, ``creator_tool``, ``saved_by``, ``times`` (records from :func:`_time`, *their*
    times), ``not_set``, ``duration``, ``gps``, ``instance_id``, ``document_id`` and ``snapchat``
    (a Snapchat tag among *its* fields, e.g. the EXIF ``UserComment`` of a Snapchat-saved image) —
    kept apart because none of it describes this file.
    """
    try:
        with open(path, "rb") as fh:
            return _extract(fh, path)
    except OSError:
        return None


def extract_bytes(data, name="<bytes>"):
    """Like :func:`extract`, for content already in memory — a payload that was decoded or decrypted
    and has no file of its own on disk."""
    if not data:
        return None
    return _extract(io.BytesIO(data), name)


def _extract(fh, path):
    head = fh.read(16)
    if not head:
        return None
    fh.seek(0)
    try:
        if len(head) >= 12 and head[4:8] == b"ftyp":
            brand = head[8:12]
            if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"avif"):
                return _result(container=brand.decode("latin-1"),
                               note="HEIF container — its EXIF lives in an item this build does not "
                                    "read (Pillow has no HEIF codec here); nothing is inferred from "
                                    "that")
            return _read_bmff(fh)
        if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" \
                or (head[:4] == b"RIFF" and head[8:12] == b"WEBP") \
                or head[:4] in (b"II*\x00", b"MM\x00*"):
            return _read_image(fh)
    except Exception as error:                              # noqa: BLE001 - see module docstring
        logger.debug(f"embedded metadata not read from {path}: {error}")
        return _result(note=f"could not be read: {error}")
    return None
