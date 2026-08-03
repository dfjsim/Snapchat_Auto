from zipfile import ZipFile
import fnmatch
import sys
import glob
import os
import json
import shutil
import logging
import re
import plistlib
from io import BytesIO

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
        "app_group_plist_storage",
    ]

    android_files = [
        "com.snapchat.android/databases",  #### Filer som behövs från Android
        "com.snapchat.android/files/file_manager/chat_snap",
        "com.snapchat.android/files/file_manager/snap",
    ]

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
        if os.path.isdir(_out("com.snapchat.android")):
            logger.info("""
##################################################################################################################
com.snapchat.android folder already found, assuming files are already extracted.
Rename the folder and run again to extract Snapchat data from zip
##################################################################################################################""")
            return os.path.realpath(_out("com.snapchat.android")).replace("\\", "/")

    snapchat_found = False
    logger.info(f"Reading contents of zip {file_name}")
    with ZipFile(file_name, "r") as zip1:
        files_in_zip = zip1.namelist()
        logger.info(f"{len(files_in_zip)} files found in zip")
        logger.info("Extracting relevant Snapchat files from zip")
        if mode == "ios":
            files_to_extract = ios_files
        elif mode == "android":
            files_to_extract = android_files
        else:
            logger.error("Invalid OS when extracting files from zip")

        if mode == "android":
            try:
                for i in files_in_zip:
                    if wanted(i, files_to_extract):
                        try:
                            index = i.find("com.snapchat.android")
                            if index == -1:
                                continue
                            else:
                                snapchat_found = True
                                data = zip1.read(i)
                                out_path = _out(i[index:])
                                if not os.path.exists(os.path.dirname(out_path)):
                                    os.makedirs(os.path.dirname(out_path))
                                try:
                                    with open(out_path, "wb") as file:
                                        file.write(data)
                                except PermissionError:
                                    pass
                        except Exception as err:
                            pass
                            # logger.info(err)
            except Exception as err:
                pass
                # logger.info(err)
            if snapchat_found:
                logger.info("Snapchat files extracted to com.snapchat.android folder")
                return os.path.realpath(_out("com.snapchat.android")).replace("\\", "/")
            else:
                logger.warning("Snapchat not found in extraction")
                os.system("pause")

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
                            if not os.path.exists(os.path.dirname(filename)):
                                os.makedirs(os.path.dirname(filename))
                            try:
                                with open(filename, "wb") as file:
                                    file.write(data)
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
                               "renamed": renamed}, mf, indent=2)
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
