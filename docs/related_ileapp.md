# Related work: iLEAPP's Snapchat module

[iLEAPP](https://github.com/abrignoni/iLEAPP) (Alexis Brignoni and contributors, MIT licence)
gained an iOS Snapchat module in 2026 —
[`scripts/artifacts/snapchat.py`](https://github.com/abrignoni/iLEAPP/blob/587a3d9c74699d3c38293e0e17064ea3677990be/scripts/artifacts/snapchat.py)
(the commit this note was written against). Its artifacts are *Messages (arroyo.db)*, *Conversations
(arroyo.db)*, *Friends*, *Memories Search Index*, *Account*, *Chat Media*, *Memories* and *Memories
Media*. Its Memories artifact cites this project's
[Memories decryption notes](snapchat_ios_memories_decryption.md) for the gallery database, the
wrapped MEO key and the cache_controller claims; this note is the other direction — what this tool
took from iLEAPP, and where the two disagree on purpose. Its outputs were compared with this tool's
on every Snapchat-bearing extraction in the test corpus.

**Where they agree.** Messages and conversations match row for row on every device. Geolocated
Memories match. Memory rows differ only by what each tool adds to a normal read (below).

## Adopted from iLEAPP

Each is credited in the module that implements it and in the report popover that explains it.

### The Memories search index — `gallery_search/<n>/<userHash>/search.sqlite3`

An index this tool did not extract or read. It is plain SQLite (FTS4) and needs **no keychain**,
which is what makes it valuable: on a device whose keychain dump is backup-class (no `egocipher`),
the coordinates in `gallery.encrypteddb` stay locked and this index is then the only record of
*where* a Memory was taken that a report can give. Per snap id:

| table | column(s) | holds |
|---|---|---|
| `snap_id_table` | `snap_id`, `language_id`, `tag_version` | one row per indexed snap; its `rowid` is the FTS `docid` |
| `snap_tag_table_content` | `c0time_tag`, `c1location_tag`, `c2visual_tag`, `c3meta_tag` | comma-separated tags: time words (`2023,February,Tuesday,afternoon,winter`), **place names down to street and postal code**, visual labels, `Image` / `Video` |
| `snap_description_table_content` | `c0caption` | the caption text |
| `snap_time_tag_table` | `time_tag` | a **local calendar date**, `YYYY-MM-DD`, no zone |
| `snap_location_tag_cluster_table`, `snap_visual_tag_cluster_table` | `cluster_name` | the app's clusters |
| `snap_visual_tag_conf_table` | `concept`, `conf` | one row per (snap, label) with the stored confidence |
| `snap_tag_synced`, `snap_geofilter_id_table` | | present on some app versions only |

The join `snap_tag_table_content.docid = snap_id_table.rowid` is iLEAPP's, verified there by matching
each row's visual tags against `snap_visual_tag_conf_table`; it held on every index in the corpus.
Everything in the index is **app-generated and reported as stored**: the place names are the app's
reverse geocoding, the date has no zone, the confidences are the app's. None of it is an observation
about the media file, and that is what the report's popover says.

Two things iLEAPP's notes do not cover, seen in the corpus: the index covers a *subset* of the
Memories (on the newest devices, one profile's subset), so most rows carry no tags; and on the newer
files a third of the indexed snaps exist **only with the `-wal` applied**, so the file is read twice
here like every other database (`scripts/data/sqlite_open.py`), and a WAL-only row is marked.

Implemented by `scripts/gallery_search.py`; joined onto the Memories report by snap id, shown on the
detail page, in the index row's collapsed block, in the timestamps list (as a string — a naive date is
never turned into an instant), and as a fourth **Geolocation** state, *place name only (search
index)*, for a Memory with no coordinates. Extracted by `scripts/data/extract_zip.py`
(`Documents/gallery_search`) and fingerprinted as `gallery_search` (`scripts/source_fingerprint.py`).

### Key rows with no Memory row

iLEAPP lists a `gallery.encrypteddb` `snap_key_iv` row whose snap id has no `ZGALLERYSNAP` row in
either reading of `scdb-27.sqlite3` as a *Recovered* Memory ("Key row with no Memory row"), with the
coordinates and address title the gallery database still holds for it. This tool kept such rows only
to adopt their key into the Memory that references them (`adopt_media_object_keys`) and dropped
their location rows. It now lists them too — `orphan_key_memories` in
`scripts/memories_media_report.py`, badged **RECOVERED**, with a `-wal` filter option of their own.

The rule for *which* rows differs, deliberately. iLEAPP skips a key row whose key pair equals a
listed Memory's; so does this tool — one media object, one row — and that rule also covers a key a
Memory adopted, since it then *is* that Memory's key. But a key row a listed Memory merely
**references** (`ZMEDIAID` / `ZDUPLICATEDFROMSNAPID`) under a *different* key is listed here and
names the referencing Memory: a differently keyed object is a different record. On the old-schema
device it was the My Eyes Only original (wrapped key, no media, but coordinates and an address) of a
duplicate that stayed in the gallery. Conversely, a key row that a Memory adopted is **not** listed
again, so this tool's count is one lower than iLEAPP's on that device: iLEAPP's extra row is the
media object's original snap id of a Memory this tool already lists under its current (MEO) id, with
the same coordinates. Both ids are searchable and each page names the other.

### The `snapchatter` document's layout (FlatBuffers)

Every `p` column in `primary.docobjects` is a FlatBuffers document. iLEAPP established the
`snapchatter` root table's string slots — 0 the user id, 1 / 14 / 15 the username, mutable username
and legacy username (equal to the store's own index tables), 2 the display name — and reads slot 2
only when slot 0 equals the row's `userId` column. `scripts/data/flatbuffers_doc.py` reads the root
table the same way, behind the same self-check, and applies it to `snapchatters__displaymetadata`
too (slot 0 the user id, slot 1 the display name), replacing a fixed byte-offset carve. The
self-check is the whole guarantee: a document of another shape yields nothing rather than a wrong
name.

### `user.plist` read by key (TSAF)

`Documents/user.plist` is a Snap TSAF container, not a plist. iLEAPP reads `username`, `user_id`,
`laguna_id` and the `client_encryption` identifier / key / IV by key, accepting the ids only when
UUID-shaped. `scripts/data/tsaf.py` does the same and serves `ClientEncryptionService.plist` with the
same reader; `getUserID` now takes the keyed `user_id` before the "first UUID in the file" regex it
always used, and the owner's row in the Contacts report shows the values.

One rule was added: a value must **immediately follow** its key. A signed-out account's `user.plist`
keeps the `username` / `user_id` / `laguna_id` keys with an empty value — the key, a lone `0x00`, the
next key — and "the next string after the key" then reports the *following key's name* as the
username (iLEAPP's Account artifact shows `username = user_id` on such a file).

A second thing worth passing on, since both tools now report these values: the `client_encryption`
key in `user.plist` is **not** the `ClientEncryptionService.plist` key that opens the story cache,
and it opens nothing else either. Every block-aligned file of all four test extractions was tested
against it by the padding of its last block — which identifies a CBC key whatever the IV or framing
— with no store matching. It is an identifier and key material belonging to the account record;
[snapchat_ios_cache_media.md](snapchat_ios_cache_media.md) has the method and the numbers.

### Three username fields

iLEAPP shows `index_snapchatterusername`, `index_snapchattermutableUsername` and
`index_snapchatterlegacyUsername`. This tool read the first and the last; it now carries all three
on every contact, each with its source table, and flags a row whose mutable username differs from
its username. In the corpus the two were equal on every row of every device — only the legacy
username records a rename — so what "mutable" adds is not established, and the report says so.

## The "Friends" trap — `snapchatter` is not the friends list

iLEAPP's *Snapchat - Friends* artifact is every row of `primary.docobjects` → `snapchatter`. That
table is the app's cache of **every Snapchatter record it has had to render**. On the old-schema
test device the account's own friends list (`app_group_plist_storage` → `snapchatter_repository`)
accounted for a handful of the rows, one more was Snapchat's own account (in the friends feed, not
in the friends list), and every other row was named in a `snapchatters__displaysuggestion` page —
Quick Add / "people you may know" suggestions the app had shown, i.e. strangers. Reported as
friends, that would tie a subject to a hundred people they never interacted with.

How to tell them apart, from the store itself:

* the friends list is what `app_group_plist_storage` (or `group.snapchat.picaboo.plist`) records —
  the Contacts report's source, named in its banner;
* `snapchatters__displaymetadata` holds display metadata for the friends list plus the official
  accounts the app converses with, on the devices tested — close to the friends list, but not it,
  which is why the Contacts report treats it as a fallback that MIGHT contain non-friends;
* the documents differ in shape — a friend's `snapchatter` document carries field slot 9, a
  suggestion's slot 11 (a fingerprint, not relied on);
* a suggestion's user id appears, as text, inside a `snapchatters__displaysuggestion` page's
  document.

The Contacts report now lists the non-friend rows **apart** — a collapsed table with no anchors, no
selection and no place in a partial report — and says why the app cached each: *Quick Add suggestion*
when a `displaysuggestion` page names the id (the one meaning verified), otherwise the docobjects
table(s) whose document names it, as stored, or "no table names it". See
[report_contacts.md](report_contacts.md).

## Where the two tools part ways

Design differences, not defects, listed so a reader of both outputs is not surprised:

* **WAL frames.** iLEAPP reads each database twice (with and without its log) and reports rows the
  log removed; it does not parse WAL frames, and its notes say so. This tool additionally carves
  superseded frames (`docs/sqlite_wal_handling.md`) — the Memories it recovers that way are the rows
  iLEAPP lacks on the newer devices, badged CARVED and proved by decryption.
* **One media object, one row** — the adopted-key rule above.
* **Chat media.** iLEAPP shows a cached chat file when its bytes are a recognised format and reports
  the rest as unrecognised; this tool decrypts what it has a key for and identifies the rest by
  magic bytes (`scripts/data/sniff.py`).
* **`ZGALLERYSNAPMINITHUMBNAIL`.** iLEAPP decrypts these rows with the snap's key. Not adopted: on
  the corpus they are 64–112-byte WebP placeholders, several per snap.
* **Scope.** The `Library/Caches` report, partial reports, the device filesystem records and the
  offline maps have no counterpart in iLEAPP; iLEAPP's per-artifact `sample_data` notes and LAVA
  output have none here.
