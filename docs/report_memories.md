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

## How each media file is located and linked

For every Memory that has an AES key/IV, `collect_media` gathers candidate cache files three ways.
Each recovered file records a `how` string (shown by its “?” icon):

1. **SCContent by CDN URL** — `CACHE_KEY = SHA-256(token)[:16 bytes]`, where `token` is the last
   path segment of `ZMEDIADOWNLOADURL` / `ZOVERLAYDOWNLOADURL` / `ZTHUMBNAILDOWNLOADURL`. Decrypt
   with the snap's AES-256-CBC key/IV.
2. **SCContent by `cache_controller.db`** — a `CACHE_FILE_CLAIM.EXTERNAL_KEY`
   (`snap-media-/overlay/-rendered-lowres-<snapid>`, `g-media-<snapid>`) names the Memory and
   points at `CACHE_KEY`. Essential for **locally-captured media** (e.g. device-recorded videos)
   whose `ZGALLERYSNAP` URL fields are empty. See `index_cache_controller`.
3. **caching-media `.pack` by decrypt-and-match** — pack names are opaque, so each folder is tried
   against every Memory's key/IV; the key that yields valid media magic bytes (after the 8-byte
   header) identifies the Memory. **Not** referenced by `cache_controller.db`.

In cases 1–2 a file may be a single `<CACHE_KEY>` or split into `<CACHE_KEY>_<start>-<end>` parts
that are concatenated in offset order before decryption (`_resolve_sccontent`); the `how` text
notes the reconstruction. If a video has no cached still, a **poster frame** is derived from the
decrypted `.mp4` and clearly labelled as a derived artifact.

## Layout — a lightweight index + per-group detail sub-pages
To keep the report usable with many Memories, it is split (`generate_report`):

* **`Memories_report.html`** — a lightweight, **sortable/filterable index table** (global search,
  per-column sort, a with/without-thumbnail filter, a user filter). One **row per Memory (snap)**
  with: thumbnail, kind, user, `ZSNAPID` / `ZENTRYID` / `ZMEDIAID`, cache-file tokens, the media
  **MD5 / SHA-256**, created time, geolocation, and a link to the detail sub-page. Each row carries
  `id="mem-<ZSNAPID>"` (the anchor other reports link to).
  The table is **virtualized** — rows live in `data/index.js` and only the visible ones are put in
  the DOM, so the index opens instantly however many Memories there are, and search/sort still
  cover all of them. Row cells are one fixed height (the full values are on the detail page; the
  cache-token cell shows the first two and counts the rest). It also carries the **pager** and the
  **selection** controls, and a **My Eyes Only** filter. Keep the `data/` and `media/` folders next
  to the HTML file. See [report_ui.md](report_ui.md).
* **`pages/<key>.html`** — one **detail sub-page per group**, holding the full detail (metadata,
  location, per-snap AES key/IV, ZGALLERYSNAP/ZGALLERYENTRY values, CDN URLs, timestamp tables, and
  the media-files table with hashes/paths and the 🗄 cache-entry links). MEDIA ID and SNAP IDs are
  shown prominently. Each member also carries an `id="mem-<ZSNAPID>"` anchor, and the page links
  back to the index.

### My Eyes Only
A Memory in Snapchat's private, separately-encrypted album is marked with a red **MEO** badge in the
index's Kind column (`m["is_meo"]`, set from `IS_ENCRYPTED` / the MEO key path — see
[snapchat_ios_memories_decryption.md](snapchat_ios_memories_decryption.md)), matches a search for
"meo" / "my eyes only", and can be isolated with the **My Eyes Only** filter (any / only / exclude).
The detail page keeps its existing "My Eyes Only" label next to the kind.

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
