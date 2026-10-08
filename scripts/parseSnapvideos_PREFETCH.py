import glob
import datetime
import sys
import os
import re
from shutil import copy2
import logging

logger = logging.getLogger(__name__)


def mergeFiles(files, directory):
    with open("./SnapFixedVideos/" + files[0].split("_")[0] + ".mp4", "ab") as full_file:
        for f in files:
            # logger.info(directory +"/" + f)
            with open(directory + "/" + f, "rb") as part_file:
                full_file.write(part_file.read())


def _offset(name):
    """Where a part starts in its file: ``<key>_<start>-<end>``, or 0 for the ``<key>_PREFETCH`` head."""
    part = name.split("_")[1]
    return 0 if part.startswith("PREFETCH") else int(part.split("-")[0])


def _merge_order_name(name):
    """The name a part sorts by. An earlier version renamed every ``<key>_PREFETCH`` to ``<key>_0-1``
    inside the extraction before sorting — the reports then quoted a name the device never had, and
    lost that part's record in the extraction manifest, which is keyed by the archive's name. The
    files are no longer touched; sorting by the old name keeps SnapFixedVideos byte-identical."""
    return name.split("_")[0] + "_0-1" if name.endswith("PREFETCH") else name


def getCache(folder):
    # every SCContent cache folder — the generation number and the account suffix both vary
    foundDir = (glob.glob(folder + "/Documents/com.snap.file_manager_*_SCContent_*")
                + glob.glob(folder + "/Library/Caches/com.snap.file_manager_*_SCContent_*"))
    for dir in foundDir:
        fileList = list(filter(pattern.match, os.listdir(dir)))
        fileList.sort(key=_merge_order_name)
        prev = []
        for j in range(len(fileList)):
            header = fileList[j]
            pat = re.compile(header.split("_")[0])
            file = list(filter(pat.match, fileList))
            try:
                file.sort(key=_offset)
            except Exception as Error:
                # logger.info(file)
                # logger.info(Error)
                os.system("pause")
            if file != prev:
                # logger.info(file)
                if len(file) > 1:
                    mergeFiles(file, dir)
                else:
                    copy2(dir + "/" + file[0], "./SnapFixedVideos/" + file[0].split("_")[0] + ".mp4")
            prev = file


def main(Application):
    global pattern

    logger.info("Merging split media files")
    uuid_pattern = re.compile("[A-F0-9-]{36}")
    for root, dirs, files in os.walk(Application):
        if uuid_pattern.match(dirs[0]):
            base_folder_data = dirs[0]
            break
    snapchatFolder = Application + "/" + base_folder_data
    pattern = re.compile(r"[a-f0-9]{32}_[\dP]")
    os.makedirs("./SnapFixedVideos", exist_ok=True)
    getCache(snapchatFolder)
    logger.info("Done merging media files")


if __name__ == "__main__":
    main()
