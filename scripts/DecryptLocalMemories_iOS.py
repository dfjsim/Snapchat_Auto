import os
import base64
import copy
import plistlib
from sqlite3.dbapi2 import DatabaseError
from Crypto.Cipher import AES
import sys
from binascii import unhexlify
from binascii import hexlify
from datetime import datetime
import calendar
import pandas as pd
import requests
import sqlite3
from scripts.data import ccl_bplist
from scripts.data import keychain as convert_keychain
from scripts.data import ffmpeg_log
from scripts import report_ui
import filetype
import subprocess
from PIL import Image
import shutil
import tempfile

# What FFmpeg wrote while checking decrypted videos are playable (see ffmpeg_log).
_FFMPEG_OUTPUT = []
import re
import ntpath
import numpy as np
os.environ["OPENCV_LOG_LEVEL"] = "OFF"
os.environ["OPENCV_FFMPEG_DEBUG"] = "0"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"
os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "0"
os.environ["OPENCV_VIDEOCAPTURE_DEBUG"] = "0"
os.environ["OPENCV_OPENCL_RAISE_ERROR"] = "0"
import cv2
#from cv2 import VideoCapture, CAP_PROP_FRAME_COUNT
import logging
import json
from platform import system

logger = logging.getLogger(__name__)

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "loglevel;0"

header_size = 0x10
page_size = 0x400
salt_sz = 0x10
hmac_sz = 0x20
reserved_sz = salt_sz + hmac_sz


def decrypt_sqlcipher(db, egocipher):
    key = egocipher

    # Decrypt from a private copy of the DB (with its -wal/-shm). Running sqlcipher on the
    # extracted file in-place can miss the -wal contents and produce an empty dump.
    _tmpdir = tempfile.mkdtemp()
    _copy = os.path.join(_tmpdir, os.path.basename(db))
    for _suf in ("", "-wal", "-shm"):
        if os.path.exists(db + _suf):
            shutil.copy(db + _suf, _copy + _suf)
    if os.path.exists(_copy):
        db = _copy

    print(f"Dekrypterar {db}")#med nyckel {key}")
    if platform == "Windows":
        if using_exe:
            try:
                subprocess.call([f"{exe_path}/scripts/data/sqlcipher3.exe", db,'pragma key="x\'' + key + '\'"', "PRAGMA cipher_compatibility = 3", ".output recovery.sql", ".dump"])
                print("Decrypted")
            except Exception as E:
                logger.error(E)
                sys.exit()
        else:
            try:
                subprocess.call([f"{exe_path}/data/sqlcipher3.exe", db,'pragma key="x\'' + key + '\'"', "PRAGMA cipher_compatibility = 3", ".output recovery.sql", ".dump"])
                print("Decrypted")
            except Exception as E:
                logger.error(E)
                sys.exit()
        recoveredFile = decryptedName
        if os.path.exists(recoveredFile):
            os.remove(recoveredFile)
        recoveredConn = None
        try:
            recoveredConn = sqlite3.connect(f"file:{recoveredFile}?mode=rwc", uri=True)
        except DatabaseError as e:
            logger.error(e)
        with open("recovery.sql", "r", encoding="utf-8") as recoverySql:
            recoveredConn.executescript(recoverySql.read())
            recoveredConn.close()
        #os.remove("recovery.sql")
        shutil.rmtree(_tmpdir, ignore_errors=True)
        logger.info("Database Recovered!")

def decryptGalleryDB(db, egocipher, persisted):
    global header, max_page
    
    if os.path.exists(decryptedName):
        logger.info(f"Decrypted database {decryptedName} already exists, assuming database is already decrypted")
        return
    key = egocipher
    if persisted != "":
        with open("temp.plist", "wb") as f:
            f.write(persisted)
        with open("temp.plist", "rb") as f:
            MEOplist = ccl_bplist.load(f)
        os.remove("temp.plist")

        obj = ccl_bplist.deserialise_NsKeyedArchiver(MEOplist)
        MEOkey = obj["masterKey"]
        MEOiv = obj["initializationVector"]
    # logger.info(MEOkey)
    # logger.info(MEOiv)

    enc_db = open(db, "rb")
    enc_db_size = os.path.getsize(db)

    header = enc_db.read(header_size)
    max_page = int(enc_db_size / page_size)
    with open(decryptedName, 'wb') as decrypted:
        decrypted.write(b'SQLite format 3\x00')

        for page in range(0, max_page):
            decrypted.write(decrypt_page(page, enc_db, key))
            decrypted.write(b'\x00' * reserved_sz)
    logger.info(f"Database decrypted: {decryptedName}")


def decrypt_page(page_offset, enc_db, key):
    if page_offset == 0:
        page_data = enc_db.read(page_size - header_size)
    else:
        page_data = enc_db.read(page_size)

    iv = page_data[-reserved_sz:-reserved_sz + salt_sz]
    decryption_suite = AES.new(key[:32], AES.MODE_CBC, iv)
    plain_text = decryption_suite.decrypt(page_data[:-reserved_sz])

    return plain_text


def getMemoryKey(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    query = """
	select
	snap_key_iv.snap_id as ID,
	snap_key_iv.KEY as KEY,
	snap_key_iv.IV as IV,
	snap_key_iv.ENCRYPTED as ENCRYPTED,
	snap_location_table.snap_id,
	snap_location_table.latitude as latitude,
	snap_location_table.longitude as longitude
	from snap_key_iv
	left join snap_location_table on ID = snap_location_table.snap_id"""

    df = pd.read_sql_query(query, conn)

    return df


def getSCDBInfo(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    query = """
	select
	ZSNAPID as ID,
	ZMEDIADOWNLOADURL,
	ZOVERLAYDOWNLOADURL
	from ZGALLERYSNAP
	"""
    # WHERE ZMEDIADOWNLOADURL IS NOT NULL
    df = pd.read_sql_query(query, conn)

    return df


def getFullSCDBInfo(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    query = """
	select
	ZSNAPID as ID,
	*
	from ZGALLERYSNAP
	"""
    # WHERE ZMEDIADOWNLOADURL IS NOT NULL
    df = pd.read_sql_query(query, conn)

    return df


def fixMEOkeys(persistedKey, df_merge):
    with open("temp.plist", "wb") as f:
        f.write(persistedKey)
    with open("temp.plist", "rb") as f:
        MEOplist = ccl_bplist.load(f)
    os.remove("temp.plist")

    obj = ccl_bplist.deserialise_NsKeyedArchiver(MEOplist)
    MEOkey = obj["masterKey"]
    MEOiv = obj["initializationVector"]

    for index, row in df_merge.iterrows():
        if row["ENCRYPTED"] == 1:
            enc_key = row["KEY"]
            enc_iv = row["IV"]
            aes = AES.new(MEOkey, AES.MODE_CBC, MEOiv)
            dec_key = hexlify(aes.decrypt(enc_key))[:64]
            aes = AES.new(MEOkey, AES.MODE_CBC, MEOiv)
            dec_iv = hexlify(aes.decrypt(enc_iv))[:32]
            df_merge.loc[index, "KEY"] = unhexlify(dec_key)
            df_merge.loc[index, "IV"] = unhexlify(dec_iv)

    return df_merge

def silent_error_handler(status, func_name, err_msg, file_name, line, userdata):
    pass

def decryptMemoriesLocal(egocipherKey, persistedKey, df_merge, df_cache):
    logger.info("Preparing for decryption of cached Memories")
    if not isinstance(egocipherKey, bytes):
        try:
            egocipherKey = unhexlify(egocipherKey)
            persistedKey = unhexlify(persistedKey)
        except:
            try:
                egocipherKey = base64.b64decode(egocipherKey)
                persistedKey = base64.b64decode(persistedKey)
            except Exception as E:
                logger.info("Could not decode keys", E)

    if persistedKey != "" and persistedKey != b'':
        df_merge = fixMEOkeys(persistedKey, df_merge)
    
    regex = re.compile("[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
    
    df_merge["filename"] = ""
    df_merge["overlayFilename"] = ""
    logger.info("Copying merged media files to cache folder")
    for file in os.listdir("SnapFixedVideos"):
        source = f"SnapFixedVideos/{file}"
        destination = f"{SCContentFolder_path}/{file.split('.')[0]}"
        if os.path.isfile(source) and not os.path.isfile(destination):
            shutil.copy(source, destination)
        else:
            continue
            #logger.info(f"Could not copy merged file {source}")
    
    logger.info("Decrypting cached Memories")
    uuid_counter = 0
    temp_dict = {'ID':[], 'CACHE_KEY':[]}
    for cache_index, cache_row in df_cache.iterrows():
        try:
            UUID = regex.findall(cache_row["EXTERNAL_KEY"])
            if UUID != []:
                UUID = str(UUID[0])
                uuid_counter += 1
                temp_dict['ID'].append(UUID)
                temp_dict['CACHE_KEY'].append(cache_row["CACHE_KEY"])
            else:
                continue
        except Exception as error:
            logger.error(f"Error finding memory ID {cache_row['EXTERNAL_KEY']} {error}")

    memory_df = pd.DataFrame(data=temp_dict)
    df_merge = pd.merge(df_merge, memory_df, on=["ID"], how = 'outer')
    df_merge = df_merge.dropna(axis=0, subset=["ZSNAPID"])
    #con = sqlite3.connect("test.db")
    #df_merge.to_sql("df_merge", con)
    
    os.environ["OPENCV_LOG_LEVEL"] = "OFF"
    os.environ["OPENCV_FFMPEG_DEBUG"] = "0"
    os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"
    os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "0"
    os.environ["OPENCV_VIDEOCAPTURE_DEBUG"] = "0"
    os.environ["OPENCV_OPENCL_RAISE_ERROR"] = "0"
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "loglevel;panic"
    for merge_index, merge_row in df_merge.iterrows():
        decryptedfile = False
        try:
            file = f"{SCContentFolder_path}{merge_row['CACHE_KEY']}"
            filename = merge_row['CACHE_KEY']
            if filetype.guess(file) == None or filetype.guess(file).extension in ['ps']: #Encrypted file or some random filetype
                # A My Eyes Only memory's key is stored WRAPPED in the account's MEO master key —
                # 48-byte KEY / 32-byte IV instead of 32/16 — and can only be unwrapped with the
                # keychain item 'com.snapchat.keyservice.persistedkey'. Without that item, AES.new
                # raised "Incorrect AES key length (48 bytes)", which was logged as a decryption
                # ERROR and read like a bug rather than a missing key.
                if len(merge_row["KEY"] or b"") != 32 or len(merge_row["IV"] or b"") != 16:
                    logger.info(f"Snap {merge_row['ID']} is My Eyes Only and its key is still "
                                f"wrapped ({len(merge_row['KEY'] or b'')}-byte KEY) — no "
                                f"'com.snapchat.keyservice.persistedkey' for this account was in "
                                f"the keychain, so it cannot be decrypted")
                    continue
                aes = AES.new(merge_row["KEY"], AES.MODE_CBC, merge_row["IV"])
                with open(file, "rb") as f:
                    enc_data = f.read()
                dec_data = aes.decrypt(enc_data)
                if filetype.guess(dec_data) == None: #Is the original decrypted data not valid?
                    if filetype.guess(dec_data[8:]) == None: #Is the decrypted data without the first 8 bytes not valid?
                        logger.error(f"Error decrypting {file}")
                        continue
                    else:
                        dec_data = dec_data[8:]
                        
                fileTypeMime = filetype.guess(dec_data)
                if fileTypeMime != None:
                    with open(f"{outputDir}/DecryptedMemories/{merge_row['CACHE_KEY']}.{fileTypeMime.extension}", "wb") as f:
                        f.write(dec_data)
                    decryptedfile = True
                else:
                    logger.error(f"could not find file extension of {file}")
                    shutil.copy(file, f"{outputDir}/DecryptedMemories/{filename}.{unknownFile}")
                    df_merge.loc[merge_index, "filename"] = f"{filename}.unknownFile"
                    continue
                    
            else: #Not encrypted file
                fileTypeMime = filetype.guess(file)
                if fileTypeMime != None:
                    shutil.copy(file, f"{outputDir}/DecryptedMemories/{filename}.{fileTypeMime.extension}")
                else:
                    logger.error(f"could not find file extension of {file}")
                    shutil.copy(file, f"{outputDir}/DecryptedMemories/{filename}.{unknownFile}")
                    df_merge.loc[merge_index, "filename"] = f"{filename}.unknownFile"
                    continue
                    
            if fileTypeMime.extension != "webp" and fileTypeMime.extension != "png":
                if decryptedfile == True:
                    df_merge.loc[merge_index, "filename"] = f"{filename}.{fileTypeMime.extension}"
                else:
                    if df_merge.loc[(df_merge["ID"] == merge_row['ID']) & (df_merge["filename"] != "")].empty:
                        df_merge.loc[merge_index, "filename"] = f"{filename}.{fileTypeMime.extension}"
            else:
                if decryptedfile == True:
                    df_merge.loc[merge_index, "overlayFilename"] = f"{filename}.{fileTypeMime.extension}"
                else:
                    if df_merge.loc[(df_merge["ID"] == merge_row['ID']) & (df_merge["overlayFilename"] != "")].empty:
                        df_merge.loc[merge_index, "overlayFilename"] = f"{filename}.{fileTypeMime.extension}"
            resultfile = f"{outputDir}/DecryptedMemories/{filename}.{fileTypeMime.extension}"
            
            if fileTypeMime.extension == "mp4": #Check if video has any frames, removes it if its 0
                try:
                    # OpenCV's FFmpeg writes its complaints straight to fd 2 from C, which put them
                    # on the console but never in the run's .log. Capture them instead and
                    # summarise below: on a cached video "moov atom not found" means only part of
                    # the file was ever cached, which is a finding, not noise.
                    with ffmpeg_log.captured_stderr(_FFMPEG_OUTPUT):
                        cv2video = cv2.VideoCapture(resultfile)
                        framecount = cv2video.get(cv2.CAP_PROP_FRAME_COUNT)
                        cv2video.release()
                    if int(framecount) == 0:
                        df_merge.loc[merge_index, "filename"] = "FrameCountError"
                        os.remove(resultfile)
                except Exception as Error:
                    logger.error(f"Error checking if {filename} is playable, {Error}")
            
            # if fileTypeMime is not None:
                # with open(outputDir + "/DecryptedMemories/" + filename + "." + fileTypeMime.extension, "wb") as f:
                    # f.write(dec_data)
                # with open(file, "wb") as f:
                    # f.write(dec_data)
                # if fileTypeMime.extension != "webp":
                    # df_merge.loc[merge_index, "filename"] = filename + "." + fileTypeMime.extension
                # else:
                    # df_merge.loc[merge_index, "overlayFilename"] = filename + "." + fileTypeMime.extension
            # else:
                # try:
                    # fileTypeMime = filetype.guess(dec_data[8:])
                    # if fileTypeMime is not None:
                        # with open(outputDir + "/DecryptedMemories/" + filename + "." + fileTypeMime.extension, "wb") as f:
                            # f.write(dec_data[8:])
                        # with open(file, "wb") as f:
                            # f.write(dec_data)
                        # if fileTypeMime.extension != "webp":
                            # df_merge.loc[merge_index, "filename"] = filename + "." + fileTypeMime.extension
                        # else:
                            # df_merge.loc[merge_index, "overlayFilename"] = filename + "." + fileTypeMime.extension
                # except:
                    # logger.info(f"could not find file extension of {file}")
                    # with open(outputDir + "/DecryptedMemories/" + filename + "." + "nokind", "wb") as f:
                        # f.write(dec_data)
                        # df_merge.loc[merge_index, "filename"] = filename + "." + "nokind"
                        
        except FileNotFoundError as fnfe:
            continue
        except Exception as error:
            logger.error(f"Error decrypting snap ID {merge_row['ID']} {error}")
        
        # for filename in os.listdir(SCContentFolder_path):
            # try:
                # if cache_row["CACHE_KEY"] in filename:
                    # file = SCContentFolder_path + filename
                    # kind_temp = filetype.guess(file)
                    # if kind_temp == None:
                        # UUID = regex.findall(cache_row["EXTERNAL_KEY"])
                        # if UUID != []:
                            # UUID = str(UUID[0])
                            # for merge_index, merge_row in df_merge.iterrows():
                                # if str(merge_row["ID"]) == UUID:
                                    # try:
                                        # aes = AES.new(merge_row["KEY"], AES.MODE_CBC, merge_row["IV"])
                                        # filename = cache_row["CACHE_KEY"]
                                        # with open(file, "rb") as f:
                                            # enc_data = f.read()
                                        # dec_data = aes.decrypt(enc_data)

                                        # fileTypeMime = filetype.guess(dec_data)
                                        # if fileTypeMime is not None:
                                            # with open(outputDir + "/DecryptedMemories/" + filename + "." + fileTypeMime.extension, "wb") as f:
                                                # f.write(dec_data)
                                            # with open(file, "wb") as f:
                                                # f.write(dec_data)
                                            # if fileTypeMime.extension != "webp":
                                                # df_merge.loc[merge_index, "filename"] = filename + "." + fileTypeMime.extension
                                            # else:
                                                # df_merge.loc[merge_index, "overlayFilename"] = filename + "." + fileTypeMime.extension
                                        # else:
                                            # try:
                                                # fileTypeMime = filetype.guess(dec_data[8:])
                                                # if fileTypeMime is not None:
                                                    # with open(outputDir + "/DecryptedMemories/" + filename + "." + fileTypeMime.extension, "wb") as f:
                                                        # f.write(dec_data[8:])
                                                    # with open(file, "wb") as f:
                                                        # f.write(dec_data)
                                                    # if fileTypeMime.extension != "webp":
                                                        # df_merge.loc[merge_index, "filename"] = filename + "." + fileTypeMime.extension
                                                    # else:
                                                        # df_merge.loc[merge_index, "overlayFilename"] = filename + "." + fileTypeMime.extension
                                            # except:
                                                # logger.info(f"could not find file extension of {file}")
                                                # with open(outputDir + "/DecryptedMemories/" + filename + "." + "nokind", "wb") as f:
                                                    # f.write(dec_data)
                                                    # df_merge.loc[merge_index, "filename"] = filename + "." + "nokind"
                                                    
                                    # except FileNotFoundError as fnfe:
                                        # continue
                                    # except Exception as error:
                                        # logger.info(f"Error decryption snap ID {merge_row['ID']} {error}")
            # except Exception as error:
                # logger.info(f"Error decryption snap ID {merge_row['ID']} {error}")
    
    logger.info("Done decrypting Memories")
    return df_merge


def timestampsconv(cocoaCore):
    if pd.isna(cocoaCore) or cocoaCore == "":
        return ""
    unix_timestamp = cocoaCore + 978307200
    finaltime = datetime.utcfromtimestamp(unix_timestamp)
    return (finaltime)


def recoverWithSqlite():
    if os.path.exists("gallery_decrypted.sqlite_r"):
        return True
    logger.info("Recovering Database")
    subprocess.call(["sqlite3", decryptedName, ".output recovery.sql", ".dump"])
    recoveredFile = decryptedName + "_r"
    if os.path.exists(recoveredFile):
        os.remove(recoveredFile)
    recoveredConn = None
    try:
        recoveredConn = sqlite3.connect(f"file:{recoveredFile}?mode=rwc", uri=True)
    except DatabaseError as e:
        logger.error(e)
    with open("recovery.sql", "r", encoding='utf-8') as recoverySql:
        recoveredConn.executescript(recoverySql.read())
        recoveredConn.close()
    os.remove("recovery.sql")
    logger.info("Database Recovered!")
    if checkDatabase(recoveredFile):
        logger.info("checkdatabase after decryption == True")
        return True
    else:
        logger.info("checkdatabase after decryption == False")
        return False


def recoverWithTool():
    logger.info(
        "OPEN UP THE GALLERY_DECRYPTED.DB IN FORENSIC SQLITE BROWSER - Make sure recovered database is named  GALLERY_DECRYPTED.DB_r")
    os.system("pause")


def isSqliteInstalled() -> bool:
    try:
        subprocess.call(["sqlite3", "-version"], stdout=subprocess.DEVNULL)
    except FileNotFoundError as e:
        logger.warning("SQLite3 not installed")
        return False
    return True


def checkDatabase(db) -> bool:
    logger.info("Checkdatabase " + db)
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        # query = """
		# SELECT name FROM sqlite_schema
		# WHERE type='table'
		# ORDER BY name"""
        query = """
        SELECT count(*) FROM sqlite_master
        """

        df = conn.execute(query)
    except sqlite3.DatabaseError as e:
        return False
    return True


def recoverDatabase():
    databaseValid = checkDatabase(decryptedName)
    logger.info(f"checkdatabase == {databaseValid}")
    if databaseValid:
        return
    else:
        if isSqliteInstalled():
            sucess = recoverWithSqlite()
            return sucess
        else:
            recoverWithTool()


def createFullSnapImages(df_merge):
    pattern = '_overlay'
    filePath = f"{outputDir}/DecryptedMemories/"
    os.makedirs(f"{outputDir}/DecryptedMemories/FullSnap", exist_ok=True)
    for index, row in df_merge.iterrows():
        try:
            filename = row['filename']
            if filename != "":
                file = filePath + filename
                # need to detect videos
                fileTypeMime = filetype.guess(file).mime
                if fileTypeMime != "video/mp4" and fileTypeMime != "video/quicktime":
                    background = Image.open(file)
                    if row['overlayFilename'] != "":
                        foreground = Image.open(filePath + row['overlayFilename'])
                        background.paste(foreground, (0, 0), foreground)
                    background.save(filePath + 'FullSnap/' + filename)
        except FileNotFoundError:
            pass
        except Exception as error:
            logger.error(f"Error creating FullSnapImage of snap ID: {row['ID']} | Filename: {row['filename']} | Error: {error}")


def _listdir_safe(path):
    """os.listdir that returns [] for a missing/unreadable directory."""
    try:
        return os.listdir(path)
    except OSError:
        return []


def generateReport(df_merge):
    logger.info("Generating report file")
    report_ui.copy_css(outputDir)

    filePath = f"./DecryptedMemories/"
    createFullSnapImages(df_merge)
    df_report = pd.DataFrame(columns=['ID', 'Image', 'Overlay'])
    for index, row in df_merge.iterrows():
        if row["ENCRYPTED"] == 1:
            memoryType = "My Eyes Only"
        else:
            memoryType = "Memory"
        id = row['ZSNAPID']
        format = int(row['ZMEDIATYPE'])
        columns = ['ID', 'Memory Type', 'Image', 'Overlay', 'Create Time (UTC)', 'Capture Time (UTC)', 'Duration', 'Camera', 'latitude', 'longitude']
        createTime = timestampsconv(row['ZCREATETIMEUTC'])
        captureTime = timestampsconv(row['ZCAPTURETIMEUTC'])
        duration = row['ZDURATION']
        camera = "Front" if row['ZCAMERAFRONTFACING'] == 1 else "Back"
        if row['overlayFilename'] != "":
            if format == 0:
                rowData = [id,
                           memoryType,
                           makeImg(filePath + "FullSnap/" + row['filename']),
                           makeImg(filePath + row['overlayFilename']),
                           createTime,
                           captureTime,
                           duration,
                           camera,
                           row['latitude'],
                           row['longitude']
                           ]
            else:
                rowData = [id,
                           memoryType,
                           makeVideo(filePath + row['filename']),
                           makeImg(filePath + row['overlayFilename']),
                           createTime,
                           captureTime,
                           duration,
                           camera,
                           row['latitude'],
                           row['longitude']
                           ]
        else:
            if format == 0:
                rowData = [id,
                           memoryType,
                           makeImg(filePath + "FullSnap/" +
                                   row['filename']),
                           "",
                           createTime,
                           captureTime,
                           duration,
                           camera,
                           row['latitude'],
                           row['longitude']
                           ]
            else:
                rowData = [id,
                           memoryType,
                           makeVideo(filePath + row['filename']),
                           "",
                           createTime,
                           captureTime,
                           duration,
                           camera,
                           row['latitude'],
                           row['longitude']
                           ]
        df_row = pd.DataFrame([rowData], columns=columns)
        df_report = pd.concat([df_report, df_row])
        df_report.sort_values(by=["Create Time (UTC)"], ascending=True, inplace=True)
        #logger.info(f'Records Added to Report: {len(df_report)}')

    template = """
    <body>%s
    </body>
    """

    html = """
    <link href="./css/bootstrap.min.css" rel="stylesheet">
    <style>
    th {
        background: #2d2d71;
        color: white;
        text-align: left;
    }
    </style>
        """
    html = html + template % df_report.to_html(classes=["table table-bordered table-striped table-hover table-xl "
                                                        "table-responsive text-wrap"], escape=False, index=False)
    html = html.replace('<a href="./DecryptedMemories/FullSnap/"><img src="./DecryptedMemories/FullSnap/" width="150"><br>Open </a>', "Could not be found or decrypted, "
                                                                             "usually because the Memory/MEO was " 
                                                                             "not locally stored")
    html = html.replace('<video width="320" height="240" controls> <source src="./DecryptedMemories/FrameCountError" type="video/mp4"> Your browser does not support the video tag. </video> <a href="./DecryptedMemories/FrameCountError" open><br>Open FrameCountError</a>',
        "Video is cached on device but not playable")
    html = html.replace('<video width="320" height="240" controls> <source src="./DecryptedMemories/" type="video/mp4"> Your browser does not support the video tag. </video> <a href="./DecryptedMemories/" open><br>Open </a>', "Could not be found or decrypted, "
                                                                             "usually because the Memory/MEO was " 
                                                                             "not locally stored")
    with open(f'{outputDir}/LocalMemories_legacy_report.html', 'w') as f:
        f.write(html)
    # `len(df_report)` is how many Memories were LISTED, not how many were decrypted — most rows on
    # a typical device have no locally cached media at all. Reporting it as "Decrypted N" claimed
    # 70 recovered on a device that produced one file. Count the files actually written instead.
    listed = len(df_report)
    decrypted = sum(1 for r in ("", "FullSnap")
                    for _n in _listdir_safe(os.path.join(outputDir, "DecryptedMemories", r))
                    if os.path.isfile(os.path.join(outputDir, "DecryptedMemories", r, _n)))
    logger.info(f'Listed {listed} Memories/MEO in the report; '
                f'{decrypted} media file(s) were decrypted to DecryptedMemories')


def makeImg(src):
    basename = ntpath.basename(src)
    return f'<a href="{src}"><img src="{src}" width="150"><br>Open {basename}</a>'
    #return f'<img src="{src}" width="200"/>'


def makeVideo(src):
    basename = ntpath.basename(src)
    return f'<video width="320" height="240" controls> <source src="{src}" type="video/mp4"> Your browser does not support the video tag. </video> <a href="{src}" open><br>Open {basename}</a>'
    #return f'<video width="200" controls><source src="{src}" type="video/mp4">Your browser does not ' \
    #       f'support the video tag.</video>'


def promptDate(type):
    date_entry = input(f'Enter {type} date in YYYY-MM-DD format. Leave blank to skip\n')

    if date_entry == "":
        return None

    year, month, day = map(int, date_entry.split('-'))
    if type == "end":
        day = day + 1
    dt = datetime(year, month, day)

    return getCocoaCoreTime(dt)


def getCocoaCoreTime(dt):
    unix_timestamp = calendar.timegm(dt.timetuple())
    cocoaCore = unix_timestamp - 978307200
    return (cocoaCore)


def filterDfByDates(df_merge, start_date, end_date):
    if start_date is not None and end_date is not None:
        return df_merge[(df_merge['ZCREATETIMEUTC'] >= start_date) & (df_merge['ZCREATETIMEUTC'] < end_date)]
    elif start_date is not None:
        return df_merge[(df_merge['ZCREATETIMEUTC'] >= start_date)]
    elif end_date is not None:
        return df_merge[(df_merge['ZCREATETIMEUTC'] < end_date)]
    else:
        return df_merge


# --------------------------------------------------------------------------------- keychain
#
# Reading the keychain used to be three nested bare `except:` blocks, each of which could fail
# *silently*: a format that parsed but matched nothing, an attribute stored as <string> where the
# code compared bytes, or a decrypt error swallowed whole. The only trace left in the log was a
# generic "could not find egocipher", which cannot be told apart from "this dump genuinely has no
# egocipher". Everything below exists so the log always answers *which* of those happened.

# The Snapchat keychain items we need, and the access group they live in.
SNAPCHAT_ACCESS_GROUP = "3MY7A92V5W.com.toyopagroup.picaboo"
EGOCIPHER_ACCOUNT = "egocipher.key.avoidkeyderivation"      # Memories DB (gallery.encrypteddb) key
PERSISTEDKEY_ACCOUNT = "com.snapchat.keyservice.persistedkey"   # My Eyes Only master key

# Attribute names an item can carry, per dump format: GrayKey / decrypted-UFED plists use the iOS
# keychain short names, objection's JSON dump uses long ones.
_AGRP_FIELDS = ("agrp", "accessGroup", "access_group")
_ACCOUNT_FIELDS = ("gena", "acct", "account")
_VALUE_FIELDS = ("v_Data", "dataHex", "data", "v_data")

_FORMAT_LABELS = {"bplist": "a binary plist", "xml": "an XML plist", "json": "a JSON dump",
                  "unknown": "an unrecognized format"}


def _kc_text(value):
    """Normalize a keychain attribute (``agrp``/``gena``/``acct``) to a plain string.

    Dumps disagree on the representation: some store these as UTF-8 <data> (at times
    NUL-terminated), others as <string>. The old code compared raw values against bytes literals,
    so a dump using the other representation matched nothing at all — without raising."""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").strip()


def _kc_field(entry, fields):
    """First non-empty value among `fields`, normalized to text."""
    for f in fields:
        if f in entry:
            text = _kc_text(entry[f])
            if text:
                return text
    return ""


def _kc_value_hex(entry):
    """The item's secret as a hex string, whatever the dump encoded it as (raw <data>, hex text, or
    base64 text). Returns "" when the item carries no usable value — a metadata-only dump."""
    for f in _VALUE_FIELDS:
        v = entry.get(f)
        if isinstance(v, (bytes, bytearray)) and v:
            return bytes(v).hex()
        if isinstance(v, str) and v.strip():
            s = "".join(v.split())
            try:                                            # objection's dataHex
                return unhexlify(s).hex()
            except Exception:
                pass
            try:
                return base64.b64decode(s, validate=True).hex()
            except Exception:
                continue
    return ""


def _key_note(value_hex):
    """How a key is described in the log — its size, never the key material itself."""
    return f"found ({len(value_hex) // 2} bytes)" if value_hex else "not present"


def _sniff_keychain(path):
    """Classify a keychain dump by its first bytes, returning (kind, head).

    Detecting the format up front replaces the old exception-chaining, where the second branch
    re-ran the very `plistlib.load` that had just failed in the first and so could never rescue it.
    """
    with open(path, "rb") as f:
        head = f.read(256)
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith(b"bplist00"):
        return "bplist", head
    if stripped.startswith((b"<?xml", b"<plist", b"<!DOCTYPE plist")):
        return "xml", head
    if stripped[:1] in (b"[", b"{"):
        return "json", head
    return "unknown", head


def _kc_items(obj, depth=0):
    """Yield the keychain-item dicts inside a loaded dump, whatever the container shape: a flat
    list (decrypted UFED output, objection JSON), a dict of per-class lists (GrayKey), or nested
    dicts. The old code assumed one specific shape and raised on every other."""
    if depth > 6:
        return
    if isinstance(obj, dict):
        if any(f in obj for f in _AGRP_FIELDS + _ACCOUNT_FIELDS):
            yield obj
            return
        for v in obj.values():
            yield from _kc_items(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _kc_items(v, depth + 1)


def _decrypt_ufed_keychain(keychain):
    """Decrypt a UFED keychain (one carrying `keychainEntries`) and return its item list.

    Keeps the old behaviour of leaving `decrypted_keychain.plist` in the run folder, but falls back
    to a temp file when the working directory is not writable — a failure that used to be swallowed
    along with everything else."""
    out = "decrypted_keychain.plist"
    try:
        convert_keychain.main(keychain, out)
    except OSError as error:
        logger.info(f"Keychain: cannot write {out} in {os.getcwd()} ({error}) — using a temp file")
        out = os.path.join(tempfile.mkdtemp(prefix="scauto_keychain_"), "decrypted_keychain.plist")
        convert_keychain.main(keychain, out)
    with open(out, "rb") as f:
        return plistlib.load(f)


def _persistedkeys_by_user(values_hex):
    """Map Snapchat userId -> persistedkey hex, from every persistedkey item in the dump.

    The item's payload is an NSKeyedArchiver plist carrying `masterKey`, `initializationVector`
    **and the `userId` it belongs to**, so on a phone signed into two accounts the right key can be
    handed to the right profile instead of guessing. Items that do not decode are skipped rather
    than failing the read — a keychain problem must never abort a run.
    """
    out = {}
    for value in values_hex:
        try:
            obj = ccl_bplist.deserialise_NsKeyedArchiver(BytesIO_load(unhexlify(value)))
            user = obj.get("userId")
        except Exception as error:
            logger.debug(f"Keychain: a persistedkey item could not be decoded ({error})")
            continue
        if isinstance(user, bytes):
            user = user.decode("utf-8", "replace")
        if user:
            out.setdefault(str(user), value)
    return out


def BytesIO_load(raw):
    """ccl_bplist.load() over a bytes buffer (it wants a file-like object)."""
    from io import BytesIO
    return ccl_bplist.load(BytesIO(raw))


# One run reads the keychain twice with the same path — once for the legacy Memories report
# (`main` below) and once for `memories_media_report` — and the read is a pure function of the
# file, so the second one is served from here. It saves ~1 s of UFED decryption, but the reason
# it matters is the log: repeating the findings (including the `persistedkey` WARNING) verbatim
# reads like two separate failures rather than one file described once. Keyed on
# (path, mtime, size), so a keychain replaced mid-session is still re-read.
_KEYCHAIN_CACHE = {}


def clear_keychain_cache():
    """Forget any cached keychain read. `Snapchat_Auto.run` calls this per run so that key
    material is not carried from one case into the next, and so each run folder gets its own
    `decrypted_keychain.plist` rather than inheriting the previous run's."""
    _KEYCHAIN_CACHE.clear()


def read_keychain_status(keychain):
    """Read a keychain dump and report exactly what was found.

    Repeat calls for an unchanged file are answered from `_KEYCHAIN_CACHE` (a copy, so a caller
    editing the result cannot affect the next one); `_read_keychain_status` does the real work.

    Returns a dict:
        egocipher / persistedkey : hex strings, "" when absent (the first item of each kind)
        egociphers               : every distinct egocipher value in the dump
        persistedkeys            : {userId: hex} — persistedkey is PER ACCOUNT, so a device signed
                                   into two accounts has two of them and keeping only the first
                                   loses the other account's My Eyes Only master key
        status : 'ok' | 'no-egocipher' | 'no-snapchat-items' | 'unreadable' | 'missing' | 'none'
        detail : one sentence naming the cause, for the Memories report banner
        format : the detected dump format
        items / snap_items : how many items were scanned / were Snapchat's

    Never raises — a keychain problem degrades the run instead of aborting it. Unlike the previous
    version, though, every outcome is logged at INFO or above (the run log is INFO-level, so DEBUG
    diagnostics would never reach the examiner's log file)."""
    path = str(keychain or "")
    try:
        stat = os.stat(path)
        key = (os.path.abspath(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None            # no file to stat — the uncached read is trivial for that case
    if key is not None and key in _KEYCHAIN_CACHE:
        # Say that this is the earlier result rather than restating it, so the log cannot be read
        # as a second, independent examination of the keychain.
        logger.info(f"Keychain: reusing the read of {os.path.basename(path)} logged above "
                    "(same file, unchanged since)")
        return copy.deepcopy(_KEYCHAIN_CACHE[key])
    res = _read_keychain_status(keychain)
    if key is not None:
        _KEYCHAIN_CACHE[key] = copy.deepcopy(res)
    return res


def _read_keychain_status(keychain):
    """The uncached read; `read_keychain_status` documents the result and caches it."""
    res = {"egocipher": "", "persistedkey": "", "persistedkeys": {}, "egociphers": [],
           "status": "none", "detail": "", "format": "",
           "items": 0, "snap_items": 0, "path": str(keychain or "")}

    if not res["path"].strip():
        res["detail"] = "No keychain file was supplied."
        logger.info("Keychain: none supplied — geolocation, My Eyes Only unwrapping and "
                    "old-schema memories will be unavailable")
        return res
    keychain = res["path"]
    if not os.path.isfile(keychain):
        res["status"] = "missing"
        res["detail"] = f"The keychain file was not found: {keychain}"
        logger.warning(f"Keychain: file not found: {keychain}")
        return res

    fmt, head = _sniff_keychain(keychain)
    logger.info(f"Keychain: {keychain} ({os.path.getsize(keychain):,} bytes, "
                f"{_FORMAT_LABELS.get(fmt, fmt)})")

    kind = ""
    try:
        if fmt == "json":
            kind = "objection JSON dump"
            with open(keychain, "rb") as f:
                raw = json.load(f)
        elif fmt in ("bplist", "xml"):
            with open(keychain, "rb") as f:
                raw = plistlib.load(f)
            if isinstance(raw, dict) and "keychainEntries" in raw:
                kind = "UFED keychain"
                logger.info(f"Keychain: UFED format ({len(raw.get('keychainEntries') or [])} "
                            "entries) — decrypting")
                raw = _decrypt_ufed_keychain(keychain)
            else:
                kind = "keychain plist (GrayKey / already decrypted)"
        else:
            res["status"] = "unreadable"
            res["detail"] = ("The keychain file is not in a format this tool recognizes — expected "
                             "a plist (GrayKey or UFED) or an objection JSON dump.")
            logger.error(f"Keychain: unrecognized format, first bytes {head[:16]!r} — expected an "
                         "XML/binary plist (GrayKey or UFED) or an objection JSON dump")
            return res
        items = list(_kc_items(raw))
    except Exception as error:
        res["status"] = "unreadable"
        res["detail"] = (f"The keychain file could not be parsed as {kind or fmt} "
                         f"({type(error).__name__}: {error}).")
        logger.error(f"Keychain: could not read {keychain} as {kind or fmt} — "
                     f"{type(error).__name__}: {error}", exc_info=True)
        return res

    res["format"], res["items"] = kind, len(items)
    found, all_found, snap_items, valueless = {}, {}, 0, []
    for entry in items:
        agrp = _kc_field(entry, _AGRP_FIELDS)
        acct = _kc_field(entry, _ACCOUNT_FIELDS)
        if agrp.startswith(SNAPCHAT_ACCESS_GROUP):
            snap_items += 1
        if acct not in (EGOCIPHER_ACCOUNT, PERSISTEDKEY_ACCOUNT):
            continue
        # Matched on the account name rather than on the access group: some dumps do not export
        # `agrp` at all, and requiring it (as before) skipped those items entirely.
        value = _kc_value_hex(entry)
        if not value:
            valueless.append(acct)
            continue
        if agrp and not agrp.startswith(SNAPCHAT_ACCESS_GROUP):
            logger.info(f"Keychain: '{acct}' sits in access group '{agrp}', not "
                        f"'{SNAPCHAT_ACCESS_GROUP}' — using it anyway")
        # EVERY matching item is kept, not just the first. A phone can be signed into more than one
        # Snapchat account and `persistedkey` is per-account (its payload carries a `userId`), so
        # the old "or acct in found: continue" silently discarded the second account's My Eyes Only
        # master key — and, with it, any chance of decrypting that account's MEO memories.
        all_found.setdefault(acct, []).append(value)
        found.setdefault(acct, value)

    for acct in valueless:
        logger.warning(f"Keychain: item '{acct}' is present but carries no readable value — the "
                       "dump looks metadata-only (secrets not exported or not decrypted)")

    res["snap_items"] = snap_items
    res["egocipher"] = found.get(EGOCIPHER_ACCOUNT, "")
    res["persistedkey"] = found.get(PERSISTEDKEY_ACCOUNT, "")
    res["persistedkeys"] = _persistedkeys_by_user(all_found.get(PERSISTEDKEY_ACCOUNT, []))
    res["egociphers"] = list(dict.fromkeys(all_found.get(EGOCIPHER_ACCOUNT, [])))
    owners = ", ".join(f"{u[:8]}…" for u in res["persistedkeys"]) or "none"
    logger.info(f"Keychain: {len(items)} item(s) scanned, {snap_items} in the Snapchat access "
                f"group; egocipher {_key_note(res['egocipher'])}, "
                f"persistedkey {_key_note(res['persistedkey'])}"
                + (f" for account(s) {owners}" if res["persistedkeys"] else ""))
    if len(res["egociphers"]) > 1:
        logger.info(f"Keychain: {len(res['egociphers'])} distinct egocipher item(s) present; "
                    "each gallery.encrypteddb is tried against all of them")

    if res["egocipher"]:
        res["status"] = "ok"
        res["detail"] = ("Keychain read: egocipher present, "
                         + ("persistedkey present." if res["persistedkey"] else
                            "persistedkey absent — old-schema My Eyes Only keys cannot be "
                            "unwrapped."))
        if not res["persistedkey"]:
            logger.warning("Keychain: 'com.snapchat.keyservice.persistedkey' is not in this dump — "
                           "old-schema My Eyes Only keys cannot be unwrapped")
    elif EGOCIPHER_ACCOUNT in valueless:
        res["status"] = "no-egocipher"
        res["detail"] = ("The keychain lists 'egocipher.key.avoidkeyderivation' but exports no "
                         "value for it — the dump holds item metadata only, not the secrets.")
    elif snap_items or found or valueless:
        res["status"] = "no-egocipher"
        res["detail"] = ("The keychain was read, but it holds no "
                         "'egocipher.key.avoidkeyderivation' item. That item is ThisDeviceOnly, so "
                         "it is absent from backup-class dumps — a full-filesystem keychain is "
                         "required.")
        logger.warning("Keychain: parsed successfully but 'egocipher.key.avoidkeyderivation' is "
                       "not in it — typically a backup-class dump (the item is ThisDeviceOnly); "
                       "geolocation and old-schema memories cannot be recovered")
    else:
        res["status"] = "no-snapchat-items"
        res["detail"] = ("The keychain was read, but it contains no Snapchat items at all — it may "
                         "belong to another device or extraction.")
        logger.warning(f"Keychain: parsed successfully but none of its {len(items)} item(s) belong "
                       f"to Snapchat — is this the keychain for this extraction?")
    return res


def readKeychain(keychain):
    """(egocipher_hex, persistedkey_hex) — the long-standing signature, kept for existing callers.
    See `read_keychain_status` for the full outcome."""
    res = read_keychain_status(keychain)
    return res["egocipher"], res["persistedkey"]


def diagnose_keychain(keychain):
    """Read a keychain and report on it without running an extraction — the `--diag-keychain`
    entry point, so a keychain can be checked on the machine holding the case data in seconds.
    Returns the `read_keychain_status` dict."""
    res = read_keychain_status(keychain)
    logger.info(f"Keychain verdict [{res['status']}]: {res['detail']}")
    # My Eyes Only needs `persistedkey` on BOTH schemas. The key a MEO memory carries in
    # ZGALLERYSNAP.ZENCRYPTION (new schema) is itself wrapped — 48-byte KEY, 32-byte IV against
    # 32/16 for a regular memory — so scdb alone is not enough for it, only for regular memories.
    if res["persistedkey"]:
        owners = ", ".join(f"{u[:8]}…" for u in res["persistedkeys"]) or "unknown account"
        logger.info(f"My Eyes Only: recoverable for account(s) {owners} (on either schema).")
    else:
        logger.info("My Eyes Only: unavailable on BOTH schemas — 'persistedkey' is not in this "
                    "dump, and a MEO memory's scdb key is wrapped with it.")
    if res["status"] == "ok":
        logger.info("Geolocation and old-schema regular memories: OK.")
    else:
        logger.info("Geolocation is unavailable. New-schema REGULAR memories are unaffected — "
                    "their keys live in scdb, not in the keychain.")
    return res


def main(enc_db, scdb, keychain, cache_df, SCContentFolder, out_dir=None):
    global decryptedName
    global outputDir
    global SCContentFolder_path
    global exe_path
    global platform
    global using_exe

    platform = system()

    if getattr(sys, 'frozen', False):
        exe_path = sys._MEIPASS
        using_exe = True
    else:
        exe_path = os.path.dirname(os.path.abspath(__file__))
        using_exe = False

    logger.info("Decrypting locally stored Memories and MEO")

    outputDir = out_dir or ("./Snapchat_LocalMemories_report_" + datetime.today().strftime('%Y%m%d_%H%M%S'))
    os.makedirs(outputDir + "//DecryptedMemories", exist_ok=True)

    #enc_db = sys.argv[1]
    #scdb = sys.argv[2]
    #keychain = sys.argv[3]
    #cache_df = sys.argv[4]

    SCContentFolder_path = SCContentFolder
    
    decryptedName = "gallery_decrypted.sqlite"

    start_date = None
    end_date = None

    if not isSqliteInstalled():
        return cache_df
        
    try:
        egocipher, persisted = readKeychain(keychain)
        if egocipher == "" or egocipher == b'':
            logger.info("Could not find keys for memories in keychain, skipping this step")
            shutil.rmtree(outputDir)
            return cache_df
        if not checkDatabase(enc_db):
            decrypt_sqlcipher(enc_db, egocipher)
            # decryptGalleryDB(enc_db, egocipher, persisted)
            # success = recoverDatabase()
            # if not success:
                # logger.info(f"{os.path.basename(enc_db)} is empty or not decrypted, cannot decrypt memories")
                # return cache_df
            df_MemoryKey = getMemoryKey(decryptedName)
        else:
            logger.info("Gallery database is already decrypted")
            decryptedName = enc_db
            df_MemoryKey = getMemoryKey(decryptedName)
        
        if len(df_MemoryKey) == 0:
            logger.info(f"No keys for memories could be found in {decryptedName} - Aborting")
            return
    
        df_SCDBInfo = getFullSCDBInfo(scdb)
        df_merge = pd.merge(df_MemoryKey, df_SCDBInfo, on=["ID"])

        df_merge = filterDfByDates(df_merge, start_date, end_date)

        df_merge = decryptMemoriesLocal(egocipher, persisted, df_merge, cache_df)
        df_merge = df_merge.replace("", np.nan)
        df_merge = df_merge.groupby("ID").first().reset_index()
        df_merge = df_merge.replace(np.nan, "")
        generateReport(df_merge)
        ffmpeg_log.log_summary(_FFMPEG_OUTPUT,
                               "the playability check on decrypted Memories videos", logger)
        _FFMPEG_OUTPUT.clear()
        logger.info(f"Report can be found in {outputDir}")
        logger.info(f"Decrypted memories can be found in {outputDir}/DecryptedMemories")
        return df_merge
    except Exception as Error:
        logger.info(f"Error: {Error}")
        logger.info("Something went wrong, contact author if you have any questions")


if __name__ == "__main__":
    # Only the keychain diagnostic is runnable standalone; the report itself is driven by
    # ParseSnapchat_iOS, which supplies the databases and cache DataFrame this module needs.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if len(sys.argv) > 2 and sys.argv[1].lstrip("-/").lower() == "diag-keychain":
        sys.exit(0 if diagnose_keychain(sys.argv[2])["status"] == "ok" else 1)
    print("usage: python -m scripts.DecryptLocalMemories_iOS --diag-keychain <keychain file>")
    sys.exit(2)
