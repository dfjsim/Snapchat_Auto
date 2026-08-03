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
in `Caches/tmp` under two different UUIDs; on the iOS 16 test device that is 14 files and **7 videos**,
and reporting 14 would overstate what is on the device.

Anchor: `cm-<sha256>`.

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

Verified on the iOS 16 device: the three `sccache.gallery-stories-snap.data` entries decrypt to
PNG 2.23 MB, PNG 2.92 MB and **MP4 2.19 MB**, matching the sizes in
[snapchat_ios_cache_media.md](snapchat_ios_cache_media.md).

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
| owned by another report | `↗ decoded in the Memories/Conversations report` | *left to the report that owns them* |
| app assets (LZC bundles, fonts, CoreML) | `<type> app asset` | *app assets not decoded* |
| genuinely unrecoverable | `🔒 not recovered` | **not recovered** |

After the change, per test device: 120 not recovered (97 elsewhere, 80 assets excluded); 13; 7; 4.

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
build on the AFU test device — so both are tried and the one whose bytes parse as a URL of the
declared length wins. `key_len` (offset 32) and `data_addr[4]` (offset 56) are unchanged between
them. Verified on that device: **108 entries parsed, 42 with a body file, and every resolved
address named an `f_*` file that exists**, with the record's size field matching the file's real
size (e.g. `cf-st.sc-cdn.net/d/8w65tBqGmff9UuJUTn9E2` → `f_000001`, 50 833 bytes).

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

## Verified on

All four Snapchat-bearing extractions in the test corpus (see `docs/snapchat_ios_cache_media.md`):

| Device | Files → distinct | What it proved |
|---|---|---|
| iOS 16 GK FFS | 716 → 534 | root+`tmp` dedup (14 files → **7 videos**, matching the findings table); `gallery-stories-snap` → PNG 2.23 MB / PNG 2.92 MB / **MP4 2.19 MB**; fonts as App asset; 53 links each way with cache_controller |
| iOS 26 UFED FFS | 258 → 246 | `gallery-stories-snap` → PNG **1.79 MB / 2.52 MB** (matching the findings table); both `SCPersistentMedia` files linked to their message |
| UFED AFU | 338 → 276 | **bare-UUID root media** with no producer prefix; 5 root MP4s sharing one MD5 correctly collapsed to **one row with 6 copies**; cronet blockfile join |
| iOS 26 GK FFS | 102 → 96 | `Cache.db` with a 105 KB `-wal` reports **"present, 0 entries"** rather than failing or being omitted |
