# cache_controller.db report

`scripts/cache_controller_report.py` → `Reports/CacheController/CacheController_report.html`.

`Documents/global_scoped/cachecontroller/cache_controller.db` is Snapchat's index of **every file
it has cached on the device** — Memory media, chat attachments, lens bundles, Discover/preview
imagery, app-install thumbnails, and more. This report surfaces that index and, crucially, links
each entry to the actual bytes on disk and to the other Snapchat Auto reports. For the anchor
scheme and the exact link rules, see [cross_report_linking.md](cross_report_linking.md).

## Report unit: one physical cache file (`CACHE_KEY`)

`CACHE_KEY` is **not** unique in `CACHE_FILE_CLAIM` — one physical file can carry several *claims*
(e.g. `W7_…` and `video~W7_…`, or a CDN-URL claim plus a `g-media-<snapid>` claim). The report
therefore groups by `CACHE_KEY`: **one row per physical file**, aggregating all of its claims. This
also yields exactly one `#ck-<CACHE_KEY>` anchor per file.

`CACHE_KEY` is also the **on-disk filename** in `Documents/com.snap.file_manager_*_SCContent_*/`.

## Index columns — the same order as the Library/Caches report

Both reports describe the same kind of thing from two sides, and laid it out differently, so moving
between them meant re-orienting every time. They now share one order — widths differ where the
content does:

| | cache_controller | Cached media (Library/Caches) |
|---|---|---|
| ▸ | expand | expand |
| Category | | |
| identity | `CACHE_KEY` | path under `Library/Caches` |
| secondary | `EXTERNAL_KEY` | producer |
| context | user | copies |
| Type / Size | | |
| File | the bytes, previewed | the bytes, previewed |
| Links | | |

A collapsed row is a **fixed height** (`CC_ROW_H` / `CM_ROW_H`) — the virtual table computes every
scroll offset from it. So a line of content that does not fit is **cut through the middle** rather
than dropped, and the cell shows a row of half-height glyphs that reads as a broken report. Every
index cell therefore has to be built to a known number of lines:

* The Category cell holds badges (`-wal only`, `changed`) and their "?" icons, on one line of their
  own, in a column wide enough for them — so the cell is never more than two lines. A "?" sliced in
  half is what prompted that.
* The **Links** cell is a `.chiprow`: `display:flex` with `flex-wrap:nowrap`, so the chips stay on
  one line and anything past the cell edge is clipped horizontally instead of wrapping into a
  second, half-visible line. It is also the `compact` form — chips only, no "?" explanations, which
  are far too long for a row. The expanded row repeats every chip *with* its explanation and wraps
  freely, so nothing is lost. In `cache_media_report.py` the `compact` argument was accepted and
  never used, so its index rendered the full detail form and wrapped on any row with more than two
  links; that is the bug this describes.

Do **not** put `mask-image`, `filter` or `transform` on `.chiprow` to fade the clipped edge. Each of
those makes the element a containing block for `position:fixed` descendants, and the "?" popover is
`position:fixed` precisely so that it escapes the cell's `overflow:hidden` (see
[report_ui.md](report_ui.md)). The fade would come back as clipped popovers.

## Poster frames for cached video

A play button says *this is a video*; it does not say **which** video, so a page of cached video
told the examiner nothing. Every published video gets a still (`publish_posters`, shared with the
Library/Caches report) written as `files/<name>_poster.jpg` and shown in the File cell and the
expanded row. It is **derived data** — this tool's own frame, not anything from the device — and
says so wherever it appears (`POSTER_BASIS`). Posters left by an earlier run into the same folder
are reused.

Two properties of a *cache* make this different from postering a normal video file, and both were
found on the test corpus:

* **Cached video is routinely truncated** (the cache holds the byte ranges the device streamed), so
  the frame is taken by reading forward from the start rather than by seeking to ~1 s: seeking into
  bytes that are not there fails *and* costs a full re-read of the file.
* **Roughly one cached video in six cannot be decoded at all, and does not fail — it blocks the
  decoder for good.** Measured on case extractions: 72 of 630 and 137 of 590. Nothing in the file
  predicts it (the hanging ones have a valid `moov` atom and are truncated no more than the ones
  that decode fine), so the work has to be *stoppable*: it runs in a subprocess that is killed if it
  stops answering. [scripts/data/poster_worker.py](../scripts/data/poster_worker.py) says why a
  thread cannot do this job — read it before simplifying it into one.

Both bounds are wall-clock and are enforced by killing the worker: `FILE_TIMEOUT_S` per video and
`BUDGET_S` for the pass. Raising the per-file bound costs time on files that were never going to
yield a frame and does not change the yield — on a 630-video case extraction, a 1 s bound and a 3 s
one both produced 558 posters. What the budget did not reach is reported, not silently dropped.

A video that ends up with no frame therefore sits next to videos that have one, showing only its
`▶ view cached file` link — which reads as a defect in the report rather than as the finding it is.
Each such entry now carries a `poster_note` saying which of **three** things happened, rendered
under the link in the expanded row. The index row is unaffected: with no poster it falls back to
the plain `▶ <ext>` play button, which still opens the bytes that are there.

1. the file has no video track at all — settled before the worker is ever asked;
2. the file was decoded and did not yield a frame, typically because only part of it was cached;
3. **the file was never attempted** — the worker could not be started, or the pass hit `BUDGET_S`.

The third case has to be kept apart from the second, and `run_jobs` is what keeps them apart: a
video it attempted is in its result (`True`/`False`), a video it never opened is *absent*. "This
video did not decode" is a finding about the evidence, and a run in which nothing was decoded
established no such thing. It was written into thousands of rows once already — see below.

### The build in which no poster was ever extracted

`_worker_command` decided between `python -m scripts.data.poster_worker` and re-entering the
application with `--poster-worker` by looking at `sys.executable`. That cannot work, because a
Nuitka **standalone** build (what the MSI installs, and what onefile unpacks and runs) sets
`sys.executable` to the basename of the *build machine's* interpreter joined onto the installation
directory — `getStandaloneSysExecutablePath` in Nuitka's `CompiledCodeHelpers.c`. Built from a `uv`
venv, every installed copy therefore reported `<install dir>\python.exe`: a file that has never
existed there. So the packaged app took the interpreter branch *and* aimed the spawn at nothing,
`Popen` raised `[WinError 2]`, and all three poster passes — Memories, Library/Caches and
cache_controller — returned zero posters in well under a second. `sys.argv[0]` is the real binary;
`__compiled__` (which Nuitka pre-seeds into every compiled module) is the marker that the app is a
build at all. Neither `sys.frozen` nor `sys._MEIPASS` is set, which is what sent the original test
to `sys.executable` in the first place.

That file also exposed a mislabel worth stating on its own: an ISO base media file's magic bytes
(`....ftyp`) say only that it *is* one. Its **major brand** says what is in it, and
`sniff.guess_media` now reads it — so `M4A `/`M4B ` is `m4a`, `qt  ` is `mov`, `heic`/`avif` are
still images, and only the generic brands (`isom`, `mp42`, `iso*`, `avc1`, `dash`) stay `mp4`.
Reporting a voice note as a video is a claim about content, and it was a false one. The set of
bytes accepted **as media** is unchanged, so the Memories decrypt-and-match linker accepts exactly
what it always did — only the extension it is given changed.

## Tables used

Columns are read **dynamically** (`SELECT *` + `cursor.description`), because they differ between
app versions (e.g. the 2023 tombstone has no `FETCH_PRIORITY_V2`).

| Table | Role in the report |
|---|---|
| `CACHE_FILE_CLAIM` | the semantic claim(s): `EXTERNAL_KEY` (what it is), `MEDIA_CONTEXT_TYPE`, `USER_ID`, and create / expire / delete timestamps (Unix epoch **ms**). |
| `CACHE_FILE_METADATA` | the physical file: `FILE_SIZE_BYTES`, `TYPE` (1 file / 2 sharded / 3 bundle), `STORAGE_TYPE`, `SHARD_INDEX`, `KNOWN_CONTENT_LENGTH_BYTES`, `LAST_READ_TIMESTAMP_MILLIS`, and two protobuf blobs (below). Joined to the claim by `CACHE_KEY`. |
| `CACHE_FILE_SAMPLED_TOMBSTONE` | a sample of files Snapchat has already deleted (`DELETION_REASON`, `BYTES_DELETED`, `DELETED_TIMESTAMP_MILLIS`). Folded into the matching entry, or shown as a "Deleted (tombstone)" entry when no claim remains. |
| `CACHE_KEY_VIRTUALIZATION` | a `VIRTUAL_CACHE_KEY` ↔ `CACHE_KEY` map. **Empty in every extraction seen so far**, so its semantics are *unconfirmed*; the report lists any rows verbatim in a clearly-labelled section and builds **no** linking logic on it. |

### `CACHE_FILE_METADATA.CHILDREN` (protobuf)
Decoded by `parse_children`. Field `1` is one child or a list; each child is
`{1: name, 2: {1: size, 2: {1: offset}}}`. Two shapes seen:

* **sharded file** (`TYPE=2`): names are byte ranges — `94208-693856`, `PREFETCH`. On disk these
  are stored as `<CACHE_KEY>_<start>-<end>` and `<CACHE_KEY>_PREFETCH` (the same split media
  `parseSnapvideos` reconstructs). What the set of shards does and does not say about the user is
  the next section.
* **bundle** (`TYPE=3`): names are child cache keys (often with a leading marker byte, e.g.
  `z<hex>`) plus a filename such as `lar_lens_notifications_geofences_v6.json`.

### What a shard set says — prefetched head vs. streamed body

Both child names are written by **Snapchat's own file manager**, so each shard file is a record of
*how the bytes arrived*: one persisted fetch per file, the child's `offset` saying where it belongs
in the reconstructed stream. Which shards exist is therefore evidence about the transfer, and it is
routinely over-read as evidence about the user.

* **`PREFETCH`** is the child at **offset 0** — the head of the file, fetched speculatively because
  the client held the item's metadata and anticipated it might be played. `_SC_SPLIT_RE` in the
  Memories report parses it as start 0 and dedupes it against a `0-N` shard at the same offset.
  Its presence does **not** mean the item was opened, played or viewed.
* **`<start>-<end>`** is a range the downloader/player actually pulled — progressive playback, a
  seek, or a resumed download. That these are demand-driven rather than a planned chunking is
  visible in the data: `_part_coverage` exists because shards on a real device **start past 0 and
  leave holes**, the device having kept only the ranges it streamed.

What may be concluded from a shard set, strongest claim first:

| On disk | What it supports |
|---|---|
| `PREFETCH` + contiguous ranges reaching `KNOWN_CONTENT_LENGTH_BYTES` | the whole content was transferred to the device |
| ranges starting past 0, or with gaps | partial streaming — and the concatenation is byte-correct but offset-wrong, which is why a decoder reports impossible NAL/atom sizes rather than simply refusing the file |
| `PREFETCH` alone, small | the head was fetched speculatively; the content was never fully transferred |

**Does the user have to have been using the app?** Two separate questions. The Snapchat process must
have been running — nothing in iOS writes these files, only the app's file manager does, over the
network. But interaction with *that item* is not required, and "running" covers states that are not
deliberate use: foreground on an unrelated screen, background refresh, a push-triggered fetch,
resumption from the app switcher. Neither timestamp closes that gap on its own:
`CACHE_FILE_CLAIM.CREATION_TIMESTAMP_MILLIS` is when the claim was registered and
`CACHE_FILE_METADATA.LAST_READ_TIMESTAMP_MILLIS` when the entry was last read back. The device's own
record of each shard file (*created* vs *modified*, from `extraction_manifest.json`) bounds the first
and last chunk writes independently of anything the app recorded, which is why the report shows it
per path and as earliest…latest across a split file.

The trigger conditions above are read from the app's own naming and from the observed shard layout;
the names, offsets and coverage behaviour are verified, the prefetch trigger set is not
instrumented. Say which of the two a report is relying on.

> **Trap:** the legacy `scripts/parseSnapvideos_PREFETCH.py` **renames `PREFETCH` → `_0-1` inside the
> extraction tree**, so a `_0-1` shard at offset 0 in a tree an earlier Snapchat Auto run touched may
> be ours and not the device's. Re-extracting reverts those names; a baseline must come from a
> freshly extracted tree. See the removal note in `TODO.md`.

### `CACHE_FILE_METADATA.CONTENT_RETRIEVAL_METADATA` (protobuf)
Decoded by `parse_retrieval`. Field `5.1`/`6.1` = the **CDN URL** the file was fetched from.
Field `8` is a **content reference whose form varies** by app version / media kind, so the report
inspects the value rather than assuming a type:

* most often a **CDN media token** (the same token after `/d/` in the URL, sometimes with a `.NNN`
  suffix) — e.g. `S8fDoGrkeolX01yylQtsf`;
* a **64-hex content SHA-256** on newer app versions (only ~13% of entries on the iOS 26 device);
* the **32-hex `CACHE_KEY`** on the iOS 16 device.

Even when field 8 **is** a 64-hex hash it is a **source-/server-side content hash that does NOT
necessarily match the bytes actually cached on disk** — verified on an `app_install_screenshot`
entry whose field 8 matched neither the cached file's real SHA-256 nor the downloaded bytes. The
report therefore:

* labels field 8 by its real column name (`CONTENT_RETRIEVAL_METADATA field 8`) with a value-type
  hint ("source content hash (SHA-256; may differ from cached bytes)" / "CDN media token" /
  "equals CACHE_KEY") and a "?" spelling out the caveat, and
* separately computes and shows the **actual cached file's MD5/SHA-256** (see below) so the bytes
  on disk always have a trustworthy hash.

### The bytes actually on disk (hash + view)
`materialize_ondisk` **streams** each on-disk entry's logical bytes (a whole `<CACHE_KEY>` file, or
its byte-range parts in order) to compute the **cached file's real MD5/SHA-256** — chunked, so any
size is safe. When the bytes are recognizable **plaintext** media (magic bytes) `publish_view` makes
the file openable as `files/<CACHE_KEY>.<ext>` so it can be opened **even when it links to no Memory
or conversation** (e.g. an "App install" screenshot):

* a **whole** file → a **hard link** to the original extracted file. Same bytes, no copy, and —
  unlike the previous "link in place to the extensionless original" — the published name ends in
  the real extension. Browsers handle an extensionless `file://` link inconsistently (Chrome
  downloads it, `<video>` refuses it), which is why every viewable file now gets a real extension.
  A real copy is made only when the filesystem refuses to link (different volume, no hard-link
  support);
* a **split** file (byte-range parts) → concatenated into `files/<CACHE_KEY>.<ext>` (the only way to
  view it as one file) when ≤ 1 GB; larger split files are hashed and noted.

The note next to each viewer states exactly which of these happened. Encrypted cache bytes are still
hashed (as stored) but never published — see *Encrypted and bundled files* below. All field labels
use the **real DB column names with the description in parentheses**.

Two more things about the bytes themselves, both new and both shared with the Memories and
Library/Caches reports so a file reads the same way in all three:

* **Embedded metadata** — every published file (and every published bundle child) is read for what
  it says about *itself* (`scripts/data/media_meta.py`: EXIF/XMP in a JPEG or WebP, PNG text; in an
  MP4/MOV the movie and track headers, QuickTime user data, `moov › meta` and XMP) and shown through
  `report_ui.embedded_meta_html`: key fields first, the Snapchat app's tag when the file carries one
  (for a received snap it names the **sender's** app, device model and OS, and the lens used — see
  [snapchat_media_tag.md](snapchat_media_tag.md)), the file's own timestamps as a table (*as written* /
  *in this report's timezone* / note), the source files of an edit (collapsed, and never the file's
  own times), the rest behind *all fields*. A timestamp is converted only when the file **states** its
  zone; one the format merely defines as UTC (`mvhd`) is converted and marked *UTC assumed*, since
  encoders have written local time there; one with no zone is left as written. The fields, the tag's
  user agent and lens id, and the source files' names join the row's search text. Most cached files
  carry nothing beyond pixel size, and the block says so.
* **The device's record of the file** — under every on-disk path: created (birth), modified, accessed
  and inode-changed times, protection class, inode, mode and owner, from `extraction_manifest.json`
  (a UFED archive's `metadata.msgpack` at nanosecond precision, else the entry's `UT` field — which
  in a UFED archive holds the access time only; see
  [snapchat_ios_cache_media.md](snapchat_ios_cache_media.md#the-devices-whole-record-of-the-file)).
  Never the extracted copy's own times, which are when *we* unzipped it — and not a claim time
  either: `CREATION_TIMESTAMP_MILLIS` is when the app registered the claim, *modified* is when the
  bytes were last written. Parts of a split file are bounded (earliest … latest); *not recorded*
  where the archive carried none. The mtime is searchable.

### Bundles: the child files are the content
For a bundle (`TYPE = 3`) the file named after the `CACHE_KEY` is **only the CHILDREN descriptor**
(a few dozen bytes), and the content sits in one file per child, named
`<CACHE_KEY>_<child name>` (e.g. `<CACHE_KEY>_z<child id>`). `child_ondisk_paths` resolves those (the
child's own cache key is also tried, for other layouts), and each child is hashed and typed
**separately** and published with its own extension. The row's file button shows the bundle's
largest recognizable child.

This is what makes a chat video viewable: such a video is a bundle whose children are the `.mp4`
and its `.webp` overlay — neither of which was reachable when only the descriptor was hashed (its
"detected type" then read as *not recognized*, which now says explicitly that a bundle's parent file
is a descriptor).

### Encrypted and bundled files — what the examiner sees
Every on-disk entry ends up in exactly one of these states, stated plainly in the row and the
detail:

| State | Shown as |
|---|---|
| plaintext media on disk | thumbnail / ▶ button opening `files/<key>.<ext>` |
| encrypted, but decrypted by the Memories report | 🔓 button opening the decrypted copy in `../Memories/media/…`, with the decrypted file's own MD5/SHA-256, its Memory's `ZSNAPID`, and a "?" explaining it is a **derived** file |
| a bundle | the child table (type + size + hashes + viewer per child) |
| 0 bytes on disk | "0 bytes" — the index entry exists but no content was stored/captured |
| a format this report cannot render | its **name** — "lens bundle (LZC)", "font", "WEBVTT subtitles", "binary plist", "zip archive", "text / JSON", "HTML", "protobuf" — plus a "?" saying it is not encrypted, just not displayable inline |
| still encrypted | 🔒 encrypted (no key available for it) |

### "Encrypted" is measured, not assumed

Until v1.5.0 this report identified files with `guess_media` alone — JPEG/MP4/PNG/WebP — and
labelled **everything else** "🔒 encrypted". Across the four test devices that padlock sat on 600
files of which only 19 were encrypted; the rest were 480 LZC lens bundles, 27 protobuf blobs, 10
WEBVTT subtitle tracks, 9 ZIP archives, 9 JSON/text files, 4 HTML pages, 4 TrueType fonts and 2
binary plists. Telling an examiner that readable evidence is locked away is worse than saying
nothing, and it buried the handful of files that really were locked.

Identification now goes through `scripts/data/sniff.py`, shared with the cached-media report.
"Encrypted" requires **both** high Shannon entropy (≥ 7.5 bits/byte over the first 8 KB) **and** a
length that is a multiple of the AES block size — the fingerprint of block-cipher output. High
entropy without block alignment is reported as exactly that (typically a partially cached
download), and anything unidentified is called "unrecognized", not encrypted. The header states
how many entries hold encrypted bytes, how many of those the Memories report can open, and how
many have no key at all; a filter selects each group.

## Categorisation

`classify_external_key` buckets each claim from its `EXTERNAL_KEY` (and `MEDIA_CONTEXT_TYPE` as a
tie-breaker): *Memory media / overlay / thumbnail* (`snap-*`/`g-media-`), *Chat media* (context
2/3), *Lens*, *Preview*, *App install*, *Video / Discover* (`topvideo~`/`firstframe`/`video~`),
*CDN media* (a bare `http(s)` URL), *Snap editor* (context 34 with a `<UUID>~<position>` key — below),
else *Other*. The row's category is the most meaningful across its claims (Memory beats Other). A file
that is an asset of a filter a Memory's overlay record lists keeps its category — *CDN media*, as a
URL-keyed claim — because that link is a relation of its own, not a statement of what the file is (see
[below](#assets-of-a-filter-listed-with-a-memory--not-its-media)).

One category comes from another database: *Creative tools asset*, a file an item of an account's
creative-tools store names ([below](#creative-tools-items--primarydocobjects--ctp__item_5)). It takes
the place of *CDN media*, *Other* or *Chat media* only — never of a category the key's shape gives
(*Lens*, *Snap editor*, a Memory's) — and only when no chat message and no Memory links to the file:
those say what the file is. A filter listing does not hold it back, being a relation and not what the
file is. The claims table keeps each claim's own category and context label (`2 (Chat media)`, a
reading of the number): the contexts labelled *Chat media* claim creative-tools assets — custom stickers
among them — as well as chat media, and on such a row a "?" beside the context says so. The
category is applied after the key's (`build_entries`), not in `classify_external_key`, so the key-based
categories are what they were.

### Context 34 — the snap editor's working copy

Claims with `MEDIA_CONTEXT_TYPE` 34 are keyed `<UUID>~<position>` and claim the files of a snap being
edited — usually plaintext media written at capture, so the report plays them. The name rests on the
device's own record, not on the number: `Documents/user_scoped/<hash>/userPreferences/pref.docobjects`
(SQLite, `docprefitem(rowid, p BLOB, key STRING UNIQUE)`) keeps the editor's current snap under the key
`SnapEditor-SnapSessionContext`. Its `p` cell is a TSAF container (root type `SESnapSessionContext`)
whose `GPBData` key is followed by two little-endian 32-bit words, the second the length of a protobuf
that follows:

| field | value |
|---|---|
| `1` | when the record was saved — Unix seconds |
| `2.2.4` (one per media item) | `.6` the item's position (1, 2, …), `.10` its **CACHE_KEY** |
| `2.2.17.7` | when the snap was edited — Unix milliseconds |
| `2.5.1`, `2.5.2` | the UUID and context of the claims on those files: `<UUID>~<position>`, 34 |

Verified on a test device (iOS 18.3, app 13.4x) against `cache_controller.db` and the files. Only the
latest session is a live row (an ended session is often emptied); earlier ones survive in `-wal` frames
a later write superseded. `scripts/data/snap_session.py` reads both readings and carves those frames,
keeping a carved record **only** when a claim corroborates it (same CACHE_KEY, claim key and context).
A record found for a row's file is shown under its claims: saved, edited, claim key, context, store and
how it was read.

The record dates an editing session and ties it to the file; it does not say what became of the snap.
A working copy saved to Memories is byte-identical to that Memory's media once decrypted (seen on a test
device), but nothing recorded on that device connects the two. The bytes do: when the Memory's media is
on the device too, the file links to it *by content* (≡, rule 5 in
[cross_report_linking.md](cross_report_linking.md)); when it is not, the retrieval from Snapchat's
servers can supply the reference (☁), and until then the file is a lead.

### Creative-tools items — primary.docobjects › ctp__item_5

Each account keeps a store at `Documents/user_scoped/<userHash>/DocObjects/primary.docobjects` (SQLite;
`userHash` is SHA-256 of the account's user id — the contacts' store too). Beside the contacts it holds
the items of the feeds the camera's creative tools are filled from — captions, filters, stickers — in
`ctp__item_5(rowid, p BLOB, item_id STRING UNIQUE)`, one row per item. `scripts/data/ctp_items.py` reads
it, every account's store, both readings, staged in a temporary folder (never beside the report: it would
put the contacts' store into the report folder). `p` is a FlatBuffers document; its root table:

| slot | value |
|---|---|
| 0 | the row's `item_id` again — the self-check: a document whose slot 0 is not its item_id is not read |
| 1 | a rank text, equal to `index_ctp__item_5rank_id.rank_id` of the row — not read |
| 2 | a `[ubyte]` vector holding a protobuf message: the item's **payload** |
| 3 | the item's own id: standard padded base64 of payload field 6 |
| 4 | a sub-table whose slot 0 is the item's feed, `feed:<TYPE>-<CONTEXT>-<n>` |
| 5–9 | small integers, a digit text and a short base64 value many items share — not read |

On the stores examined the `item_id` is `<own id>-<feed>-<n>`; it is read from its column, never rebuilt.
The payload parses as a protobuf to its last byte. Its field 2 holds exactly one field, whose number is
the item's **kind** as stored — `2.11` on the captions feed's items, `2.16` on the filters feed's, `2.7`
and `2.15` on two feeds of other contexts — field 6 is the own id as bytes (8 bytes on most items, 13 on
one kind), field 4 the same id as an integer when it is 8 bytes. Its texts are style and font names,
colours, a JSON text and the URLs of the item's assets: a font file and a caption asset under `2.11`, a
filter image (`2.16.2.1.1`), its CDN copy (`2.16.2.1.2`), a filter font (`2.16.7.1.5.1`) and a geofilter
PNG (`2.16.15.3.3`) under `2.16`, CDN assets under `2.7.10.1` / `2.7.11.1` and `2.15.1.3.4.1` / `.2`. They
are shown as stored, under their field numbers, which are numbers and not names. A field 2 that is not a
message of one field (a number, a text) gives no kind, and the payload's texts are still read. No item in
this layout carries a date, and none names a snap (verified on the stores examined); a document of another
layout is not read, so what it holds is not known.

The feed is named from the store's **feed tree**, `ctp__feedtree(rowid, p BLOB, context INTEGER UNIQUE)`:
per context a FlatBuffers document whose slot 2 is an NSKeyedArchiver archive (`keyed_archive`) of a
`CTPFeed` — `FEED_ID` (`TYPE`, `CONTEXT`), `NAME`, `SOURCE.COMPUTE_ENDPOINT` (the
`/snapchat.creativetools.<service>.ComputeFeedService/ComputeFeed` it is fetched from) and `CHILD_FEEDS`
(slot 0 is the context, slot 1 a Cocoa time). A feed's short name is the word after `.creativetools.` in
its endpoint (`captions`, `filters`, `custom-stickers`, …), else its `NAME`. Only context 2 has a tree on
the devices examined, so items of `feed:19-1-0` or `feed:16-6-0` read *not in ctp__feedtree* — their
numbers are never read as a name. Both readings of the tree are read: the current one names a feed, and a
feed only the checkpointed version lists is named as prior state — *(prior state)* beside the name, and
the "?" says the `-wal`'s tree does not list it. A tree document that does not hold a `CTPFeed` archive is
not read; a feed missing from the trees that were read then reads *ctp__feedtree not decoded*, never *not
in ctp__feedtree*, since whether the unread tree lists it is not known — and the run log counts such
documents per store.

**The match** (`ctp_items.match`) is an exact identity of whole texts, in this order, the first rule that
finds an item deciding:

1. **the whole key** is a text the item holds — a URL in the form `snap_overlay.normalise_url` gives it,
   the one URL rule the reports share (so a key with an empty trailing `?` is the item's URL), anything
   else as it is; the item_id and the own id are texts the item holds too;
2. **the key after a word and `:` or `~`** (`music:<url>`, `customSticker:<id>`, `customSticker~<id>`;
   never `://`, which is a URL) is such a text — a payload text, the item_id or the own id;
3. failing those, that part of the key **read as base64 is the same bytes** as the item's own id —
   payload field 6, or the item_id or slot 3 read as base64 — in either alphabet, padded or not, when
   that part reads as base64 by the rule `--trace-ids` reads ids by (`base64_text.base64_bytes`: padded
   with `=`, or using `+` or `/`, or mixing upper case, lower case and digits). An unpadded id with none
   of these is not read as base64 and matches by its text only.

Never matched: a URL's query values on their own (`bo=`, `mo=`, `uc=` — a `bo=` value decodes to a
protobuf of fetch options with no id in it, and one value is shared by many cached files whose `/d/` id
no item holds), a path segment alone, a part of a text, a text shorter than 8 characters or of digits
only, and a text two items of one store hold — attributed to neither, and not matched in that store by
a later rule either: the first rule that finds the text in a store decides for it, so the bytes rule
never picks one of the items whose text was ambiguous. The same asset URL in two
accounts' stores is listed twice, each saying whose store holds it. A document of another layout is
still an item: its `item_id` column is indexed whatever the document holds, so a key naming the item by
it matches, and the detail says *document not decoded (layout differs)* rather than reading it on a
guess; the run log counts such documents per store. A document whose reading fails is one of those, and
a store that cannot be read at all loses its items, never the report.

Both readings are read. The `-wal` rewrites these rows: an item can have one version in each reading,
and the two readings then do not agree on its row. The version shown is the one that holds the matched
text — the one both readings hold, else the `-wal`'s, else the checkpointed file's — and its *(read from)*
cell is that version's own reading. A text the `-wal`'s version holds is badged *-wal only*, and when the
checkpointed version holds it too, the explanation adds that the row was rewritten and that the older
version's other texts are not shown; a text only the checkpointed version holds is badged *no -wal only* —
prior state, not the store's current content. The header's store line says whether the two readings
differ of the two tables read, `ctp__item_5` and `ctp__feedtree`, never of the whole store: its other
tables, the contacts among them, are not compared.

Verified on the two test devices where an item names a cached file: every changed entry's item is the one
a byte search (`instr(p, <key>)`) of a read-only copy of the store returns, in the reading its badge
names. On the device whose caption items hold `bo=` values of cached files, those files are not matched:
no item holds their key. On the third device whose store holds items, no claim's key occurs in any item,
and no entry changes.

What an item this report reads does **not** say: when the account had it, whether it put it in a snap,
or which. It says that the app keeps, in that account's store, an item whose asset or id the cached file
is named by. A document of another layout is not read, so what it holds is not known.

## Possible Memory — leads, never links

Some cached media is a Memory's media with nothing on the device to say so: the snap editor's working
copy of a snap later saved to Memories is byte-identical to that Memory's media once decrypted (seen on
a test device), and no claim, id or record connects them. What the device does record is time. So for an
on-disk media file that no identifier, chat or byte comparison links — in the categories *Snap editor*,
*Not in the index* and *Memory media* (a Memory-shaped claim whose row is gone), or *Other* when the app
claimed it as Memories media (context 19) — `scripts/memory_leads.py` lists the Memories of the same kind
(video or image, from `ZMEDIATYPE` and the file's magic bytes) with a time within ±10 minutes of one of
the file's: each claim's `CREATION_TIMESTAMP_MILLIS`, and the device's birth, modified and access times
of the file, against the Memory's `ZGALLERYSNAP.ZCREATETIMEUTC`, `ZCAPTURETIMEUTC` and its entry's
`ZCREATETIMEUTC`. At most five, ranked by the closest pair, each with every difference, and the panel
says how many Memories fell inside the window. `ZDURATION` is not used: it has been seen to differ from
the media's real length. A file a creative-tools item names is never a lead, whatever its category: the
item explains it.

It is a lead, not a link: shown in its own *Possible Memory — NOT proven* panel with a dashed chip,
never as the 🧠 link, never counted as linked, never followed by a partial report (no closure edge).
The *Linked* filter has its own option for it. The panel's *📋 Copy snap IDs* copies the leads' snap ids
for the Cloud download window: retrieving a lead from Snapchat's servers either proves it — the file
then links *by content* — or rules it out. On the test device every working copy that was a Memory's
media had that Memory as its first lead.

The same leads are shown **from the Memory's side** too, so an examiner working through Memories does not
miss them: a *≈ possible file* badge in the Memories index's Kind column (and *≈ possible file (grouped)*
on a folded group's row when one of its Memories has one), a *Possible cached file* filter, and a
*Possible cached file — NOT proven* panel on the Memory's page listing each file with every difference
and where this Memory ranks among that file's leads. They are not worked out twice: only this report
knows which files nothing else accounts for, and it renders after the Memories report, so it writes them
as `data/memory_leads.js` (`memory_leads.write_script`, keyed by snap id, carrying this run's id) and the
Memories pages load that file with `<script src>` like their own data — `memory_leads.MEMORY_JS` draws
the badge, the filter and the panel. A page whose folder has no such file, or one from another run, shows
no lead. A partial extract's file names only the Memories the extract holds.

## Assets of a filter listed with a Memory — not its media

A Memory's overlay record (`scdb-27` `ZGALLERYSNAPDETAIL.ZOVERLAY`, read by `scripts/data/snap_overlay.py`;
see [report_memories.md](report_memories.md#the-overlay-record-zgallerysnapdetailzoverlay)) lists the
snap's geofilters with the URLs of their image, sky image and font. `load_memory_index` keeps those URLs
(`overlay_urls`, iOS only), and an entry whose claim `EXTERNAL_KEY` is exactly one of them — the whole URL,
by `snap_overlay.normalise_url` — gets `entry["filter_memories"]`: one link per listing Memory
(`_overlay_links_for`; the rule in full is in [cross_report_linking.md](cross_report_linking.md)). On the
corpus these are context-25 claims on WebP filter images, PNG sky images and TrueType fonts.

* **The chip** is dashed and says *🧠 Memory … · filter listed* (*· filter selected* only when the record
  names that filter as selected), or, when several Memories list the asset — common: the same asset URL
  is listed in many Memories' records, sometimes under different filters — one *🧠 N Memories · filter
  listed* chip that opens the Memories report filtered to all of them (its `#find=` is the listing
  Memories' snap ids, not the entry's CACHE_KEY), narrowed in a partial extract to the Memories it holds.
  When one record lists the asset under several filters, the link takes the selected filter's field if
  the record names one, so the chip and the detail say what the Memory's own page says.
* **The detail** has a section *Listed with a Memory's filters — not its media*: per Memory, the asset,
  where in the record its URL sits (`filters.geoFilters[i]…`, and any other place the same record lists
  it), the filter (type · carousel group · idValue), what the record says about it being selected (*yes*
  / *no — the record names another filter* / *not recorded*), and the claim — flagged *another account's
  claim* when its `USER_ID` is not the Memory's account. Each Memory's "?" quotes the record's field,
  what it says of the selected filter and the Memory's `ZGALLERYSNAP.ZHASOVERLAYIMAGE`.
* **The Linked filter** gains *filter listed with a Memory (not its media)* (link value `Filter`), the
  header a count line with its "?", the page a dashed-chip style — each written only when some entry has
  such a link, so a report with none is byte for byte what it was. The run log gives the count on a line
  of its own; it is not part of *linked to Memories*.
* **Search** finds the entry by each listing Memory's snap id.

None of it is the Memory link: `entry["memory"]` and the Memory rules 1–5 are untouched, the file is
never decrypted with the Memory's key, and an entry with such a link is never a *Possible Memory* lead —
the record accounts for the file. The partial-report edge is its own (`EDGE_MEMORY_FILTER_ASSET`, never
`EDGE_MEMORY_CACHE`), followed only by the relations `mem_filter_assets` / `cache_filter_memories`, both
off by default ([report_partial.md](report_partial.md)).

## Locating the bytes on disk

`_resolve_on_disk` matches a `CACHE_KEY` against the SCContent index (`index_sccontent`, reused
from the Memories report):

* a whole `<CACHE_KEY>` file, and/or
* its `<CACHE_KEY>_<start>-<end>` byte-range parts (+ `PREFETCH`), concatenated conceptually, and
* for bundles, each child file — `<CACHE_KEY>_<child name>` first, then the child's own cache key.

It reports the source path(s) (archive-relative, via `device_path`) and total bytes present. This
is the answer to the TODO question *"can we link each cache_controller entry to an extracted cache
file?"* — yes, by `CACHE_KEY` as the filename, with parts/children resolved too.

## The UI
One sortable table, one row per file, with a global search and Category / On-disk / Linked filters.
Clicking a row expands a detail panel (all claims, full metadata, children, on-disk paths + hashes
+ viewer, bundle children, decrypted copies, CDN URL, deletion record, links). Clicking a **link**
or a **“?”** inside a row does *not* toggle it. Every link and the on-disk status carry a **“?”**
explaining how they were derived. Timestamps are Unix-epoch-ms, formatted in the chosen timezone
(DST-aware) via `make_ms_formatter` (which reuses the Memories timezone formatter by converting
ms → Cocoa seconds).

The table is **virtualized**: rows live in `data/index.js`, each row's detail HTML in a
`data/detail-<n>.js` chunk loaded only when that row is expanded, and only the visible rows are put
in the DOM — so the report opens instantly no matter how many entries `cache_controller.db` has
(measured: ~0.7 s for 101 200 rows). Search covers the whole index, not just what is on screen.
Rows are searchable **by URL** (full or partial): a row's own `CONTENT_RETRIEVAL_METADATA` source
URL, and — for the ~2 entries in 3 that have no retrieval metadata — the CDN URLs of the Memory it
is linked to (`load_memory_index` → `snap_urls`), which the detail panel also lists.
A **pager** (rows per page + page navigation) sits under the toolbar, and **Expand all** applies to
the current page and refuses more than 500 rows at once. Entries can be **ticked as relevant to the
case**, filtered with **Selected only** and saved with **💾 Save selections** — shared with the
Memories report through `Reports/selection.js`. See [report_ui.md](report_ui.md). Keep the `data/`
and `files/` folders next to the HTML file.

A file a creative-tools item names (`entry["ctp_items"]`) has a detail section *Named by a
creative-tools item — primary.docobjects › ctp__item_5*, after the snap editor's session record: per
item, whose store it is (the account, *the claiming account's own store* or *another account's store*,
and the store's device path), its item_id and own id, its feed as the tree names it, its kind (*payload
field 2.k*), which part of the key names it and where in the item (*the whole key* / *the key after
music:* … with the field path, and a "?" spelling out the match) and the reading; then the item's texts
as stored, URLs first, at most 40. The row's search text gains the item_id, the own id, the feed, its
short name and endpoint, the kind and those texts — not the category's name, which a file that kept its
category is not filed under. The header gains a count line naming the store(s) read and their `-wal`
state (whether the readings differ is said of the two tables read, not of the store), and the Category
filter the *Creative tools asset* value — each only when
some entry has an item, so a report with none is byte for byte what it was; the run log gives the
count on a line of its own. There is no chip: it is not a link.

## Coverage caveats (does every SCContent file have a claim?)

**No.** `cache_controller.db` does not index every physical file in the
`com.snap.file_manager_*_SCContent_*` folders, and an on-disk copy can live in a **different
user's** SCContent scope than the account that claims it. Worked example, on a device with two
accounts:

* One Memory's media is claimed **only** under the second account as a `g-media-<snapid>` key
  (context 19), stored range-sharded (`PREFETCH` + byte-range parts).
* A **byte-identical, plaintext** (`ftyp mp42`) full copy of the same media also sits in the
  **active** account's SCContent folder — with **no** claim / metadata / tombstone /
  virtualization row anywhere. It is an orphan: most likely a consolidated ("defragmented") copy
  materialized in the active account's file-manager scope during playback/use, not a second Memory.

**Those files are now listed.** Any cache file on disk that no row of `cache_controller.db` leads to
gets its own entry, in the category **"Not in the index"** (`orphan_entries`): the report would
otherwise only show what the index remembers, and a recovered file it has forgotten would be
invisible. Such an entry has no `EXTERNAL_KEY`, no owning account and no timestamps — only the
bytes, their hashes and what the content itself shows — and says so. A file is only an orphan when
**none** of the indexed entries resolved to it, so a bundle's children and a sharded file's
byte-range parts stay under their parent.

Implications for the report / examiner:

* The report's **on-disk resolution lists every matching copy** across all SCContent folders
  (whole + parts), so orphaned duplicates in another user's scope *do* show up under the entry —
  but the entry's **attribution** (user, Memory link) comes from the `CACHE_FILE_CLAIM`, which is
  authoritative. A copy's containing `SCContent_<userId>` folder is **not** a reliable owner.
* Because the physical file is content-addressed by `CACHE_KEY`, the same key names both copies;
  grouping by `CACHE_KEY` keeps them under one entry.

## Cross-report links
See [cross_report_linking.md](cross_report_linking.md). In short: **→ Memory** by snap UUID in the
`EXTERNAL_KEY` (primary), then `SHA-256(url token)[:16] == CACHE_KEY` (fallback), then `ZMEDIAID`
(fallback), then a `ZSNAPID` inside a key of another shape (fallback), then a MemData identifier the Memory records about itself (`ZMEMDATAIDS` / `ZMEMDATAID`,
fallback), then byte-identity with a copy retrieved from Snapchat's servers (last; ☁ on the chip — see
[cloud_download.md](cloud_download.md)); **→ chat** via the chat report's `cache_links.json` manifest, by `CACHE_KEY`
and — so that every cache entry of a message links back, not only the file the chat report showed —
by the `<conversation>:<message>:<part>` triple inside the claim's `EXTERNAL_KEY`. Apart from all of
these, **→ Memory, filter listed**: a claim key that is the URL of an asset of a geofilter a Memory's
overlay record lists links to that Memory under a relation of its own, never as its media (see
[above](#assets-of-a-filter-listed-with-a-memory--not-its-media)). A creative-tools item that names a file
is **information on the entry, not a link**: the item store has no report of its own, so there is no
target, no anchor, no `report_ui.xref` and no partial-report edge — the section belongs to the entry and
travels with it into a partial extract, and no copy of the store is staged into the report folder.

## Android

The Android app keeps the **same** database — same tables, same claims, same `CACHE_KEY`-named cache
files — in its private-data folder: `databases/native_content_manager/cache_controller.db`, with the
files under `files/native_content_manager/com.snap.file_manager_<n>_SCContent_<userId>/`. The report
runs unchanged on it: `cache_controller_paths`, `index_sccontent` and `find_app_container` recognise an
Android app folder (`android_layout.is_app_dir`: a `databases/` folder and no `Documents/`) and search
it for both (`android_layout.scan`), `load_memory_index` reads the Memories from `memories.db`
(`memories_android_report.memory_index`), and the few explanations that name a platform's columns take
their words from `PLATFORM_WORDS` — `memories_snap._id` rather than `ZSNAPID`. Device paths are shown
as `/data/data/com.snapchat.android/…`. The overlay record a filter-asset link is made from is read on
iOS only — its Android counterpart, if `memories.db` has one, was not examined — so the Android index has
no `overlay_urls` and its report none of that link's chips, filter option or header line. The same goes
for the creative-tools items: `ctp_items.find_stores` looks in the iOS layout
(`Documents/user_scoped/*/DocObjects/`), its Android counterpart was not examined, and on an Android app
folder it finds no store — no item, no category, no section or header line. See
[snapchat_android.md](snapchat_android.md).

## Standalone use
```
python -m scripts.cache_controller_report <extraction_root_or_app_container> [outdir] \
    [--tz local|utc|<IANA name>|<±HH:MM>]
```
Run under an existing `Reports/` tree (as the app does) so the chat manifest and sibling links
resolve; run alone and it still produces the full index (cross-links just won't have targets).
