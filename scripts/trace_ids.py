"""Where does an identifier occur in an extraction? (``--trace-ids``)

A report can only link what it knows to look for. When two artifacts plainly belong together — a
cached file and a Memory, say — but no report connects them, the question is whether the device
recorded the connection somewhere nobody reads yet. This answers it the direct way: every file the
run extracted is searched for each identifier, in every encoding an app is likely to have stored it
in, and every place it occurs is listed.

What is searched for, per identifier:

* the text as given, in any letter case, as UTF-8 and as UTF-16LE (``text``, ``text-utf16le``);
  an identifier with a ``~N`` suffix (the shape of some cache claim keys) is also searched without it;
* for 32 hex digits — a UUID with or without dashes, a CACHE_KEY — also the dashless hex (``hex``),
  the 16 bytes themselves (``bytes``), a UUID's little-endian byte order (``bytes-uuid-le``) and the
  bytes in base64 (``base64``, ``base64url``).

Where it is searched:

* every file under ``<run folder>/ExtractedData``, raw, in chunks (``-shm`` index files skipped);
* SQLite databases also row by row, through :mod:`scripts.data.sqlite_open` — both readings, with
  and without the ``-wal`` — so a hit is reported as table, column and row, and marked by which
  reading holds it. A value split across overflow pages is invisible to the raw pass and found here;
* a ``-wal`` hit is placed in its frame and page, and a frame a later one superseded is marked as
  such: that is deleted prior state, which neither reading of the database returns.

What it reports is **where**, never **what**: file, offset, table, column, row number, the
encoding and the kind of cell. No cell value or file content is written to the log or the JSON —
the output names locations in a case extraction, and that is all it should carry. It still names
the identifiers searched for, so it stays with the case like the rest of the run folder.
"""
import base64
import datetime
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass

from scripts.data import sqlite_open

logger = logging.getLogger(__name__)

#: Raw files are read in chunks of this size; consecutive chunks overlap by the longest needle less
#: one byte, so a match straddling a boundary is still seen exactly once.
CHUNK = 32 << 20
#: A database at most this large is always read row by row, hit or not: a value long enough to spill
#: onto overflow pages is cut by page headers, which the raw pass cannot see through.
SQL_PASS_MAX_BYTES = 256 << 20
#: Shorter search forms are dropped: they would match by chance.
MIN_NEEDLE = 6
#: Progress is logged after roughly this many bytes.
PROGRESS_EVERY = 512 << 20

SQLITE_MAGIC = b"SQLite format 3\x00"
_WAL_MAGICS = (0x377F0682, 0x377F0683)
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_SUFFIX = re.compile(r"^(.+?)(~\d+)$")

#: Exit codes, in the convention of the other diagnostics: found / nothing found / bad arguments.
EXIT_FOUND, EXIT_NONE, EXIT_USAGE = 0, 1, 2

#: How each kind of hit is described in the log — the JSON carries the same as ``kind``.
KIND_TEXT = {
    "sqlite": "a row of the database",
    "sqlite-file": "the database file itself",
    "wal": "the database's write-ahead log",
    "file": "the file",
}


@dataclass(frozen=True)
class Needle:
    """One encoding of one identifier.

    ``folded`` needles are ASCII text: they are searched in a lower-cased copy of the bytes, so one
    pass finds any letter case, and the case actually found is reported afterwards.
    """
    index: int
    form: str
    data: bytes
    folded: bool


def needles_for(index, identifier):
    """Every search form of ``identifier`` (see the module docstring), duplicates removed."""
    ident = identifier.strip()
    bases = [ident]
    mo = _SUFFIX.match(ident)
    if mo:
        bases.append(mo.group(1))
    out, seen = [], set()

    def add(form, data, folded):
        if len(data) < MIN_NEEDLE or (data, folded) in seen:
            return
        seen.add((data, folded))
        out.append(Needle(index, form, data, folded))

    for i, base in enumerate(bases):
        suffix = "" if i == 0 else " (without the ~N suffix)"
        add("text" + suffix, base.lower().encode("utf-8"), True)
        add("text-utf16le" + suffix, base.lower().encode("utf-16-le"), True)
        digits = base.replace("-", "")
        if not _HEX32.match(digits):
            continue
        raw = bytes.fromhex(digits)
        if "-" in base:
            add("hex" + suffix, digits.lower().encode("ascii"), True)
        add("bytes" + suffix, raw, False)
        add("bytes-uuid-le" + suffix, uuid.UUID(bytes=raw).bytes_le, False)
        add("base64" + suffix, base64.b64encode(raw).rstrip(b"="), False)
        add("base64url" + suffix, base64.urlsafe_b64encode(raw).rstrip(b"="), False)
    return out


def _case_of(found, needle):
    """``upper`` / ``lower`` / ``mixed`` for a folded hit, from the bytes actually found."""
    if not needle.folded or found.upper() == found.lower():      # no letters: no case to report
        return ""
    if found == found.upper():
        return "upper"
    if found == found.lower():
        return "lower"
    return "mixed"


def scan_bytes(buf, needles, start=0, base=0):
    """``[(needle, offset, case)]`` for every occurrence in ``buf`` ending at or after ``start``.

    ``base`` is the file offset of ``buf[0]``. Matches that end before ``start`` lie entirely in the
    overlap carried from the previous chunk and were reported with it.
    """
    hits = []
    low = None
    for needle in needles:
        if needle.folded:
            if low is None:
                low = buf.lower()
            hay = low
        else:
            hay = buf
        i = hay.find(needle.data)
        while i != -1:
            if i + len(needle.data) > start:
                hits.append((needle, base + i, _case_of(buf[i:i + len(needle.data)], needle)))
            i = hay.find(needle.data, i + 1)
    return hits


def scan_file(path, needles, chunk=CHUNK):
    """Raw occurrences of ``needles`` in one file: ``[(needle, offset, case)]``."""
    if not needles:
        return []
    overlap = max(len(n.data) for n in needles) - 1
    hits = []
    with open(path, "rb") as fh:
        tail, consumed = b"", 0
        while True:
            block = fh.read(chunk)
            if not block:
                break
            buf = tail + block
            hits += scan_bytes(buf, needles, start=len(tail), base=consumed - len(tail))
            consumed += len(block)
            tail = buf[-overlap:] if overlap else b""
    return hits


# ------------------------------------------------------------------------------- write-ahead logs

def _wal_layout(wal_path):
    """``(page_size, frames)`` of a -wal, ``frames`` being ``[(page_no, superseded)]`` in file order.

    ``(0, [])`` when it is not a write-ahead log. Superseded = a later frame holds the same page.
    """
    try:
        with open(wal_path, "rb") as fh:
            header = fh.read(32)
    except OSError:
        return 0, []
    if len(header) < 32 or int.from_bytes(header[:4], "big") not in _WAL_MAGICS:
        return 0, []
    pages = [page_no for _idx, page_no, _data in sqlite_open.wal_page_images(wal_path[:-4])]
    last = {page_no: i for i, page_no in enumerate(pages)}
    page_size = int.from_bytes(header[8:12], "big")
    page_size = 65536 if page_size == 1 else page_size
    return page_size, [(page_no, last[page_no] != i) for i, page_no in enumerate(pages)]


def _wal_place(offset, page_size, frames):
    """Where in a -wal an offset falls: ``{"frame", "page", "superseded"}`` or a header note."""
    if offset < 32:
        return {"where": "log header"}
    frame, within = divmod(offset - 32, 24 + page_size)
    if frame >= len(frames):
        return {"where": "after the last complete frame"}
    page_no, superseded = frames[frame]
    return {"frame": frame, "page": page_no,
            "where": "frame header" if within < 24 else "page image",
            "superseded": superseded}


# ---------------------------------------------------------------------------------- SQLite rows

def _cell_kind(value):
    """The kind of a cell — never its value."""
    if isinstance(value, str):
        return "text"
    head = bytes(value[:16])
    if head.startswith(b"bplist00"):
        return "blob: binary plist"
    if head.startswith(SQLITE_MAGIC):
        return "blob: SQLite database"
    if head[:2] == b"\x1f\x8b":
        return "blob: gzip"
    return "blob"


def _rows(conn, table):
    """``(row label, column names, values)`` for every row of ``table``.

    The label is the rowid. A WITHOUT ROWID table has none, and its primary key is evidence content,
    so its rows are numbered in table order instead.
    """
    try:
        cur = conn.execute(f'SELECT rowid AS "__rowid", * FROM "{table}"')
        cols = [d[0] for d in cur.description][1:]
        for row in cur:
            yield row[0], cols, row[1:]
        return
    except sqlite3.DatabaseError:
        pass
    try:
        cur = conn.execute(f'SELECT * FROM "{table}"')
    except sqlite3.DatabaseError as error:
        logger.debug(f"{table}: not readable ({error})")
        return
    cols = [d[0] for d in cur.description]
    for n, row in enumerate(cur, 1):
        yield f"row #{n} (no rowid)", cols, row


def _sql_hits(conn, needles):
    """``{(table, column, row, form, case, cell kind)}`` for one reading of a database."""
    found = set()
    if conn is None:
        return found
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    except sqlite3.DatabaseError as error:
        logger.debug(f"schema not readable: {error}")
        return found
    for table in tables:
        for label, cols, values in _rows(conn, table):
            for col, value in zip(cols, values):
                if isinstance(value, str):
                    data = value.encode("utf-8", "surrogatepass")
                elif isinstance(value, (bytes, bytearray, memoryview)):
                    data = bytes(value)
                else:
                    continue
                if len(data) < MIN_NEEDLE:
                    continue
                for needle, _off, case in scan_bytes(data, needles):
                    found.add((table, col, label, needle.index, needle.form, case,
                               _cell_kind(value)))
    return found


def scan_database(db_path, needles):
    """Row-level hits in both readings of ``db_path``, each marked with the reading that holds it."""
    workdir = tempfile.mkdtemp(prefix="scauto_trace_")
    views = None
    try:
        views = sqlite_open.open_views(db_path, workdir)
        merged = _sql_hits(views.merged, needles)
        main = merged if views.main_only is views.merged else _sql_hits(views.main_only, needles)
    except Exception as error:                                     # noqa: BLE001 - one bad file
        logger.warning(f"--trace-ids: could not read {db_path} as a database ({error})")
        return []
    finally:
        if views is not None:
            views.close()
        shutil.rmtree(workdir, ignore_errors=True)
    out = []
    for key in sorted(merged | main, key=lambda k: tuple(str(x) for x in k)):
        reading = (sqlite_open.BOTH if key in merged and key in main
                   else sqlite_open.WAL_ONLY if key in merged else sqlite_open.MAIN_ONLY)
        table, col, label, index, form, case, kind = key
        out.append({"id": index, "form": form, "case": case, "kind": "sqlite", "table": table,
                    "column": col, "row": label, "cell": kind, "reading": reading})
    return out


# ------------------------------------------------------------------------------------ the walk

def _is_sqlite(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _files(root):
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            if name.endswith("-shm"):
                continue
            yield os.path.join(dirpath, name)


def extracted_root(run_folder):
    """The folder to search: ``<run>/ExtractedData`` when present, else the folder itself."""
    candidate = os.path.join(run_folder, "ExtractedData")
    return candidate if os.path.isdir(candidate) else run_folder


def trace(run_folder, identifiers, progress=None):
    """Search ``run_folder``'s extracted files for ``identifiers``; return the report as a dict."""
    progress = progress or logger.info
    root = extracted_root(run_folder)
    needles = [n for i, ident in enumerate(identifiers) for n in needles_for(i, ident)]
    started = time.monotonic()
    try:
        from scripts import memories_media_report as _mr
        manifest = _mr.load_path_manifest(root)

        def shown(path):
            return _mr.device_path(path, src_root=root, manifest=manifest)
    except Exception:                                              # noqa: BLE001 - display only
        def shown(path):
            return "/" + os.path.relpath(path, root).replace("\\", "/")

    hits, scanned_bytes, scanned_files, next_note = [], 0, 0, PROGRESS_EVERY
    databases, raw_hit = set(), set()          # SQLite files seen; databases the raw pass hit
    for path in _files(root):
        try:
            size = os.path.getsize(path)
            found = scan_file(path, needles)
        except OSError as error:
            logger.warning(f"--trace-ids: could not read {path} ({error})")
            continue
        scanned_files += 1
        scanned_bytes += size
        if scanned_bytes >= next_note:
            progress(f"--trace-ids: {scanned_files} files, {scanned_bytes / (1 << 30):.1f} GB read, "
                     f"{len(hits)} hit(s) so far")
            next_note = scanned_bytes + PROGRESS_EVERY
        rel = os.path.relpath(path, root).replace("\\", "/")
        is_wal = path.endswith("-wal")
        is_db = not is_wal and _is_sqlite(path)
        if is_db:
            databases.add(path)
        if found and (is_db or is_wal):
            raw_hit.add(path[:-4] if is_wal else path)
        layout = _wal_layout(path) if (is_wal and found) else (0, [])
        for needle, offset, case in found:
            hit = {"id": needle.index, "form": needle.form, "case": case, "file": rel,
                   "device_path": shown(path), "offset": offset}
            if is_wal and layout[0]:
                hit["kind"] = "wal"
                hit.update(_wal_place(offset, *layout))
            else:
                hit["kind"] = "sqlite-file" if is_db else "file"
            hits.append(hit)

    # The row-level pass: every database small enough, and any larger one the raw pass hit (in the
    # file or in its -wal).
    for path in sorted(databases):
        if path not in raw_hit and os.path.getsize(path) > SQL_PASS_MAX_BYTES:
            continue
        rel = os.path.relpath(path, root).replace("\\", "/")
        rows = scan_database(path, needles)
        for row in rows:
            row.update({"file": rel, "device_path": shown(path)})
        hits += rows
        # A raw hit in the database file that no row of either reading accounts for sits in a free
        # page, unallocated space or a deleted record: say so, rather than leave the examiner to
        # wonder why the file matches and the table does not.
        in_rows = {r["id"] for r in rows}
        for h in hits:
            if h["kind"] == "sqlite-file" and h["file"] == rel:
                h["in_rows"] = h["id"] in in_rows

    hits = _drop_suffix_echoes(hits)
    payload = {
        "tool": "Snapchat_Auto --trace-ids",
        "version": _version(),
        "created_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_folder": os.path.abspath(run_folder),
        "searched": root,
        "elapsed_s": round(time.monotonic() - started, 1),
        "files_scanned": scanned_files,
        "bytes_scanned": scanned_bytes,
        "ids": [{"index": i, "id": ident, "forms": [n.form for n in needles if n.index == i]}
                for i, ident in enumerate(identifiers)],
        "hits": hits,
    }
    return payload


def _drop_suffix_echoes(hits):
    """Drop the hits of a suffix-less form at a place the full identifier was found too.

    ``ABC~1`` stored in a cell is also an occurrence of ``ABC``; reporting both says the same thing
    twice. The suffix-less form is kept where it stands on its own, which is the point of searching it.
    """
    def place(h):
        return (h["id"], h["file"], h["kind"], h.get("offset"), h.get("table"), h.get("column"),
                h.get("row"))
    full = {place(h) for h in hits if "without the ~N suffix" not in h["form"]}
    return [h for h in hits if "without the ~N suffix" not in h["form"] or place(h) not in full]


def _version():
    try:
        from scripts.app_version import get_version
        return get_version()
    except Exception:                                              # noqa: BLE001
        return ""


#: The log lists at most this many row-level hits per identifier; the JSON always has all of them.
LOG_ROWS_PER_ID = 40
#: Offsets shown per group of raw hits in the log.
LOG_OFFSETS = 5


def describe(payload):
    """Human-readable lines for the log, grouped by identifier. Locations only.

    Row-level hits are listed one per line. Raw hits are grouped by file, kind and encoding, with a
    count and the first offsets — a database holds the same value in many pages, and a log that
    repeats one line per page hides the few lines that matter. The JSON keeps every hit.
    """
    lines = []
    for ident in payload["ids"]:
        mine = [h for h in payload["hits"] if h["id"] == ident["index"]]
        lines.append(f"{ident['id']}: {len(mine)} location(s)" + ("" if mine else " - not found"))
        rows = [h for h in mine if h["kind"] == "sqlite"]
        for h in rows[:LOG_ROWS_PER_ID]:
            case = f", {h['case']} case" if h.get("case") else ""
            lines.append(f"  {h.get('device_path') or h['file']} > {h['table']}.{h['column']} "
                         f"row {h['row']} [{h['cell']}; {h['form']}{case}; reading {h['reading']}]")
        if len(rows) > LOG_ROWS_PER_ID:
            lines.append(f"  ... and {len(rows) - LOG_ROWS_PER_ID} more row(s) - see the JSON")
        groups = {}
        for h in mine:
            if h["kind"] == "sqlite":
                continue
            if h["kind"] == "wal":
                state = ("SUPERSEDED frames (deleted prior state)" if h.get("superseded")
                         else "latest frames" if "frame" in h else h.get("where", ""))
            elif h["kind"] == "sqlite-file":
                state = ("database file" if h.get("in_rows", True) else
                         "database file, in no row either reading returns (free page, unallocated "
                         "space or a deleted record)")
            else:
                state = "file"
            key = (h.get("device_path") or h["file"], state, h["form"], h.get("case", ""))
            groups.setdefault(key, []).append(h["offset"])
        for (where, state, form, case), offsets in groups.items():
            shown = ", ".join(str(o) for o in offsets[:LOG_OFFSETS])
            more = f", ... ({len(offsets)} in all)" if len(offsets) > LOG_OFFSETS else ""
            case = f", {case} case" if case else ""
            lines.append(f"  {where} - {state} @ {shown}{more} [{form}{case}]")
    return lines


def write_report(run_folder, payload):
    """Write ``trace_ids_<stamp>.json`` into the run folder; return its path."""
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(run_folder, f"trace_ids_{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return path


def read_identifiers(args):
    """The identifiers from the command line: each argument, or ``@file`` for one per line."""
    out = []
    for arg in args:
        if arg.startswith("@"):
            with open(arg[1:], encoding="utf-8-sig") as fh:
                out += [line.strip() for line in fh if line.strip() and not line.startswith("#")]
        elif arg.strip():
            out.append(arg.strip())
    return out
