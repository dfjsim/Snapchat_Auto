"""The strict NSKeyedArchiver resolver (scripts/data/keyed_archive.py) behind the MemData and overlay
readers.

It must hand back plain values decided by the archive's own class names, and give None — never a
partial tree — for anything that is not such an archive or does not hold together. Every input is
synthetic.
"""
import plistlib

from scripts.data import keyed_archive as ka
from overlay_fixture import Obj, archive


def test_objects_collections_and_strings_come_back_as_plain_values():
    blob = archive(Obj("SynthRoot", name="a text", count=3, flag=True, nothing=None,
                       items=["one", Obj("SynthItem", n=1)], table={"k": "v"}, raw=b"\x00\x01"))
    tree = ka.unarchive(blob)
    assert tree == {"__class": "SynthRoot", "name": "a text", "count": 3, "flag": True,
                    "nothing": None, "items": ["one", {"__class": "SynthItem", "n": 1}],
                    "table": {"__class": "NSDictionary", "k": "v"}, "raw": b"\x00\x01"}
    assert ka.class_name(tree) == "SynthRoot" and ka.class_name("text") is None
    assert ka.unarchive(blob, "SynthRoot") == tree
    assert ka.unarchive(blob, "AnotherRoot") is None


def _raw(objects, root=1, archiver="NSKeyedArchiver"):
    return plistlib.dumps({"$version": 100000, "$archiver": archiver,
                           "$top": {"root": plistlib.UID(root)}, "$objects": objects},
                          fmt=plistlib.FMT_BINARY)


def test_foundation_objects_are_converted_by_class_and_others_kept_as_they_are():
    objects = ["$null",
               {"s": plistlib.UID(2), "d": plistlib.UID(4), "u": plistlib.UID(6),
                "m": plistlib.UID(8), "$class": plistlib.UID(10)},
               {"NS.string": "a mutable string", "$class": plistlib.UID(3)},
               {"$classname": "NSMutableString", "$classes": ["NSMutableString", "NSString"]},
               {"NS.data": b"\x10\x20", "$class": plistlib.UID(5)},
               {"$classname": "NSMutableData", "$classes": ["NSMutableData", "NSData"]},
               {"NS.uuidbytes": b"\x01" * 16, "$class": plistlib.UID(7)},
               {"$classname": "NSUUID", "$classes": ["NSUUID", "NSObject"]},
               # a subclass of NSDictionary is a dictionary by its own $classes
               {"NS.keys": [], "NS.objects": [], "$class": plistlib.UID(9)},
               {"$classname": "SynthTable", "$classes": ["SynthTable", "NSDictionary"]},
               {"$classname": "SynthRoot", "$classes": ["SynthRoot", "NSObject"]}]
    assert ka.unarchive(_raw(objects)) == {
        "__class": "SynthRoot", "s": "a mutable string", "d": b"\x10\x20",
        "u": {"__class": "NSUUID", "NS.uuidbytes": b"\x01" * 16},     # not converted on a guess
        "m": {"__class": "SynthTable"}}


def test_a_repeated_reference_is_one_object_and_a_cycle_terminates():
    objects = ["$null", {"self": plistlib.UID(1), "$class": plistlib.UID(2)},
               {"$classname": "SynthNode", "$classes": ["SynthNode"]}]
    tree = ka.unarchive(_raw(objects))
    assert tree["self"] is tree


def test_anything_that_does_not_hold_together_is_none():
    name = {"$classname": "SynthRoot", "$classes": ["SynthRoot"]}
    broken = {
        "another archiver": _raw(["$null", {"$class": plistlib.UID(2)}, name], archiver="Other"),
        "a reference outside the table": _raw(["$null", {"x": plistlib.UID(9),
                                                         "$class": plistlib.UID(2)}, name]),
        "a class with no name": _raw(["$null", {"$class": plistlib.UID(2)}, {"$classes": []}]),
        # a class hierarchy that holds a dictionary or a list where a class name belongs
        "a $classes entry that is a dictionary": _raw(
            ["$null", {"$class": plistlib.UID(2)},
             {"$classname": "SynthRoot", "$classes": ["SynthRoot", {"x": 1}]}]),
        "a $classes entry that is a list": _raw(
            ["$null", {"$class": plistlib.UID(2)},
             {"$classname": "SynthRoot", "$classes": [["SynthRoot"]]}]),
        "a $classes entry that is a number": _raw(
            ["$null", {"$class": plistlib.UID(2)},
             {"$classname": "SynthRoot", "$classes": ["SynthRoot", 7]}]),
        "a class reference to $null": _raw(["$null", {"$class": plistlib.UID(0)}]),
        "keys and values that do not pair": _raw(
            ["$null", {"NS.keys": [plistlib.UID(3)], "NS.objects": [], "$class": plistlib.UID(2)},
             {"$classname": "NSDictionary"}, "k"]),
        "a key that is not a string": _raw(
            ["$null", {"NS.keys": [7], "NS.objects": [8], "$class": plistlib.UID(2)},
             {"$classname": "NSDictionary"}]),
        "a root outside the table": _raw(["$null"], root=5),
    }
    for why, blob in broken.items():
        assert ka.unarchive(blob) is None, why
    for blob in (None, b"", b"bplist00 but not one", b"\x0a\x03abc", "a text", 12):
        assert ka.unarchive(blob) is None
    assert ka.unarchive(plistlib.dumps({"plain": "plist"}, fmt=plistlib.FMT_BINARY)) is None


def test_a_value_of_a_type_no_check_foresaw_is_none_not_an_exception(monkeypatch):
    def unforeseen(*_args):
        raise TypeError("a value of a type no layout check foresaw")
    monkeypatch.setattr(ka, "_resolve", unforeseen)
    assert ka.unarchive(archive(Obj("SynthRoot", name="a text"))) is None
