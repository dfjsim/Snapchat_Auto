# How the reports link to each other (anchors & link bases)

Snapchat Auto produces several sibling HTML reports under `Reports/`:

```
Reports/
  index.html
  run_id.txt                                  identifies this set of reports
  selection.js                                the examiner's row selections, shared by every report
  Conversations/Conversations_report.html     + pages/, media/, data/, assets/,
                                                conversation_pages.json, cache_links.json
  Contacts/Contacts_report.html               + data/
  Memories/Memories_report.html               + pages/, media/, maps/, data/,
                                                memory_pages.json, media_by_cache_key.json
  CacheController/CacheController_report.html + files/, data/
  CacheMedia/CacheMedia_report.html          + files/, data/, by_cache_key.json
  Communications_legacy/Communications_legacy_report.html   + cacheFiles/, cache_links.json
  LocalMemories_legacy/LocalMemories_legacy_report.html
```

Wherever the same underlying artifact appears in more than one report, the reports link to each
other with plain `#anchor` fragments, so an examiner can jump between (say) a cached file and the
Memory or chat message it belongs to. This page is the single reference for **the anchor scheme
and exactly how each cross-link is derived**. Each per-report page documents its own internals:
[Conversations](report_conversations.md), [Contacts](report_contacts.md),
[Memories](report_memories.md), [cache_controller](report_cache_controller.md),
[Communications (legacy)](report_communications.md).

> Every media file and every cross-report link in the reports carries a small round **“?” icon**.
> Clicking it shows, in plain language, *how that specific association was made* (which identifier
> matched, whether it was a primary or fallback method, how the bytes were located/decrypted). The
> text below is what those icons summarise.

## Anchor scheme (stable IDs)

| Report | Anchor id | On what element | Written by |
|---|---|---|---|
| Conversations index | `conv-<conversation id>` | each conversation's index-table row | `generate_index` in `scripts/conversations_report.py` |
| Conversation detail page | `conv-<conversation id>` | the conversation's metadata block | `render_conversation_page` |
| Conversation detail page | `msg-<server message id>` | each message row (e.g. `msg-12.0`) | `build_messages` / `_message_rows` |
| Contacts | `ct-<user id>` | each contact's row | `generate_report` in `scripts/contacts_report.py` |
| Memories index | `mem-<ZSNAPID>` | each memory's index-table row | `generate_report` in `scripts/memories_media_report.py` |
| Memories detail sub-page | `mem-<ZSNAPID>` | each member block on `pages/<key>.html` | `_render_group_detail` |
| cache_controller | `ck-<CACHE_KEY>` | each physical-file row | `generate_report` in `scripts/cache_controller_report.py` |
| Cached media (Library/Caches) | `cm-<sha256>` | each distinct-content row | `generate_report` in `scripts/cache_media_report.py` |
| Communications (legacy) | `cf-<CACHE_KEY>` | each cached chat attachment | `path_to_image_html` in `scripts/ParseSnapchat_iOS.py` |

A message with no `server_message_id` (one the app had not finished sending) is anchored on its
position in the conversation instead: `msg-row<N>`. Duplicate anchors get a `-2`, `-3`, … suffix.

The Memories report is split into a lightweight index (`Memories_report.html`) plus one detail
sub-page per group (`pages/<key>.html`); the same `mem-<ZSNAPID>` anchor exists on both, so links can
target either. `generate_report` writes `Memories/memory_pages.json` (`snap_id → pages/<key>.html`)
so other reports can resolve a snap to its detail page.

`<ZSNAPID>` is the exact `ZGALLERYSNAP.ZSNAPID` string (upper-case UUID). `<CACHE_KEY>` is the
32-hex `cache_controller.db` key, which is also the on-disk filename in the `SCContent` folder.
Links are relative between siblings, e.g. `../Memories/Memories_report.html#mem-<ZSNAPID>`.

The Conversations report is split the same way: a lightweight index plus one detail page per
conversation, with `Conversations/conversation_pages.json` (`conversation id → pages/<key>.html`)
mapping between them. Links into it target a **message row** (`msg-…`), which the virtual table
resolves and expands even when that row is not in the DOM.

In the legacy Communications report the anchor id is the **attachment filename**, which is the
`CACHE_KEY` for files copied out of `SCContent` but *not* for `SCPersistentMedia` copies (see
below) — so the `cf-…` anchor is taken from the manifest rather than assumed.

**How the jump behaves** (scrolling clear of the sticky toolbar, expanding the target row in a
virtualized table, and working on repeat clicks into an already-open tab) is documented in
[report_ui.md](report_ui.md#cross-report-navigation-nav_js).

### When the target is several rows: `#find=`

Some associations are one-to-many — the same cached content under several paths, one pack stored as
a series of chunk files, one `CACHE_KEY` matching several `Library/Caches` copies. Those links use
`#find=<token>[|<token>…]` instead of an anchor: the receiving report filters itself to the tokens
and expands **every** match, so the examiner sees the whole set rather than whichever row the link
happened to name. Built with `report_ui.find_fragment`; see
[report_ui.md](report_ui.md#links-whose-target-is-a-set-of-rows-find). Used by:

| From | To | Tokens | When |
|---|---|---|---|
| cache_controller | Cached media | the entry's `CACHE_KEY` | it matches ≥ 2 `Library/Caches` files |
| Cached media | cache_controller | every linked `CACHE_KEY` | the file matches ≥ 2 cache entries |
| Memories (detail) | Cached media | the pack's item hash | always — a pack is many chunk files |

A single-target link stays a plain `#anchor`, which highlights the row it lands on.

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

A memory-linked cache entry shows **two** links: the index row
(`Memories_report.html#mem-<ZSNAPID>`) **and**, when `memory_pages.json` is present, the detail
sub-page (`pages/<key>.html#mem-<ZSNAPID>`). Both open in the `scauto_memories` tab.

### Memory → cache_controller
Per recovered media file, the Memory report links to `#ck-<CACHE_KEY>` **only when that key is
present in `cache_controller.db`** (`all_cache_keys`). The key is the one used to locate the file:
either `SHA-256(url token)[:16]` or the `cache_controller` `EXTERNAL_KEY` target.

### Memory → Cached media (Library/Caches)
`caching-media` `.pack` files are *not* indexed by `cache_controller.db`, so the report that
inventories their bytes on disk is the Library/Caches one. Each such file links there with
`#find=<item hash>` — a pack is stored as a numbered series of `.pack` chunks, i.e. several rows,
so the link filters that report to the pack and expands all of its chunks
(`PACK_IN_CACHEMEDIA_BASIS`).

### cache_controller → the chat report
The chat report writes `cache_links.json` with **two** indexes over the attachments it rendered.
The Conversations report writes version 3:

```json
{"version": 3, "report": "Conversations",
 "by_key":     {"<CACHE_KEY>": [{"conversation_id": …, "server_message_id": "12.0",
                                 "anchor": "msg-12.0", "title": "…",
                                 "href": "Conversations/pages/<key>.html#msg-12.0"}]},
 "by_message": {"<conversation id>|<server message id>": [ …the same records… ]}}
```

`href` (relative to the reports root) is the addition: with one page per conversation the anchor
alone no longer says *which document* to open. The legacy Communications report still writes its
own version-2 manifest, whose records have no `href` and whose anchors are `cf-<filename>` into its
single document. `load_chat_links` prefers `Conversations/`, then `Communications_legacy/`, then
`Communications/`, and stamps the single-document reports' records with the `base` document so the
link can be built either way. Version 1 (a bare `CACHE_KEY → records` map) is still understood.

The cache_controller report links an entry to a chat message by, in order:

1. **`by_key`** — this physical file *is* the attachment the chat report displayed.
2. **`by_message` (fallback).** A chat claim's `EXTERNAL_KEY` is
   `<type>:<conversation id>:<message id>:<part>[:…]` (e.g. `thumbnail~1:19e0693c-…:12:0:0`), so the
   conversation + `<message>.<part>` it carries is matched against the manifest. This is what links
   **every** cache entry of a message — full media (`1:…`), thumbnail (`thumbnail~1:…`) and raw
   content claim (`content~1:…`) — and not just the one file the chat report happened to display.
   A message with two attachments (e.g. a thumbnail and a video) therefore links back from both.
   The "?" spells out that such a link points at the *message*, not at that exact file.

Chips are deduplicated per (conversation, message).

### the chat report → cache_controller
Each cached attachment links back to `#ck-<CACHE_KEY>` — the `cclink` in `path_to_image_html`
(legacy) and the cache chip in the expanded message row (Conversations). Both take the key from
the same `cacheControllerKey`:

* attachments copied out of `SCContent` are **named after their `CACHE_KEY`** — used directly;
* **`SCPersistentMedia`** copies ("media saved in chat") are named
  `<type>_<conversation>_<message>_<part>_<n>.<ext>`, which is *not* a cache key. They are matched
  to a claim carrying the same `<conversation>:<message>:<part>` triple
  (`mapPersistentMediaToCacheKeys`), and the link uses that claim's `CACHE_KEY`. Previously these
  produced a dead `#ck-<filename>` link. The link's `title` states which `EXTERNAL_KEY` made the
  match.

  Two details matter here, because a message has **several** claims on the same triple:

  1. **Which claim.** The claim whose type equals the file's own wins; otherwise a `thumbnail…`
     file takes `thumbnail~1:…` and anything else takes the full media `1:…` (then `content~1:…`).
     So a message with a thumbnail and a video produces **two different** links — the PNG to
     `thumbnail~1:<conv>:<msg>:<part>` and the video to `1:<conv>:<msg>:<part>` — rather than both
     landing on the thumbnail's entry.
  2. **Matched against every claim**, not the filtered set. `mergeCache` keeps only claims whose
     `CACHE_KEY` file is directly recognizable media, which drops exactly the full-media claim of a
     saved video: a chat video is a **bundle**, so the file named after its `CACHE_KEY` is the small
     CHILDREN descriptor and the video is a child file. The mapping therefore uses the raw
     `CACHE_FILE_CLAIM` rows.

  This is verifiable byte for byte, and worth doing when validating on a new extraction: the
  attachment's SHA-256 must equal the linked entry's bytes, or one of its bundle children's — for a
  video, typically a named child of the bundle the cache entry resolves to. Every attachment matched
  on the corpus this was built against.

### cache_controller → the decrypted copy of an encrypted cache file
Memory media is cached **encrypted**, so its bytes cannot be displayed from the cache entry itself.
The Memories report writes `Reports/Memories/media_by_cache_key.json`
(`CACHE_KEY → [{path, role, ext, bytes, snap_id, md5, sha256}]`) for every media file it decrypted
from a cache key, and the cache_controller report links/embeds that decrypted copy — clearly
labelled as a derived file, with the original cached bytes' hashes shown next to it.

## Ordering / dependency
`ParseSnapchat_iOS.main` runs the reports in the order **Communications (legacy) → Conversations →
Contacts → Memories → CacheMedia → cache_controller**. That matters:

* the **Conversations** report renders the message frame the parser built for the legacy report,
  taken before that frame's content is turned into HTML, and writes the chat manifest;
* the **Contacts** report takes the conversation summary the Conversations report returns, which is
  how a contact row links to a conversation page and shows its message count;
* the **cache_controller** report reads the chat manifest (`Conversations/cache_links.json`, else
  the legacy one) and the two manifests the Memories report just wrote (`memory_pages.json`,
  `media_by_cache_key.json`), and reads each `scdb-27.sqlite3` directly for the Memory index.

* the **CacheMedia** report (everything under `Library/Caches` that `cache_controller.db` does
  *not* index) runs before cache_controller and writes `CacheMedia/by_cache_key.json`, which
  is what lets a cache_controller entry link forward to a copy of its bytes found under
  `Library/Caches`. The two reports are disjoint by construction — see
  [report_cache_media.md](report_cache_media.md).

So there is no circular dependency, and the back-links from the chat/Memories reports are static
URLs that resolve to anchors the cache_controller report emits. Running cache_controller alone
still produces the full index; only the cross-links are missing.
