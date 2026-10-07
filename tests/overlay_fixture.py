"""Synthetic overlay records (scdb-27 ``ZGALLERYSNAPDETAIL.ZOVERLAY``) for the filter-asset tests.

:func:`archive` writes an NSKeyedArchiver binary plist the way the archiver lays one out — a flat
``$objects`` table, ``UID`` references, one ``$classname`` entry per class — from plain values:
:class:`Obj` for an object of a named class, a ``dict`` for an NSDictionary, a ``list`` for an
NSArray. :func:`overlay` builds a ``SOJUGallerySnapOverlay`` around a list of geofilters, and
:func:`scdb` an app folder whose scdb-27 joins those records to their snaps. Every value is invented:
example.net hosts, made-up ids.
"""
import hashlib
import os
import plistlib
import sqlite3

USER = "11111111-2222-4333-8444-555555555555"   # the account whose Memories and claims these are
HASH = hashlib.sha256(USER.encode()).hexdigest()  # its userHash: the profile folder's name
OTHER_USER = "22222222-3333-4444-8555-666666666666"   # a second account of the device

IMAGE_URL = "https://cf-st.example.net/d/AAAAAAAAAAAA?mo=QUJD%3D&uc=1"
SKY_URL = "https://geofilter.example.net/png/0a0a0a0a-1111-4222-8333-444444444444"
FONT_URL = "https://fonts.example.net/geofilter-fonts/open/Synthetic.ttf"
SHARED_ADDRESS = "https://bitmoji.example.net/image"

SNAP_A = "aaaaaaaa-0000-4000-8000-000000000001"
SNAP_B = "bbbbbbbb-0000-4000-8000-000000000002"
SNAP_C = "cccccccc-0000-4000-8000-000000000003"


class Obj:
    """An archived object of class ``class_name`` with these fields."""

    def __init__(self, class_name, /, **fields):
        self.name, self.fields = class_name, fields


def archive(root, archiver="NSKeyedArchiver"):
    """``root`` as an NSKeyedArchiver binary plist."""
    objects, classes = ["$null"], {}

    def class_ref(name):
        if name not in classes:
            objects.append({"$classname": name, "$classes": [name, "NSObject"]})
            classes[name] = plistlib.UID(len(objects) - 1)
        return classes[name]

    def ref(value):
        if value is None:
            return plistlib.UID(0)
        if isinstance(value, (bool, int, float)):
            return value                                   # scalars are stored inline
        objects.append(None)
        index = len(objects) - 1
        if isinstance(value, (str, bytes)):
            objects[index] = value
        elif isinstance(value, list):
            objects[index] = {"NS.objects": [ref(v) for v in value],
                              "$class": class_ref("NSArray")}
        elif isinstance(value, dict):
            keys = list(value)
            objects[index] = {"NS.keys": [ref(k) for k in keys],
                              "NS.objects": [ref(value[k]) for k in keys],
                              "$class": class_ref("NSDictionary")}
        else:
            entry = {k: ref(v) for k, v in value.fields.items()}
            entry["$class"] = class_ref(value.name)
            objects[index] = entry
        return plistlib.UID(index)

    top = ref(root)
    return plistlib.dumps({"$version": 100000, "$archiver": archiver, "$top": {"root": top},
                           "$objects": objects}, fmt=plistlib.FMT_BINARY)


def geofilter(ident, image=None, params=None, sky=None, fonts=(), kind="STATIC",
              group="GEO_GROUP"):
    """One ``SOJUGalleryGeoFilter``."""
    fields = {"idValue": ident, "type": kind, "imageUrlParams": dict(params or {}),
              "unlockableContentType": "UNRECOGNIZED_VALUE",
              "carouselGroup": Obj("SOJUUnlockablesCarouselGroup", groupName=group,
                                   carouselScore=1.0)}
    if image is not None:
        fields["imageUrl"] = image
    if sky is not None:
        fields["arSegmentation"] = Obj("SOJUUnlockablesArSegmentationFilter", sky=Obj(
            "SOJUContextFilterSkyItem", replacementSkyUrl=sky, skyType="SKY", styleType="STYLE"))
    if fonts:
        fields["geofilterMarkups"] = [
            Obj("SOJUGeofilterMarkup", displayParameters=Obj("SOJUGeofilterDisplayParameters",
                                                             font=f)) for f in fonts]
    return Obj("SOJUGalleryGeoFilter", **fields)


def overlay(geofilters, selected=None, selected_ids=None, root="SOJUGallerySnapOverlay"):
    """A ``ZOVERLAY`` blob listing ``geofilters``."""
    filters = {"geoFilters": list(geofilters)}
    if selected is not None:
        filters["geoFilterSelectedId"] = selected
    if selected_ids is not None:
        filters["geoFilterSelectedIds"] = list(selected_ids)
    return archive(Obj(root, filters=Obj("SOJUGalleryFilters", **filters), audioDisabled=False))


#: A Memory's creation time in these fixtures (Cocoa seconds), and a claim time a minute later
#: (Unix milliseconds) — close enough to make the Memory a lead of an otherwise unexplained file.
CREATED_COCOA = 1_700_000_000 - 978_307_200
NEAR_MS = 1_700_000_060_000


def scdb(app, details, has_flag=True, wal_delete=None):
    """An app folder whose scdb-27 holds one snap row per ``details`` item ``(snap id, blob,
    ZHASOVERLAYIMAGE)``, each with its ZGALLERYSNAPDETAIL row — an image Memory created at
    :data:`CREATED_COCOA`.

    ``wal_delete`` names a snap whose two rows a ``-wal`` deletes after the checkpoint, so only the
    reading without the -wal holds them. Returns the database path.
    """
    folder = os.path.join(app, "Documents", "gallery_data_object", "1", HASH)
    os.makedirs(folder, exist_ok=True)
    db = os.path.join(folder, "scdb-27.sqlite3")
    conn = sqlite3.connect(db)
    conn.execute("pragma journal_mode=wal")
    conn.execute("create table ZGALLERYSNAP (Z_PK integer primary key, ZSNAPID varchar, "
                 "ZDETAIL integer, ZMEDIATYPE integer, ZCREATETIMEUTC timestamp"
                 + (", ZHASOVERLAYIMAGE integer" if has_flag else "") + ")")
    conn.execute("create table ZGALLERYSNAPDETAIL (Z_PK integer primary key, Z_ENT integer, "
                 "Z_OPT integer, ZSNAP integer, ZOVERLAY blob)")
    for pk, (sid, blob, flag) in enumerate(details, 1):
        # the detail rows are numbered apart from the snaps, so the join must be ZSNAP = Z_PK
        detail_pk = 100 + pk
        values = (pk, sid, detail_pk, 0, CREATED_COCOA) + ((flag,) if has_flag else ())
        conn.execute(f"insert into ZGALLERYSNAP values ({', '.join('?' * len(values))})", values)
        conn.execute("insert into ZGALLERYSNAPDETAIL values (?, 1, 1, ?, ?)", (detail_pk, pk, blob))
    conn.commit()
    conn.close()
    if wal_delete:
        checkpointed = open(db, "rb").read()
        conn = sqlite3.connect(db)
        conn.execute("delete from ZGALLERYSNAPDETAIL where ZSNAP in "
                     "(select Z_PK from ZGALLERYSNAP where ZSNAPID = ?)", (wal_delete,))
        conn.execute("delete from ZGALLERYSNAP where ZSNAPID = ?", (wal_delete,))
        conn.commit()
        wal = open(db + "-wal", "rb").read()
        conn.close()
        with open(db, "wb") as fh:
            fh.write(checkpointed)
        with open(db + "-wal", "wb") as fh:
            fh.write(wal)
    if os.path.exists(db + "-shm"):
        os.remove(db + "-shm")
    return db


def cache_db(app, claims, wal_delete=None):
    """An app folder's cache_controller.db with these ``(CACHE_KEY, context, EXTERNAL_KEY[, USER_ID])``
    claims, made at :data:`NEAR_MS` by :data:`USER` unless another account is given; ``wal_delete``
    is a CACHE_KEY whose claims a ``-wal`` deletes."""
    folder = os.path.join(app, "Documents", "global_scoped", "cachecontroller")
    os.makedirs(folder, exist_ok=True)
    db = os.path.join(folder, "cache_controller.db")
    conn = sqlite3.connect(db)
    conn.execute("pragma journal_mode=wal")
    conn.execute("create table CACHE_FILE_CLAIM (USER_ID text, CACHE_KEY text, MEDIA_CONTEXT_TYPE "
                 "integer, EXTERNAL_KEY text, CREATION_TIMESTAMP_MILLIS integer, PRIMARY KEY "
                 "(USER_ID, CACHE_KEY, MEDIA_CONTEXT_TYPE, EXTERNAL_KEY))")
    conn.executemany("insert into CACHE_FILE_CLAIM values (?, ?, ?, ?, ?)",
                     [((claim[3] if len(claim) > 3 else USER), claim[0], claim[1], claim[2], NEAR_MS)
                      for claim in claims])
    conn.commit()
    conn.close()
    if wal_delete:
        checkpointed = open(db, "rb").read()
        conn = sqlite3.connect(db)
        conn.execute("delete from CACHE_FILE_CLAIM where CACHE_KEY = ?", (wal_delete,))
        conn.commit()
        wal = open(db + "-wal", "rb").read()
        conn.close()
        with open(db, "wb") as fh:
            fh.write(checkpointed)
        with open(db + "-wal", "wb") as fh:
            fh.write(wal)
    if os.path.exists(db + "-shm"):
        os.remove(db + "-shm")
    return db


WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 4000


def cached_file(app, cache_key, data=WEBP):
    """A file of the cache, in :data:`USER`'s SCContent folder, named by its CACHE_KEY."""
    folder = os.path.join(app, "Documents", f"com.snap.file_manager_3_SCContent_{USER}")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, cache_key), "wb") as fh:
        fh.write(data)
