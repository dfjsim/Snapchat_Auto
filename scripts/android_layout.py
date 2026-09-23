"""
Where Snapchat keeps its data on an Android device, located in an extracted tree.

``scripts/data/extract_zip.py`` writes each of the app's data areas under its **device path**, so an
extraction folder looks like the device::

    <root>/data/data/com.snapchat.android/                private data (databases, files, shared_prefs)
    <root>/data/user/<n>/com.snapchat.android/            the same, for another Android user / profile
    <root>/data/user_de/<n>/com.snapchat.android/         device-encrypted storage
    <root>/data/media/<n>/Android/data/com.snapchat.android/   the app's folder on shared storage

A folder written by an earlier version of this tool holds the private data flat, as
``<root>/com.snapchat.android/``; that is read as user 0's private data.

What is looked for, and why each is worth finding:

* ``databases/arroyo.db`` — chats. The same database, with the same schema, as on iOS: it is written by
  the messaging core the two apps share, so the iOS chat parsing applies to it unchanged.
* ``databases/native_content_manager/cache_controller.db`` and the
  ``files/native_content_manager/com.snap.file_manager_<n>_SCContent_<userId>/`` folders — the index of
  every cached file and the files themselves, again shared with iOS (where they live under
  ``Documents/``).
* ``databases/main.db`` — the ``Friend`` table (contacts) and the app's own ``Preferences``.
* ``databases/memories.db`` — Memories: one row per snap in ``memories_snap``, with its key, IV and
  location.
* ``databases/core.db`` — ``DataConsumption``, which names the older ``files/file_manager/<type>/``
  caches.
* ``shared_prefs/*.xml`` — the signed-in account's identifiers.

Everything is located by looking, never assumed: the lookups are recursive where a folder can sit in
more than one place (``lock_screen_mode`` keeps a second copy of some of them), and whatever was not
found is said so in the log rather than left to a report that silently has no rows.
"""

import os
import re
import glob
import json
import logging
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

PACKAGE = "com.snapchat.android"

#: The databases the Android parser reads, by the file name the app gives them.
KNOWN_DATABASES = {
    "arroyo": "arroyo.db",
    "main": "main.db",
    "core": "core.db",
    "memories": "memories.db",
    "journal": "journal.db",
    "cache_controller": "cache_controller.db",
    # the pre-2020 name of main.db
    "tcspahn": "tcspahn.db",
}

_SIDECAR_RE = re.compile(r"-(wal|shm|journal)$", re.I)
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _norm(path):
    return path.replace("\\", "/")


def find_app_dirs(root):
    """Every private-data folder of the app under an extraction folder, user 0 first.

    ``root`` may also be the app folder itself (what a standalone run is often pointed at).
    """
    root = _norm(os.path.abspath(root))
    if os.path.basename(root) == PACKAGE and (os.path.isdir(os.path.join(root, "databases"))
                                              or os.path.isdir(os.path.join(root, "files"))):
        return [root]
    found = []
    for pattern in (f"data/data/{PACKAGE}", f"data/user/*/{PACKAGE}", PACKAGE):
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            path = _norm(path)
            if os.path.isdir(path) and path not in found:
                found.append(path)
    return found


def find_other_areas(root):
    """``{"de": [...], "ext": [...]}`` — the app's device-encrypted and shared-storage folders."""
    root = os.path.abspath(root)
    de = [_norm(p) for p in sorted(glob.glob(os.path.join(root, "data", "user_de", "*", PACKAGE)))]
    ext = [_norm(p) for p in sorted(glob.glob(os.path.join(root, "data", "media", "*", "Android", "*",
                                                           PACKAGE)))]
    return {"de": [p for p in de if os.path.isdir(p)], "ext": [p for p in ext if os.path.isdir(p)]}


def is_app_dir(path):
    """True for an Android app's private-data folder (``databases/`` and no iOS ``Documents/``)."""
    return (os.path.isdir(os.path.join(path, "databases"))
            and not os.path.isdir(os.path.join(path, "Documents")))


def is_android_tree(root):
    """True when ``root`` holds Snapchat's Android data in either layout this tool writes."""
    return bool(find_app_dirs(root))


def extraction_root(app_dir):
    """The extraction folder an app folder was written into (what paths are shown relative to)."""
    app_dir = _norm(os.path.abspath(app_dir))
    for suffix in (f"/data/data/{PACKAGE}", f"/{PACKAGE}"):
        if app_dir.endswith(suffix):
            return app_dir[:-len(suffix)] or "/"
    mo = re.search(rf"/data/user/\d+/{re.escape(PACKAGE)}$", app_dir)
    if mo:
        return app_dir[:mo.start()] or "/"
    return os.path.dirname(app_dir)


def list_databases(app_dir):
    """``{file name: path}`` of every SQLite database under the app's ``databases/`` folder (and its
    sub-folders), sidecars left out. The first found wins for a name that occurs twice."""
    out = {}
    base = os.path.join(app_dir, "databases")
    if not os.path.isdir(base):
        return out
    for dirpath, _dirs, files in os.walk(base):
        for name in sorted(files):
            if _SIDECAR_RE.search(name):
                continue
            path = _norm(os.path.join(dirpath, name))
            try:
                with open(path, "rb") as fh:
                    head = fh.read(16)
            except OSError:
                continue
            if head.startswith(b"SQLite format 3"):
                out.setdefault(name, path)
    return out


def scan(app_dir):
    """One walk of the app folder for the three things that can sit at more than one depth.

    Returns ``(cache_controller.db paths, SCContent folders, {file_manager type: folder})``. The cache
    folders themselves are not descended into: they can hold a hundred thousand files, none of which
    is what is being looked for.
    """
    controllers, sccontent, file_manager = [], [], {}
    preferred = _norm(os.path.join(app_dir, "databases", "native_content_manager",
                                   "cache_controller.db"))
    for dirpath, dirs, files in os.walk(app_dir):
        if "cache_controller.db" in files:
            controllers.append(_norm(os.path.join(dirpath, "cache_controller.db")))
        keep = []
        for name in sorted(dirs):
            full = _norm(os.path.join(dirpath, name))
            if name.startswith("com.snap.file_manager_") and "_SCContent_" in name:
                sccontent.append(full)
            elif name == "file_manager":
                try:
                    for sub in sorted(os.listdir(full)):
                        if os.path.isdir(os.path.join(full, sub)):
                            file_manager.setdefault(sub, _norm(os.path.join(full, sub)))
                except OSError:
                    pass
            else:
                keep.append(name)
        dirs[:] = keep
    # the location the app writes it to first, anything else after it
    controllers.sort(key=lambda p: (p != preferred, p))
    sccontent.sort(key=lambda p: ("/files/native_content_manager/" not in p, p))
    return controllers, sccontent, file_manager


def find_shared_prefs(app_dir):
    """Every ``shared_prefs/*.xml`` file of the app."""
    return sorted(_norm(p) for p in glob.glob(os.path.join(app_dir, "shared_prefs", "*.xml")))


# --------------------------------------------------------------------------- shared_prefs

def read_prefs(path):
    """An Android ``SharedPreferences`` XML file as ``[(name, type, value)]``, in file order.

    ``type`` is the element name (``string``, ``long``, ``int``, ``boolean``, ``float``, ``set``).
    Values are returned as stored: a ``set`` becomes a list of its strings. A file that is not a
    preferences XML (Android 12+ can also write them in a binary format) gives ``[]``.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    if not data.lstrip().startswith(b"<"):
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out = []
    for node in root:
        name = node.attrib.get("name")
        if name is None:
            continue
        if node.tag == "set":
            value = [child.text or "" for child in node]
        elif node.tag == "string":
            value = node.text or ""
        else:
            value = node.attrib.get("value", node.text)
        out.append((name, node.tag, value))
    return out


class AndroidLayout:
    """What was found for one Android user's copy of the app. Built by :func:`discover`."""

    def __init__(self, root, app_dir):
        self.root = _norm(os.path.abspath(root))
        self.app = app_dir
        self.databases = list_databases(app_dir)
        self.cache_controllers, self.sccontent_dirs, self.file_manager = scan(app_dir)
        self.shared_prefs = find_shared_prefs(app_dir)
        self.other_areas = find_other_areas(self.root)

    def db(self, role):
        """The path of one of :data:`KNOWN_DATABASES` by role, or ""."""
        if role == "cache_controller":
            return self.cache_controllers[0] if self.cache_controllers else ""
        name = KNOWN_DATABASES.get(role, role)
        return self.databases.get(name, "")

    def device_path(self, path):
        """``/data/data/com.snapchat.android/…`` for a file under the extraction folder."""
        path = _norm(os.path.abspath(path))
        if path.lower().startswith(self.root.lower() + "/"):
            rel = path[len(self.root) + 1:]
            if rel.startswith(PACKAGE + "/") or rel == PACKAGE:     # the flat, older layout
                rel = f"data/data/{rel}"
            return "/" + rel
        return path

    def log_inventory(self):
        """Log what was found — and what was not — so a run log describes the extraction's layout."""
        logger.info(f"Snapchat for Android: app data folder {self.device_path(self.app)}")
        if self.databases:
            logger.info("  databases: " + ", ".join(sorted(self.databases)))
        else:
            logger.warning("  no database found under databases/ — the reports will be empty")
        for role in ("arroyo", "main", "memories", "core"):
            if not self.db(role):
                logger.info(f"  {KNOWN_DATABASES[role]}: not present")
        if self.cache_controllers:
            for path in self.cache_controllers:
                logger.info(f"  cache index: {self.device_path(path)}")
        else:
            logger.info("  cache_controller.db: not present (no index of the cached files)")
        if self.sccontent_dirs:
            counts = []
            for path in self.sccontent_dirs:
                try:
                    n = sum(1 for _ in os.scandir(path))
                except OSError:
                    n = 0
                counts.append(f"{os.path.basename(path)} ({n} file(s))")
            logger.info("  native content cache: " + "; ".join(counts))
        else:
            logger.info("  no com.snap.file_manager_*_SCContent_* folder (no native content cache)")
        if self.file_manager:
            parts = []
            for name, path in sorted(self.file_manager.items()):
                try:
                    n = sum(1 for e in os.scandir(path) if e.is_file())
                except OSError:
                    n = 0
                parts.append(f"{name} ({n})")
            logger.info("  files/file_manager: " + ", ".join(parts))
        if self.shared_prefs:
            logger.info(f"  shared_prefs: {len(self.shared_prefs)} file(s)")
        for area, paths in sorted(self.other_areas.items()):
            for path in paths:
                logger.info(f"  also present: {self.device_path(path)}")


def _redact(text):
    """A name with every UUID in it replaced, so a survey carries structure and not identities."""
    return _UUID_RE.sub("<uuid>", str(text))


def survey(layout, depth=3):
    """The app folder's **structure**, with no content: which tables, columns, preference keys and
    folders this app version has, and nothing about the person whose device it is.

    * every database under ``databases/``: its tables, their columns and row counts;
    * every ``shared_prefs`` file: its key names and value types, never the values;
    * the folder tree to ``depth`` levels: files and bytes per folder, never a file name.

    Every UUID in a name (the account id in ``com.snap.file_manager_*_SCContent_<userId>``, a user id
    embedded in a preference key) is replaced by ``<uuid>``.
    """
    from scripts.data import sqlite_open

    out = {"schema": 1, "package": PACKAGE, "databases": {}, "shared_prefs": {}, "folders": {}}
    for name, path in sorted(layout.databases.items()):
        entry = {"bytes": os.path.getsize(path),
                 "wal_bytes": os.path.getsize(path + "-wal") if os.path.exists(path + "-wal") else 0,
                 "tables": {}}
        try:
            views = sqlite_open.open_views(path)
        except Exception as error:                             # noqa: BLE001 — a survey never fails
            entry["error"] = str(error)
            out["databases"][_redact(name)] = entry
            continue
        try:
            conn = views.merged
            tables = [r[0] for r in conn.execute(
                "select name from sqlite_master where type = 'table' order by name")]
            for table in tables:
                try:
                    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                    rows = conn.execute(f'select count(*) from "{table}"').fetchone()[0]
                except Exception as error:                     # noqa: BLE001 — virtual tables etc.
                    cols, rows = [], f"unreadable: {error}"
                entry["tables"][_redact(table)] = {"columns": cols, "rows": rows}
        finally:
            views.close()
        out["databases"][_redact(name)] = entry
    for path in layout.shared_prefs:
        out["shared_prefs"][_redact(os.path.basename(path))] = [
            [_redact(key), kind] for key, kind, _value in read_prefs(path)]
    base = layout.app
    for dirpath, dirs, files in os.walk(base):
        rel = os.path.relpath(dirpath, base).replace("\\", "/")
        parts = [] if rel == "." else rel.split("/")
        key = _redact("/".join(parts[:depth]) or ".")
        slot = out["folders"].setdefault(key, {"files": 0, "bytes": 0})
        slot["files"] += len(files)
        for name in files:
            try:
                slot["bytes"] += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return out


def write_survey(layout, path):
    """Write :func:`survey` as JSON; return the path, or "" if it could not be written."""
    try:
        data = survey(layout)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        return path
    except Exception as error:                                 # noqa: BLE001 — never costs a run
        logger.debug(f"Could not write the layout survey: {error}")
        return ""


def discover(root):
    """``[AndroidLayout]`` — one per Android user whose private data holds the app, user 0 first."""
    root = os.path.abspath(root)
    apps = find_app_dirs(root)
    base = root
    if apps and os.path.basename(_norm(root)) == PACKAGE and apps[0] == _norm(root):
        base = extraction_root(root)
    return [AndroidLayout(base, app) for app in apps]


def load_manifest(root):
    """The Android ``extraction_manifest.json`` extract_zip wrote into ``root``, or ``{}``."""
    path = os.path.join(root, "extraction_manifest.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if data.get("platform") == "android" else {}
