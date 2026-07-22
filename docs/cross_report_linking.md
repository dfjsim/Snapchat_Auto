# How the reports link to each other (anchors & link bases)

Snapchat Auto produces several sibling HTML reports under `Reports/`:

```
Reports/
  index.html
  Communications/Communications_report.html
  Memories/Memories_report.html
  LocalMemories_legacy/LocalMemories_legacy_report.html
  CacheController/CacheController_report.html
```

Wherever the same underlying artifact appears in more than one report, the reports link to each
other with plain `#anchor` fragments, so an examiner can jump between (say) a cached file and the
Memory or chat message it belongs to. This page is the single reference for **the anchor scheme
and exactly how each cross-link is derived**. Each per-report page documents its own internals:
[Communications](report_communications.md), [Memories](report_memories.md),
[cache_controller](report_cache_controller.md).

> Every media file and every cross-report link in the reports carries a small round **“?” icon**.
> Clicking it shows, in plain language, *how that specific association was made* (which identifier
> matched, whether it was a primary or fallback method, how the bytes were located/decrypted). The
> text below is what those icons summarise.

## Anchor scheme (stable IDs)

| Report | Anchor id | On what element | Written by |
|---|---|---|---|
| Memories | `mem-<ZSNAPID>` | each snap's "Snap ID" header | `_render_group` in `scripts/memories_media_report.py` |
| cache_controller | `ck-<CACHE_KEY>` | each physical-file row | `generate_report` in `scripts/cache_controller_report.py` |
| Communications | `cf-<CACHE_KEY>` | each cached chat attachment | `path_to_image_html` in `scripts/ParseSnapchat_iOS.py` |

`<ZSNAPID>` is the exact `ZGALLERYSNAP.ZSNAPID` string (upper-case UUID). `<CACHE_KEY>` is the
32-hex `cache_controller.db` key, which is also the on-disk filename in the `SCContent` folder.
Links are relative between siblings, e.g. `../Memories/Memories_report.html#mem-<ZSNAPID>`.

## The links, and how each is derived

### cache_controller → Memory
Tried in priority order; the first that matches wins, and the icon records which one:

1. **Snap-scoped claim (primary).** A `CACHE_FILE_CLAIM.EXTERNAL_KEY` of the form
   `snap-media-<UUID>`, `snap-overlay-<UUID>`, `snap-rendered-lowres-<UUID>` or `g-media-<UUID>`
   whose UUID equals a `ZGALLERYSNAP.ZSNAPID`.
2. **CDN URL token (fallback).** The file's `CACHE_KEY` equals `SHA-256(token)[:16 bytes]` where
   `token` is the last path segment of the Memory's `ZMEDIADOWNLOADURL` / `ZOVERLAYDOWNLOADURL` /
   `ZTHUMBNAILDOWNLOADURL`. This catches downloaded media whose claim is only a URL, with no
   snap-scoped key.
3. **ZMEDIAID (fallback).** A UUID inside an `EXTERNAL_KEY` matches the Memory's `ZMEDIAID`
   (used only when it is *not* also a `ZSNAPID`).

> On both test extractions the primary method already resolves every linkable entry — the two
> fallbacks add nothing there. They exist for app versions / cloud-only memories where a physical
> file carries a URL claim but no snap-scoped claim. See the measurement in `DONE.md`.

### Memory → cache_controller
Per recovered media file, the Memory report links to `#ck-<CACHE_KEY>` **only when that key is
present in `cache_controller.db`** (`all_cache_keys`). The key is the one used to locate the file:
either `SHA-256(url token)[:16]` or the `cache_controller` `EXTERNAL_KEY` target. (`caching-media`
`.pack` files are *not* indexed by `cache_controller.db`, so they get no such link.)

### cache_controller → Communications (chat)
The Communications report writes `Reports/Communications/cache_links.json`, mapping
`CACHE_KEY → [{conversation_id, server_message_id}]` for every cached file it attached to a
message. The cache_controller report loads that manifest and links matching entries to
`#cf-<CACHE_KEY>`.

### Communications → cache_controller
Each cached attachment links back to `#ck-<CACHE_KEY>` (the `cclink` in `path_to_image_html`).

## Ordering / dependency
`ParseSnapchat_iOS.main` runs the reports in the order **Communications → Memories →
cache_controller**. That matters: the cache_controller report reads the chat manifest the
Communications report just wrote, and reads each `scdb-27.sqlite3` directly for the Memory index —
so there is no circular dependency, and the back-links from Communications/Memories are static URLs
that resolve to anchors the cache_controller report emits.
