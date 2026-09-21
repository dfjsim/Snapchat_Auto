"""
The app's own search index over Memories: ``Documents/gallery_search/<n>/<userHash>/search.sqlite3``.

Plain SQLite (FTS4), **no keychain involved** — which is what makes it worth reading: on a device
whose keychain dump is backup-class (no ``egocipher``), the coordinates in ``gallery.encrypteddb``
stay locked, and this index is then the only record of *where* a Memory was taken that the tool
can give. Everything in it is **app-generated and reported as stored**: the place names are the
app's own reverse geocoding (down to street and postal code on the tested devices), the date is a
local calendar date with no zone, the time words (``afternoon``, ``winter``) are the app's
classification, and the visual tags are the app's labels with the confidence it stored. None of it
is an observation about the media file.

What the file holds, per snap id (``snap_id_table.snap_id``, a Memory's ``ZSNAPID``):

* ``snap_tag_table_content`` — FTS4 content, joined on ``docid = snap_id_table.rowid``: the
  time tags, location tags, visual tags and meta tag (``Image`` / ``Video``), comma-separated;
* ``snap_description_table_content`` — the caption text, same join;
* ``snap_time_tag_table`` — the date string; ``snap_location_tag_cluster_table`` and
  ``snap_visual_tag_cluster_table`` — the cluster names; ``snap_visual_tag_conf_table`` — one row
  per (snap, concept) with its confidence;
* ``snap_tag_synced`` / ``snap_geofilter_id_table`` — present on some app versions only.

The ``docid = rowid`` join is what iLEAPP established and verified (Alexis Brignoni,
``scripts/artifacts/snapchat.py``, ``snapchatGallerySearch``, MIT); the table set was seen to vary
between app versions, so every table is optional here. The database is read twice, with and
without its ``-wal`` (:mod:`scripts.data.sqlite_open`): on two tested devices a third of the rows
existed only with the log applied.
"""

import os
import glob
import logging
import sqlite3

from scripts.data import sqlite_open

logger = logging.getLogger(__name__)

SEARCH_INDEX_BASIS = (
    "From the app's own search index over Memories (Documents/gallery_search/…/search.sqlite3), a "
    "plain SQLite database that needs no keychain. Every value is app-generated and shown as "
    "stored: the place names are the app's reverse geocoding of the Memory's location, the date "
    "is a local calendar date with no time zone, the time words are the app's classification, "
    "and the visual tags are the app's labels with the confidence it recorded. None of it is an "
    "observation about the media file, and a Memory the app has not indexed has no row here. "
    "Read with and without the database's -wal, and marked when only one reading holds it. "
    "Join and table layout after iLEAPP (Alexis Brignoni), scripts/artifacts/snapchat.py.")


def _split(text):
    """A comma-separated FTS field as a list, blanks dropped."""
    if text in (None, ""):
        return []
    return [part.strip() for part in str(text).split(",") if part and part.strip()]


def _tables(conn):
    try:
        return {r[0] for r in conn.execute("select name from sqlite_master where type = 'table'")}
    except sqlite3.DatabaseError:
        return set()


def find(app, user_hash):
    """The profile's ``search.sqlite3``, or ``""``."""
    hits = glob.glob(os.path.join(app, "Documents", "gallery_search", "*", user_hash,
                                  "search.sqlite3"))
    return hits[0] if hits else ""


def load(search_db, workdir=None):
    """``{snap_id: record}`` for every snap the index knows, or ``{}``.

    A record is ``{snap_id, date, time_words, places, place_cluster, visual_tags, concepts,
    visual_cluster, kind, caption, language, tag_version, wal}``; ``concepts`` is
    ``[(label, confidence)]`` by falling confidence, ``wal`` the row's :mod:`sqlite_open` marker
    (a snap whose rows disagree between the two readings is marked by its ``snap_id_table`` row).
    Never raises: an unreadable or empty index is a normal outcome.
    """
    out = {}
    if not (search_db and os.path.isfile(search_db)):
        return out
    try:
        views = sqlite_open.open_views(search_db, workdir)
    except sqlite3.DatabaseError as error:
        logger.debug(f"Could not open {search_db}: {error}")
        return out
    try:
        tables = _tables(views.merged)
        if "snap_id_table" not in tables:
            logger.info(f"Memories search index: {os.path.basename(search_db)} has no "
                        "snap_id_table; nothing read")
            return out

        def both(query):
            rows, marks = sqlite_open.query_both(views, query)
            return list(zip(rows, marks))

        # query_both marks only the rows the -wal has removed; a snap the -wal ADDED is just as
        # much a finding (it was indexed after the last checkpoint), so the id rows are read from
        # each view and compared here.
        id_query = "select rowid, snap_id, language_id, tag_version from snap_id_table"
        merged_ids = [r for r in both(id_query)]
        main_ids = set()
        if views.main_only is not None and views.main_only is not views.merged:
            try:
                main_ids = {str(r[1]).upper() for r in views.main_only.execute(id_query)
                            if r[1]}
            except sqlite3.DatabaseError:
                main_ids = set()
            has_main = True
        else:
            has_main = False
        for (rowid, snap_id, language, version), mark in merged_ids:
            if not snap_id:
                continue
            key = str(snap_id).upper()
            if mark == sqlite_open.BOTH and has_main and key not in main_ids:
                mark = sqlite_open.WAL_ONLY
            rec = out.setdefault(key, {
                "snap_id": str(snap_id), "date": "", "time_words": [], "places": [],
                "place_cluster": "", "visual_tags": [], "concepts": [], "visual_cluster": "",
                "kind": "", "caption": "", "language": "", "tag_version": None, "wal": mark,
                "_docids": set()})
            rec["language"] = rec["language"] or (language or "")
            rec["tag_version"] = rec["tag_version"] if rec["tag_version"] is not None else version
            rec["_docids"].add(rowid)
            if mark != sqlite_open.BOTH:
                rec["wal"] = mark
        by_docid = {}
        for rec in out.values():
            for docid in rec["_docids"]:
                by_docid[docid] = rec

        if "snap_tag_table_content" in tables:
            for (docid, t, loc, vis, meta), _mark in both(
                    "select docid, c0time_tag, c1location_tag, c2visual_tag, c3meta_tag "
                    "from snap_tag_table_content"):
                rec = by_docid.get(docid)
                if rec is None:
                    continue
                rec["time_words"] = rec["time_words"] or _split(t)
                rec["places"] = rec["places"] or _split(loc)
                rec["visual_tags"] = rec["visual_tags"] or _split(vis)
                rec["kind"] = rec["kind"] or (meta or "")
        if "snap_description_table_content" in tables:
            for (docid, caption), _mark in both(
                    "select docid, c0caption from snap_description_table_content"):
                rec = by_docid.get(docid)
                if rec is not None and caption and not rec["caption"]:
                    rec["caption"] = str(caption)
        for table, column, field in (("snap_time_tag_table", "time_tag", "date"),
                                     ("snap_location_tag_cluster_table", "cluster_name",
                                      "place_cluster"),
                                     ("snap_visual_tag_cluster_table", "cluster_name",
                                      "visual_cluster")):
            if table not in tables:
                continue
            for (snap_id, value), _mark in both(f"select snap_id, {column} from {table}"):
                rec = out.get(str(snap_id or "").upper())
                if rec is not None and value and not rec[field]:
                    rec[field] = str(value)
        if "snap_visual_tag_conf_table" in tables:
            for (snap_id, concept, conf), _mark in both(
                    "select snap_id, concept, conf from snap_visual_tag_conf_table"):
                rec = out.get(str(snap_id or "").upper())
                if rec is not None and concept:
                    pair = (str(concept), float(conf) if isinstance(conf, (int, float)) else None)
                    if pair not in rec["concepts"]:
                        rec["concepts"].append(pair)
        for rec in out.values():
            rec["concepts"].sort(key=lambda c: -(c[1] if c[1] is not None else -1))
            del rec["_docids"]
    except sqlite3.DatabaseError as error:
        logger.info(f"Memories search index: could not read {search_db} ({error})")
    finally:
        views.close()
    return out


def summary(rec):
    """One line for a Memory's search-index record, for the index page and the log."""
    bits = []
    if rec.get("date"):
        bits.append(rec["date"] + (f" ({', '.join(rec['time_words'])})" if rec.get("time_words")
                                   else ""))
    if rec.get("place_cluster") or rec.get("places"):
        bits.append(rec.get("place_cluster") or rec["places"][0])
    if rec.get("kind"):
        bits.append(rec["kind"])
    return " · ".join(bits)
