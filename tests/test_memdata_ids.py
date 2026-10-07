"""ZGALLERYSNAP.ZMEMDATAIDS and ZGALLERYENTRY.ZMEMDATAID are read, not shown as "<blob N bytes>".

Both are NSKeyedArchiver plists of one small record type — a uuid, a creation time in Unix
milliseconds and an entry type — ZMEMDATAIDS holding up to two of them (``snapMemDataId``,
``entryMemDataId``) and ZMEMDATAID one. The rule is strict: the archive's classes must be those
records, and every uuid must parse, or the blob is shown by size as before.

Every input is synthetic.
"""
import plistlib

from scripts import memories_media_report as mr

SNAP_MD = "0a0b0c0d-1111-4222-8333-444455556666"
ENTRY_MD = "f0e0d0c0-7777-4888-9999-aaaabbbbcccc"
MS = 1_700_000_000_123


def _archive(objects, root=1):
    return plistlib.dumps({"$version": 100000, "$archiver": "NSKeyedArchiver",
                           "$top": {"root": plistlib.UID(root)}, "$objects": objects},
                          fmt=plistlib.FMT_BINARY)


def _container(snap=True, entry=True, uuid_as_bytes=False):
    import uuid
    objects = ["$null"]
    root = {"$class": None}
    objects.append(root)
    objects.append({"$classname": "NSUUID", "$classes": ["NSUUID", "NSObject"]})
    nsuuid = plistlib.UID(len(objects) - 1)

    def record(u, ms, kind):
        value = {"NS.uuidbytes": uuid.UUID(u).bytes, "$class": nsuuid} if uuid_as_bytes else u
        objects.append(value)
        u_ref = plistlib.UID(len(objects) - 1)
        objects.append(ms)
        ms_ref = plistlib.UID(len(objects) - 1)
        rec = {"uuid": u_ref, "creationTimeMs": ms_ref, "entryType": kind, "$class": None}
        objects.append(rec)
        return rec, plistlib.UID(len(objects) - 1)

    snap_rec, snap_ref = record(SNAP_MD, MS, 1)
    entry_rec, entry_ref = record(ENTRY_MD, MS + 1000, 0)
    objects.append({"$classname": mr.MEMDATA_RECORD, "$classes": [mr.MEMDATA_RECORD, "NSObject"]})
    rec_class = plistlib.UID(len(objects) - 1)
    objects.append({"$classname": mr.MEMDATA_CONTAINER,
                    "$classes": [mr.MEMDATA_CONTAINER, "NSObject"]})
    snap_rec["$class"] = entry_rec["$class"] = rec_class
    root["$class"] = plistlib.UID(len(objects) - 1)
    root["snapMemDataId"] = snap_ref if snap else plistlib.UID(0)
    root["entryMemDataId"] = entry_ref if entry else plistlib.UID(0)
    return _archive(objects)


def test_the_container_gives_both_records():
    recs = mr.decode_memdata(_container())
    assert [(r["slot"], r["uuid"], r["created_ms"], r["entry_type"]) for r in recs] == [
        ("snapMemDataId", SNAP_MD.upper(), MS, 1),
        ("entryMemDataId", ENTRY_MD.upper(), MS + 1000, 0)]


def test_an_empty_slot_is_left_out():
    assert [r["slot"] for r in mr.decode_memdata(_container(snap=False))] == ["entryMemDataId"]


def test_an_nsuuid_is_read_as_well_as_a_string():
    assert mr.decode_memdata(_container(uuid_as_bytes=True))[0]["uuid"] == SNAP_MD.upper()


def test_a_bare_record_is_the_entry_column():
    objects = ["$null", {"uuid": plistlib.UID(2), "creationTimeMs": MS, "entryType": 3,
                         "$class": plistlib.UID(3)}, ENTRY_MD,
               {"$classname": mr.MEMDATA_RECORD, "$classes": [mr.MEMDATA_RECORD]}]
    assert mr.decode_memdata(_archive(objects)) == [
        {"slot": "", "uuid": ENTRY_MD.upper(), "created_ms": MS, "entry_type": 3}]


def test_anything_else_stays_a_blob():
    other = ["$null", {"uuid": plistlib.UID(2), "$class": plistlib.UID(3)}, SNAP_MD,
             {"$classname": "SomethingElse", "$classes": ["SomethingElse"]}]
    bad_uuid = ["$null", {"uuid": plistlib.UID(2), "$class": plistlib.UID(3)}, "not-a-uuid",
                {"$classname": mr.MEMDATA_RECORD, "$classes": [mr.MEMDATA_RECORD]}]
    # a damaged class hierarchy — a dictionary where a class name belongs — is not read, nor raised
    bad_classes = ["$null", {"uuid": plistlib.UID(2), "$class": plistlib.UID(3)}, SNAP_MD,
                   {"$classname": mr.MEMDATA_RECORD, "$classes": [mr.MEMDATA_RECORD, {"x": 1}]}]
    for blob in (_archive(other), _archive(bad_uuid), _archive(bad_classes), b"bplist00garbage",
                 b"\x00" * 40, None):
        assert mr.decode_memdata(blob) is None
    assert mr._other_value("ZMEMDATAIDS", b"\x01" * 12, None) == "<blob 12 bytes>"


def test_the_value_cell_the_time_row_and_the_search_term():
    fmt, _label = mr.make_time_formatter("utc")
    text = mr._other_value("ZMEMDATAIDS", _container(), fmt)
    assert text.startswith(f"snap {SNAP_MD.upper()} · created 2023-11-14 22:13:20")
    assert f"entry {ENTRY_MD.upper()}" in text and "entry type 1" in text
    m = mr._bare_memory("S-1", {"userHash": "aa" * 32})
    m["memdata"] = mr._memdata_records(_container(entry=False), "ZMEMDATAIDS", fmt)
    rows = [r for r in mr._memory_times(m) if r[0].startswith("MemData")]
    assert rows == [("MemData id created (snap)", m["memdata"][0]["created"],
                     "scdb-27 › ZGALLERYSNAP.ZMEMDATAIDS › snapMemDataId.creationTimeMs")]
