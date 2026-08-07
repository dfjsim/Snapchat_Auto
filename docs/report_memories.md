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
chip on the affected Memory, counts them in the header, and offers a **Media: incomplete only /
complete only** filter. Files stored as plaintext are marked *completeness not verified* — there is
no padding to check and no shard layout to measure, so claiming either way would be a guess.

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
  per-column sort, a with/without-thumbnail filter, a user filter, an incomplete-media filter).
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
  The search text behind each row also carries the Memory's **CDN URLs** (media / overlay /
  thumbnail, download and redirect), so a full or partial URL — pasted from `scdb-27`, from a
  detail page, or from the cache_controller report — finds its Memory. The URLs themselves are
  shown on the detail sub-page.
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
