"""
The device filesystem's own record of each extracted file — its four timestamps, owner, mode,
inode and data-protection class — read from whatever the extraction archive carries, into one shape.

Why this module exists
----------------------
An extraction archive records more about a file than its bytes, and the two acquisition tools in
use record it differently:

* A **Cellebrite UFED / CLBX** archive carries ``metadata<N>/metadata.msgpack`` beside
  ``filesystem<N>/``: one map of *every path on the volume* to its stat record —
  ``atime``/``btime``/``ctime``/``mtime`` in **nanoseconds**, ``uid``/``gid``, ``inode``, ``links``,
  ``mode``, ``prot`` (the iOS data-protection class) and ``xattr``. Its ZIP entries also carry a ``UT``
  extra field flagged mtime / atime / ctime, but all three slots hold one value, the **access** time,
  in whole seconds — it matched the table's ``atime`` on every file whose access and modification
  times differ. So a CLBX entry's ``UT`` is read as the access time and nothing else. The archive is
  recognised by the table (:func:`clbx_tables`), not by its ``version`` member (``CLBX-…``), which an
  older UFED archive does not have.
* A **GrayKey** archive carries the record in each entry's extra fields: a ``UT`` field with the
  mtime, atime and ctime — and, for an iOS device, a non-standard fourth value, the APFS birth time —
  plus Info-ZIP ``ux`` (uid/gid) and tool-specific fields (``S2`` = SHA-256 of the content, ``IN`` =
  inode + device, ``GK``; see TODO.md). Compared with UFED's stat record of the same device, each of
  the four matched only its own named time.
* A **UFED archive of an Android phone** has no table; its ``UT`` holds the mtime, the atime and 0
  (the partial — BFU, user-data, app-selective — archives fill only the mtime).
* Any other ZIP carries at least the ``UT`` modification time, or nothing.

`extract_zip` records which of these it read in the manifest's ``archive`` (:func:`archive_kind`), and
:func:`manifest_times` reads a manifest written before that the way its archive means it.

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
#: GrayKey: the file's inode (8 bytes, little-endian) followed by its device number (4 bytes). Read as
#: such because, in an Android archive, the entries a phone mounts at several paths (/data/data,
#: /data/user/0, /data_mirror/data_ce/null/0) carry the same pair, and each partition (/data, /system,
#: /vendor) carries one device number of its own.
IN_ID = 0x4E49
#: GrayKey: the SHA-256 of the entry's content as the acquisition tool computed it (32 bytes).
S2_ID = 0x3253
#: GrayKey: two bytes, ``0100`` on every sample. Not read; one of the fields that mark a GrayKey entry.
GK_ID = 0x4B47
_GRAYKEY_IDS = frozenset((S2_ID, IN_ID, GK_ID))

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


def from_zip_entry(info, extended=False, ut_access_only=False):
    """The record an archive entry's own extra fields carry, or ``None`` when they carry no time.

    The ``UT`` field is read for every timestamp it flags, not only the first: a GrayKey archive writes
    four (flags ``0b1111``) for an iOS device and three for an Android one, a UFED archive of an Android
    phone three, a plain zip tool one. The fourth is not in the ``UT`` specification; it is recorded as
    the birth time with its basis stated: compared with UFED's stat record of the same device, it
    matched the named birth time and no other. A time of 0 or less is a value the archive did not
    record — a UFED archive of an Android phone writes 0 as every file's change time — and is left out
    rather than shown as 1970.

    ``ut_access_only`` is for an entry of a UFED / CLBX archive, whose ``UT`` holds the access time in
    every slot whatever its flags say: the record then carries that one value as ``atime`` — never a
    modification, change or birth time — under the source ``zip-ut-clbx``.

    ``extended`` also reads what only the Android extraction uses so far: the permission bits of the
    entry's Unix mode, GrayKey's inode / device number (:data:`IN_ID`) and its SHA-256 of the content
    (:data:`S2_ID`, kept as ``archive_sha256`` so the extracted bytes can be checked against it).
    """
    source = "zip-ut-clbx" if ut_access_only else "zip-ut"
    record = None
    for header, body in _extra_fields(info.extra):
        if header == UT_ID and body:
            flags = body[0]
            count = (len(body) - 1) // 4
            values = list(struct.unpack_from(f"<{count}i", body, 1)) if count else []
            kinds = [k for bit, k in ((1, "mtime"), (2, "atime"), (4, "ctime")) if flags & bit]
            if not values:
                continue
            record = record or {"source": source, "precision": "s"}
            if ut_access_only:
                if values[0] > 0:
                    record["atime"] = values[0] * NS
                continue
            for kind, value in zip(kinds, values):
                if value > 0:
                    record[kind] = value * NS
            if flags & 8 and len(values) > len(kinds) and values[len(kinds)] > 0:
                record["btime"] = values[len(kinds)] * NS
                record["btime_basis"] = ("the archive's fourth UT time — not part of the UT "
                                         "specification; read as the birth time because, compared "
                                         "with UFED's stat record of the same device, it matched the "
                                         "named birth time and no other")
        elif extended and header == IN_ID and len(body) >= 8:
            record = record or {"source": source, "precision": "s"}
            record["inode"] = int.from_bytes(body[:8], "little")
            if len(body) >= 12:
                record["dev"] = int.from_bytes(body[8:12], "little")
        elif extended and header == S2_ID and len(body) == 32:
            record = record or {"source": source, "precision": "s"}
            record["archive_sha256"] = body.hex()
        elif header == UX_ID and len(body) >= 3:
            try:
                uid_size = body[1]                         # body[0] is the field's version (1)
                uid = int.from_bytes(body[2:2 + uid_size], "little")
                gid_size = body[2 + uid_size]
                gid = int.from_bytes(body[3 + uid_size:3 + uid_size + gid_size], "little")
            except (IndexError, ValueError):
                continue
            record = record or {"source": source, "precision": "s"}
            record["uid"], record["gid"] = uid, gid
    if extended and record is not None:
        mode = (info.external_attr >> 16) & 0xFFFF
        # A mode with no permission bits at all is an archive that did not record them (a UFED
        # archive of an Android phone writes 0o100000 for every file), not a file nobody may read.
        if mode & 0o7777:
            record["mode"] = mode
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


def clbx_tables(names):
    """The ``metadata<N>/metadata.msgpack`` stat tables among an archive's entry names. Having one is
    what makes an archive UFED / CLBX, whose entries' ``UT`` field holds only the access time."""
    return [n for n in names if n.lower().endswith("/metadata.msgpack")
            and n.lower().split("/")[0].startswith("metadata")]


def archive_kind(names, infos):
    """Which reading of the ``UT`` field applies to an archive — recorded in the extraction manifest as
    ``archive``, so a reader can tell where a time came from.

    ``"clbx"``: the archive has a stat table; its entries' ``UT`` is the access time only.
    ``"graykey"``: an extracted entry (``infos``) carries GrayKey's own extra fields, or the fourth
    ``UT`` value. ``"zip"``: neither, and the ``UT`` field is read as its specification defines it — a
    UFED archive of an Android phone is one.
    """
    if clbx_tables(names):
        return "clbx"
    for info in infos:
        for header, body in _extra_fields(info.extra):
            if header in _GRAYKEY_IDS or (header == UT_ID and body and body[0] & 8):
                return "graykey"
    return "zip"


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
    for table in clbx_tables(names):
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


# --------------------------------------------------------------------------- earlier builds' manifests

#: A UFED/CLBX archive's volumes are ``filesystem<N>/``, so the archive path a container or app folder
#: was read from starts with one.
_CLBX_PREFIX = re.compile(r"^/?filesystem\d*/", re.I)

_LEGACY_LOGGED = set()


def legacy_archive_kind(manifest):
    """``(kind, basis)`` for a manifest written before ``archive`` was recorded: ``"clbx"``, ``"zip"``
    (its ``UT`` fields were read as the specification defines them, which is right for any archive
    but a CLBX one) or ``None`` when the folder does not say."""
    records = [r for r in (manifest.get("fs") or {}).values() if isinstance(r, dict)]
    if any(r.get("source") == "ufed-metadata" for r in records):
        return "clbx", "the folder holds records from the archive's metadata.msgpack"
    # where each container (iOS) or app folder (Android) was read from in the archive
    prefixes = [str(p) for p in (manifest.get("container_prefixes") or {}).values()]
    prefixes += [str(p) for root in (manifest.get("roots") or {}).values() if isinstance(root, dict)
                 for p in root.get("archive_roots") or []]
    if any(_CLBX_PREFIX.match(p) for p in prefixes):
        return "clbx", "the app's files were read from under filesystem<N>/, the layout of one"
    if prefixes or any(r.get("btime_basis") or r.get("archive_sha256") for r in records):
        return "zip", ""
    return None, ""


def manifest_times(manifest, where=""):
    """``(mtimes, fs)`` out of an extraction manifest, read the way the archive it came from means them.

    A manifest that records its ``archive`` is right as written. One written before that read a
    UFED/CLBX entry's ``UT`` field — the access time — as the modification time: into ``mtimes`` for
    every file (1.6.0-beta.2 to 1.6.1-beta.1 recorded nothing else), and into ``fs`` for a file the
    stat table had no record of. When the folder shows its archive was CLBX
    (:func:`legacy_archive_kind`), those values become ``atime`` under the source ``zip-ut-clbx``, and
    ``mtimes`` keeps only the stat table's own modification times. When the folder cannot say, they
    keep their place under ``zip-ut-unclassified``, whose label states both readings. Either case is
    logged once per manifest (``where``), with the advice to re-extract.
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    mtimes = dict(manifest.get("mtimes") or {})
    fs = dict(manifest.get("fs") or {})
    if manifest.get("archive") or not (mtimes or fs):
        return mtimes, fs
    kind, basis = legacy_archive_kind(manifest)
    if kind == "zip":
        return mtimes, fs
    source = "zip-ut-clbx" if kind == "clbx" else "zip-ut-unclassified"
    for path, record in fs.items():
        if isinstance(record, dict) and record.get("source") == "zip-ut":
            record = dict(record, source=source)
            if kind == "clbx":
                access = record.get("atime") or record.get("mtime")
                for field in ("mtime", "atime", "ctime", "btime", "btime_basis"):
                    record.pop(field, None)
                if access:
                    record["atime"] = access
            fs[path] = record
    for path, stamp in list(mtimes.items()):
        record = fs.get(path)
        if isinstance(record, dict) and record.get("source") == "ufed-metadata":
            if record.get("mtime"):
                mtimes[path] = record["mtime"] // NS
            else:
                del mtimes[path]
            continue
        if kind == "clbx":
            del mtimes[path]
        if not record and isinstance(stamp, (int, float)) and stamp > 0:
            fs[path] = {"source": source, "precision": "s",
                        ("atime" if kind == "clbx" else "mtime"): int(stamp) * NS}
    if where not in _LEGACY_LOGGED:
        _LEGACY_LOGGED.add(where)
        name = where or "The extraction manifest"
        if kind == "clbx":
            logger.warning(f"{name} was written by an earlier build, which read this UFED/CLBX "
                           f"archive's UT field as the modification time. It is the access time, "
                           f"and is shown as such ({basis}). Re-extract from the archive for the "
                           f"device's full record of each file.")
        else:
            logger.warning(f"{name} was written by an earlier build and does not say which tool "
                           f"made the archive: its UT times are the modification time if GrayKey "
                           f"made it and the access time if UFED did, and are shown with both "
                           f"readings. Re-extract from the archive to tell them apart.")
    return mtimes, fs


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
    "zip-ut-clbx": "the archive entry's UT extra field (whole seconds), which in a UFED/CLBX archive "
                   "holds the access time only",
    "zip-ut-unclassified": "the archive entry's UT extra field (whole seconds), in an extraction folder "
                           "that does not say which tool made the archive: the modification time if "
                           "GrayKey made it, the access time if UFED did — re-extract to tell",
}


def source_label(record):
    return SOURCE_LABELS.get((record or {}).get("source"), "the extraction archive")


DEVICE_FS_BASIS = (
    "What the DEVICE's filesystem recorded about the cache file, as the extraction archive carries "
    "it — never the extracted copy's own timestamps, which are the moment this tool unzipped it. The "
    "acquisition tools record it differently, and the report names which it read:\n\n"
    "• A Cellebrite UFED (CLBX) archive carries a stat record per path in metadata.msgpack: created "
    "(birth), modified, accessed and inode-changed times at nanosecond precision, the owner, mode, "
    "inode and the iOS data-protection class, plus extended attributes. Its ZIP entries' own UT "
    "field holds only the access time, so for a file the table has no record of, that is all that "
    "is shown.\n\n"
    "• A GrayKey archive carries the times in each entry's UT extra field at whole seconds — "
    "modified, accessed, changed and, for an iOS device, a fourth, non-standard value: the birth "
    "time, which it matched in UFED's stat record of the same device.\n\n"
    "• Any other archive carries what its UT field records — at least the modification time — or "
    "nothing («not recorded»).\n\n"
    "Read the four with care: «modified» is when the content was last written; «created» when the "
    "file came into being; «accessed» and «inode changed» can be set by the acquisition itself, so "
    "they date the last read or metadata change, not necessarily the user's activity. Identical "
    "instants are shown on one line. A claim "
    "time in cache_controller.db (CREATION_TIMESTAMP_MILLIS) is a different record again: when the "
    "app registered the claim, not when the bytes were written.")
