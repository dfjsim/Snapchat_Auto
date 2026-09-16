"""
Metadata carried **inside** a media file — EXIF, XMP and PNG text in images, the ``moov`` header
and QuickTime user data in ISO base media files — read so a report can set it beside the
timestamps the app's databases hold.

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

Only the standard library and Pillow are used; Pillow is already a dependency for thumbnails.
HEIF/HEIC is not read (Pillow has no HEIF codec here), and the result says so rather than
reporting "no metadata".
"""

import io
import re
import struct
import logging
import calendar
from datetime import datetime, timezone, timedelta

from PIL import Image, ExifTags

logger = logging.getLogger("snapchat_auto")

#: The EXIF fields shown first, in this order; everything else goes behind "all fields".
KEY_TAGS = ("Make", "Model", "Software", "LensMake", "LensModel", "Orientation",
            "ExifImageWidth", "ExifImageHeight", "ImageDescription", "Artist", "Copyright",
            "UserComment", "HostComputer", "BodySerialNumber", "LensSerialNumber", "ImageUniqueID")

#: QuickTime / ISO BMFF metadata keys shown first (``ilst`` atoms and ``keys``-namespaced names).
KEY_QT = ("com.apple.quicktime.make", "com.apple.quicktime.model",
          "com.apple.quicktime.software", "com.apple.quicktime.location.ISO6709",
          "©mak", "©mod", "©swr", "©xyz", "©nam", "©cmt", "©ART", "©too")

#: Fields every encoder writes and that say nothing about a device, a place or a moment: pixel size,
#: orientation, colour space, the EXIF version. A file carrying only these is not flagged as having
#: metadata worth a look — on a real device three quarters of the cached JPEGs carry exactly this set.
STRUCTURAL = frozenset((
    "ExifImageWidth", "ExifImageHeight", "Orientation", "ColorSpace", "ResolutionUnit",
    "XResolution", "YResolution", "YCbCrPositioning", "ExifVersion", "FlashPixVersion",
    "ComponentsConfiguration", "CompressedBitsPerPixel", "InteroperabilityIndex",
    "InteroperabilityVersion", "SceneCaptureType", "Duration (mvhd)", "Compression",
    "JpegIFOffset", "JpegIFByteCount", "PhotometricInterpretation", "SamplesPerPixel",
    "BitsPerSample", "PlanarConfiguration"))

_EXIF_TIME_TAGS = (("DateTimeOriginal", "OffsetTimeOriginal", "SubsecTimeOriginal"),
                   ("DateTimeDigitized", "OffsetTimeDigitized", "SubsecTimeDigitized"),
                   ("DateTime", "OffsetTime", "SubsecTime"))

_EXIF_DT = re.compile(r"^\s*(\d{4})[:\-](\d\d)[:\-](\d\d)[ T](\d\d):(\d\d):(\d\d)")
_OFFSET = re.compile(r"^\s*([+-])(\d\d):?(\d\d)\s*$")
_ISO_DT = re.compile(r"^\s*(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)(?::(\d\d))?(?:\.\d+)?\s*"
                     r"(Z|[+-]\d\d:?\d\d)?\s*$")
_ISO6709 = re.compile(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:([+-]\d+(?:\.\d+)?))?")
_XMP_DATES = ("xmp:CreateDate", "xmp:ModifyDate", "xmp:MetadataDate", "exif:DateTimeOriginal",
              "exif:DateTimeDigitized", "photoshop:DateCreated", "tiff:DateTime")

_MAX_VALUE = 200            # characters of a value shown before it is cut
_MAX_OTHER = 80             # fields kept in "other" per file, so a hostile file cannot flood a page
_ISOBMFF_1904 = calendar.timegm((1904, 1, 1, 0, 0, 0, 0, 0, 0))
_MAX_BOX_WALK = 4 * 1024 * 1024        # bytes of moov/udta walked at most


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

def _from_exif(exif):
    """(times, key fields, other fields, gps) out of a Pillow ``Image.Exif``."""
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
    return times, key, other, gps


def _from_xmp(blob):
    """Dated XMP properties (``xmp:CreateDate`` …) as time records; attribute or element form."""
    times = []
    if not blob:
        return times
    text = blob.decode("utf-8", "replace") if isinstance(blob, (bytes, bytearray)) else str(blob)
    for prop in _XMP_DATES:
        match = (re.search(rf'{re.escape(prop)}="([^"]+)"', text)
                 or re.search(rf"<{re.escape(prop)}>([^<]+)</{re.escape(prop)}>", text))
        if not match:
            continue
        record = _iso_time(f"XMP {prop}", match.group(1))
        if record:
            times.append(record)
    return times


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
    times, key, other, gps = ([], [], [], {})
    if exif:
        times, key, other, gps = _from_exif(exif)
    if info.get("xmp"):
        times += _from_xmp(info["xmp"])
    if fmt == "PNG":
        png_times, png_other = _png_text({k: v for k, v in info.items() if k != "xmp"})
        times += png_times
        other += png_other
        if info.get("XML:com.adobe.xmp"):
            times += _from_xmp(info["XML:com.adobe.xmp"])
    sources = []
    if exif:
        sources.append("EXIF")
    if info.get("xmp") or info.get("XML:com.adobe.xmp"):
        sources.append("XMP")
    if fmt == "PNG" and other:
        sources.append("PNG text")
    return _result(container=fmt, pixels=f"{size[0]}×{size[1]}", times=times, key=key,
                   other=other[:_MAX_OTHER], gps=gps, sources=sources)


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


def _qt_text(payload):
    """Text out of a QuickTime international-text udta atom, or a plain ``data`` atom child."""
    for kind, s, e in _boxes(payload, 0, len(payload)):
        if kind == b"data" and e - s > 8:
            return payload[s + 8:e].decode("utf-8", "replace")
    if len(payload) >= 4:
        size, _lang = struct.unpack_from(">HH", payload, 0)
        return payload[4:4 + size].decode("utf-8", "replace")
    return payload.decode("utf-8", "replace")


def _ilst(payload, names):
    """``{name: text}`` out of an ``ilst``; ``names`` maps a 1-based index to a ``keys`` name."""
    out = {}
    for kind, s, e in _boxes(payload, 0, len(payload)):
        idx = struct.unpack(">I", kind)[0]
        name = names.get(idx) if idx in names else kind.decode("latin-1", "replace")
        text = _qt_text(payload[s:e])
        if name and text:
            out[name] = text
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


def _read_bmff(fh):
    fh.seek(0)
    head = fh.read(16)
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    # the moov box is often at the END of a large file: find it by walking the top level
    moov = None
    total = fh.seek(0, io.SEEK_END)
    pos = 0
    brand = head[8:12].decode("latin-1", "replace").strip()
    while pos + 8 <= total:
        fh.seek(pos)
        hdr = fh.read(16)
        if len(hdr) < 8:
            break
        size, kind = struct.unpack_from(">I4s", hdr, 0)
        header = 8
        if size == 1 and len(hdr) >= 16:
            size = struct.unpack_from(">Q", hdr, 8)[0]
            header = 16
        elif size == 0:
            size = total - pos
        if size < header:
            break
        if kind == b"moov":
            fh.seek(pos + header)
            moov = fh.read(min(size - header, _MAX_BOX_WALK))
            break
        pos += size
    times, key, other = [], [], []
    duration = None
    if moov is None:
        return _result(container=brand, note="no moov box found — the file is truncated before "
                                             "its header, or the header was never cached")
    udta_items = {}
    for kind, s, e in _boxes(moov, 0, len(moov)):
        if kind == b"mvhd":
            created, modified, duration = _mvhd(moov[s:e])
            for label, stamp in (("mvhd creation_time", created),
                                 ("mvhd modification_time", modified)):
                record = _bmff_time(label, stamp)
                if record:
                    times.append(record)
        elif kind == b"udta":
            udta = moov[s:e]
            for k2, s2, e2 in _boxes(udta, 0, len(udta)):
                if k2 == b"meta":
                    meta = udta[s2 + 4:e2]                # version/flags precede the children
                    names = {}
                    for k3, s3, e3 in _boxes(meta, 0, len(meta)):
                        if k3 == b"keys":
                            names = _keys(meta[s3:e3])
                    for k3, s3, e3 in _boxes(meta, 0, len(meta)):
                        if k3 == b"ilst":
                            udta_items.update(_ilst(meta[s3:e3], names))
                else:
                    text = _qt_text(udta[s2:e2])
                    name = k2.decode("latin-1", "replace")
                    if text and text.isprintable():
                        udta_items.setdefault(name, text)
    gps = {}
    for name, text in list(udta_items.items()):
        low = name.lower()
        if low in ("com.apple.quicktime.creationdate", "©day", "creationdate"):
            record = _iso_time(f"QuickTime {name}", text)
            if record:
                times.append(record)
                udta_items.pop(name)
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
        if name in udta_items:
            key.append((name, _cut(udta_items.pop(name))))
    if duration:
        key.insert(0, ("Duration (mvhd)", f"{duration:.1f} s"))
    for name in sorted(udta_items):
        other.append((name, _cut(udta_items[name])))
    sources = []
    if any(t["label"].startswith("mvhd") for t in times):
        sources.append("mvhd")
    if udta_items or gps or any(t["label"].startswith("QuickTime") for t in times):
        sources.append("QuickTime user data")
    return _result(container=brand, times=times, key=key, other=other[:_MAX_OTHER], gps=gps,
                   sources=sources)


# ------------------------------------------------------------------------------ entry point

def _result(container="", pixels="", times=(), key=(), other=(), gps=None, sources=(), note=""):
    """The one shape every reader returns.

    ``present`` is whether the file carries anything at all; ``notable`` whether it carries anything
    worth flagging — a timestamp, a fix, or a field that is not one every encoder writes
    (:data:`STRUCTURAL`). The distinction matters on real data: most cached JPEGs hold only pixel
    size and orientation, and a chip on three quarters of the rows would say nothing.
    """
    times, key, other = list(times), list(key), list(other)
    gps = dict(gps or {})
    notable = bool(times or gps or any(name not in STRUCTURAL for name, _v in key + other))
    out = {"container": container, "times": times, "key": key, "other": other, "gps": gps,
           "present": bool(times or key or other or gps), "notable": notable,
           "sources": list(sources)}
    if pixels:
        out["pixels"] = pixels
    if note:
        out["note"] = note
    return out


def extract(path):
    """Embedded metadata of one media file on disk, or ``None`` when it is not a format read here.

    The result is a dict: ``container`` (format / brand), ``pixels`` (images), ``times`` (records
    from :func:`_time`), ``key`` and ``other`` (``[(field, value)]``), ``gps`` (``lat``/``lon``/
    ``alt`` when the file carries a fix), ``present`` (whether anything at all was found),
    ``notable`` (whether any of it is worth flagging — see :func:`_result`) and ``sources`` (which
    metadata stores were present). ``note`` says why a file could not be read when that is worth
    telling the examiner (HEIF; a video with no header).
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
