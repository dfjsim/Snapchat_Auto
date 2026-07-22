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

## Grouping and layout
Memories that reference the same media (same `ZMEDIAID`) are grouped so media, encryption and
matching timestamps show once; per-snap identity and timestamps show per snap. Each snap's header
carries the `id="mem-<ZSNAPID>"` anchor other reports link to.

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
