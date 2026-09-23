from zipfile import ZipFile
import fnmatch
import functools
import sys
import glob
import os
import json
import struct
import shutil
import hashlib
import logging
import re
import plistlib
from io import BytesIO

from scripts.data import device_fs

logger = logging.getLogger(__name__)

# Each iOS container (app sandbox, app group, plugin) has this metadata file at its root,
# whose MCMMetadataIdentifier names the owning bundle/group id. This is the iLEAPP
# "Bundle ID by AppGroup & Plugin" technique — see docs referenced in the project notes.
_META_PLIST = ".com.apple.mobile_container_manager.metadata.plist"
_SNAP_BUNDLE_ID = "com.toyopagroup.picaboo"
_DATA_RE = re.compile(r"/Containers/Data/Application/([0-9A-Fa-f-]{36})/")
_GROUP_RE = re.compile(r"/Containers/Shared/AppGroup/([0-9A-Fa-f-]{36})/")


def _is_snap_group(identifier):
    il = (identifier or "").lower()
    return il.startswith("group.") and ("picaboo" in il or "snapchat" in il)


def discover_snapchat_containers(zip1, names):
    """Resolve Snapchat's iOS container GUIDs from each container's metadata plist.

    Returns (data_uuids, group_guids) — the Data/Application sandbox UUID(s) owned by
    com.toyopagroup.picaboo and the Shared/AppGroup GUID(s) of Snapchat's app group(s).
    Empty data_uuids signals the caller to fall back to a broad filename scan.
    """
    data_uuids, group_guids = set(), set()
    for n in names:
        if not n.endswith(_META_PLIST):
            continue
        try:
            identifier = plistlib.load(BytesIO(zip1.read(n))).get("MCMMetadataIdentifier") or ""
        except Exception:
            continue
        md = _DATA_RE.search(n)
        if md and identifier == _SNAP_BUNDLE_ID:
            data_uuids.add(md.group(1).lower())
            continue
        mg = _GROUP_RE.search(n)
        if mg and _is_snap_group(identifier):
            group_guids.add(mg.group(1).lower())
    return data_uuids, group_guids


# Characters iOS allows in a filename but Windows does not. The URL-keyed PINCache caches
# (SCCache/*, global_scoped/sccache.*, user_scoped/**) name each entry after the CDN URL it was
# fetched from, query string included — so those names carry "?" and, on some, "*" or "|". Writing
# them unchanged fails on Windows, and the failure used to be swallowed by a bare `except: pass`,
# which silently dropped exactly the files whose name IS their provenance.
#
# ":" keeps its long-standing "_" mapping (SCPersistentMedia names are matched on both spellings —
# see _PERSISTENT_NAME_RE in ParseSnapchat_iOS.py). The rest are percent-encoded, which matches how
# these names are already encoded and round-trips through the URL decode the cached-media report
# does, so the original CDN URL is still recoverable from the name on disk.
_ILLEGAL_WIN = {"?": "%3F", "*": "%2A", "<": "%3C", ">": "%3E", "|": "%7C", '"': "%22"}


def _safe_rel(rel):
    """A ZIP member's path made safe to write on Windows without losing what the name encodes."""
    out = rel.replace(":", "_")
    for bad, encoded in _ILLEGAL_WIN.items():
        out = out.replace(bad, encoded)
    return "".join(c if ord(c) >= 32 else "%%%02X" % ord(c) for c in out)


def wanted(path, patterns):
    """Whether a ZIP entry is one we extract.

    A pattern is matched as a plain substring, or — when it contains ``*`` — as a glob against the
    whole path, so a folder whose name varies (``com.snap.file_manager_*_SCContent_*``) can be
    named once instead of being spelled out.
    """
    for pattern in patterns:
        if "*" in pattern:
            if fnmatch.fnmatch(path.replace("\\", "/"), f"*{pattern}*"):
                return True
        elif pattern in path:
            return True
    return False


#: Snapchat's Android package name — the directory every one of its data areas is named after.
ANDROID_PACKAGE = "com.snapchat.android"

# Where an Android extraction keeps one app's files, and the device path each is written under.
#
# A full file system extraction shows the **same** files through several of the paths Android mounts
# them at, and which of them an archive carries depends on the tool: one GrayKey archive of a Pixel
# holds every file of an app's private data three times — /data/data/<pkg>, /data/user/0/<pkg> and
# /data_mirror/data_ce/null/0/<pkg> — and a UFED archive holds the app's external folder under
# Dump/data/media/0/Android/data/<pkg> and again under four Dump/mnt/runtime/*/emulated/0/… views.
# Matching the package name anywhere in the path, as this module used to, wrote every copy onto the
# same file and merged the app's external folder into its private one. So each entry is mapped to one
# canonical device path instead, and where several entries map to the same path the one read through
# the canonical mount (lowest rank) is kept.
#
# Each pattern is (regex, area, rank). The regex runs on the entry name with separators normalised
# and any leading "/" removed; `pre` is the tool's own prefix ("Dump/", "") and `rest` the path inside
# the app's directory. Areas:
#   ce  — the app's private data (credential-encrypted storage): databases, files, shared_prefs, cache
#   de  — device-encrypted storage, readable before the first unlock
#   ext — the app-specific folders on shared storage: Android/data, Android/media, Android/obb
@functools.lru_cache(maxsize=4)
def _android_patterns(package):
    pkg = re.escape(package)
    roots = (
        (re.compile(rf"^(?P<pre>(?:.*?/)??)data/data/{pkg}(?P<rest>/.*|$)"), "ce", 0),
        (re.compile(rf"^(?P<pre>(?:.*?/)??)data/user/(?P<user>\d+)/{pkg}(?P<rest>/.*|$)"), "ce", 1),
        (re.compile(rf"^(?P<pre>(?:.*?/)??)data_mirror/data_ce/[^/]+/(?P<user>\d+)/{pkg}"
                    rf"(?P<rest>/.*|$)"), "ce", 2),
        (re.compile(rf"^(?P<pre>(?:.*?/)??)data/user_de/(?P<user>\d+)/{pkg}(?P<rest>/.*|$)"), "de", 0),
        (re.compile(rf"^(?P<pre>(?:.*?/)??)data_mirror/data_de/[^/]+/(?P<user>\d+)/{pkg}"
                    rf"(?P<rest>/.*|$)"), "de", 2),
        (re.compile(rf"^(?P<pre>(?:.*?/)??)data/media/(?P<user>\d+)/Android/(?P<sub>data|media|obb)/"
                    rf"{pkg}(?P<rest>/.*|$)"), "ext", 0),
        (re.compile(rf"^(?P<pre>(?:.*?/)??)(?:mnt/runtime/[^/]+/emulated|mnt/user/\d+/emulated|"
                    rf"storage/emulated|mnt/pass_through/\d+/emulated)/(?P<user>\d+)/Android/"
                    rf"(?P<sub>data|media|obb)/{pkg}(?P<rest>/.*|$)"), "ext", 2),
        (re.compile(rf"^(?P<pre>(?:.*?/)??)(?:sdcard|storage/self/primary)/Android/"
                    rf"(?P<sub>data|media|obb)/{pkg}(?P<rest>/.*|$)"), "ext", 3),
    )
    # The last resort for an archive that holds the app's directory on its own ("com.snapchat.android/
    # databases/…", as an app-only export or an older version of this tool would leave it): only when
    # no entry matched a device path above, and only when the next segment is a directory an Android
    # app's private data actually has — the package name also appears inside other apps' data and in
    # /data/misc/profiles, which are not the app's own files.
    flat = re.compile(
        rf"^(?P<pre>(?:.*?/)??){pkg}(?P<rest>/(?:databases|files|shared_prefs|cache|no_backup|"
        rf"code_cache|app_[^/]+|lock_screen_mode)(?:/.*|$))")
    return roots, flat


def android_canonical_root(area, user="0", sub="data", package=ANDROID_PACKAGE):
    """The device path an Android data area is written under, relative to the extraction folder."""
    user = str(int(user)) if str(user).isdigit() else "0"
    if area == "ce":
        return f"data/data/{package}" if user == "0" else f"data/user/{user}/{package}"
    if area == "de":
        return f"data/user_de/{user}/{package}"
    return f"data/media/{user}/Android/{sub}/{package}"


def android_entry(name, flat=False, package=ANDROID_PACKAGE):
    """``(canonical relative path, area, rank, archive path up to the package)`` for one archive
    entry of the app's Android data, or ``None`` when the entry is not one of its files.

    ``flat`` also accepts an app directory that sits in the archive with no device path around it —
    only asked for when nothing in the archive matched a device path.
    """
    if package not in name:                                  # the cheap test, for a million entries
        return None
    path = name.replace("\\", "/").lstrip("/")
    roots, flat_re = _android_patterns(package)
    # The match that starts EARLIEST in the path wins, whichever pattern it is: an app's own folders
    # can contain a path shaped like another mount point (one Pixel's Play services data holds
    # cache/data/user/0/<its own package>/…), and that inner path must stay inside the app's tree.
    best = None
    for regex, area, rank in roots:
        mo = regex.match(path)
        if mo and (best is None or len(mo.group("pre")) < len(best[0].group("pre"))):
            best = (mo, area, rank)
    if best is not None:
        mo, area, rank = best
        groups = mo.groupdict()
        root = android_canonical_root(area, groups.get("user") or "0", groups.get("sub") or "data",
                                      package)
        rest = mo.group("rest")
        archive_root = path[:len(path) - len(rest)] if rest else path
        return root + rest, area, rank, archive_root
    if flat:
        mo = flat_re.match(path)
        if mo:
            rest = mo.group("rest")
            return (android_canonical_root("ce", package=package) + rest, "ce", 9,
                    path[:len(path) - len(rest)])
    return None


def _is_symlink(info):
    """A ZIP entry recording a symbolic link (Unix mode in the high half of external_attr)."""
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


#: The "extended timestamp" extra field (`UT`), which carries **UTC** seconds.
_ZIP_UT_ID = 0x5455


def zip_mtime(info):
    """An archive entry's modification time as unix seconds (UTC), or None if it records none.

    This is the file's time **on the device**: an extraction ZIP preserves it, which is worth stating
    because it is not obvious. Two extractions of one phone taken by different tools fifteen days
    apart carry the same stamps for the same Snapchat cache files, which archive-creation stamping
    could not produce.

    Read from the ``UT`` extra field rather than from the header's DOS date/time. The DOS field is a
    *local* wall clock with no zone recorded and a two-second resolution, so turning it into a real
    instant means guessing which machine's clock wrote it — and a forensic report may not display a
    guess as a file's timestamp. The ``UT`` field is unambiguous, and every entry of every extraction
    ZIP in the corpus has one. Absent, this returns None and the report says the time was not
    recorded, which is the honest answer and the reason this cannot simply set the extracted file's
    own mtime: a file always *has* one, so it could no longer say it does not know.
    """
    extra = info.extra or b""
    pos = 0
    while pos + 4 <= len(extra):
        header, size = struct.unpack_from("<HH", extra, pos)
        body = extra[pos + 4:pos + 4 + size]
        pos += 4 + size
        if header == _ZIP_UT_ID and body and body[0] & 1 and len(body) >= 5:
            return struct.unpack_from("<i", body, 1)[0]
    return None


class SnapchatNotFound(RuntimeError):
    """The extraction holds no data of the Snapchat app for the platform the run was asked for."""


def _extract_android(zip1, names, dest, out, package=ANDROID_PACKAGE):
    """Write every file of Snapchat's Android data areas under its device path (see
    :func:`android_entry`) and record what the archive said about each one.

    The whole of each area is taken — the databases, ``files/`` (the cache folders, whose file names
    are only meaningful next to the databases that index them), ``shared_prefs`` and ``cache`` — rather
    than a list of folders: which of them an app version uses moves between versions, and a folder
    this tool did not ask for is a folder the reports cannot account for.

    Returns the extraction folder, i.e. the directory ``data/`` is written into. Raises
    :class:`SnapchatNotFound` when the archive holds none of the app's files.
    """
    def root_of(rel):
        return rel[:rel.index(package) + len(package)]

    def pick(flat):
        chosen, seen_at, dropped, differing, symlinks = {}, {}, 0, 0, 0
        for name in names:
            hit = android_entry(name, flat=flat, package=package)
            if hit is None or name.endswith("/"):
                continue
            rel, area, rank, archive_root = hit
            seen_at.setdefault(root_of(rel), set()).add(archive_root)
            try:
                info = zip1.getinfo(name)
            except KeyError:
                continue
            if _is_symlink(info):
                symlinks += 1
                continue
            current = chosen.get(rel)
            if current is not None:
                dropped += 1
                if (current[4].CRC, current[4].file_size) != (info.CRC, info.file_size):
                    differing += 1
                if current[0] <= rank:
                    continue
            chosen[rel] = (rank, name, area, archive_root, info)
        return chosen, seen_at, dropped, differing, symlinks

    chosen, seen_at, dropped, differing, symlinks = pick(flat=False)
    if not chosen:
        chosen, seen_at, dropped, differing, symlinks = pick(flat=True)
    if not chosen:
        raise SnapchatNotFound(
            f"No Snapchat data ({ANDROID_PACKAGE}) was found in this extraction: no file under "
            f"/data/data/{ANDROID_PACKAGE}, /data/user/<n>/{ANDROID_PACKAGE}, "
            f"/data/user_de/<n>/{ANDROID_PACKAGE} or …/Android/data/{ANDROID_PACKAGE}. "
            f"Check that the app is installed on the device and that the extraction is a full "
            f"file system extraction.")

    wanted_names = {record[1] for record in chosen.values()}
    fs_meta = device_fs.load_ufed_metadata(zip1, names, lambda name: name in wanted_names)

    roots, renamed, mtimes, fs = {}, {}, {}, {}
    written = failed = hash_checked = 0
    hash_mismatch = []
    for rel in sorted(chosen):
        _rank, name, area, archive_root, info = chosen[rel]
        root = root_of(rel)
        stats = roots.setdefault(root, {"area": area, "files": 0, "bytes": 0,
                                        "archive_roots": []})
        if archive_root not in stats["archive_roots"]:
            stats["archive_roots"].append(archive_root)
        safe = _safe_rel(rel)
        if safe != rel:
            renamed[safe] = rel
        target = out(safe)
        digest = hashlib.sha256()
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zip1.open(name) as src, open(target, "wb") as dst:
                for block in iter(lambda: src.read(1 << 20), b""):
                    digest.update(block)
                    dst.write(block)
        except (OSError, RuntimeError, ValueError) as error:
            # an entry the archive cannot give back (a bad CRC, a name the filesystem refuses) is
            # counted and named in the log: silently leaving it out is how evidence goes missing
            failed += 1
            logger.warning(f"Could not extract {name}: {error}")
            continue
        written += 1
        stats["files"] += 1
        stats["bytes"] += info.file_size
        stamp = zip_mtime(info)
        if stamp is not None and stamp <= 0:
            stamp = None                                     # not recorded, not 1970
        record = fs_meta.get(name) or device_fs.from_zip_entry(info, extended=True)
        if record and record.get("archive_sha256"):
            # the acquisition tool's own hash of this entry (GrayKey): the bytes written here must
            # be the bytes it read, and a difference is named rather than trusted away
            hash_checked += 1
            record["archive_sha256_matches"] = record["archive_sha256"] == digest.hexdigest()
            if not record["archive_sha256_matches"]:
                hash_mismatch.append(name)
        if stamp is not None:
            mtimes[safe] = stamp
        if record:
            fs[safe] = record
        try:
            if record and record.get("precision") == "ns" and record.get("mtime"):
                os.utime(target, ns=(record.get("atime") or record["mtime"], record["mtime"]))
            elif stamp is not None and stamp > 0:
                os.utime(target, (stamp, stamp))
        except (OSError, KeyError, TypeError, ValueError):
            pass                                             # a time the filesystem will not take

    for root, stats in roots.items():
        others = sorted(seen_at.get(root, set()) - set(stats["archive_roots"]))
        if others:
            stats["also_seen_at"] = others
    # An owner of uid 0 / gid 0 on EVERY file of an app's folders is not the device's record: Android
    # gives each app's private files the app's own uid (10000 and up). A UFED archive of an Android
    # phone writes 0/0 for every entry, so there the owner is a placeholder and is left out rather
    # than shown as "root". An archive that records owners (GrayKey) keeps them.
    owned = [r for r in fs.values() if "uid" in r or "gid" in r]
    placeholder_owner = bool(owned) and all(r.get("uid", 0) == 0 and r.get("gid", 0) == 0
                                            for r in owned)
    if placeholder_owner:
        for r in owned:
            r.pop("uid", None)
            r.pop("gid", None)
    logger.info(f"Snapchat ({package}) data areas in this extraction:")
    for root, stats in sorted(roots.items()):
        where = ", ".join(stats["archive_roots"][:3])
        logger.info(f"  /{root}: {stats['files']} file(s), {stats['bytes'] / (1024 * 1024):.1f} MB "
                    f"(archive: {where})")
    if dropped:
        logger.info(f"{dropped} archive entr(y/ies) were the same files seen through another mount "
                    f"point (/data/user/0, /data_mirror, /mnt/runtime …) and were written once"
                    + (f" — {differing} of them differ in size or CRC from the copy kept, which "
                       f"was read through the canonical path" if differing else ""))
    if symlinks:
        logger.info(f"{symlinks} symbolic link(s) in the app's folders were not written (a link is "
                    f"a pointer to another path, not a file of the app's)")
    if renamed:
        logger.info(f"{len(renamed)} file name(s) contained characters Windows does not allow and "
                    f"were percent-encoded on disk; their exact names are in extraction_manifest.json")
    if failed:
        logger.warning(f"{failed} file(s) could not be extracted — see the lines above")
    if hash_checked:
        if hash_mismatch:
            logger.warning(f"{len(hash_mismatch)} of {hash_checked} file(s) do NOT match the SHA-256 "
                           f"the acquisition tool recorded in the archive, e.g. {hash_mismatch[0]}")
        else:
            logger.info(f"All {hash_checked} extracted file(s) that carry the acquisition tool's own "
                        f"SHA-256 in the archive match it")
    if placeholder_owner:
        logger.info("The archive records owner uid 0 / gid 0 for every file of the app — not the "
                    "device's owner (an Android app's files belong to its own uid); the owner is "
                    "left out of the reports")
    try:
        with open(out("extraction_manifest.json"), "w", encoding="utf-8") as mf:
            json.dump({"platform": "android", "package": package,
                       # canonical device folder -> the area, how much of it was written, the
                       # archive paths it was read from (the tool's own prefix included) and the
                       # other mount points the archive carried the same files under
                       "roots": roots,
                       # iOS folders carry a truncated device prefix here; Android files are written
                       # under their device path already, so there is nothing to put back
                       "container_prefixes": {},
                       "renamed": renamed, "mtimes": mtimes, "fs": fs,
                       "duplicates_skipped": dropped, "duplicates_differing": differing,
                       "symlinks_skipped": symlinks, "failed": failed,
                       "archive_hashes_checked": hash_checked,
                       "archive_hash_mismatches": hash_mismatch,
                       "owner_placeholder": placeholder_owner}, mf, indent=2)
    except OSError as err:
        logger.debug(f"Could not write extraction manifest: {err}")
    logger.info(f"Snapchat files extracted: {written} file(s) under "
                f"{os.path.realpath(out('data'))}")
    return os.path.realpath(dest if dest not in ("", ".") else ".").replace("\\", "/")


def extract(file_name, mode, dest="."):

    def _out(rel):
        return os.path.join(dest, rel) if dest not in ("", ".") else rel

    ios_files = [
        "Documents/user_scoped",  ### Filer som behövs från iOS
        "Documents/global_scoped",
        # Every SCContent cache folder, not just one. The folder name varies: the number is a
        # file-manager generation (3, 4, …) and the suffix is usually — but NOT always — the
        # account's user id, so a device can carry e.g. both
        # "com.snap.file_manager_3_SCContent_<uuid>" and "com.snap.file_manager_4_SCContent_".
        # Matching only the first spelling left those files in the ZIP, which made their
        # cache_controller entries look like files that were not on the device.
        "Documents/com.snap.file_manager_*_SCContent_*",
        "Library/Caches/com.snap.file_manager_*_SCContent_*",
        "Documents/user.plist",
        "Documents/contentmanagerV3_",
        # The AES key + fixed IV for sccache.gallery-stories-snap.data. A TSAF container despite
        # the .plist name; no keychain needed. See docs/snapchat_ios_cache_media.md.
        "Documents/ClientEncryptionService.plist",
        # The whole Caches tree, not just the two folders below (which it already covers): the
        # story renders at its root, Caches/tmp, the URL-keyed PINCache stores, the cronet HTTP
        # cache and the sccache.* caches are all evidence, and which of them a device has varies by
        # app version. This is the largest single contributor to extraction time — see the byte
        # count logged below.
        "Library/Caches",
        "group.snapchat.picaboo",
        "gallery_data_object",
        "scdb-27.sqlite",
        "gallery_encrypted_db",
        # The app's own search index over Memories (Documents/gallery_search/<n>/<userHash>/
        # search.sqlite3): place names, a local date and the app's visual tags per snap, in
        # plain SQLite - no keychain needed. See docs/related_ileapp.md.
        "Documents/gallery_search",
        "app_group_plist_storage",
    ]

    # Android takes the app's whole data areas rather than a list of folders: see _extract_android.

    if dest not in ("", "."):
        os.makedirs(dest, exist_ok=True)

    if mode == "ios":
        if os.path.isdir(_out("Application")) or os.path.isdir(_out("AppGroup")):
            logger.info("""
##################################################################################################################
Application or AppGroup folder already found, assuming files are already extracted.
Rename the folders and run again to extract Snapchat data from zip
##################################################################################################################""")
            return os.path.realpath(_out("Application")).replace("\\", "/"), os.path.realpath(_out("AppGroup")).replace("\\", "/")
    elif mode == "android":
        # this build's layout (the device paths, see android_entry), or the flat
        # "com.snapchat.android" folder earlier versions wrote — the Android parser reads both
        if (os.path.isdir(_out("data")) and os.path.isfile(_out("extraction_manifest.json"))) \
                or os.path.isdir(_out(ANDROID_PACKAGE)):
            logger.info("""
##################################################################################################################
Android Snapchat data already found in the extraction folder, assuming files are already extracted.
Rename the folder and run again to extract Snapchat data from zip
##################################################################################################################""")
            return os.path.realpath(dest if dest not in ("", ".") else ".").replace("\\", "/")

    logger.info(f"Reading contents of zip {file_name}")
    with ZipFile(file_name, "r") as zip1:
        files_in_zip = zip1.namelist()
        logger.info(f"{len(files_in_zip)} files found in zip")
        logger.info("Extracting relevant Snapchat files from zip")
        if mode == "ios":
            files_to_extract = ios_files
        elif mode != "android":
            logger.error("Invalid OS when extracting files from zip")

        if mode == "android":
            return _extract_android(zip1, files_in_zip, dest, _out)

        if mode == "ios":
            # Resolve Snapchat's containers first, then only pull files from within them.
            data_uuids, group_guids = discover_snapchat_containers(zip1, files_in_zip)
            if data_uuids:
                logger.info(f"Located Snapchat containers: {len(data_uuids)} app-data, "
                            f"{len(group_guids)} app-group (via container metadata)")
            else:
                logger.warning("Could not resolve Snapchat containers from metadata; "
                               "falling back to a broad filename scan")

            def _in_snapchat(path):
                if not data_uuids:
                    return True  # discovery failed -> no scoping (legacy behaviour)
                md = _DATA_RE.search(path)
                if md and md.group(1).lower() in data_uuids:
                    return True
                mg = _GROUP_RE.search(path)
                return bool(mg and mg.group(1).lower() in group_guids)

            # We write files under dest/Application/<UUID>/... (dropping the ZIP path to the left
            # of "Application"). Remember that dropped prefix per container so reports can rebuild
            # the full on-device path (e.g. private/var/mobile/Containers/Data/Application/<UUID>).
            container_prefixes = {}
            renamed = {}
            # relative path on disk -> the file's mtime on the device, from the archive entry. Kept in
            # the manifest rather than applied to the extracted copy: see zip_mtime for why a report
            # has to be able to say "not recorded".
            mtimes = {}
            # relative path on disk -> everything the device's filesystem recorded about the file
            # (all four timestamps, owner, mode, inode, protection class), from the richest source
            # the archive has: a UFED archive's metadata.msgpack, else the entry's own extra fields.
            # See scripts/data/device_fs.py. `mtimes` stays beside it for manifests older readers
            # understand.
            fs = {}
            fs_meta = device_fs.load_ufed_metadata(
                zip1, files_in_zip, lambda name: _in_snapchat(name) and wanted(name, files_to_extract))
            caches_bytes = sanitized = 0
            try:
                for i in files_in_zip:
                    if not _in_snapchat(i):
                        continue
                    if wanted(i, files_to_extract):
                        try:
                            try:
                                index = i.find("Application")
                                if index == -1:
                                    raise Exception
                            except:
                                index = i.find("AppGroup")
                            data = zip1.read(i)
                            if "Library/Caches" in i.replace("\\", "/"):
                                caches_bytes += len(data)
                            original = i[index:].replace("\\", "/")
                            rel = _safe_rel(i[index:])
                            if rel.replace("\\", "/") != original:
                                # record the exact on-device name: for the URL-keyed caches the
                                # filename IS the provenance, so the report must be able to quote
                                # it verbatim rather than the sanitised spelling
                                renamed[rel.replace("\\", "/")] = original
                                sanitized += 1
                            filename = _out(rel)
                            tail = i[index:].replace("\\", "/").split("/")
                            if len(tail) >= 2:
                                container_prefixes.setdefault("/".join(tail[:2]),
                                                              i[:index].replace("\\", "/").strip("/"))
                            record = None
                            try:
                                info = zip1.getinfo(i)
                                stamp = zip_mtime(info)
                                # a directory entry is recorded by neither: it is not a file the
                                # reports show, and its stat record would only be noise
                                if stamp is not None and not i.endswith("/"):
                                    mtimes[rel.replace("\\", "/")] = stamp
                                if not i.endswith("/"):
                                    record = fs_meta.get(i) or device_fs.from_zip_entry(info)
                                if record:
                                    fs[rel.replace("\\", "/")] = record
                            except Exception:
                                stamp = None                  # no timestamp is a state, not a failure
                            if not os.path.exists(os.path.dirname(filename)):
                                os.makedirs(os.path.dirname(filename))
                            try:
                                with open(filename, "wb") as file:
                                    file.write(data)
                                if stamp is not None and stamp > 0:
                                    # Give the copy the device's own time — after writing it, or the
                                    # write would put it back. Every ordinary unzip tool does this and
                                    # `zipfile` alone does not, so the tree is a faithful copy and
                                    # anything that stats a file gets the device's answer rather than
                                    # the moment we unzipped it.
                                    #
                                    # The manifest is still what the reports read: a file always HAS
                                    # an mtime, so from the file alone "the device recorded this" is
                                    # indistinguishable from "the archive recorded nothing", and an
                                    # extraction folder made by an older build carries our unzip times
                                    # with no way to say so.
                                    try:
                                        if record and record.get("precision") == "ns":
                                            # the nanosecond record, where the archive has one
                                            os.utime(filename, ns=(record.get("atime") or record["mtime"],
                                                                   record["mtime"]))
                                        else:
                                            os.utime(filename, (stamp, stamp))
                                    except (OSError, KeyError, TypeError):
                                        pass                  # a time the filesystem will not take
                            except PermissionError:
                                pass
                        except Exception as err:
                            pass
                            # logger.info(err)
            except Exception as err:
                pass
                # logger.info(err)
            if caches_bytes:
                logger.info(f"Extracted {caches_bytes / (1024 * 1024):.1f} MB from Library/Caches "
                            f"(cached media, PINCache stores, cronet HTTP cache)")
            if sanitized:
                logger.info(f"{sanitized} file name(s) contained characters Windows does not allow "
                            f"(mostly the '?' in URL-keyed cache names) and were percent-encoded on "
                            f"disk; their exact on-device names are in extraction_manifest.json")
            try:
                with open(_out("extraction_manifest.json"), "w", encoding="utf-8") as mf:
                    json.dump({"container_prefixes": container_prefixes,
                               # sanitised path -> the exact name the file had on the device
                               "renamed": renamed,
                               # path on disk -> the file's mtime ON THE DEVICE, unix seconds UTC.
                               # The extracted copy's own mtime is when we unzipped it and says
                               # nothing about the evidence, so a report must read this instead.
                               "mtimes": mtimes,
                               # path on disk -> the device filesystem's whole record of the file:
                               # btime/mtime/atime/ctime as integer nanoseconds UTC with the source
                               # and its precision, owner, mode, inode, protection class, xattrs.
                               "fs": fs}, mf, indent=2)
            except Exception as err:
                logger.debug(f"Could not write extraction manifest: {err}")
            if not os.path.exists(_out("Application")):
                logger.warning("Can't find any Snapchat-files in extraction. Snapchat is probably not installed")
                os.system("pause")
                sys.exit()
            if not os.path.exists(_out("AppGroup")):
                logger.info("Snapchat files extracted to Application folder - Could not find files located in AppGroup")
                return os.path.realpath(_out("Application")).replace("\\", "/"), ""
            else:
                logger.info("Snapchat files extracted to Application and AppGroup folders")
                return os.path.realpath(_out("Application")).replace("\\", "/"), os.path.realpath(_out("AppGroup")).replace(
                    "\\", "/"
                )


if __name__ == "__main__":
    main(sys.argv[1:])
