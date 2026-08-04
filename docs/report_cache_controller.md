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

The Category cell holds badges (`-wal only`, `changed`) and their "?" icons. The row is a fixed
height, so a line that does not fit is **cut through the middle** rather than dropped — which is how
a "?" came out sliced in half. The badges are held on one line of their own and the column is wide
enough for them, so the cell is never more than two lines.

## Poster frames for cached video

A play button says *this is a video*; it does not say **which** video, so a page of cached video
told the examiner nothing. Every published video gets a still (`publish_posters`, shared with the
Library/Caches report) written as `files/<name>_poster.jpg` and shown in the File cell and the
expanded row. It is **derived data** — this tool's own frame, not anything from the device — and
says so wherever it appears (`POSTER_BASIS`). Posters left by an earlier run into the same folder
are reused.

Two properties of a *cache* make this different from posting a normal video file, and both were
found on the test corpus:

* **Cached video is routinely truncated** (the cache holds the byte ranges the device streamed), so
  the frame is taken by reading forward from the start rather than by seeking to ~1 s: seeking into
  bytes that are not there fails *and* costs a full re-read of the file.
* **A single unreadable file must not be able to stall the report.** A 1,859-byte cached "video"
  whose ftyp brand is `M4A ` — an audio container, which every magic-byte identifier calls an
  `.mp4` — opened fine and then blocked inside one OpenCV `read()` indefinitely (killed at 70 s).
  `poster_within` skips audio-only brands and gives the extraction a hard time bound on a daemon
  thread, so the worst case is a missing thumbnail. With both, 45 of 47 posters on the iOS 16 device
  take **1.5 s**.

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
  are stored as `<CACHE_KEY>_<start>-<end>` (the same split media `parseSnapvideos` reconstructs).
* **bundle** (`TYPE=3`): names are child cache keys (often with a leading marker byte, e.g.
  `z<hex>`) plus a filename such as `lar_lens_notifications_geofences_v6.json`.

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

### Bundles: the child files are the content
For a bundle (`TYPE = 3`) the file named after the `CACHE_KEY` is **only the CHILDREN descriptor**
(90 bytes on the test device), and the content sits in one file per child, named
`<CACHE_KEY>_<child name>` (e.g. `4bfc4bba…_z2a132f1f…`). `child_ondisk_paths` resolves those (the
child's own cache key is also tried, for other layouts), and each child is hashed and typed
**separately** and published with its own extension. The row's file button shows the bundle's
largest recognizable child.

This is what makes a chat video viewable: message *12.0* of the test extraction is a bundle whose
children are the 219 KB `.mp4` and its 40 KB `.webp` overlay — neither of which was reachable when
only the descriptor was hashed (its "detected type" then read as *not recognized*, which now says
explicitly that a bundle's parent file is a descriptor).

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

Per test device, after the change: 100 encrypted with **7** unopenable (was 253 padlocked);
91 / **5**; 112 / **2**; 87 / **0**.

## Categorisation

`classify_external_key` buckets each claim from its `EXTERNAL_KEY` (and `MEDIA_CONTEXT_TYPE` as a
tie-breaker): *Memory media / overlay / thumbnail* (`snap-*`/`g-media-`), *Chat media* (context
2/3), *Lens*, *Preview*, *App install*, *Video / Discover* (`topvideo~`/`firstframe`/`video~`),
*CDN media* (a bare `http(s)` URL), else *Other*. The row's category is the most meaningful across
its claims (Memory beats Other).

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

## Coverage caveats (does every SCContent file have a claim?)

**No.** `cache_controller.db` does not index every physical file in the
`com.snap.file_manager_*_SCContent_*` folders, and an on-disk copy can live in a **different
user's** SCContent scope than the account that claims it. Worked example (iOS 16 test device, 2
accounts):

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
(fallback); **→ chat** via the chat report's `cache_links.json` manifest, by `CACHE_KEY`
and — so that every cache entry of a message links back, not only the file the chat report showed —
by the `<conversation>:<message>:<part>` triple inside the claim's `EXTERNAL_KEY`.

## Standalone use
```
python -m scripts.cache_controller_report <extraction_root_or_app_container> [outdir] \
    [--tz local|utc|<IANA name>|<±HH:MM>]
```
Run under an existing `Reports/` tree (as the app does) so the chat manifest and sibling links
resolve; run alone and it still produces the full index (cross-links just won't have targets).
