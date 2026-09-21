# Memories media report

`scripts/memories_media_report.py` → `Reports/Memories/Memories_report.html`.

Recovers every Snapchat **Memory** and links it to all of its recovered media (full-resolution
stills, videos, preview frames), geolocation and per-snap metadata, across both storage schemas
and multiple user profiles.

The **decryption** mechanics — where the AES key/IV live (new vs old schema), the SQLCipher
`gallery.encrypteddb`, My Eyes Only unwrapping, geolocation, and when the keychain is required —
are documented in depth in [snapchat_ios_memories_decryption.md](snapchat_ios_memories_decryption.md).
**This page focuses on how the report links a Memory to its media files**, which is what the “?”
icon next to each media file explains in the report itself.

## The keychain banner

When no `egocipher` was recovered, the index carries a red banner. Its first sentence is the
`detail` from `read_keychain_status` (`scripts/DecryptLocalMemories_iOS.py`), so it names the
actual cause — no keychain supplied, path not found, unparseable dump, parsed but no `egocipher`,
or no Snapchat items — rather than the single catch-all message it used to show. The same causes
are logged at INFO/WARNING during the run, and can be checked on their own with
`--diag-keychain` (see [Diagnosing a keychain](snapchat_ios_memories_decryption.md#diagnosing-a-keychain)).

Note what the banner does **not** imply: on the new schema, My Eyes Only memories carry their
key/IV in `scdb` and decrypt with no keychain at all, so MEO media appearing in the report says
nothing about whether the keychain was read. **Geolocation is the reliable tell** — it always
requires `egocipher`.

## How each media file is located and linked

`collect_media` gathers candidate cache files three ways. Each recovered file records a `how`
string (shown by its “?” icon):

1. **SCContent by CDN URL** — `CACHE_KEY = SHA-256(token)[:16 bytes]`, where `token` is the last
   path segment of `ZMEDIADOWNLOADURL` / `ZOVERLAYDOWNLOADURL` / `ZTHUMBNAILDOWNLOADURL`. Decrypt
   with the snap's AES-256-CBC key/IV.
2. **SCContent by `cache_controller.db`** — a `CACHE_FILE_CLAIM.EXTERNAL_KEY` names the Memory and
   points at `CACHE_KEY`. Essential for **locally-captured media** (e.g. device-recorded videos)
   whose `ZGALLERYSNAP` URL fields are empty. `classify_snap_claim` decides which shapes count —
   including `<snapid>_memories_backup_transcoded`, which puts the UUID *first* — and the lookup
   uses the Memory's `ZSNAPID` **and** its `media_refs` (`ZMEDIAID` / `ZDUPLICATEDFROMSNAPID`),
   since a claim can name the media object instead of the snap. Both reports read that one list;
   see [cross_report_linking.md](cross_report_linking.md#which-external_key-shapes-name-a-memory).
3. **caching-media `.pack` by decrypt-and-match** — pack names are opaque, so each folder is tried
   against every Memory's key/IV; the key that yields valid media magic bytes (after the 8-byte
   header) identifies the Memory. **Not** referenced by `cache_controller.db`.

In cases 1–2 a file may be a single `<CACHE_KEY>` or split into `<CACHE_KEY>_<start>-<end>` parts
that are concatenated in offset order before decryption (`_resolve_sccontent`); the `how` text
notes the reconstruction. If a video has no cached still, a **poster frame** is derived from the
decrypted `.mp4` and clearly labelled as a derived artifact.

### A Memory with no key is still searched (cases 1–2)

Locating a cache file needs no key — only *reading an encrypted one* does, and not every cache is
encrypted. So Memories with no usable key (My Eyes Only whose `persistedkey` is missing) go through
the same addressing, and `decrypt_sccontent` identifies plaintext from the magic bytes before it
ever looks at a key: exactly the caches stored in the clear are recovered, and nothing else. Such a
file's `how` says *"the cached bytes are stored in the clear: no key was needed, and none was
used"* rather than claiming a decryption that did not happen, and the locked-MEO notice in the
Encryption block stops saying the media "may still be present on disk" once it is.

Gating this on `m["key"]` — as it was — meant a Memory could show **no media and no 🗄 cache link**
while the cache_controller report reconstructed and played the very same bytes from the very same
shards. To keep the keyless scan cheap, `_sccontent_head` types the file from its first 16 bytes,
so a keyless Memory never costs a full read and concatenation of a file that cannot be used; a
shard set with no offset-0 shard reads as *unknown* (skipped), never as *not media*.

Case 3 has no keyless equivalent: a `.pack` is linked to its Memory **by** decrypting it, so
without a key there is nothing to match on.

Case 3 identifies the owning key with a **32-byte probe** (`pack_matches`) rather than decrypting
the whole item once per candidate key. Every acceptance test in `decrypt_pack` reads within the
first 24 plaintext bytes and CBC decrypts a prefix independently of the rest, so the probe reaches
the same verdict — but it is the difference between minutes and hours on a gallery with tens of
thousands of Memories, since a folder is tried against every key until one matches.

## Partially cached media — the file is genuine but not the whole media
The cache holds only the byte ranges the device actually **streamed**, so recovered media is
routinely incomplete. This is not a decryption failure: the key is right and the bytes present are
genuine. It matters to the examiner because an incomplete video plays for a few seconds and stops,
or breaks up part way through, and nothing about the file itself says why. Three checks classify
every recovered file as complete / incomplete / not verified:

* **Missing tail** — SCContent media is AES-256-CBC with **PKCS#7** padding, so a complete file
  always ends in valid padding. Decrypted bytes that do not are truncated (`_has_pkcs7`); a random
  final block only looks like valid padding about 1 time in 255.
* **Holes between shards** — `_part_coverage` walks the `<start>-<end>` parts in offset order and
  measures each one's **actual size on disk** (so it holds whichever end convention the name uses),
  reporting every gap. A gap matters more than a short tail: concatenating across it leaves every
  later byte at the wrong offset, so a decoder reads impossible atom/NAL sizes rather than simply
  stopping. The declared range is used only to notice a shard shorter than its own name claims.
* **Short packs** — `decrypt_pack` returns the payload length the pack header declares; fewer bytes
  than that means `-<n>.pack` chunks were evicted or never downloaded.

A ciphertext whose length is not a block multiple is a partial cache, **not** a dead loss: the
block-aligned prefix is decrypted and kept rather than the file being discarded.

In the report, an incomplete file gets a red **⚠ incomplete — partially cached** badge in its
*Source cache* cell whose “?” states exactly what is missing (byte offsets, counts), its row is
tinted, and the detail page carries a banner above the media table. The index shows a **PART**
chip on the affected Memory and counts them in the header. Files stored as plaintext are marked
*completeness not verified* — there is no padding to check and no shard layout to measure, so
claiming either way would be a guess.

### The index filter says which of four states, and how many are in it

The index filter used to be **Media: incomplete only / complete only** over a flag that was simply
*any file incomplete*. Two things were wrong with it, and both were invisible from the report:

* **"complete only" was not a claim the tool could make.** It swallowed the *completeness not
  verified* files described above — the very distinction the file-level badge is careful about —
  and it also swallowed Memories with **no recovered media at all**, which is not a statement about
  completeness in either direction. Every corpus device had Memories filed under "complete" that
  nothing had checked.
* **"incomplete only" returned nothing on a device with no incomplete media**, which is
  indistinguishable from a filter that does not work.

So the control is **Recovered media**, one state per Memory (`_media_state`), each option carrying
its count and disabled when it is empty (`_media_filter_options`):

| option | means |
|---|---|
| partially cached (incomplete) | at least one recovered file stops short of the real media |
| verified complete | every recovered file was checked and nothing says bytes are missing |
| completeness not verified | plaintext storage — no padding to check, no shard layout to measure |
| no media recovered | nothing was decrypted or found; the metadata row is still evidence the Memory existed |

### Geolocation is four states, not with/without

**Geolocation** filters the index on `_geo_state`, which the index cell (`_geo_compact`) reads too —
one function, so the filter and the cell cannot disagree about what a Memory has:

| option | means |
|---|---|
| coordinates recovered | a latitude/longitude came out of `snap_location_table` in the gallery database |
| place name only (search index) | no coordinates, but the app's own search index names a place for this Memory — its reverse geocoding, as stored |
| on the device, none recovered | `ZGALLERYSNAP.ZHASLOCATION` says the app recorded a location, but no coordinates were read for it |
| no location | the app recorded none |

The two middle states are the reason this is not a two-way filter. Geolocation lives in the
**encrypted** gallery database, so a run without the FFS keychain recovers none of it — and folding
those Memories into "no location" would report *this tool's* gap as a fact about the device. Told
apart, the same rows say something useful: there is a location here, and it is still to be had from
the extraction. The options carry their counts and grey out when empty (`report_ui.counted_options`),
so "coordinates recovered — 0" answers "why did that return nothing?" on the face of the control.

A **carved** Memory has no `ZGALLERYSNAP` row at all, so it can only read "no location" or the search
index's place: what that row would have said went with the row, rather than never having been on the
device.

### The app's search index — place names and a local date without the keychain

`Documents/gallery_search/<n>/<userHash>/search.sqlite3` is the app's own FTS index over Memories,
plain SQLite, **no keychain involved**. Per snap id it holds a local calendar date, time words
(`afternoon`, `winter`), the app's reverse geocoding **down to street and postal code**, a place
cluster, `Image` / `Video`, the caption and visual concept labels with their confidence.
`scripts/gallery_search.py` reads it (both WAL views — on the newer devices a third of the rows are
WAL-only) and `index()` joins the record onto each Memory by snap id as `m["search"]`. It is shown:

* on the detail page, in its own section, every value as stored;
* in the index row's collapsed block and in the search tokens, so "Rue …" or a caption finds the row;
* in the timestamps list as *Search index date* — a string, never an instant, because the date
  states no zone;
* as the fourth Geolocation state above, and in the geolocation cell when there are no coordinates.

Everything in it is app-generated and the popover (`gallery_search.SEARCH_INDEX_BASIS`) says so: it
is what the app decided about the Memory, not an observation about the media. On the AFU test device
(backup-class keychain, no `egocipher`) it is the only location the report can give. A snap the
index knows but no reading of `scdb-27` lists becomes a RECOVERED row (*search index row with no
Memory row*) — no corpus device has one, so that path is covered by tests only. Adopted from iLEAPP;
see [related_ileapp.md](related_ileapp.md).

### Recovered rows: key rows with no Memory row

`gallery.encrypteddb` can hold a `snap_key_iv` row — and a `snap_location_table` /
`snap_address_title` row — for a snap id that has no `ZGALLERYSNAP` row in either reading of
`scdb-27`: a Memory the app no longer lists. `orphan_key_memories` turns each such row into a
**RECOVERED** Memory (badge, `-wal` filter option *no Memory row (key row survives)*, header count),
with its key (unwrapped when this account's persistedkey is at hand, else locked), coordinates and
address, and empty value panels — the detail page says why they are empty (`ORPHAN_KEY_BASIS`).
Skipped: a key row whose pair a listed Memory already holds (the same media object under another id;
this covers the keys `adopt_media_object_keys` gives a MEO Memory). Listed, and naming the referrer:
a row a listed Memory references through `ZMEDIAID` / `ZDUPLICATEDFROMSNAPID` under a *different*
key — on the old-schema device, the My Eyes Only original of a duplicate that stayed in the gallery.
`ZDUPLICATEDFROMSNAPID` is in the index's search tokens, so either id finds the other. Adopted from
iLEAPP's "Key row with no Memory row"; the difference in rules is in
[related_ileapp.md](related_ileapp.md).

Poster frames are still extracted from partial video: what the cache holds starts at the beginning
of the file, so the opening frames decode. For those files `generate_poster` skips the seek (a seek
into missing bytes fails and costs a full re-read) and takes the first frame that decodes, bounded
by `_POSTER_MAX_READS`. FFmpeg's decoder complaints (`Invalid NAL unit size`, `Error splitting the
input into NAL units`) are silenced by `_quiet_stderr`, which redirects **fd 2** — the
`OPENCV_FFMPEG_*` environment variables do not help, as the capture options reach only the demuxer
while those messages come from the decoder context.

## Layout — a lightweight index + per-group detail sub-pages
To keep the report usable with many Memories, it is split (`generate_report`):

* **`Memories_report.html`** — a lightweight, **sortable/filterable index table** (global search,
  per-column sort, a with/without-thumbnail filter, a user filter, a recovered-media filter, a
  geolocation filter, an embedded-metadata filter and a time window).
  One **row per Memory (snap)**
  with: thumbnail, kind, user, `ZSNAPID` / `ZENTRYID` / `ZMEDIAID`, cache-file tokens, the media
  **MD5 / SHA-256**, created time, geolocation, and a link to the detail sub-page. Each row carries
  `id="mem-<ZSNAPID>"` (the anchor other reports link to).
  The table is **virtualized** — rows live in `data/index.js` and only the visible ones are put in
  the DOM, so the index opens instantly however many Memories there are, and search/sort still
  cover all of them. Row cells are one fixed height (the full values are on the detail page; the
  cache-token cell shows the first two and counts the rest). It also carries the **pager** and the
  **selection** controls, and a **My Eyes Only** filter. Keep the `data/` and `media/` folders next
  to the HTML file. See [report_ui.md](report_ui.md).
  The search text behind each row also carries what the row has no column for: the Memory's
  **CDN URLs** (media / overlay / thumbnail, download and redirect), so a full or partial URL —
  pasted from `scdb-27`, from a detail page, or from the cache_controller report — finds its
  Memory; its **AES-256 key and IV in hex**, so a key seen in another tool finds the Memory it
  decrypts; and the **fields found inside its media files** (camera make and model, software,
  GPS, the file's own timestamps — see below). All of these are listed in the row's expanded area,
  behind *CDN URLs, AES key / IV, embedded metadata — also matched by Search*, which is where to
  confirm what a search hit on. The block is a collapsed `<details>` because six URLs are taller
  than the row; opening it tells the virtual table to re-measure (`SCV.remeasure`).
* **`pages/<key>.html`** — one **detail sub-page per group**, holding the full detail (metadata,
  location, per-snap AES key/IV, ZGALLERYSNAP/ZGALLERYENTRY values, CDN URLs, timestamp tables, and
  the media-files table with hashes/paths and the 🗄 cache-entry links). MEDIA ID and SNAP IDs are
  shown prominently. Each member also carries an `id="mem-<ZSNAPID>"` anchor, and the page links
  back to the index.

### A group is one row, with its members inside it

A group of Memories is shown as **one row** — its earliest member — with the rest rendered in that
row's expanded area, each with its own `ZSNAPID`, timestamps, selection box and Details button
(`pages/<key>.html#mem-<ZSNAPID>`, which the sub-page already answers). A group exists precisely
because its members are the same media object under several snap rows, so three rows for it read as
three findings.

Every Memory still has **its own row** in `data/index.js`, with its own anchor, its own
`mem-<ZSNAPID>` selection id and every cross-report link into it untouched — that is what makes this
cheap. The fold is a way of *drawing* those rows, implemented in the shared table
(`report_ui`: `C.folded`, `foldHit`, `openFoldHits`); see
[report_ui.md](report_ui.md#folded-rows-cfolded-scvfoldhits-openfoldhits) for the four rules that keep
it honest — a group is found by any member's values, a lead reached through a member is opened on that
member, "Select all shown" ticks the members the filters match and no others, and Clear all filters
unfolds. **Fold groups** in the toolbar turns it off, giving every Memory a row of its own again; the
cost, and the reason the fold is the default, is that sorting then scatters a group's members to
wherever their own values put them.

The expansion is not only for groups: **every** row's expanded area lists that Memory's timestamps,
each with where it was read from, which is where the time filter's matches can be seen (the index has
room for one time column, and the filter searches them all).

**A lead row carries two checkboxes.** The first is that row's own Memory — the same tick as its detail
page, and what the selection file records. The second stands for the whole group: filled when every
Memory grouped in the row is selected, a squared-off mark when only some are, empty when none are, and
clicking it selects or clears all of them. So "is this group selected?" is answered without expanding
the row. It appears only where there is a group and only while the fold is on. See
[report_ui.md](report_ui.md#the-groups-own-selection-box-cgroupbox-scvselectgroup) for why the two are
separate controls rather than one; the short version is that the first box's id *is* the Memory's
selection id, and one box writing several ids would stop it reporting its own row's state.

Note the deliberate difference from **Select all shown**, which follows the filters: it ticks the
Memories that match, which may be some members of a group and not the rest — which is exactly the state
the group box's middle mark exists to show.

### Every timestamp says where it came from

Three different things record a time for one Memory, and they need not agree: the app's database, the
media file's own header, and the device's filesystem. The report keeps them apart and **tags every
value with its source** rather than folding them into one column (`_memory_times` returns
`(label, value, source)`; the row's expanded area draws the source as a third, muted column, and the
`?` beside *Timestamps* — `TIME_SOURCES_HINT` — explains the tags):

| Tag | What it is | Clock |
|---|---|---|
| `scdb-27 › ZGALLERYSNAP.<col>` / `ZGALLERYENTRY.<col>` | the app's record — every `*TIME*`/`*DATE*` column of the snap row and of the entry/album row it belongs to | Cocoa seconds (since 2001-01-01 UTC), converted to the run's timezone |
| `inside <file>` | written **into** the recovered media by whatever produced it: EXIF `DateTime*`, XMP `CreateDate`, PNG `Creation Time`, an MP4's `mvhd` creation/modification time, a QuickTime `creationdate` (`©day` / `com.apple.quicktime.creationdate`), the EXIF GPS stamp | converted to the run's timezone when the file **states** its zone (EXIF `OffsetTime*`, an ISO 8601 offset); marked *UTC assumed* where only the format defines the field as UTC (`mvhd`, the GPS stamp); otherwise shown *as written* and tagged *no timezone in the file* — a wall clock on the writing device's clock, never guessed into an instant |
| `extraction archive › <path>` | what the **device's filesystem** recorded about the cache file — created (birth), modified, accessed, inode changed — from a UFED archive's `metadata.msgpack` or the ZIP entry's `UT` field via `extraction_manifest.json` (see [snapchat_ios_cache_media.md](snapchat_ios_cache_media.md#the-devices-whole-record-of-the-file)); never the extracted copy's own times, which are when *we* unzipped it | UTC, converted to the run's timezone at the source's precision (nanoseconds from UFED, seconds from `UT`); identical instants on one line, a split file's parts bounded; *accessed* and *inode changed* can be the acquisition's; *not recorded* when the archive carried none |

The index's **Created** column header carries a `?` naming its field (`ZGALLERYSNAP.ZCREATETIMEUTC`).
On the detail sub-page each source is shown **once, where it belongs**: the two database tables say
which store and encoding they came from; a file's own timestamps sit under that file's *Embedded
metadata* block as a small table (field, the value *as written*, the value in the report's timezone —
or *not converted* / *UTC assumed* — and the reason), not repeated anywhere else; and the device's
record of the cache file — its four timestamps, protection class, inode, owner — sits on the line of the
path it belongs to, in the *Media files* table's source-path cell (a file rebuilt from byte-range parts
has a record per part, each timestamp bounded as earliest … latest). In the index row every recorded
timestamp is a line — *created*, *modified / accessed* when they coincide, *inode changed* — bounded
the same way, so the row stays a row.

**Zones are never assumed silently.** A file time is converted to the run's timezone only when the
file *states* its zone (EXIF `OffsetTime*`, an ISO 8601 offset). Where the file states none but the
format defines the field as UTC — an `mvhd` time, the EXIF GPS stamp — the conversion is shown with
*UTC assumed* beside it, because encoders (Apple's among them) have written local time there. Where
nothing is known the value is shown as written. `media_meta` records this as the time's `basis`
(`stated` / `format` / none); `report_ui.file_time_rows` turns it into the caveat.

Two things are excluded on purpose. A **poster frame** this tool generated is never read: it is ours,
not evidence, and its encoder's stamps would be this run's. And `gallery.encrypteddb` rows (keys,
coordinates) carry no time of their own; they are dated only by the snap row they belong to.

### Embedded metadata — what the file says about itself

`scripts/data/media_meta.py` reads the metadata **inside** each recovered file, once per distinct
content, at the point every published file passes through (`_save_media` → `stub["meta"]`): EXIF and
XMP in a JPEG or WebP, text chunks in a PNG, the `mvhd` header and QuickTime user data (`©xyz`
location, `©mak`, `keys`/`ilst` items) in an ISO base media file. Only Pillow and the standard library
are used; HEIF/HEIC is **not** read in this build (no HEIF codec), and the page says so rather than
reporting "no metadata". The reader never raises — a truncated or hostile header costs that file its
block, not the report.

It is shown three ways. On the detail sub-page, **Embedded metadata — inside the media files** sits
directly under the id band, next to the thumbnail: per file, the container and pixel size, the fields
that identify a device or place (`KEY_TAGS`: make, model, software, lens, orientation, serials,
`KEY_QT` for QuickTime), the GPS fix as an OpenStreetMap link (labelled as *the file's own fix, not
the app's*), the file's own timestamps as a table, and everything else the file holds behind **all
fields — N more**. The renderer is `report_ui.embedded_meta_html`, shared with the two cache reports,
so the same file reads the same way wherever the examiner meets it. A file with nothing inside says so — *none — the file carries no EXIF, XMP or dated
header* — because "no EXIF" is itself a finding. On the index, an **EXIF** chip in the Kind column and
an **Embedded metadata** filter (*with* / *none found*, counted), and the fields in the row's expanded
area and its search text.

Expect *none found* to be the common state: Snapchat's servers re-encode media, so a cached file
usually carries nothing. When one does carry EXIF it most often came from the camera roll, and then its
make, model and GPS fix describe the device that took the picture — not necessarily this one. The
hints say so (`report_ui.EMBEDDED_BASIS`, `_META_FILTER_HINT`). Pixel size and orientation alone do
not make a file *notable* (`media_meta.STRUCTURAL`): every encoder writes those, and on a real device
three quarters of the cached JPEGs carry exactly that set and nothing else — a chip on all of them
would say nothing. The **EXIF** chip, the filter and the search token follow `notable`; the block on
the detail page shows whatever is there.

### Finding a Memory by time

The toolbar's **Time** control (shared — see
[report_ui.md](report_ui.md#the-datetime-window-report_uitime_filter-time_js)) filters on **every**
timestamp a Memory carries: the capture time in the index column, every `ZGALLERYSNAP` and
`ZGALLERYENTRY` time column, the timestamps inside its media files and the cache files' device
mtimes, which are otherwise only in the row's expanded area. Either a range
(*between*) or a tolerance (*within ± N minutes/hours/days of*); a Memory matches when any one of its
times falls in the window, and expanding the row shows which. A Memory inside a folded group is found
by its own times too, and the group opens with the matching member highlighted.

Two consequences to know. The keys come from the displayed strings, so the window means the time **as
the report shows it**, in the run's timezone — and a file time whose zone the file did not state is
compared *as written*, which the row's source tag says. And a Memory with no readable timestamp at all
— a **carved** Memory has no `ZGALLERYSNAP` row, and if its media carries no dated header and the
archive recorded no mtime it has nothing — is hidden while a window is set: it cannot be shown to fall
inside one. Its detail says so in place of the timestamps, and clearing the filter brings it back. A
carved Memory whose media *does* carry an `mvhd` time or whose cache file has a recorded mtime is
findable by those, with the expansion stating that no database time exists.

### My Eyes Only
A Memory in Snapchat's private, separately-encrypted album is marked with a red **MEO** badge in the
index's Kind column (`m["is_meo"]`, set from `IS_ENCRYPTED` / the MEO key path — see
[snapchat_ios_memories_decryption.md](snapchat_ios_memories_decryption.md)), matches a search for
"meo" / "my eyes only", and can be isolated with the **My Eyes Only** filter (any / only / exclude).
The detail page keeps its existing "My Eyes Only" label next to the kind.

A MEO Memory is **not** automatically undecryptable. One that was *moved* into My Eyes Only points
at the media object it came from (`ZMEDIAID` / `ZDUPLICATEDFROMSNAPID`), whose `snap_key_iv` row
survives with `encrypted=0` although its `ZGALLERYSNAP` row does not — so its cached media decrypts
with no `persistedkey` at all. `adopt_media_object_keys` gives such a Memory that key (only an
unwrapped 32/16 pair, only from an id the row actually references), records it in
`m["key_source"]`, and the Encryption block labels it as the media object's key rather than the
snap's — as does each media file's "?" basis. The mechanism, and its limits, are in
[snapchat_ios_memories_decryption.md](snapchat_ios_memories_decryption.md#a-memory-moved-into-my-eyes-only-keeps-its-original-key).
`stats["meo_from_media_object"]` counts these separately from `meo_unwrapped`, so the keychain is
never credited for a key it did not supply.

When such a Memory's key is still **wrapped** (`m["key_wrapped"]` — no usable key was produced), the
detail page's Encryption block says so, and `_meo_locked_html` distinguishes the three reasons
rather than always asserting the last: the supplied keychain holds **no**
`com.snapchat.keyservice.persistedkey` item at all, it holds one but for **another** account, or it
holds **this** account's and the unwrap still failed. `meo_owners` (the userIds the keychain carries
a persistedkey for, `None` when the item's payload named no userId) is threaded from `main` for
this; "we cannot tell which account" must never reach the examiner as "there is none".

The account is named by its **userId**, with the `userHash` beside it (`_account_label`). Naming it
by the hash alone — as the notice once did — reads as a *different* account: the hash is
`sha256(userId)` and appears nowhere else the examiner looks (`ZSAVERUSERID`, the keychain items,
the index's user filter and the `user …` line under each Snap ID all use the userId), so a correct
statement about the signed-in account looked like a statement about some third one.

### Selecting memories for the case
Each index row and each memory block on a detail sub-page carries the **same checkbox** — both
pages load `Reports/selection.js`, so they always agree — plus **Selected only** filtering and a
**💾 Save selections** button. Why the selection has to be saved to a file (and cannot simply live
in the browser) is explained in [report_ui.md](report_ui.md#selecting-rows--and-where-a-file-report-can-keep-them).

### Offline maps
When the examiner configures an **offline map tile server** in the GUI (or passes `tile_server=` to
`main`), `render_maps` renders a small static map for every geolocated Memory and the detail page
shows it under the location, with a link that opens the tile server centred on the same
coordinates. Implementation: [`scripts/offline_maps.py`](../scripts/offline_maps.py).

* Nothing is fetched when no server is configured — the reports never reach the network on their own,
  and the server is one the examiner runs.
* Tiles are cached in memory and memories at the same coordinates share one image (on the test set:
  22 geolocated memories → 3 images, 27 tile requests).
* The image is a **derived artifact** and is labelled as such: the imagery is the examiner's tile
  server's, only the marker position comes from `gallery.encrypteddb`'s `snap_location_table`. The
  caption's "?" records the server, the zoom and how many tiles were stitched.
* A server that stops answering degrades gracefully: the report is still produced, with a warning in
  the log and a note of the missing tiles.

### Two-level grouping (`assign_groups`, union-find)
1. **ZMEDIAID** — memories referencing the same media object.
2. **Identical media bytes** — memories whose recovered media share a **non-zero-byte MD5**,
   matched **across user accounts** (zero-byte files excluded, since they would all collide).

Both relations are unioned (connected components), so "same bytes, different ZMEDIAID" — even on two
different accounts — land on one sub-page. Group key = a short stable hash of the member snap ids.

### One copy per distinct content in `media/`

A group exists precisely because its members are the same media under another snap row, so two snaps
of a group routinely recover the very same bytes from the very same cache file. Those bytes are
published **once**: `_save_media` keeps a per-run `{md5: file}` map and returns the file already
written instead of writing a second copy, so every Memory that recovered the content links to one
file. The same applies to poster frames — one poster per distinct video, not per Memory, so a shared
video is decoded once rather than once per snap.

Consequences worth knowing before reading a folder listing or a `media_by_cache_key.json`:

* The published name embeds the snap id of the Memory the file was written for **first**. That is a
  name, not a statement of ownership. What says which Memories a file belongs to is the group page's
  file table, which now names every Memory that recovered each row (with that Memory's own role, when
  it differs from the row's — the same bytes can be one snap's full media and another's thumbnail).
* Only the copy on disk is shared. Each Memory keeps its own entry with its own role, cache key,
  source paths and basis, so `media_by_cache_key.json` still holds one record per cache key per snap;
  several of them simply name the same `path`.
* The first writer's name wins, so the name depends on the order Memories are walked in — which is
  `sqlite_open.read_table`'s over `ZGALLERYSNAP`, fixed for a given database. Group keys and index
  row order already depend on that same order.
* **Zero-byte files are never de-duplicated**, matching `assign_groups`' own exclusion: every empty
  file has the same MD5, so collapsing them would point one group's page at a file first published
  for a Memory in another group. Excluding them keeps the invariant that a shared file is always
  shared *within* one group — which holds because byte-identity is itself one of the two grouping
  relations, so content-identical Memories are in the same group by construction.

This is also what removed the last unreferenced files from the folder: the file table lists one row
per distinct content, so before this the equivalent copies belonging to the group's other members
were published and linked by no page at all.

### Manifests for cross-report links
`generate_report` writes two files the cache_controller report reads (it runs after Memories):

* **`memory_pages.json`** (`snap_id → pages/<key>.html`) — so a memory-linked cache entry can offer
  **both** an index-row link and a direct detail-page link.
* **`media_by_cache_key.json`** (`CACHE_KEY → [{path, role, ext, bytes, snap_id, md5, sha256}]`) —
  every media file decrypted *from a cache key*. Memory media is cached **encrypted**, so the
  cache_controller report cannot display those bytes; this manifest lets it link to the plaintext
  copy recovered here (labelled as derived, with both files' hashes side by side) instead of
  showing an unopenable blob. `.pack` files are excluded — they have no cache key.

See [cross_report_linking.md](cross_report_linking.md).

## Cross-scope on-disk copies
Each recovered media file's source paths are grouped by the account `SCContent_<userId>` scope
they physically live in. When a copy sits in a **different account's scope** than the Memory's
owner account (`map_userids` maps the owner `userHash` → `userId`), the file is flagged with a
⚠ "cross-scope copy" badge and a "?" explaining it — typically an untracked/materialized duplicate
(e.g. a consolidated copy in the active account's cache). Ownership is unchanged; the flag mirrors
the same treatment in the cache_controller report. See
[report_cache_controller.md](report_cache_controller.md#coverage-caveats-does-every-sccontent-file-have-a-claim).

## Link to the cache_controller report
For each recovered media file whose `CACHE_KEY` is present in `cache_controller.db`
(`all_cache_keys`), the file's "Source cache" cell shows a 🗄 link to
`../CacheController/CacheController_report.html#ck-<CACHE_KEY>`. `.pack` files (not indexed there)
get no such link. See [cross_report_linking.md](cross_report_linking.md).

## Standalone use
```
python -m scripts.memories_media_report <extraction_root_or_app_container> [keychain.plist] \
    [outdir] [--padding both|strip|keep] [--tz local|utc|<IANA name>|<±HH:MM>]
```
