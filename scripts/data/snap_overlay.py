"""A Memory's overlay record — ``ZGALLERYSNAPDETAIL.ZOVERLAY`` in scdb-27 — and the asset URLs of the
geofilters it lists.

``scdb-27.sqlite3`` keeps at most one ``ZGALLERYSNAPDETAIL`` row per Memory: ``ZSNAP`` is the Memory's
``ZGALLERYSNAP.Z_PK`` (``ZGALLERYSNAP.ZDETAIL`` points back), and ``ZOVERLAY`` is an NSKeyedArchiver
archive whose root is a ``SOJUGallerySnapOverlay``. It is in the plain database on both storage
schemas — no keychain is needed, and ``gallery.encrypteddb`` holds nothing like it. The root's
``filters`` (``SOJUGalleryFilters``) holds lists of filters, each list beside a field of its own that
names the selected one; for the geofilters that is ``geoFilters`` (``SOJUGalleryGeoFilter``) and
``geoFilterSelectedId`` / ``geoFilterSelectedIds``.

Three fields of a geofilter hold a URL (:data:`ASSET_FIELDS`, relative to one ``geoFilters[i]``): its
image (``imageUrl``), its sky image (``arSegmentation.sky.replacementSkyUrl``) and the font of its
text (``geofilterMarkups[j].displayParameters.font``). A ``cache_controller.db`` claim whose
EXTERNAL_KEY is one of those URLs — compared by :func:`normalise_url` on both sides, so the whole URL,
query included — is a cached asset of a filter the record lists. That is all the match says. A record
commonly lists several filters and names the selected one separately (often none), so a listed filter
is not shown to be on the Memory, and such a file is never the Memory's media.

A geofilter whose ``imageUrlParams`` dictionary has entries (the Bitmoji filters) gives one shared
address as its ``imageUrl`` and the image in those parameters: that URL names no image of its own, so
it is never an asset. The class and field names are the archive's own; a record of another layout gives no asset,
never a guess. Both the cache_controller report and the Memories report read the record through this
module, so the two ends of a link make the same match.
"""

import logging
import sqlite3

from scripts.data import keyed_archive
from scripts.data import sqlite_open

logger = logging.getLogger(__name__)

ROOT_CLASS = "SOJUGallerySnapOverlay"
GEOFILTER_CLASS = "SOJUGalleryGeoFilter"
CLASS = keyed_archive.CLASS

#: The fields of one ``filters.geoFilters[i]`` that hold an asset's URL, and what that asset is to the
#: filter. ``[]`` steps into each item of a list.
ASSET_FIELDS = (
    (("imageUrl",), "filter image"),
    (("arSegmentation", "sky", "replacementSkyUrl"), "sky image"),
    (("arSegmentation", "sky", "blimpUrl"), "sky item blimpUrl"),
    (("geofilterMarkups", "[]", "displayParameters", "font"), "font of the filter text"),
)

#: What the record says about one asset's filter being the selected one.
SELECTED_TEXT = {True: "yes — the record names this filter",
                 False: "no — the record names another filter",
                 None: "not recorded — the record names no selected geofilter"}

_QUERY = ("SELECT s.ZSNAPID, d.ZOVERLAY, {flag} FROM ZGALLERYSNAPDETAIL d "
          "JOIN ZGALLERYSNAP s ON s.Z_PK = d.ZSNAP")


def normalise_url(url):
    """The form an asset URL is matched in: one rule, for a record's URLs and the claim keys they are
    matched against alike. ``""`` for anything that is not an ``http`` / ``https`` URL.

    * the scheme is lower-cased, as URL schemes are case-insensitive; nothing else changes case — not
      the host, not the path, not the query;
    * one trailing ``?`` or ``#`` is dropped when it opens an empty query or an empty fragment — the
      first ``?`` of a text with no ``#``, the first ``#`` — and only one: some claim keys are the
      record's URL with an empty query added. A ``?`` or ``#`` that ends a query or a fragment with
      something in it is part of that query or fragment, and stays;
    * nothing is unquoted or trimmed: the record and the claim keys both store the base64 padding of a
      query value as ``%3D``, and a text that spells it ``=`` is a different text.
    """
    if not isinstance(url, str):
        return ""
    scheme, sep, rest = url.partition("://")
    if not sep or scheme.lower() not in ("http", "https"):
        return ""
    if rest.endswith("#") and rest.find("#") == len(rest) - 1:
        rest = rest[:-1]                     # a "#" that opens an empty fragment
    elif rest.endswith("?") and rest.find("?") == len(rest) - 1 and "#" not in rest:
        rest = rest[:-1]                     # a "?" that opens an empty query (not one in a fragment)
    return f"{scheme.lower()}://{rest}" if rest else ""


def unarchive(blob):
    """A ``ZOVERLAY`` archive as a plain tree (:func:`keyed_archive.unarchive`), or None when it is
    not an archive of a ``SOJUGallerySnapOverlay``."""
    return keyed_archive.unarchive(blob, ROOT_CLASS)


def _geofilters(record):
    """``(filters, [(position, geofilter)])`` of a record; empty when it has no such list."""
    filters = record.get("filters") if isinstance(record, dict) else None
    if not isinstance(filters, dict):
        return {}, []
    listed = filters.get("geoFilters")
    if not isinstance(listed, list):
        return filters, []
    return filters, [(i, g) for i, g in enumerate(listed)
                     if keyed_archive.class_name(g) == GEOFILTER_CLASS]


def _text(value):
    """An id or name as text: a string as stored, an integer in digits, anything else ""."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def selected_ids(record):
    """The geofilter ids the record names as selected (``geoFilterSelectedId`` and
    ``geoFilterSelectedIds``), in order, empties dropped."""
    filters, _listed = _geofilters(record)
    named = [filters.get("geoFilterSelectedId")]
    many = filters.get("geoFilterSelectedIds")
    named += many if isinstance(many, list) else []
    return list(dict.fromkeys(t for t in (_text(v) for v in named) if t))


def _filter_facts(g):
    group = g.get("carouselGroup")
    return {"filter_id": _text(g.get("idValue")), "filter_type": _text(g.get("type")),
            "group": _text(group.get("groupName")) if isinstance(group, dict) else "",
            "content": _text(g.get("unlockableContentType"))}


def _at(node, path):
    """``(sub-path, value)`` for every value at ``path`` below ``node``."""
    if not path:
        yield "", node
        return
    step, rest = path[0], path[1:]
    if step == "[]":
        for j, item in enumerate(node if isinstance(node, list) else ()):
            for sub, value in _at(item, rest):
                yield f"[{j}]{sub}", value
    elif isinstance(node, dict) and step != CLASS and step in node:
        for sub, value in _at(node[step], rest):
            yield f".{step}{sub}", value


def filter_assets(record):
    """Every asset URL the record's geofilters hold, in record order.

    Each asset: ``url`` (as stored), ``key`` (:func:`normalise_url`), ``role`` (from
    :data:`ASSET_FIELDS`), ``field`` (its path in the record, ``filters.geoFilters[i]…``), the filter's
    ``filter_id`` (idValue), ``filter_type``, ``group`` (carousel group name) and ``content``
    (unlockableContentType), and ``selected``: True when the record names this filter as the
    selected one, False when it names another, None when it names none.
    """
    _filters, listed = _geofilters(record)
    chosen = set(selected_ids(record))
    out = []
    for i, g in listed:
        facts = _filter_facts(g)
        params = g.get("imageUrlParams")
        shared_address = isinstance(params, dict) and any(k != CLASS for k in params)
        for path, role in ASSET_FIELDS:
            if shared_address and path == ("imageUrl",):
                continue                     # the parameters say which image, not the URL
            for sub, url in _at(g, path):
                key = normalise_url(url)
                if not key:
                    continue
                out.append(dict(facts, url=url, key=key, role=role,
                                field=f"filters.geoFilters[{i}]{sub}",
                                selected=(None if not chosen else facts["filter_id"] in chosen)))
    return out


def summary(record):
    """What the Memory page says about a record: how many geofilters it lists, the ones it names as
    selected (each with its type and group when the record lists it), and its assets."""
    _filters, listed = _geofilters(record)
    facts = [_filter_facts(g) for _i, g in listed]
    selected = []
    for ident in selected_ids(record):
        known = next((f for f in facts if f["filter_id"] == ident), None)
        selected.append(dict(known) if known else {"filter_id": ident, "filter_type": "",
                                                   "group": "", "content": ""})
    return {"geofilters": len(listed), "selected": selected, "assets": filter_assets(record)}


def _has_column(views, table, column):
    """Whether the app's current schema (the -wal-applied reading) has ``table.column``."""
    if views.merged is None:
        return False
    try:
        return any(row[1] == column
                   for row in views.merged.execute(f'PRAGMA table_info("{table}")').fetchall())
    except sqlite3.DatabaseError:
        return False


def read_overlays(views):
    """Every overlay record of one scdb-27 (``sqlite_open`` views), joined to its Memory's ZSNAPID.

    The join is made inside each reading, so a record only the checkpointed reading holds is joined to
    the snap row of that same reading. Returns ``[{"snap_id", "wal", "has_overlay_image",
    "geofilters", "selected", "assets"}]`` — :func:`summary`, plus ``ZGALLERYSNAP.ZHASOVERLAYIMAGE``
    as stored (None when the schema has no such column) and the reading the record came from: the
    current reading first, a version only the checkpointed one holds after it, its assets merged in
    where they differ (each asset carries its own ``wal``). A missing table gives ``[]``.
    """
    flag = "s.ZHASOVERLAYIMAGE" if _has_column(views, "ZGALLERYSNAP", "ZHASOVERLAYIMAGE") else "NULL"
    rows, markers = sqlite_open.query_both(views, _QUERY.format(flag=flag))
    out, by_snap = [], {}
    for (sid, blob, has_image), mark in zip(rows, markers):
        if not sid or blob is None:
            continue
        record = unarchive(blob)
        if record is None:
            logger.debug(f"ZGALLERYSNAPDETAIL.ZOVERLAY of snap row {sid}: not a {ROOT_CLASS} archive")
            continue
        found = summary(record)
        rec = by_snap.get(str(sid))
        if rec is None:
            rec = by_snap[str(sid)] = dict(
                found, snap_id=str(sid), wal=mark, assets=[],
                has_overlay_image=(has_image if isinstance(has_image, int)
                                   and not isinstance(has_image, bool) else None))
            out.append(rec)
        have = {(a["field"], a["key"]) for a in rec["assets"]}
        rec["assets"] += [dict(a, wal=mark) for a in found["assets"]
                          if (a["field"], a["key"]) not in have]
    return out
