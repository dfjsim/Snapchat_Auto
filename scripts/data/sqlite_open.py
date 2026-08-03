"""Read a SQLite database **twice**: with its ``-wal`` applied, and without.

A write-ahead log holds committed pages that have not yet been checkpointed into the main
database file. Which of the two files a row lives in is evidence:

* opening the main file **with** the ``-wal`` gives the app's current state — this is what every
  SQLite client does by default, and what this project used to do exclusively;
* opening the main file **without** it gives the state as of the last checkpoint. Rows visible
  only there were updated or deleted afterwards, so they are recoverable prior state that the
  merged view can never show.

Both views are produced from **copies** staged in the working directory. Copying rather than
opening the evidence in place means SQLite never creates a ``-shm`` beside the original and never
checkpoints it — a read-only open can still do both. :func:`scripts.memories_media_report.decrypt_gallery_db`
already stages files this way for the SQLCipher path; this module generalises it.

Typical use::

    views = open_views(db_path, workdir)
    rows, markers = read_table(views, "CACHE_FILE_CLAIM")
    ...
    views.close()

``rows`` is the union of both views; ``markers[i]`` says which view row ``i`` came from —
:data:`BOTH`, :data:`WAL_ONLY` or :data:`MAIN_ONLY`.
"""

import os
import shutil
import sqlite3
import logging
import tempfile

logger = logging.getLogger(__name__)

# Where a row was seen. BOTH is the ordinary case and needs no badge in a report; the others are
# what make reading twice worth doing.
BOTH = "main+wal"
WAL_ONLY = "wal-only"
MAIN_ONLY = "main-only"
# Not produced by read_table: reserved for data recovered from a -wal frame that a LATER frame has
# already superseded, which neither of the two readings above can reach (a reading only ever sees
# the newest frame for a page). Such data is carved, not queried — it never went through SQLite's
# record parser — so anything marked with it must be corroborated independently before use.
CARVED = "wal-carved"

# Explanations the reports show behind a "?" so an examiner can evaluate the row without knowing
# SQLite internals (docs/forensics_tool_guidelines.md: explain how every association was made).
MARKER_HELP = {
    BOTH: ("This row is present both in the database file itself and after its write-ahead log "
           "(-wal) is applied, so the two readings agree."),
    WAL_ONLY: ("This row exists only once the write-ahead log (-wal) is applied — it was written "
               "recently and has not yet been checkpointed into the database file. It is part of "
               "the app's current state; a tool that ignores the -wal would not show it."),
    MAIN_ONLY: ("This row exists only in the database file WITHOUT its write-ahead log (-wal). "
                "The -wal contains a newer version of this data, so the row was changed or "
                "deleted after the last checkpoint. It is recoverable prior state, NOT the app's "
                "current state — do not report it as a live record."),
    CARVED: ("This data was NOT read from the database by SQLite. It was carved out of a "
             "write-ahead log (-wal) frame that a later frame in the same log has already "
             "superseded — a page image neither reading of the database can reach, because both "
             "only ever see the newest frame for a page. It is deleted prior state, recovered "
             "from free space. Treat it as a lead that must be corroborated: whatever is reported "
             "from it has to be verified independently (here, a carved key is kept only when it "
             "actually decrypts the file it is claimed to belong to)."),
}


# --------------------------------------------------------------------------- WAL frame carving

_WAL_HEADER_BYTES = 32
_WAL_FRAME_HEADER_BYTES = 24


def wal_page_images(db_path):
    """Yield ``(frame_index, page_number, page_bytes)`` for **every** frame of ``db_path``'s -wal.

    :func:`open_views` gives the two states SQLite itself can produce — the checkpointed file, and
    the file with the log applied. Neither reaches a page image that a later frame in the same log
    has replaced, because applying the log keeps only the newest frame per page. Those superseded
    images are where rows deleted mid-log survive, so this yields frames in file order and leaves
    it to the caller to decide what is stale.

    The source file is only ever read; nothing is written, checkpointed or recovered.
    """
    wal = db_path + "-wal"
    try:
        size = os.path.getsize(wal)
    except OSError:
        return
    if size <= _WAL_HEADER_BYTES:
        return
    with open(wal, "rb") as fh:
        header = fh.read(_WAL_HEADER_BYTES)
        magic = int.from_bytes(header[0:4], "big")
        if magic not in (0x377F0682, 0x377F0683):          # little/big-endian checksum variants
            logger.debug(f"{wal}: not a WAL header ({magic:#x})")
            return
        page_size = int.from_bytes(header[8:12], "big")
        if page_size == 1:                                 # the documented encoding for 65536
            page_size = 65536
        if page_size < 512 or page_size & (page_size - 1):
            logger.debug(f"{wal}: implausible page size {page_size}")
            return
        index = 0
        while True:
            frame_header = fh.read(_WAL_FRAME_HEADER_BYTES)
            if len(frame_header) < _WAL_FRAME_HEADER_BYTES:
                return
            page = fh.read(page_size)
            if len(page) < page_size:
                return
            yield index, int.from_bytes(frame_header[0:4], "big"), page
            index += 1


def superseded_wal_pages(db_path):
    """The -wal page images that are stale: every frame for a page except the last one.

    These are exactly the images `open_views` cannot show. Returns ``[(frame_index, page_no,
    bytes)]`` in file order.
    """
    frames = list(wal_page_images(db_path))
    last_for_page = {}
    for i, (idx, page_no, _data) in enumerate(frames):
        last_for_page[page_no] = i
    return [(idx, page_no, data) for i, (idx, page_no, data) in enumerate(frames)
            if last_for_page.get(page_no) != i]

_SIDECARS = ("-wal", "-shm")


class Views:
    """The two readings of one database, plus what was found on disk.

    ``merged`` is the database with its ``-wal`` applied (the app's current state) and is the
    connection callers should use when they only want one. ``main_only`` is the last checkpointed
    state, or ``None`` when it could not be opened — a database whose schema lives entirely in the
    ``-wal`` has nothing readable in the main file, which is a real shape (an empty ``NSURLCache``
    ``Cache.db`` looks exactly like this), not an error.
    """

    def __init__(self, path, merged, main_only, info, tmpdir=None):
        self.path = path
        self.merged = merged
        self.main_only = main_only
        self.info = info
        self._tmpdir = tmpdir

    @property
    def has_wal(self):
        return bool(self.info.get("wal_bytes"))

    def close(self):
        for conn in (self.merged, self.main_only):
            try:
                if conn is not None:
                    conn.close()
            except sqlite3.Error:
                pass
        self.merged = self.main_only = None
        if self._tmpdir and os.path.isdir(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _stage(src, dest_dir, sidecars):
    """Copy ``src`` (and optionally its -wal/-shm) into ``dest_dir``; return the copied path."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    shutil.copyfile(src, dest)
    if sidecars:
        for suffix in _SIDECARS:
            sidecar = src + suffix
            if os.path.exists(sidecar):
                try:
                    shutil.copyfile(sidecar, dest + suffix)
                except OSError as error:
                    logger.debug(f"Could not stage {sidecar}: {error}")
    return dest


def _open_ro(path):
    """Open a staged copy read-only, or None if it is not a usable database."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()   # force a real read
        return conn
    except sqlite3.DatabaseError as error:
        logger.debug(f"Could not open {path} read-only: {error}")
        return None


def open_views(db_path, workdir=None):
    """Open ``db_path`` twice — with and without its ``-wal``. Returns a :class:`Views`.

    ``workdir`` is where the copies are staged; a temporary directory is used (and removed by
    :meth:`Views.close`) when it is not given. The evidence file is only ever read.
    """
    info = {
        "path": db_path,
        "db_bytes": _size(db_path),
        "wal_bytes": _size(db_path + "-wal"),
        "shm_bytes": _size(db_path + "-shm"),
        "main_only_ok": False,
        "differs": None,                    # filled in lazily by read_table
        "note": "",
    }
    owned_tmp = None
    if workdir:
        base = os.path.join(workdir, "sqlite_views", _safe_name(db_path))
    else:
        owned_tmp = tempfile.mkdtemp(prefix="scauto_sqlite_")
        base = owned_tmp

    merged = main_only = None
    try:
        merged = _open_ro(_stage(db_path, os.path.join(base, "withwal"), sidecars=True))
    except OSError as error:
        logger.warning(f"Could not stage {db_path} for reading: {error}")

    if not info["wal_bytes"]:
        # No -wal: the two readings are identical by construction, so don't stage a second copy.
        info["note"] = "no -wal alongside this database; both readings are identical"
        info["main_only_ok"] = True
        info["differs"] = False
        return Views(db_path, merged, merged, info, owned_tmp)

    try:
        main_only = _open_ro(_stage(db_path, os.path.join(base, "nowal"), sidecars=False))
    except OSError as error:
        logger.debug(f"Could not stage the WAL-less copy of {db_path}: {error}")
    info["main_only_ok"] = main_only is not None
    if main_only is None:
        info["note"] = ("the database file on its own could not be opened (its schema or content "
                        "lives entirely in the -wal); only the -wal-applied reading is available")
    return Views(db_path, merged, main_only, info, owned_tmp)


def _safe_name(path):
    """A short, filesystem-safe folder name for one staged database."""
    base = os.path.basename(path) or "db"
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in base)[:64]


def _columns(conn, table):
    try:
        cur = conn.execute(f'SELECT * FROM "{table}" LIMIT 0')
        return [d[0] for d in cur.description]
    except sqlite3.DatabaseError:
        return []


def _pk_columns(conn, table):
    """Declared primary-key columns of ``table``, in key order, or [] when it has none."""
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.DatabaseError:
        return []
    keyed = [(r[5], r[1]) for r in rows if r[5]]            # (pk position, column name)
    return [name for _pos, name in sorted(keyed)]


def _fetch(conn, table):
    """``(rows as dicts, column names)`` for a whole table, or ``([], [])``."""
    if conn is None:
        return [], []
    try:
        cur = conn.execute(f'SELECT * FROM "{table}"')
    except sqlite3.DatabaseError as error:
        logger.debug(f"{table} not readable: {error}")
        return [], []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()], cols


def _key_of(row, pk_cols, cols):
    """A hashable identity for a row: its primary key, else the whole row."""
    if pk_cols:
        return tuple(_hashable(row.get(c)) for c in pk_cols)
    return tuple(_hashable(row.get(c)) for c in cols)


def _hashable(value):
    return bytes(value) if isinstance(value, (bytearray, memoryview)) else value


def read_table(views, table):
    """Read ``table`` from both views. Returns ``(rows, markers)``.

    ``rows`` is the merged reading first (the app's current state, in its own order), followed by
    any rows that exist **only** in the main file. ``markers`` is a parallel list of :data:`BOTH` /
    :data:`WAL_ONLY` / :data:`MAIN_ONLY`.

    Rows are matched on the table's declared primary key, falling back to the whole row when there
    is none. A row whose key is in both views but whose *values* differ is reported twice — once
    from each view — because which value was current at which point is exactly what the examiner
    needs to see.
    """
    merged_rows, cols = _fetch(views.merged, table)
    if views.main_only is None or views.main_only is views.merged:
        # Nothing to compare against: either there is no -wal (identical by construction) or the
        # WAL-less copy would not open. Either way every row is simply "as read".
        return merged_rows, [BOTH] * len(merged_rows)

    main_rows, main_cols = _fetch(views.main_only, table)
    cols = cols or main_cols
    if not cols:
        return merged_rows, [BOTH] * len(merged_rows)

    pk_cols = [c for c in _pk_columns(views.merged, table) if c in cols]

    main_by_key = {}
    for row in main_rows:
        main_by_key.setdefault(_key_of(row, pk_cols, cols), []).append(row)

    rows, markers, consumed = [], [], set()
    for row in merged_rows:
        key = _key_of(row, pk_cols, cols)
        counterparts = main_by_key.get(key)
        if not counterparts:
            # nothing checkpointed under this key — the row exists only once the -wal is applied
            rows.append(row)
            markers.append(WAL_ONLY)
            continue
        # An identical checkpointed row means the two readings agree. Otherwise this version of
        # the row exists only with the -wal applied, and the checkpointed version is prior state.
        rows.append(row)
        markers.append(BOTH if any(_same(row, c, cols) for c in counterparts) else WAL_ONLY)
        if key in consumed:
            continue                                       # its counterparts are already emitted
        consumed.add(key)
        for other in counterparts:
            if not _same(row, other, cols):
                rows.append(other)
                markers.append(MAIN_ONLY)

    for key, counterparts in main_by_key.items():
        if key in consumed:
            continue
        for row in counterparts:
            rows.append(row)
            markers.append(MAIN_ONLY)

    differs = any(m != BOTH for m in markers)
    views.info["differs"] = bool(views.info.get("differs")) or differs
    return rows, markers


def _same(a, b, cols):
    return all(_hashable(a.get(c)) == _hashable(b.get(c)) for c in cols)


def read_sql(db_path, query, workdir=None, marker_col="_wal", params=None):
    """Run ``query`` against both readings and return a DataFrame with a ``_wal`` marker column.

    This is the drop-in replacement for ``pd.read_sql_query(query, sqlite3.connect(db))``. The
    query may join and filter freely; rows are matched between the two readings on the **whole
    result row**, since an arbitrary query has no primary key to lean on. Rows present only in the
    checkpointed reading are appended and marked :data:`MAIN_ONLY`.

    Returns ``(DataFrame, info)``. On any failure the DataFrame is empty rather than raising, so a
    missing or unreadable table never aborts a report.
    """
    import pandas as pd                                        # local: keep this module importable

    views = open_views(db_path, workdir)
    try:
        try:
            cur = views.merged.execute(query, params or [])
            cols = [d[0] for d in cur.description]
            merged = cur.fetchall()
        except (sqlite3.DatabaseError, AttributeError) as error:
            logger.debug(f"query failed on {db_path}: {error}")
            return pd.DataFrame(), dict(views.info)

        rows = list(merged)
        markers = [BOTH] * len(merged)
        if views.main_only is not None and views.main_only is not views.merged:
            try:
                main = views.main_only.execute(query, params or []).fetchall()
            except sqlite3.DatabaseError as error:
                logger.debug(f"query failed on the WAL-less copy of {db_path}: {error}")
                main = []
            seen = {tuple(_hashable(v) for v in row) for row in merged}
            for row in main:
                if tuple(_hashable(v) for v in row) not in seen:
                    rows.append(row)
                    markers.append(MAIN_ONLY)
            views.info["differs"] = bool(views.info.get("differs")) or len(rows) != len(merged)

        df = pd.DataFrame(rows, columns=cols)
        # assign as a whole column: per-cell assignment is what breaks under pandas 3's strict
        # dtype rules (see docs/pandas3_python314_compat.md)
        df[marker_col] = markers
        return df, dict(views.info)
    finally:
        views.close()


def query_both(views, query, params=None):
    """Run ``query`` against both readings; return ``(rows, markers)`` as plain tuples.

    The raw-tuple counterpart of :func:`read_sql`, for callers that do not want pandas. Rows are
    matched on the whole result row, since an arbitrary query has no primary key.
    """
    def run(conn):
        if conn is None:
            return []
        try:
            return conn.execute(query, params or []).fetchall()
        except sqlite3.DatabaseError as error:
            logger.debug(f"query failed: {error}")
            return []

    merged = run(views.merged)
    rows, markers = list(merged), [BOTH] * len(merged)
    if views.main_only is not None and views.main_only is not views.merged:
        seen = {tuple(_hashable(v) for v in row) for row in merged}
        for row in run(views.main_only):
            if tuple(_hashable(v) for v in row) not in seen:
                rows.append(row)
                markers.append(MAIN_ONLY)
        if len(rows) != len(merged):
            views.info["differs"] = True
    return rows, markers


def table_columns(db_path, table, workdir=None):
    """The column names of ``table``, as a set — for queries built from a version-varying schema.

    Read from the -wal-applied view, which is the app's current schema. A column that exists only
    in the checkpointed view would be one the app has since dropped, which is a schema migration,
    not evidence.
    """
    views = open_views(db_path, workdir)
    try:
        return set(_columns(views.merged, table))
    finally:
        views.close()


def read_all(db_path, table, workdir=None):
    """Convenience: open ``db_path``, read one table both ways, close. ``(rows, markers, info)``."""
    views = open_views(db_path, workdir)
    try:
        rows, markers = read_table(views, table)
        return rows, markers, dict(views.info)
    finally:
        views.close()


def describe(info):
    """One line about a database's WAL state, for a report's source block."""
    if not info:
        return ""
    bits = [f"{info.get('db_bytes', 0)} bytes"]
    if info.get("wal_bytes"):
        bits.append(f"-wal {info['wal_bytes']} bytes")
        if info.get("shm_bytes"):
            bits.append(f"-shm {info['shm_bytes']} bytes")
        if info.get("differs") is True:
            bits.append("the two readings DIFFER")
        elif info.get("differs") is False:
            bits.append("both readings agree")
    else:
        bits.append("no -wal")
    if info.get("note"):
        bits.append(info["note"])
    return "; ".join(bits)
