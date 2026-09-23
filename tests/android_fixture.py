"""A synthetic Snapchat-for-Android extraction, for the Android tests.

Every value is invented — ids, names, message text, keys and media alike; they only reproduce the
*shape* of the artifacts (table and column names, the wire format of a message body, the layout of the
cache folders). The tables carry only the columns the parsers read and the ones they are keyed on;
nothing here is a copy of any device's database.

``build_app(root)`` writes the app's private-data folder under its device path
(``<root>/data/data/com.snapchat.android``) the way ``extract_zip`` leaves it; ``build_zip(path,
style)`` writes the same files into an archive shaped like one acquisition tool's:

* ``graykey`` — leading ``/``, and every file of the app's private data three times
  (``/data/data``, ``/data/user/0`` and ``/data_mirror/data_ce/null/0``), as a GrayKey archive of an
  Android phone carries it;
* ``ufed`` — the ``Dump/`` prefix, and the app's shared-storage folder again under
  ``Dump/mnt/runtime/*/emulated/0``.
"""
import io
import os
import sqlite3
import struct
import zipfile

from Crypto.Cipher import AES

PKG = "com.snapchat.android"

OWNER = "aaaaaaaa-0000-4000-8000-000000000001"
FRIEND_A = "bbbbbbbb-0000-4000-8000-000000000002"
MEMBER_B = "cccccccc-0000-4000-8000-000000000003"
CONV_A = "11111111-0000-4000-8000-00000000000a"
CONV_G = "22222222-0000-4000-8000-00000000000b"
CONV_EMPTY = "33333333-0000-4000-8000-00000000000c"
GROUP_TITLE = "Invented group"

CHAT_KEY = "0123456789abcdef0123456789abcdef"              # a chat photo, plaintext in the cache
BUNDLE_KEY = "aaaabbbbccccdddd0000111122223333"            # a chat video stored as a bundle
SHARD_KEY = "99998888777766665555444433332222"             # a chat photo stored as two shards
ENC_CHAT_KEY = "1234abcd1234abcd1234abcd1234abcd"          # a chat photo stored encrypted
CHAT_AES_KEY = bytes(range(64, 96))
CHAT_AES_IV = bytes(range(96, 112))
MEM_KEY = "fedcba9876543210fedcba9876543210"               # a Memory's media, encrypted
SNAP_ID = "44444444-0000-4000-8000-00000000000d"
MEDIA_ID = "55555555-0000-4000-8000-00000000000e"
ENTRY_ID = "66666666-0000-4000-8000-00000000000f"
MEM_AES_KEY = bytes(range(32))
MEM_AES_IV = bytes(range(16, 32))

T0 = 1_700_000_000_000                                     # ms, an invented instant

# a minimal JPEG: SOI, an APP0 JFIF header, EOI — enough for every magic-byte test
JPEG = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\x00" * 64 + b"\xff\xd9")
# the start of an MP4: an ftyp box with a generic brand, then filler
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 200


# --------------------------------------------------------------- protobuf bodies

def _tag(field, wire):
    return bytes([(field << 3) | wire])


def _len(n):
    out = b""
    while True:
        byte = n & 0x7F
        n >>= 7
        out += bytes([byte | (0x80 if n else 0)])
        if not n:
            return out


def _sub(field, payload):
    return _tag(field, 2) + _len(len(payload)) + payload


def text_body(text):
    """A content_type 1 message body: the text at 4.4.2.1."""
    return _sub(4, _sub(4, _sub(2, _sub(1, text.encode("utf-8")))))


def media_body(caption="", key=None, iv=None):
    """A content_type 2 body with a caption at 4.4.7.11.1, and — when given — a media key / IV pair
    nested in 4.4.7 (where exactly is not something the parser assumes)."""
    seven = _sub(11, _sub(1, caption.encode("utf-8"))) if caption else _sub(1, b"\x01")
    if key is not None:
        seven += _sub(1, _sub(3, _sub(1, key) + _sub(2, iv)))
    return _sub(4, _sub(4, _sub(7, seven)))


# --------------------------------------------------------------- databases

def _db(path, script, rows=()):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(script)
    for sql, values in rows:
        conn.executemany(sql, values)
    conn.commit()
    conn.close()


def _arroyo(path):
    messages = [
        (CONV_A, 1, 1, text_body("hello from A"), T0 + 1000, T0 + 5000, 1, FRIEND_A, 0),
        (CONV_A, 2, 2, text_body("reply from the owner"), T0 + 2000, 0, 1, OWNER, 0),
        (CONV_A, 3, 3, media_body("a caption"), T0 + 3000, T0 + 6000, 2, FRIEND_A, 0),
        (CONV_G, 1, 1, text_body("group hello"), T0 + 4000, 0, 1, MEMBER_B, 0),
        (CONV_A, 4, 4, media_body(), T0 + 7000, 0, 2, FRIEND_A, 0),       # the bundled video
        (CONV_A, 5, 5, media_body(), T0 + 8000, 0, 2, FRIEND_A, 0),       # the sharded photo
        (CONV_A, 6, 6, media_body(key=CHAT_AES_KEY, iv=CHAT_AES_IV), T0 + 9000, 0, 2, FRIEND_A,
         0),                                                              # the encrypted photo
    ]
    _db(path, """
        create table required_values (key text primary key, value text not null);
        create table conversation (client_conversation_id text primary key not null,
            conversation_metadata blob, creation_timestamp integer);
        create table feed_entry (client_conversation_id text primary key not null,
            display_timestamp integer not null, last_updated_timestamp integer not null,
            conversation_title text, conversation_type integer not null);
        create table conversation_message (
            client_conversation_id text not null, client_message_id integer not null,
            server_message_id integer, message_content blob not null,
            creation_timestamp integer not null, read_timestamp integer not null,
            content_type integer not null, sender_id text, is_saved integer not null,
            local_message_references blob,
            primary key (client_conversation_id, client_message_id));
    """, [
        ("insert into required_values values (?,?)", [("USERID", OWNER)]),
        ("insert into conversation values (?,?,?)",
         [(CONV_A, b"", T0), (CONV_G, b"", T0), (CONV_EMPTY, b"", T0 - 86_400_000)]),
        ("insert into feed_entry values (?,?,?,?,?)",
         [(CONV_A, T0 + 3000, T0 + 3000, None, 0),
          (CONV_G, T0 + 4000, T0 + 4000, GROUP_TITLE, 1),
          (CONV_EMPTY, T0 - 86_400_000, T0 - 86_000_000, None, 0)]),
        ("insert into conversation_message (client_conversation_id, client_message_id, "
         "server_message_id, message_content, creation_timestamp, read_timestamp, content_type, "
         "sender_id, is_saved) values (?,?,?,?,?,?,?,?,?)", messages),
    ])


def _main(path):
    _db(path, """
        create table CombinedUsername (_id integer primary key autoincrement,
            originalUsername text not null unique, mutableUsername text, encodedUsername text);
        create table Friend (_id integer primary key autoincrement, _lastModifiedTimestamp integer,
            username text not null, combinedUsernameRowId integer not null,
            userId text not null unique, displayName text, phone text, birthday integer,
            addedTimestamp integer,  -- when the one user added the other (invented comment)
            reverseAddedTimestamp integer, streakLength integer, streakExpiration integer,
            friendLinkType integer, isOfficial integer not null default 0,
            isPopular integer not null default 0, syncSource integer not null default 0);
        create table FriendWhoAddedMe (_id integer primary key autoincrement,
            friendRowId integer not null unique, userId text not null unique);
        create table SuggestedFriend (_id integer primary key autoincrement,
            friendRowId integer not null unique, userId text not null unique);
    """, [
        ("insert into CombinedUsername (originalUsername, mutableUsername) values (?,?)",
         [("owner_name", None), ("alpha_before", "alpha_now"), ("member_b", None)]),
        ("insert into Friend (username, combinedUsernameRowId, userId, displayName, phone, "
         "addedTimestamp, reverseAddedTimestamp, friendLinkType) values (?,?,?,?,?,?,?,?)",
         [("owner_name", 1, OWNER, "Owner Display", None, None, None, None),
          ("alpha_before", 2, FRIEND_A, "Alpha Display", "+15550000000", T0 - 5_000_000,
           T0 - 4_000_000, 0),
          ("member_b", 3, MEMBER_B, "Member B", None, None, None, 6)]),
        ("insert into SuggestedFriend (friendRowId, userId) values (?,?)", [(3, MEMBER_B)]),
    ])


def _cache_controller(path):
    _db(path, """
        create table CACHE_FILE_CLAIM (USER_ID text, CACHE_KEY text, MEDIA_CONTEXT_TYPE integer,
            EXTERNAL_KEY text, IS_AUTHORITATIVE integer, EXPIRATION_TIMESTAMP_MILLIS integer,
            DELETED_TIMESTAMP_MILLIS integer, CONTENT_CLAIM_METADATA blob,
            CONTENT_ATTRIBUTION integer, CREATION_TIMESTAMP_MILLIS integer,
            primary key (USER_ID, CACHE_KEY, MEDIA_CONTEXT_TYPE, EXTERNAL_KEY));
        create table CACHE_FILE_METADATA (USER_ID text, CACHE_KEY text, STORAGE_TYPE integer,
            TYPE integer, FILE_SIZE_BYTES integer, TOTAL_DISK_USED_BYTES integer,
            KNOWN_CONTENT_LENGTH_BYTES integer, LAST_READ_TIMESTAMP_MILLIS integer,
            DELETED_TIMESTAMP_MILLIS integer, CHILDREN blob, CONTENT_RETRIEVAL_METADATA blob,
            SHARD_INDEX integer, CONTENT_LIFECYCLE blob, primary key (USER_ID, CACHE_KEY));
        create table CACHE_FILE_SAMPLED_TOMBSTONE (USER_ID text not null, CACHE_KEY text not null,
            MEDIA_CONTEXT_TYPE integer not null, IS_SHARED integer not null,
            DELETION_REASON integer not null, BYTES_DELETED integer not null,
            DELETED_TIMESTAMP_MILLIS integer not null,
            primary key (USER_ID, CACHE_KEY, MEDIA_CONTEXT_TYPE));
        create table CACHE_KEY_VIRTUALIZATION (USER_ID text, VIRTUAL_CACHE_KEY text,
            CACHE_KEY text, primary key (USER_ID, VIRTUAL_CACHE_KEY));
    """, [
        ("insert into CACHE_FILE_CLAIM (USER_ID, CACHE_KEY, MEDIA_CONTEXT_TYPE, EXTERNAL_KEY, "
         "DELETED_TIMESTAMP_MILLIS, CREATION_TIMESTAMP_MILLIS) values (?,?,?,?,?,?)",
         [(OWNER, CHAT_KEY, 3, f"1:{CONV_A}:3:0:0", 0, T0 + 3100),
          (OWNER, BUNDLE_KEY, 3, f"1:{CONV_A}:4:0:0", 0, T0 + 7100),
          (OWNER, SHARD_KEY, 3, f"1:{CONV_A}:5:0:0", 0, T0 + 8100),
          (OWNER, ENC_CHAT_KEY, 3, f"1:{CONV_A}:6:0:0", 0, T0 + 9100),
          (OWNER, MEM_KEY, 19, f"g-media-{SNAP_ID}", 0, T0 - 3_000_000)]),
        ("insert into CACHE_FILE_METADATA (USER_ID, CACHE_KEY, STORAGE_TYPE, TYPE, "
         "FILE_SIZE_BYTES) values (?,?,?,?,?)",
         [(OWNER, CHAT_KEY, 1, 1, len(JPEG)), (OWNER, MEM_KEY, 1, 1, 0)]),
    ])


def _pkcs7(data):
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def encrypted_memory():
    return AES.new(MEM_AES_KEY, AES.MODE_CBC, MEM_AES_IV).encrypt(_pkcs7(JPEG))


def _memories(path, key_encoding="base64"):
    import base64
    if key_encoding == "base64":
        key, iv = base64.b64encode(MEM_AES_KEY).decode(), base64.b64encode(MEM_AES_IV).decode()
    else:
        key, iv = MEM_AES_KEY.hex(), MEM_AES_IV.hex()
    _db(path, """
        create table memories_entry (_id text not null primary key, snap_ids blob not null,
            create_time integer not null, latest_snap_create_time integer not null,
            earliest_snap_create_time integer not null, title text, is_private integer not null,
            status integer not null default 0);
        create table memories_media (_id text not null primary key, size integer, format text,
            download_url text, redirect_info text);
        create table memories_snap (_id text not null primary key, media_id text not null,
            media_type integer not null, create_time integer not null, time_zone_id text,
            width integer not null, height integer not null, duration real not null,
            memories_entry_id text not null, has_location integer not null, latitude real,
            longitude real, media_key text, media_iv text, encrypted_media_key text,
            encrypted_media_iv text, snap_capture_time integer not null,
            thumbnail_download_url text, overlay_download_url text);
        create table memories_meo_confidential (user_id text not null primary key,
            hashed_passcode text not null, master_key text not null, master_key_iv text not null);
    """, [
        ("insert into memories_entry values (?,?,?,?,?,?,?,?)",
         [(ENTRY_ID, SNAP_ID.encode(), T0 - 3_600_000, T0 - 3_600_000, T0 - 3_600_000, None, 0,
           0)]),
        ("insert into memories_media (_id, size, format) values (?,?,?)",
         [(MEDIA_ID, len(JPEG), "image/jpeg")]),
        ("insert into memories_snap values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         [(SNAP_ID, MEDIA_ID, 0, T0 - 3_600_000, "America/Toronto", 1080, 1920, 0.0, ENTRY_ID, 1,
           45.5, -73.6, key, iv, None, None, T0 - 3_600_500, None, None)]),
    ])


def _prefs(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n<map>\n"
                 f'    <string name="key_username">owner_name</string>\n'
                 f'    <string name="key_user_id">{OWNER}</string>\n'
                 f'    <long name="unrelated_counter" value="7" />\n'
                 "</map>\n")


def build_app(root, key_encoding="base64"):
    """Write the app's private-data folder under ``root``; return its path."""
    app = os.path.join(root, "data", "data", PKG)
    db = os.path.join(app, "databases")
    _arroyo(os.path.join(db, "arroyo.db"))
    _main(os.path.join(db, "main.db"))
    _memories(os.path.join(db, "memories.db"), key_encoding)
    _cache_controller(os.path.join(db, "native_content_manager", "cache_controller.db"))
    sc = os.path.join(app, "files", "native_content_manager",
                      f"com.snap.file_manager_3_SCContent_{OWNER}")
    os.makedirs(sc, exist_ok=True)
    with open(os.path.join(sc, CHAT_KEY), "wb") as fh:
        fh.write(JPEG)
    with open(os.path.join(sc, MEM_KEY), "wb") as fh:
        fh.write(encrypted_memory())
    # a bundle: the file named after the key is a small descriptor, the content is in child files
    for name, data in ((BUNDLE_KEY, b"\x0a\x08zchild01"), (BUNDLE_KEY + "_zchild01", MP4),
                       (BUNDLE_KEY + "_zoverlay", b"not media")):
        with open(os.path.join(sc, name), "wb") as fh:
            fh.write(data)
    # a chat photo stored encrypted with the key its message carries
    with open(os.path.join(sc, ENC_CHAT_KEY), "wb") as fh:
        fh.write(AES.new(CHAT_AES_KEY, AES.MODE_CBC, CHAT_AES_IV).encrypt(_pkcs7(JPEG)))
    # a file stored as two byte-range shards
    for name, data in ((f"{SHARD_KEY}_0-40", JPEG[:40]), (f"{SHARD_KEY}_40-{len(JPEG)}", JPEG[40:])):
        with open(os.path.join(sc, name), "wb") as fh:
            fh.write(data)
    _prefs(os.path.join(app, "shared_prefs", "user_session_shared_pref.xml"))
    return app


#: The device times every archived file carries here (Unix seconds): modified, accessed, changed,
#: and — as a GrayKey archive writes it — a fourth, the birth time.
FILE_TIMES = (1_700_000_100, 1_700_000_200, 1_700_000_300, 1_700_000_000)


def _entry(name):
    """A ZipInfo carrying a four-time ``UT`` extra field, the way a GrayKey archive writes one."""
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.extra = struct.pack("<HHB4i", 0x5455, 17, 0x0F, *FILE_TIMES)
    return info


def build_zip(path, tmp_root, style="graykey", key_encoding="base64"):
    """Write the app into an archive shaped like one acquisition tool's; return the archive path."""
    app = build_app(tmp_root, key_encoding)
    files = []
    for dirpath, _dirs, names in os.walk(app):
        for name in names:
            full = os.path.join(dirpath, name)
            files.append((os.path.relpath(full, app).replace("\\", "/"), full))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        if style == "graykey":
            prefixes = [f"/data/data/{PKG}", f"/data/user/0/{PKG}",
                        f"/data_mirror/data_ce/null/0/{PKG}"]
        else:
            prefixes = [f"Dump/data/data/{PKG}"]
        for prefix in prefixes:
            for rel, full in files:
                with open(full, "rb") as fh:
                    zf.writestr(_entry(f"{prefix}/{rel}"), fh.read())
        # an unrelated app, and the package name where it is NOT the app's data
        zf.writestr("/data/data/com.example.other/databases/x.db" if style == "graykey"
                    else "Dump/data/data/com.example.other/databases/x.db", b"other")
        zf.writestr(("/" if style == "graykey" else "Dump/")
                    + f"data/misc/profiles/cur/0/{PKG}/primary.prof", b"profile")
        ext = f"Android/data/{PKG}/files/note.txt"
        if style == "graykey":
            zf.writestr(f"/data/media/0/{ext}", b"external")
        else:
            zf.writestr(f"Dump/data/media/0/{ext}", b"external")
            zf.writestr(f"Dump/mnt/runtime/full/emulated/0/{ext}", b"external")
    return path


def zip_bytes(entries):
    """An in-memory archive of ``{name: bytes}`` — for the extraction unit tests."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    buf.seek(0)
    return buf
