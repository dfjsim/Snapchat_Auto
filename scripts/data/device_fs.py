"""
The device filesystem's own record of each extracted file — its four timestamps, owner, mode,
inode and data-protection class — read from whatever the extraction archive carries, into one shape.

Why this module exists
----------------------
An extraction archive records more about a file than its bytes, and the two acquisition tools in
use record it differently:

* A **Cellebrite UFED / CLBX** archive (``version`` = ``CLBX-…``) carries ``metadata<N>/metadata.msgpack``
  beside ``filesystem<N>/``: one map of *every path on the volume* to its stat record —
  ``atime``/``btime``/``ctime``/``mtime`` in **nanoseconds**, ``uid``/``gid``, ``inode``, ``links``,
  ``mode``, ``prot`` (the iOS data-protection class) and ``xattr``. Its ZIP entries also carry a
  standard ``UT`` extra field, but at whole seconds and without the birth time.
* A **GrayKey** archive carries the record in each entry's extra fields: a ``UT`` field with **four**
  timestamps — mtime, atime, ctime and a non-standard fourth that is at or before mtime in practically
  every entry, i.e. the APFS birth time — plus Info-ZIP ``ux`` (uid/gid) and tool-specific fields
  (``S2`` = SHA-256 of the content, ``NI``, ``KG``; see TODO.md).
* Any other ZIP carries at least the ``UT`` modification time, or nothing.

Every report used to show only the modification time. The rest matters: the **birth time** dates the
file's creation independently of the app's database; the access and inode-change times can show
whether the acquisition itself touched the file; the protection class says what unlock state the file
was readable in. So `extract_zip` records the whole thing per extracted file, from the richest source
the archive has, and the reports render it through one function so it reads the same way everywhere.

Two rules. Times are kept as **integer nanoseconds since 1970, UTC** whatever the source, with the
source's ``precision`` recorded, so a nanosecond record is never shown as if it were more exact than it
is and a whole-second one is never padded. And nothing is inferred: a time the archive did not record is
simply absent from the record, never filled from the extracted copy's own stat.
"""

import re
import struct
import logging

logger = logging.getLogger("snapchat_auto")

#: The "extended timestamp" extra field: UTC seconds, flags bit 0 = mtime, 1 = atime, 2 = ctime.
UT_ID = 0x5455
#: Info-ZIP "new Unix" extra field: uid / gid.
UX_ID = 0x7875

NS = 1_000_000_000

#: The four timestamps, in the order they are shown. The label says what the field means on APFS;
#: the note is the caveat a reader needs before treating the value as a fact about the user's actions.
TIME_KINDS = (
    ("btime", "created", "the file's birth time on the device (APFS creation time)"),
    ("mtime", "modified", "when the file's content was last written on the device"),
    ("atime", "accessed", "when the file was last read on the device — an acquisition that reads the "
                          "file can set this to the acquisition time"),
    ("ctime", "inode changed", "when the file's metadata (owner, mode, name, …) last changed on the "
                               "device — an acquisition can set this too; it is not the content time"),
)

#: iOS data-protection classes, by the value APFS stores in ``prot``.
PROTECTION_CLASSES = {
    0: "none recorded (0)",
    1: "NSFileProtectionComplete (1) — readable only while unlocked",
    2: "NSFileProtectionCompleteUnlessOpen (2)",
    3: "NSFileProtectionCompleteUntilFirstUserAuthentication (3) — readable after first unlock",
    4: "NSFileProtectionNone (4) — readable at any time",
}


def _extra_fields(extra):
    pos, out = 0, []
    extra = extra or b""
    while pos + 4 <= len(extra):
        header, size = struct.unpack_from("<HH", extra, pos)
        out.append((header, extra[pos + 4:pos + 4 + size]))
        pos += 4 + size
    return out


def from_zip_entry(info):
    """The record an archive entry's own extra fields carry, or ``None`` when they carry no time.

    The ``UT`` field is read for every timestamp it flags, not only the first: a GrayKey archive writes
    four (flags ``0b1111``), a UFED archive three, a plain zip tool one. The fourth is not in the
    ``UT`` specification; it is recorded as the birth time with its basis stated, because across the
    corpus it is at or before the modification time in practically every entry, which is what a birth
    time does and an access or change time does not.
    """
    record = None
    for header, body in _extra_fields(info.extra):
        if header == UT_ID and body:
            flags = body[0]
            count = (len(body) - 1) // 4
            values = list(struct.unpack_from(f"<{count}i", body, 1)) if count else []
            kinds = [k for bit, k in ((1, "mtime"), (2, "atime"), (4, "ctime")) if flags & bit]
            if not values:
                continue
            record = record or {"source": "zip-ut", "precision": "s"}
            for kind, value in zip(kinds, values):
                record[kind] = value * NS
            if flags & 8 and len(values) > len(kinds):
                record["btime"] = values[len(kinds)] * NS
                record["btime_basis"] = ("the archive's fourth UT time — not part of the UT "
                                         "specification; read as the birth time because it is at or "
                                         "before the modification time in practically every entry")
        elif header == UX_ID and len(body) >= 3:
            try:
                uid_size = body[1]                         # body[0] is the field's version (1)
                uid = int.from_bytes(body[2:2 + uid_size], "little")
                gid_size = body[2 + uid_size]
                gid = int.from_bytes(body[3 + uid_size:3 + uid_size + gid_size], "little")
            except (IndexError, ValueError):
                continue
            record = record or {"source": "zip-ut", "precision": "s"}
            record["uid"], record["gid"] = uid, gid
    return record


def _xattr_text(value):
    if isinstance(value, (bytes, bytearray)):
        try:
            text = value.decode("utf-8")
            if text.isprintable():
                return text[:200]
        except UnicodeDecodeError:
            pass
        return f"<{len(value)} bytes> {bytes(value[:24]).hex()}"
    return str(value)[:200]


def from_ufed_record(raw):
    """A ``metadata.msgpack`` stat record in the shared shape (times already nanoseconds)."""
    record = {"source": "ufed-metadata", "precision": "ns"}
    for kind in ("btime", "mtime", "atime", "ctime"):
        value = raw.get(kind)
        if isinstance(value, int) and value > 0:
            record[kind] = value
    for field in ("size", "inode", "links", "uid", "gid", "mode", "prot"):
        if isinstance(raw.get(field), int):
            record[field] = raw[field]
    xattr = raw.get("xattr")
    if isinstance(xattr, dict) and xattr:
        record["xattr"] = {str(k)[:120]: _xattr_text(v) for k, v in list(xattr.items())[:40]}
    return record


def load_ufed_metadata(zip_file, names, keep):
    """``{zip entry name: record}`` for the entries ``keep(name)`` accepts, out of every
    ``metadata<N>/metadata.msgpack`` the archive carries.

    The map covers the whole volume — half a million records on a phone — so it is **streamed**
    pair by pair with `msgpack.Unpacker.read_map_header`, keeping only the records of files this run
    extracts; loading it whole would cost hundreds of megabytes for a few thousand entries of
    interest. ``filesystem<N>/`` is the folder the paths of ``metadata<N>`` are relative to, mounted at
    the ``mount_point`` its ``filesystem.msgpack`` states (``/`` on every archive seen).
    """
    try:
        import msgpack
    except ImportError:                                    # the dependency is declared; be explicit
        logger.warning("msgpack is not installed — the UFED per-file metadata cannot be read")
        return {}
    out = {}
    tables = [n for n in names if n.lower().endswith("/metadata.msgpack")
              and n.lower().split("/")[0].startswith("metadata")]
    for table in tables:
        folder = table.split("/")[0]
        fs_prefix = "filesystem" + folder[len("metadata"):] + "/"
        mount = "/"
        try:
            fs_meta = msgpack.unpackb(zip_file.read(table.rsplit("/", 1)[0] + "/filesystem.msgpack"),
                                      raw=False)
            mount = fs_meta.get("mount_point") or "/"
        except Exception:
            pass
        prefix = fs_prefix + mount.strip("/")
        prefix = prefix if prefix.endswith("/") else prefix + "/"
        kept = 0
        try:
            with zip_file.open(table) as fh:
                unpacker = msgpack.Unpacker(fh, raw=False, max_buffer_size=64 * 1024 * 1024)
                count = unpacker.read_map_header()
                for _ in range(count):
                    path = unpacker.unpack()
                    raw = unpacker.unpack()
                    name = prefix + str(path).lstrip("/")
                    if isinstance(raw, dict) and keep(name):
                        out[name] = from_ufed_record(raw)
                        kept += 1
        except Exception as error:                          # noqa: BLE001 - one table must not cost the run
            logger.warning(f"Could not read {table}: {error} — the ZIP entries' own timestamps are "
                           f"used instead")
            continue
        logger.info(f"Read {table}: per-file metadata for {kept} extracted file(s) ({count} on the "
                    f"volume) — birth/access/change times at nanosecond precision, protection class, "
                    f"owner and xattrs")
    return out


# --------------------------------------------------------------------------- rendering

_HMS = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def format_ns(ns, epochfmt, precision="s"):
    """A nanosecond instant through the run's second-resolution formatter, with the fraction shown
    only when the record actually has one — a whole-second source is not padded to look exact."""
    if ns is None:
        return ""
    seconds, rest = divmod(int(ns), NS)
    text = epochfmt(seconds)
    if precision == "ns" and text and _HMS.match(text):
        return text[:19] + f".{rest // 1000:06d}" + text[19:]
    return text


def summarize(records, epochfmt):
    """``(time lines, attribute pairs)`` for one file — or for the byte-range parts of one file.

    Time lines are ``(labels, shown, note, kinds)``: one line per distinct instant, so a file written
    once and never touched reads «modified / accessed / inode changed» on one line rather than the
    same value three times. Across several parts each timestamp is bounded — «earliest … latest (N
    parts)» — since the parts are one media file and a table cell has to stay a cell. Attributes are
    the non-time facts (protection class, inode, mode, owner, hard links, size, xattrs), with a value
    that differs between parts stated as such rather than picked from one of them.
    """
    records = [r for r in records if r]
    if not records:
        return [], []
    precision = "ns" if any(r.get("precision") == "ns" for r in records) else "s"
    lines, seen = [], {}
    for kind, label, note in TIME_KINDS:
        values = [r[kind] for r in records if r.get(kind) is not None]
        if not values:
            continue
        low, high = min(values), max(values)
        if low == high:
            shown = format_ns(low, epochfmt, precision)
        else:
            shown = (f"{format_ns(low, epochfmt, precision)} … {format_ns(high, epochfmt, precision)} "
                     f"({len(values)} parts)")
        if kind == "btime":
            basis = next((r["btime_basis"] for r in records if r.get("btime_basis")), "")
            if basis:
                note = basis
        if shown in seen:
            seen[shown][0].append(label)
            seen[shown][2].append(kind)
            seen[shown][1].append(note)
        else:
            seen[shown] = ([label], [note], [kind])
            lines.append(shown)
    time_lines = [(" / ".join(seen[shown][0]), shown, "; ".join(seen[shown][1]), seen[shown][2])
                  for shown in lines]

    attrs = []
    for label, values in _attribute_columns(records):
        distinct = list(dict.fromkeys(values))
        if len(distinct) == 1:
            attrs.append((label, distinct[0]))
        else:
            attrs.append((label, "varies between parts: " + "; ".join(distinct[:4])
                          + (" …" if len(distinct) > 4 else "")))
    return time_lines, attrs


def _attribute_columns(records):
    """``[(label, [value per record])]`` for every attribute any record carries."""
    columns, order = {}, []

    def add(label, value):
        if label not in columns:
            columns[label] = []
            order.append(label)
        columns[label].append(value)

    for r in records:
        if r.get("prot") is not None:
            add("protection class", PROTECTION_CLASSES.get(r["prot"], f"class {r['prot']}"))
        if r.get("inode") is not None:
            add("inode", str(r["inode"]))
        if r.get("mode") is not None:
            add("mode", f"{r['mode'] & 0o7777:04o}")
        if r.get("uid") is not None or r.get("gid") is not None:
            add("owner", f"uid {r.get('uid', '?')} / gid {r.get('gid', '?')}")
        if r.get("links") not in (None, 1):
            add("hard links", str(r["links"]))
        for name, value in (r.get("xattr") or {}).items():
            add(f"xattr {name}", value)
    return [(label, columns[label]) for label in order]


def times(record, epochfmt):
    """``[(kind, label, shown, note)]`` — every timestamp one record has, in display order."""
    out = []
    if not record:
        return out
    for kind, label, note in TIME_KINDS:
        if record.get(kind) is None:
            continue
        shown = format_ns(record[kind], epochfmt, record.get("precision", "s"))
        if kind == "btime" and record.get("btime_basis"):
            note = record["btime_basis"]
        out.append((kind, label, shown, note))
    return out


def attributes(record):
    """``[(label, value)]`` — the non-time facts of one record."""
    return summarize([record], lambda seconds: "")[1] if record else []


SOURCE_LABELS = {
    "ufed-metadata": "UFED/CLBX metadata.msgpack (nanosecond stat record)",
    "zip-ut": "the archive entry's UT extra field (whole seconds)",
}


def source_label(record):
    return SOURCE_LABELS.get((record or {}).get("source"), "the extraction archive")


DEVICE_FS_BASIS = (
    "What the DEVICE's filesystem recorded about the cache file, as the extraction archive carries "
    "it — never the extracted copy's own timestamps, which are the moment this tool unzipped it. Two "
    "acquisition tools record it differently, and the report names which it read:\n\n"
    "• A Cellebrite UFED (CLBX) archive carries a stat record per path in metadata.msgpack: created "
    "(birth), modified, accessed and inode-changed times at nanosecond precision, the owner, mode, "
    "inode and the iOS data-protection class, plus extended attributes.\n\n"
    "• A GrayKey archive carries the times in each entry's UT extra field at whole seconds — "
    "modified, accessed, changed, and a fourth, non-standard value read here as the birth time "
    "because it is at or before the modification time in practically every entry.\n\n"
    "• Any other archive carries at least the modification time, or nothing («not recorded»).\n\n"
    "Read the four with care: «modified» is when the content was last written; «created» when the "
    "file came into being; «accessed» and «inode changed» can be set by the acquisition itself (a "
    "GrayKey acquisition commonly leaves both at the acquisition time), so they date the last read or "
    "metadata change, not the user's activity. Identical instants are shown on one line. A claim "
    "time in cache_controller.db (CREATION_TIMESTAMP_MILLIS) is a different record again: when the "
    "app registered the claim, not when the bytes were written.")
