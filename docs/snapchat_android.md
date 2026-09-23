# Snapchat for Android

How the Android run finds Snapchat's data in an extraction, and what each report reads.

Two of the databases read here are the ones the iOS reports read — the chat database and the
cached-file index are written by the messaging and content-caching core the two apps share, with the
same schema. Everything Android-only (the contacts table, the Memories database, the preferences) is
read only where the table has the column, and what was not found is said in the run log.

The tests build archives in the GrayKey and UFED layouts described below
(`tests/test_android_extraction.py`) and a synthetic app folder (`tests/android_fixture.py`), and run
every report end to end through the same entry point as a real run (`tests/test_android_pipeline.py`,
`tests/test_android_memories.py`).

## Where the app keeps its data

The app's private data is `/data/data/com.snapchat.android` — the same folder Android also mounts as
`/data/user/0/com.snapchat.android` and `/data_mirror/data_ce/null/0/com.snapchat.android`. Another
Android user or work profile has its own copy under `/data/user/<n>/`.

| Path under the app folder | Holds | Read by |
|---|---|---|
| `databases/arroyo.db` | conversations and messages (`conversation_message`, `conversation`, `feed_entry`, `required_values`) — **the same schema as on iOS** | Conversations |
| `databases/native_content_manager/cache_controller.db` | the index of every cached file (`CACHE_FILE_CLAIM`, `CACHE_FILE_METADATA`, …) — **the same schema as on iOS** | cache_controller, Conversations, Memories |
| `files/native_content_manager/com.snap.file_manager_<n>_SCContent_<userId>/` | the cached files, named after their `CACHE_KEY` (whole files, `<key>_<start>-<end>` byte ranges, bundle children) — the same layout as iOS's `Documents/com.snap.file_manager_*` | cache_controller, Conversations, Memories |
| `databases/main.db` | `Friend` (every user the app has to show), `CombinedUsername`, `FriendWhoAddedMe`, `SuggestedFriend`, `BestFriend`, … | Contacts |
| `databases/memories.db` | `memories_snap`, `memories_entry`, `memories_media`, `memories_meo_confidential` | Memories |
| `databases/core.db` | `DataConsumption` — an index of the `files/file_manager/<type>/` caches | legacy Communications |
| `files/file_manager/<type>/` | the app's own cache folders (`memories_media`, `memories_thumbnail`, `memories_overlay`, `chat_snap`, `snap`, …) | Memories, legacy Communications |
| `shared_prefs/*.xml` | the app's preferences, some of which name the signed-in account | Contacts (the owner's row) |

`lock_screen_mode/` holds a second, separate copy of some of these folders; the cache index and the
cache folders are searched for wherever they are under the app folder
(`scripts/android_layout.py`, `scan`).

## Extraction — one device path per file

A full file system extraction of an Android phone shows the same files through several mount points,
and which of them an archive carries depends on the tool (as read from a GrayKey and a UFED full file
system extraction of an Android 12 Pixel):

| | GrayKey (`<serial>_files_full.zip`) | UFED (`EXTRACTION_FFS.zip`) |
|---|---|---|
| prefix | `/` | `Dump/` |
| an app's private data | **three times**: `/data/data/<pkg>`, `/data/user/0/<pkg>`, `/data_mirror/data_ce/null/0/<pkg>` | once: `Dump/data/data/<pkg>` |
| device-encrypted data | `/data/user_de/0/<pkg>` and `/data_mirror/data_de/null/0/<pkg>` | `Dump/data/user_de/0/<pkg>` |
| the app's folder on shared storage | `/data/media/0/Android/data/<pkg>` | `Dump/data/media/0/Android/data/<pkg>` **and** four `Dump/mnt/runtime/<view>/emulated/0/Android/data/<pkg>` views |

The three GrayKey copies are one file: each archive entry's `IN` extra field (below) carries the same
inode and device number for all three. The extractor used to match the package name anywhere in the
path and strip everything before it, so it wrote every copy onto one file and **merged the shared-
storage folder into the private one**.

`scripts/data/extract_zip.py` (`android_entry`) now maps each entry to its canonical device path and
writes it once:

| written under | from |
|---|---|
| `data/data/com.snapchat.android/…` | `data/data/…`, `data/user/0/…`, `data_mirror/data_ce/<vol>/0/…` |
| `data/user/<n>/com.snapchat.android/…` | the same, for user `<n>` ≠ 0 |
| `data/user_de/<n>/com.snapchat.android/…` | `data/user_de/<n>/…`, `data_mirror/data_de/<vol>/<n>/…` |
| `data/media/<n>/Android/<data\|media\|obb>/com.snapchat.android/…` | `data/media/<n>/…`, `mnt/runtime/*/emulated/<n>/…`, `storage/emulated/<n>/…`, `sdcard/…` |

When several entries map to one path, the one read through the canonical mount is kept (`/data/data`
before `/data/user/0` before the mirrors). The match that starts **earliest** in the entry name wins,
because an app's own folders can contain a path shaped like another mount point — one Pixel's Play
services folder holds `cache/data/user/0/<its own package>/…` — which has to stay inside the app's
tree. A path that only *mentions* the package (the ART profile under `/data/misc/profiles`, another
app's `backup_chunk_listings/<pkg>`, the APK under `/data/app`) is not the app's data and is not
extracted. Symbolic links are not written. An archive holding just the app's folder with no device
path around it is accepted as user 0's private data, and so is the flat `ExtractedData/
com.snapchat.android` folder an earlier version of this tool wrote.

Duplicates are not always identical. On a phone acquired while running, the same file read at two
moments through two mounts can differ (seen on a Play services log file and database on the Pixel);
the copy read through the canonical path is the one kept, and the run log and
`extraction_manifest.json` count how many duplicates differed.

### What the archive records about each file

| field | GrayKey (Android) | UFED (Android) |
|---|---|---|
| `UT` (0x5455) | modified, accessed, changed — whole seconds | modified, accessed; **changed is always 0** |
| `ux` (0x7875) | the file's real uid / gid (the app's own uid) | **0 / 0 for every file** |
| Unix mode (`external_attr`) | real permissions (`0600`, `0660`, …) | file type only, **no permission bits** |
| `IN` (0x4E49) | inode (8 bytes LE) + device number (4 bytes LE) | — |
| `S2` (0x3253) | SHA-256 of the content | — |
| `GK` (0x4B47) | 2 bytes, `0100` on every sample | — |

`device_fs.from_zip_entry(info, extended=True)` reads all of it, and the Android extraction:

* **checks every extracted file against the archive's own SHA-256** (`S2`) as it writes it, and says
  in the log how many matched — a chain-of-custody line per file (`archive_sha256_matches` in the
  manifest);
* leaves out a time of **0** (not recorded, not 1970) and a mode with no permission bits;
* drops the owner when **every** file of the app records uid 0 / gid 0 — an Android app's private
  files belong to the app's own uid, so an archive that says root for all of them recorded a
  placeholder, not the device's owner.

`IN` is read as inode + device because, in the GrayKey archive, the mirrors of `/data` carry the same
pair as the canonical path, and each partition carries a device number of its own (`/data`,
`/system` and `/vendor` differ). There is no birth time in either Android archive.

`extraction_manifest.json` (`"platform": "android"`) records, per canonical folder, the archive paths
it was read from and the mount points the same files were also seen at, then the per-file device
records (`mtimes`, `fs`), the renamed files, and the duplicate / symlink / hash counts.

## The reports

The Android run (`scripts/ParseSnapchat_Android.py`) builds the Conversations, Contacts, Memories and
cache_controller reports into the same folders, with the same cross-report links, as the iOS run.
There is no Library/Caches report — Android has no such folder, and the app's own
`files/file_manager` caches are not yet listed as a whole anywhere (the Memories report finds a
snap's files in them, the legacy report its chat snaps) — and no partial reports yet (`--selection`
is refused).

### Conversations — `arroyo.db`

Parsed by the iOS functions unchanged (`getChats`, `fixSenders`, `mergeCacheChats`; see
[report_communications.md](report_communications.md)): message text from `4.4.2.1` / `4.4.7.11.1`,
events described from `4.4.8`, messages whose media is no longer cached kept and labelled, both
readings of the database. A conversation's title comes from `feed_entry.conversation_title` and its
type from `feed_entry.conversation_type`; there is no friends plist on Android, so a private
conversation is named after its first non-owner sender.

Chat media is attached through the same join the iOS parser uses (`getCacheArroyo` / `mergeCacheChats`
against `cache_controller.db`'s `CACHE_FILE_CLAIM`), which keys on the conversation and message ids a
claim's `EXTERNAL_KEY` carries (`<type>:<conversation>:<message>:<part>`). A claim whose key does not
carry them attaches nothing — its file is still listed in the cache_controller report, and no message
is given a file it cannot be tied to. Everything the run publishes has been shown to be real media
(see below), so the join can miss an attachment but never invent one.

The shared join takes a claim's file only when a **whole** file named after the `CACHE_KEY` is media.
The same cache also stores media as byte-range shards and as bundles (a small descriptor named after
the key, the content in `<key>_<child>` files); on iOS a chat video stored that way reaches the report
through `SCPersistentMedia`, which Android does not have. So the Android run rebuilds them first
(`materialize_chat_media`): shards are concatenated in offset order, a bundle gives its largest media
child, and — for a file that is encrypted at rest — decrypted with a key / IV pair carried in the
message's own protobuf (`message_key_pairs`: every 32-byte value beside every 16-byte value, at any
depth, tried against the file). **Only bytes that decode to media by their magic bytes are ever
kept**, so a wrong key, a wrong shard set or a wrong child produces nothing. The attachment's "?" says
which was done, and its link still goes to the key's own cache_controller row, where every shard or
child is listed with its hashes.

### Contacts — `main.db` `Friend`

Every row of `Friend` (`userId`, `username`, `displayName`), with `CombinedUsername` for the username
pair: when `mutableUsername` is set and differs from `originalUsername`, the mutable one is shown as
the username and the original as the legacy username. The table is **not the friends list** — it is
every user the app has to show (people who added the account, people it added, group members,
suggestions) — so the report carries that warning, and each row's detail states what the table itself
records about the link, as stored:

* `addedTimestamp` / `reverseAddedTimestamp` (Unix ms), `friendLinkType` and `syncSource` as stored
  numbers, the phone number, birthday, streak, and the official / popular / brand flags;
* which other friend table lists the user (`FriendWhoAddedMe`, `SuggestedFriend`, `BestFriend`,
  `ContactFriend`);
* **the column's own comment**: SQLite keeps each table's `CREATE TABLE` statement verbatim in
  `sqlite_master`, comments included, so where the app wrote one beside a column
  (`schema_comments`), the report quotes it as what the database itself says about that column.

The numbers stored in `friendLinkType` and `syncSource` are shown as stored, without a meaning
attached.

The device owner is the `arroyo.db` `required_values` row `USERID` (the same record the iOS parser
falls back to), else the first `shared_prefs` value whose key is a user id. The owner's row carries an
account block, each value labelled with its file and key and shown as stored: every
`required_values` row; `shared_prefs/identity_persistent_store.xml` and `LoginSignupStore.xml` whole
(the two ALEAPP reports, with its three Unix-millisecond timestamp keys formatted); and from every
other preference file the values whose key names a user id, username, display name, phone, e-mail or
birthday.

### Memories — `memories.db`

See `scripts/memories_android_report.py`. One row per `memories_snap` row, with its Memory
(`memories_entry`) and media object (`memories_media`):

* times: `create_time` and `snap_capture_time` (Unix ms) in the report's timezone, `time_zone_id` as
  stored;
* location: `latitude` / `longitude` as stored, in degrees — **no keychain is needed**;
* My Eyes Only: `memories_entry.is_private`; such a snap's key is stored wrapped
  (`encrypted_media_key` / `encrypted_media_iv`), and `memories_meo_confidential` (the bcrypt hash of
  the passcode, and the master key and IV in base64) is shown as stored at the top of the report. No
  passcode is needed to unwrap the keys while that row exists (below);
* `memories_entry.snap_ids` / `highlighted_snap_ids` are FlatBuffers documents — a root table whose
  slot 0 is a vector of strings — and are shown read (`flatbuffers_doc.string_vector_field`, which
  follows every offset: with more than one id the strings are not at fixed positions, so reading "the
  text at byte 0x20" only works for a one-element vector);
* media — a cached file is tied to the snap by something of the snap's own, and every route is named
  on the file's row (*Role / how it was linked*):

  | route | the file |
  |---|---|
  | MD5 of a request string | in the app's cache folders `files/file_manager/<type>/` (`memories_media`, `memories_thumbnail`, `memories_overlay`, …), a name beginning with the MD5, upper-case hex, of `<media_id>.media`, `<snap_id>.thumbnail` or `<snap_id>.overlay` — recompute it to check |
  | named after the snap | in those folders, a name carrying the snap's `_id` or `media_id` |
  | claim | `cache_controller.db`: an `EXTERNAL_KEY` carrying the snap's `_id` or `media_id` (**any** UUID in the key is indexed, not only the iOS prefixes) |
  | url token | a `CACHE_KEY` equal to the SHA-256 of a download URL's token |

  Each file is read as it is, then decrypted with, in order: the snap's `media_key` / `media_iv`
  (base64, AES-256-CBC with PKCS#7); for a My Eyes Only snap, `encrypted_media_key` /
  `encrypted_media_iv` unwrapped with the master key and IV `memories_meo_confidential` stores
  (base64; each wrapped value is AES-256-CBC under them, and must unwrap to valid padding and exactly
  32 and 16 bytes); and every key / IV pair (a 32-byte field 1 beside a 16-byte field 2) inside the
  snap's `snapdoc` protobuf. **Only bytes that are media by their magic bytes are accepted**, so a
  wrong key, a wrong file or a wrong route can never publish anything; the State column names the key
  that opened the file. A file that is not media but is a thumbnail package — a little-endian int32
  count, that many int32 sizes, then the images, with every byte accounted for — is split into its
  images.

Each route proves itself on the file it finds: nothing is published that did not come out as media.
The app's own Memories search index, `databases/clientsearch.db` (FTS4 tables of captions, titles,
place names, visual and time tags), is not read yet (TODO.md).

The cache_controller report links a cache entry to the Memory by the same routes, naming the Android
columns (`memories_snap._id`, `memories_snap.media_id`) in its explanation.

### cache_controller — `databases/native_content_manager/cache_controller.db`

The iOS report unchanged apart from where it looks (`cache_controller_paths`, `index_sccontent`,
`find_app_container` all recognise an Android app folder) and the words a few explanations use
(`PLATFORM_WORDS`). Device paths are shown as `/data/data/com.snapchat.android/…`.

### Communications (legacy)

`scripts/getCacheAndroid.py`, the original single-page report: `core.db` `DataConsumption` joined to
`files/file_manager/chat_snap` and `…/snap` and to `arroyo.db`. Produced only when the legacy reports
are asked for, like the iOS ones, and only when all three databases are present.

## The layout survey — `android_survey.json`

Beside the run log, every Android run writes the app folder's **structure**, with no content: each
database's tables with their columns and row counts, each preferences file's key names and value
types (never the values), and the folder tree to three levels with file counts and sizes (never a file
name). Every UUID in a name is replaced by `<uuid>`. It shows which tables, columns, preference keys
and folders the app version on the device has — including any the parser does not read — without any
of the evidence, so it can leave the case when the parser has to follow a new app version.

## Related work: ALEAPP's Snapchat module

[ALEAPP](https://github.com/abrignoni/ALEAPP) (Alexis Brignoni and contributors, MIT licence) has an
Android Snapchat module, `scripts/artifacts/snapchat.py`, whose sample data covers Android 13–16
images. It reads `main.db` `Friend` (as the friends list, keeping rows with an `addedTimestamp`),
`memories.db` (`memories_entry`, `memories_snap` joined to `memories_media`, and
`memories_meo_confidential`, recovering the four-digit passcode from its bcrypt hash), and the key /
value pairs of `shared_prefs/identity_persistent_store.xml` and `LoginSignupStore.xml`. This tool
reads the same tables for the Contacts and Memories reports and shows those two preference files
whole on the owner's row, reading the same three keys as Unix milliseconds (credited in
`ParseSnapchat_Android` and in that block's "?"). Where it parts ways: it lists every `Friend` row
rather than filtering on `addedTimestamp`, and states per row what the table records instead; it
reads `memories_entry.snap_ids` by following the FlatBuffers offsets rather than at fixed byte
positions; and it does not brute-force the My Eyes Only passcode — the master key the same table
stores unwraps the keys without it.
