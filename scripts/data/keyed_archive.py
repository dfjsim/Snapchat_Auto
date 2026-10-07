"""NSKeyedArchiver archives as plain trees, resolved strictly by the archive's own class names.

An NSKeyedArchiver binary plist stores an object graph flat: ``$objects`` is a table, every reference
is a ``UID`` index into it, ``$top`` names the root, and each object names its class through a
``$class`` reference to a ``{"$classname", "$classes"}`` entry. Several Snapchat columns are such
archives. Three readers resolve them here — a Memory's MemData identifiers
(``ZGALLERYSNAP.ZMEMDATAIDS``, ``memories_media_report.decode_memdata``), its overlay record
(``ZGALLERYSNAPDETAIL.ZOVERLAY``, ``snap_overlay``) and the creative-tools feed tree (``ctp__feedtree.p``
slot 2, ``ctp_items.decode_feed_tree``, which reads a None as "tree not read"); the older readers
built on ``ccl_bplist.deserialise_NsKeyedArchiver`` (the ``ZENCRYPTION`` keys, the keychain's persisted
key, ``arroyo_content``, ``ParseSnapchat_iOS``) and ``ufed_keychain`` still follow the ``UID``
references themselves.

:func:`unarchive` follows them once and hands back ordinary Python values:

* an object of the archive's own classes is a ``dict`` of its keys, with its class name under
  :data:`CLASS` (``"__class"``);
* an ``NSDictionary`` (``NS.keys`` / ``NS.objects``) is a ``dict`` too, with :data:`CLASS` set;
* an ``NSArray`` / ``NSSet`` / ``NSOrderedSet`` is a ``list``, an ``NSString`` a ``str``, an
  ``NSData`` ``bytes``; ``$null`` is ``None``; any other Foundation object (an ``NSUUID``, an
  ``NSDate``) stays a ``dict`` of its own keys, so nothing it holds is converted on a guess.

Which conversion applies is decided by the archive's own class names — the ``$classname`` and, for a
subclass, the ``$classes`` it lists — never by the keys an object happens to have. The reader is
strict: anything that is not an NSKeyedArchiver archive, a reference that points outside the table,
a class entry without a name or with a ``$classes`` list that is not all names, keys and values of
different lengths, or a root of another class than the caller asked for gives ``None``, never a
partial tree.

A reference the archive repeats resolves to the same Python object, so a cycle in the archive is a
cycle in the tree: walk it by known paths, as the readers here do, not exhaustively.
"""

import plistlib

ARCHIVER = "NSKeyedArchiver"
#: The key a resolved object carries its class name under.
CLASS = "__class"

_DICTIONARIES = frozenset(("NSDictionary", "NSMutableDictionary"))
_LISTS = frozenset(("NSArray", "NSMutableArray", "NSSet", "NSMutableSet", "NSOrderedSet",
                    "NSMutableOrderedSet"))
_STRINGS = frozenset(("NSString", "NSMutableString"))
_DATA = frozenset(("NSData", "NSMutableData"))


class _NotThisArchive(Exception):
    """The archive does not have the layout this module reads."""


def unarchive(blob, root_class=None):
    """The root object of an NSKeyedArchiver ``blob`` as a plain tree, or None.

    ``root_class``, when given, is the class the root must be (its ``$classname``); a root of any
    other class gives None. Never raises.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)) or bytes(blob[:8]) != b"bplist00":
        return None
    try:
        archive = plistlib.loads(bytes(blob))
    except Exception:                                      # noqa: BLE001 - not a plist that loads
        return None
    if not isinstance(archive, dict) or archive.get("$archiver") != ARCHIVER:
        return None
    objects = archive.get("$objects")
    top = archive.get("$top")
    if not isinstance(objects, list) or not isinstance(top, dict) \
            or not isinstance(top.get("root"), plistlib.UID):
        return None
    memo = {}
    try:
        root = _resolve(top["root"], objects, memo)
    except (_NotThisArchive, RecursionError, TypeError):
        # TypeError: a value of a type the layout checks above did not foresee — still not an
        # archive this module reads, and a damaged evidence value must not stop a report
        return None
    if root_class is not None and (not isinstance(root, dict) or root.get(CLASS) != root_class):
        return None
    return root


def class_name(node):
    """The class a resolved object carries (:data:`CLASS`), or None for anything else."""
    return node.get(CLASS) if isinstance(node, dict) else None


def text_paths(blob):
    """``[(path, text)]`` for every text of a keyed archive, in the archive's order, or None when
    ``blob`` is not one (:func:`unarchive`).

    The path is where the text sits, by the archive's own names: the root's class, then an object's
    key (``.filters``), a list's position (``[3]``) — and ``{}`` for an entry of a dictionary, whose
    keys are values the archive holds, not names, and are never written into a path. A part of the
    tree reached twice (the archive repeats a reference) is listed once, under its first path.
    For ``--trace-ids`` and ``--survey-claim-links``, which say where an id sits, never what is
    around it.
    """
    root = unarchive(blob)
    if root is None:
        return None
    out, seen = [], set()
    stack = [(root, class_name(root) or "")]
    while stack:
        node, path = stack.pop()
        if isinstance(node, str):
            out.append((path, node))
            continue
        if not isinstance(node, (dict, list)) or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, list):
            steps = [(value, f"{path}[{i}]") for i, value in enumerate(node)]
        else:
            dictionary = class_name(node) in _DICTIONARIES
            steps = [(value, path + ("{}" if dictionary else f".{key}"))
                     for key, value in node.items() if key != CLASS]
        stack.extend(reversed(steps))
    return out


def _class_of(entry, objects):
    """``(class name, the names of its class hierarchy)`` of one object's ``$class`` entry."""
    ref = entry.get("$class")
    if not isinstance(ref, plistlib.UID) or not 0 <= ref.data < len(objects):
        raise _NotThisArchive("a $class reference outside the object table")
    meta = objects[ref.data]
    name = meta.get("$classname") if isinstance(meta, dict) else None
    if not isinstance(name, str) or not name:
        raise _NotThisArchive("a class entry with no $classname")
    lineage = meta.get("$classes")
    if not isinstance(lineage, list):
        return name, frozenset((name,))
    if not all(isinstance(c, str) for c in lineage):
        raise _NotThisArchive("a class entry whose $classes are not all names")
    return name, frozenset(lineage)


def _resolve(value, objects, memo):
    if isinstance(value, plistlib.UID):
        index = value.data
        if not 0 <= index < len(objects):
            raise _NotThisArchive("a reference outside the object table")
        if index in memo:
            return memo[index]
        entry = objects[index]
        if entry == "$null":
            memo[index] = None
            return None
        if isinstance(entry, dict) and "$class" in entry:
            return _resolve_object(index, entry, objects, memo)
        memo[index] = out = _resolve(entry, objects, memo)
        return out
    if isinstance(value, list):
        return [_resolve(v, objects, memo) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, objects, memo) for k, v in value.items()}
    return value


def _resolve_object(index, entry, objects, memo):
    """One object that names its class: converted by that class, or kept as its own keys."""
    name, lineage = _class_of(entry, objects)
    kinds = lineage | {name}
    if kinds & _STRINGS and isinstance(entry.get("NS.string"), str):
        memo[index] = entry["NS.string"]
        return memo[index]
    if kinds & _DATA and isinstance(entry.get("NS.data"), bytes):
        memo[index] = entry["NS.data"]
        return memo[index]
    if kinds & _DICTIONARIES:
        keys, values = entry.get("NS.keys"), entry.get("NS.objects")
        if not isinstance(keys, list) or not isinstance(values, list) or len(keys) != len(values):
            raise _NotThisArchive("a dictionary whose keys and values do not pair up")
        out = memo[index] = {CLASS: name}
        for k, v in zip(keys, values):
            key = _resolve(k, objects, memo)
            if not isinstance(key, str) or key == CLASS:
                raise _NotThisArchive("a dictionary key that is not a string")
            out[key] = _resolve(v, objects, memo)
        return out
    if kinds & _LISTS:
        items = entry.get("NS.objects")
        if not isinstance(items, list):
            raise _NotThisArchive("a collection with no NS.objects list")
        out = memo[index] = []
        out.extend(_resolve(v, objects, memo) for v in items)
        return out
    out = memo[index] = {CLASS: name}
    for key, v in entry.items():
        if key == CLASS:
            raise _NotThisArchive("an object with a key this module reserves")
        if key != "$class":
            out[key] = _resolve(v, objects, memo)
    return out
