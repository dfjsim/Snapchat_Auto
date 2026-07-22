from zipfile import ZipFile
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


def extract(file_name, mode, dest="."):

    def _out(rel):
        return os.path.join(dest, rel) if dest not in ("", ".") else rel

    ios_files = [
        "Documents/user_scoped",  ### Filer som behövs från iOS
        "Documents/global_scoped",
        "Documents/com.snap.file_manager_3_SCContent_",
        "Documents/user.plist",
        "Documents/contentmanagerV3_",
        "Library/Caches/SCPersistentMedia",
        "Library/Caches/caching-media",  # Memories .pack media cache
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
                    if any(int_file in i for int_file in files_to_extract):
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
            try:
                for i in files_in_zip:
                    if not _in_snapchat(i):
                        continue
                    if any(int_file in i for int_file in files_to_extract):
                        try:
                            try:
                                index = i.find("Application")
                                if index == -1:
                                    raise Exception
                            except:
                                index = i.find("AppGroup")
                            data = zip1.read(i)
                            rel = i[index:].replace(":", "_")
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
            try:
                with open(_out("extraction_manifest.json"), "w", encoding="utf-8") as mf:
                    json.dump({"container_prefixes": container_prefixes}, mf, indent=2)
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
