# Cached media & documents report (`Library/Caches`)

`scripts/cache_media_report.py` → `Reports/CacheMedia/CacheMedia_report.html`.

## Scope — and the boundary with the cache_controller report

| Report | Covers |
|---|---|
| **cache_controller** | every file `cache_controller.db` indexes, i.e. the `com.snap.file_manager_*_SCContent_*` folders |
| **this one** | everything else under `Library/Caches` |

Both reports state this in their header, in the same words, because "cached media" otherwise means
something different in each.

**The exclusion is by folder name, wherever it appears** — not by parent directory. SCContent
folders live under `Library/Caches` as well as `Documents/` (`com.snap.file_manager_1_SCContent_*`
vs `_3_*`), and `index_sccontent` already globs both. Excluding by parent would list every
Caches-side SCContent file in both reports under two different identifiers. Verified: 0 rows of
this report resolve to an SCContent path.

## Report unit: one row per distinct recovered content

Keyed by the **SHA-256 of the recovered payload** (of the raw bytes when nothing was recovered),
with every on-disk copy listed inside the row. The same video is written at the `Caches` root *and*
in `Caches/tmp` under two different UUIDs, so counting files rather than distinct content routinely
doubles the apparent number of videos on the device.

Anchor: `cm-<sha256>`.

The index columns are in the **same order as the cache_controller report** and cached video carries
a poster frame, both described in
[report_cache_controller.md](report_cache_controller.md#index-columns--the-same-order-as-the-librarycaches-report).

App assets are hidden by default. That filter is cleared — not re-applied — when another report
links to a row, because `reset` means *stop hiding anything*; re-hiding them left every link to an
app-asset row (an icon or lens resource the cache_controller report matched by content) landing on
nothing at all.

## What it recovers

Content is identified by **magic bytes, never by name or extension** — the story thumbnails are
images inside an NSKeyedArchiver plist, so an `ftyp`/JPEG check misses every one, and
`sccache.dynamic-caption.data` looks encrypted but is a font cache. The decode chain, in order,
with every step recorded in the row's "?":

| Input | Action |
|---|---|
| plaintext media | used as-is |
| `bplist00` | unarchived; the largest `$objects` data blob is the media |
| opaque, length % 16 == 0 | AES-256-CBC from offset 0 with the `ClientEncryptionService.plist` key/IV, PKCS#7 stripped, then unarchived |
| gzip | decompressed, re-sniffed |
| **zstd** | decompressed with the standard library's `compression.zstd` (Python 3.14, PEP 784 — no third-party binding needed), then re-sniffed; a Snapchat resource bundle has its member names listed |
| `LZC` / TSAF / font | identified and classified, not treated as media |

### The story-cache key

`Documents/ClientEncryptionService.plist` is a **Snap TSAF container, not a plist** — `plistlib`
raises `Invalid file` on it. The reader locates the `encryption_key` / `initialization_vector` /
`identifier` markers and takes the next printable run after each, validating that they base64-decode
to 32 and 16 bytes. **No keychain is required**, so this works on filesystem-only extractions.

The key and IV are never written to the report, the logs or any manifest — only the fact that they
were recovered.

The decrypted sizes match the findings in
[snapchat_ios_cache_media.md](snapchat_ios_cache_media.md), which is where the format work lives.

## Categories

* **Evidentiary media** — story renders, story thumbnails, search/Discover and map imagery, bitmoji,
  cached web resources;
* **Document** — parsed, not merely listed: the `cronet` DNS host cache and HTTP cache, the
  NSURLCache `Cache.db`, `sccache.nyc-impala` TSAF API responses, `KSCrash` session state;
* **App asset** — caption fonts, CoreML lens models, `LZC` bundles, shader caches. Listed (a
  URL-keyed name still records that the asset was fetched) but **hidden by the default filter**;
* **Covered by another report** — `caching-media` packs (Memories) and `SCPersistentMedia`
  (Conversations): inventoried and cross-linked, never decoded twice.

### "Not recovered" counts only actual failures

The headline figure used to include every row this report deliberately does not decode, so on the
iOS 26 single-account device it read **227 not recovered** when 188 of those were `caching-media` packs the
Memories report decrypts in full and the real number was **4**. Those rows also showed a padlock in
the File column, which said the opposite of the truth.

Three counts are now reported separately, and the File column says which one a row is:

| | File column | Counted as |
|---|---|---|
| owned by another report | that report's own decrypted copy, shown inline (below) | *left to the report that owns them* |
| app assets (LZC bundles, fonts, CoreML) | `<type> app asset` | *app assets not decoded* |
| genuinely unrecoverable | `🔒 not recovered` | **not recovered** |

### Bytes another report decrypted are *shown*, not described

Saying "↗ decoded in the Memories report" next to no image still reads as a failure — and it was
next to the one group of rows whose plaintext certainly exists, since the Memories report holds the
per-snap key. Those rows now display **that report's copy** (`_decrypted_elsewhere`, from the
`media_by_pack.json` record's `path`), in the green "decrypted" style, linking into the Memories
report; the expanded row states which Memory the key came from and where the plaintext file is.

The same rows carry **two** Memory chips — the index row and the Memory's own detail page — as the
cache_controller report has always done.

## Attribution — exact only

No duration/size/`mvhd`-time correlation. Each link records the method, in priority order:

0. **A `caching-media` pack the Memories report decrypted**, via `Memories/media_by_pack.json`
   (keyed by `<folder>/<item hash>`). A pack filename is an opaque hash indexed by no database, so
   decrypt-and-match in the Memories report — which holds the keys — is the only link that exists.
   Without this manifest every pack was an unexplained padlock with no link at all: 110 of 191 rows
   on that device. It now links 203 of 246.
1. **A UUID in the filename that a `CACHE_FILE_CLAIM.EXTERNAL_KEY` names** → its `CACHE_KEY`. The
   `<USERNAME>~<snapId>` form also yields the **owner username**, which the filename never gives.
2. **The full `<conversation>:<message>:<part>` triple** → the Conversations report's message.
   Matching on the embedded UUID alone is far too coarse — it is conversation-level and matched 20
   unrelated claims for one file on the iOS 26 device.
3. **Byte-identical content** in SCContent. This is the only exact link a root render has, since its
   own UUID is ephemeral. SCContent is indexed by **size** first (`stat` only) and only
   size-matching files are hashed, so the tree is never hashed wholesale; files are hashed **as
   stored** as well as after decryption, or every plaintext story snap would be missed.
4. **The CDN token in a URL-keyed filename** → `SHA-256(token)[:16]` against a Memory's download URL.

> **A root-level filename UUID is never presented as a snap id.** It is a scratch identifier minted
> when the file was written; every one was searched across the whole app container, as ASCII and as
> its 16-byte binary form, with zero hits. Rows at the root carry an explicit warning saying so.

## Cross-report links

Out: `#ck-<CACHE_KEY>` (cache_controller), `#mem-<ZSNAPID>` (Memories), `#msg-<id>` (Conversations).
Back: this report writes `CacheMedia/by_cache_key.json`, which the cache_controller report reads to
show a **🗂 Library/Caches** chip on entries whose bytes also exist there. That is why this report
runs **before** cache_controller in the pipeline.

## Filenames that Windows cannot hold

The URL-keyed caches name each entry after the CDN URL, query string included, so those names carry
`?`. Extraction percent-encodes the characters Windows rejects — before this they failed to write
and were silently dropped by a bare `except: pass`, losing exactly the files whose name *is* their
provenance (14 files on the iOS 16 device). `extraction_manifest.json` records
`sanitised path → exact on-device name`, and the report shows the original next to the copy.
Percent-decoding either spelling yields the same URL.

## Standalone use

```
python -m scripts.cache_media_report <extraction_root_or_app_container> [outdir] \
    [--tz local|utc|<IANA name>|<±HH:MM>]
```

## The cronet HTTP cache

Snapchat embeds Chromium's network stack, and on some devices its **blockfile disk cache** holds
Snapchat CDN media that no Snapchat database indexes. `parse_blockfile_entries` reads the 256-byte
`EntryStore` records in `data_1` and joins each cached **request URL to the `f_XXXXXX` file holding
its response body**, via the stream address in the record.

The key offset moved between Chromium versions — 96 in the classic layout, **100** in the Cronet
build — so both are tried and the one whose bytes parse as a URL of the declared length wins.
`key_len` (offset 32) and `data_addr[4]` (offset 56) are unchanged between them. The join is only
reported where it holds: every resolved address must name an `f_*` file that exists, with the
record's size field matching that file's real size.

URLs the raw block-file scan finds but no `EntryStore` accounts for are still listed, marked
**"not joined"** — a link that was not established is never implied.

## Known limits

* **Resource bundles are described, not unpacked.** A decompressed Snapchat resource bundle's
  members are located by their names rather than by walking its (undocumented) structure, so
  the report says what the bundle contains without claiming a byte-exact extraction of each
  member. They are UI assets, not user media.
* Only **inline** cache keys are read; an entry whose key was long enough to be stored out-of-line
  (`long_key`) falls back to the scan and is reported as not joined.
* `caching-media` packs are not decrypted here; the Memories report owns them.

## Validated against

All four Snapchat-bearing extractions in the test corpus (see `docs/snapchat_ios_cache_media.md`),
which between them exercise the conditions this report has to get right:

* root + `tmp` deduplication, and several root MP4s sharing one MD5 collapsing to one row with
  several copies;
* `gallery-stories-snap` decryption on both storage schemas, against the sizes in the findings doc;
* **bare-UUID root media** with no producer prefix, and the cronet blockfile join;
* `SCPersistentMedia` files linked back to the message they belong to;
* a `Cache.db` with a `-wal`, reported as "present, 0 entries" rather than failing or being omitted;
* links resolving both ways with the cache_controller report.
