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
  `zfe09d729…`) plus a filename such as `lar_lens_notifications_geofences_v6.json`.

### `CACHE_FILE_METADATA.CONTENT_RETRIEVAL_METADATA` (protobuf)
Decoded by `parse_retrieval`. Field `5.1`/`6.1` = the **CDN URL** the file was fetched from.
Field `8` is a **content reference whose form varies** by app version / media kind, so the report
inspects the value rather than assuming a type:

* most often a **CDN media token** (the same token after `/d/` in the URL, sometimes with a `.NNN`
  suffix) — e.g. `S8fDoGrkeolX01yylQtsf`;
* a **64-hex content SHA-256** on newer app versions (only ~13% of entries on the 2026 device);
* the **32-hex `CACHE_KEY`** on the 2023 device.

It is labelled accordingly in the detail panel ("Content SHA-256" only when it is genuinely 64 hex,
otherwise "CDN media token" or "Content ref … equals CACHE_KEY"). **Do not** read field 8 as a
hash without checking its length — an earlier version of this report mislabelled every field-8
value as "Content SHA-256".

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
* for bundles, each child's own cache key.

It reports the source path(s) (archive-relative, via `device_path`) and total bytes present. This
is the answer to the TODO question *"can we link each cache_controller entry to an extracted cache
file?"* — yes, by `CACHE_KEY` as the filename, with parts/children resolved too.

## The UI
One sortable table, one row per file, with a global search and Category / On-disk / Linked filters.
Clicking a row expands a detail panel (all claims, full metadata, children, on-disk paths, CDN URL
+ hash, deletion record, links). Every link and the on-disk status carry a **“?”** explaining how
they were derived. Timestamps are Unix-epoch-ms, formatted in the chosen timezone (DST-aware) via
`make_ms_formatter` (which reuses the Memories timezone formatter by converting ms → Cocoa seconds).

## Coverage caveats (does every SCContent file have a claim?)

**No.** `cache_controller.db` does not index every physical file in the
`com.snap.file_manager_*_SCContent_*` folders, and an on-disk copy can live in a **different
user's** SCContent scope than the account that claims it. Worked example (2023 GK device, 2
accounts):

* Memory media `6382911a…` is claimed **only** under owner `5803ed5b` as `g-media-EB854B71…`
  (context 19), stored range-sharded (`PREFETCH` + byte-range parts).
* A **byte-identical, plaintext** (`ftyp mp42`) full copy of the same media also sits in the
  **active** account `3559758e`'s SCContent folder — with **no** claim / metadata / tombstone /
  virtualization row anywhere. It is an orphan: most likely a consolidated ("defragmented") copy
  materialized in the active account's file-manager scope during playback/use, not a second Memory.

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
(fallback); **→ chat** via the Communications report's `cache_links.json` manifest.

## Standalone use
```
python -m scripts.cache_controller_report <extraction_root_or_app_container> [outdir] \
    [--tz local|utc|<IANA name>|<±HH:MM>]
```
Run under an existing `Reports/` tree (as the app does) so the chat manifest and sibling links
resolve; run alone and it still produces the full index (cross-links just won't have targets).
