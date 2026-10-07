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

Two things follow from this page being the single reference, and both are load-bearing:

* **The link scheme is also the relation graph.** A partial report grows the examiner's selection along
  exactly the associations described below — a ticked cache entry pulling in its Memory is this page's
  `cache_controller → Memory` link, read as a relation. So the generators hand those associations to
  `partial_report.expand` as *edges* rather than deriving them a second time; an edge is recorded in
  whichever direction its owner works it out, and both relations over it find it. See
  [report_partial.md](report_partial.md).
* **Every cross-report link is emitted through `report_ui.xref`.** That is the one place that can mark a
  link whose target is not in the folder, which a partial report needs and a full report never triggers
  (with no closure `xref` returns the caller's markup untouched). A new link that does not go through it
  will silently point at nothing in a partial extract.

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

A message with no `server_message_id` (one the app had not finished sending) is anchored on the
**device's own** id instead — `msg-c<client_message_id>`, which `arroyo.db` assigns and which is unique
within a conversation, so it is a fact about the row exactly as the server id is. Only a message with
neither id falls back to its **position**, `msg-row<N>`; that is the one anchor in this table that is
not evidence, and `partial_report` refuses to match a selection on it (see
[report_partial.md](report_partial.md)). Duplicate anchors get a `-2`, `-3`, … suffix.

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
| cache_controller | Memories | every listing Memory's snap id | the file is an asset of a filter ≥ 2 Memories' overlay records list |

A single-target link stays a plain `#anchor`, which highlights the row it lands on.

## The links, and how each is derived

### cache_controller → Memory
Tried in priority order; the first that matches wins, and the icon records which one:

1. **Snap-scoped claim (primary).** A `CACHE_FILE_CLAIM.EXTERNAL_KEY` of a Memory-scoped shape
   whose UUID equals a `ZGALLERYSNAP.ZSNAPID`. The shapes are the single list
   `SNAP_CLAIM_PREFIXES` / `SNAP_CLAIM_SUFFIXES` in `memories_media_report`, read by
   `classify_snap_claim` — see [the claim shapes](#which-external_key-shapes-name-a-memory) below.
2. **CDN URL token (fallback).** The file's `CACHE_KEY` equals `SHA-256(token)[:16 bytes]` where
   `token` is the last path segment of the Memory's `ZMEDIADOWNLOADURL` / `ZOVERLAYDOWNLOADURL` /
   `ZTHUMBNAILDOWNLOADURL`. This catches downloaded media whose claim is only a URL, with no
   snap-scoped key.
3. **ZMEDIAID (fallback).** A UUID inside an `EXTERNAL_KEY` matches the Memory's `ZMEDIAID`
   (used only when it is *not* also a `ZSNAPID`).
   **ZSNAPID in a full-media key of another shape (fallback).** A full-media claim
   (`MEDIA_CONTEXT_TYPE` 19) whose `EXTERNAL_KEY` is none of the Memory-scoped shapes carries the Memory's
   `ZSNAPID` — `<snapId>~1`. An exact identifier, so it links, in both reports: the Memories report
   locates the same file (`collect_media`, through `index_claim_uuids`) and decrypts it with the Memory's
   key, so the Memory's page shows what the cache_controller report says belongs to it. A `<UUID>~<n>`
   whose UUID is no snap id (the snap editor's, context 34) links to nothing.
4. **A MemData identifier (fallback).** A UUID inside an `EXTERNAL_KEY` is one the Memory records
   about itself in `ZGALLERYSNAP.ZMEMDATAIDS` (`snapMemDataId` / `entryMemDataId`) or in its entry's
   `ZGALLERYENTRY.ZMEMDATAID` (newer app versions; `memories_media_report.decode_memdata`). An entry's
   id is shared by every snap of the entry, so an id that more than one Memory records links to none
   of them. Like rule 3, a recorded identifier — never a time or content match. The key shapes this
   applies to (e.g. `<UUID>~1`) name no Memory by themselves, so they stay out of the shape list
   below.
5. **Proven by content (last).** The file is byte-identical (SHA-256) to a Memory's media — first as
   this run **recovered it from the device** (decrypted with the Memory's own key, or stored plain), then
   as it was **retrieved from Snapchat's servers** ([cloud_download.md](cloud_download.md)), decrypted or
   as received. `cloud_memories.find_identical` makes the comparison (sizes first, then SHA-256) and
   never compares the device's copies with a file some Memory's media was recovered from: identifiers
   link that one already. `Memories/media_by_content.json` carries the matches (`by_cache_key` for this
   report, `by_sha256` for the Library/Caches one); the chip gains ≡ (the device's copy) or ☁ (a server
   copy), and the row's detail says which copy and where it came from — for a server copy, the
   retrieval and its authority. The only link here that no identifier on the device supports, so any of
   rules 1-4 wins over it. The snap editor's working copy of a snap later saved to Memories is the case
   it was built for: on a test device the working copies that are a Memory's media link this way
   without any retrieval.

### cache_controller → Memory: an asset of a filter its overlay record lists

Not one of the rules above, and never the Memory's media. A Memory's overlay record —
`ZGALLERYSNAPDETAIL.ZOVERLAY`, the row whose `ZSNAP` is the Memory's `Z_PK`, an NSKeyedArchiver archive
of `SOJUGallerySnapOverlay` (see
[report_memories.md](report_memories.md#the-overlay-record-zgallerysnapdetailzoverlay)) — lists the
snap's geofilters, and three fields of a geofilter hold a URL: `imageUrl`,
`arSegmentation.sky.replacementSkyUrl` and `geofilterMarkups[j].displayParameters.font`. A claim whose
`EXTERNAL_KEY` is one of those URLs is a cached asset of a listed filter
(`cache_controller_report._overlay_links_for`, reading `scripts/data/snap_overlay.py`):

* **The whole URL, by one rule** (`snap_overlay.normalise_url`): an http(s) URL, its scheme lower-cased,
  one empty trailing `?` or `#` dropped — some claim keys are the record's URL with an empty query added
  — and nothing else changed: nothing unquoted (both sides store base64 padding as `%3D`), no case
  change of host, path or query. An id inside the URL is not enough: the same last path segment recurs
  under other hosts and paths, and one `mo=` / `bo=` value under other ids. A re-fetch of the same
  asset under another `uc=` is therefore a missed link, never a wrong one.
* **Not the shared address.** A geofilter whose `imageUrlParams` dictionary has entries (the Bitmoji
  filters) gives one shared address as its `imageUrl`, the image being in the parameters: that URL is
  never an asset. The rule lives in `snap_overlay.filter_assets`, not in a caller.
* **Listed, not shown to be used.** The record commonly lists several geofilters and names the selected
  one separately (`filters.geoFilterSelectedId` / `geoFilterSelectedIds`, often none), so a listed
  filter is not shown to be on the Memory. The chip says *filter listed* — *filter selected* only when
  the record names that filter's `idValue` — and the "?" says what the record names, and states the
  Memory's `ZGALLERYSNAP.ZHASOVERLAYIMAGE`, to compare with the Memory's own overlay entry.
* **Any account.** Neither the claim's context nor its account is restricted: on a device with two
  accounts, one account's claim can be an asset the other account's Memories list. The link is made,
  and its "?" and the detail's claim cell say the claim is another account's.
* **A relation of its own.** It is `entry["filter_memories"]`, never `entry["memory"]`: not counted as
  linked to a Memory, never a lead, never decrypted with the Memory's key, its own `Filter` value of the
  Linked filter, its own partial-report edge (`EDGE_MEMORY_FILTER_ASSET`; relations `mem_filter_assets`
  and `cache_filter_memories`, both off by default). An entry linked to a Memory as its media is not
  linked to the same Memory again this way. One asset is commonly listed for many Memories, so several
  are one dashed `#find=` chip, and the detail lists each Memory with its own "?".

Both reports read the record through the same module and match by the same rule, so the Memory's page
lists the same files (see below) without a manifest passing between them.

### Not a link: a creative-tools item that names a cached file

An item of an account's creative-tools store (`primary.docobjects` › `ctp__item_5`, read by
`scripts/data/ctp_items.py`) can name a cached file — the claim key is one of the item's asset URLs, or
names the item by its id — and the cache_controller report says so on the entry, as an explanation of
what the file is (*Creative tools asset*). It is not a cross-report link: the store has no report, so
there is no target and no anchor, it goes through neither `report_ui.xref` nor a partial-report edge, and
nothing points back at the entry. The rule and the store's layout are in
[report_cache_controller.md](report_cache_controller.md#creative-tools-items--primarydocobjects--ctp__item_5).

### Which `EXTERNAL_KEY` shapes name a Memory

One list, read by **both** reports, because they link in opposite directions and a shape only one
of them recognises is a cached file tied to a Memory whose own page does not list it:

| Shape | Role |
|---|---|
| `snap-media-<UUID>`, `snap-asset-raw-media-<UUID>`, `g-media-<UUID>` | full |
| `snap-overlay-<UUID>` | overlay |
| `snap-rendered-lowres-<UUID>` | rendered |
| `snap-thumbnail-<UUID>` | thumbnail |
| `<UUID>_memories_backup_transcoded` | transcoded |

Two rules earn their place here:

* **Match the shape exactly, never by substring.** Testing "does the text before the UUID contain
  *media*" swept in `https://…/previewmedia/<UUID>` — not a Memory claim at all — and those UUIDs
  then reached the deleted-Memory carver as candidate snap ids, where on one extraction they were
  the larger part of everything it tested.
* **The UUID is not always at the end.** `<UUID>_memories_backup_transcoded` puts it first, so a
  prefix test sees an empty string and drops the claim. These are `MEDIA_CONTEXT_TYPE` 19 (full
  media) and decrypt with the Memory's own key to a complete MP4 — on the corpus they are the only
  copy of some minute-long videos, recovered by nothing else in the report. Verified on two devices.

After these, every `EXTERNAL_KEY` in the corpus that carries a Memory's `ZSNAPID` or `ZMEDIAID` is
matched. To re-check that on a new extraction, group the claims by shape (UUID replaced by a
placeholder) and ask, per shape, whether its UUIDs are snap ids, `ZMEDIAID`s or neither — a shape
that is neither and *should* be is the only kind of gap this design can still have.

A memory-linked cache entry shows **two** links: the index row
(`Memories_report.html#mem-<ZSNAPID>`) **and**, when `memory_pages.json` is present, the detail
sub-page (`pages/<key>.html#mem-<ZSNAPID>`). Both open in the `scauto_memories` tab.

### Memory → cache_controller
Per recovered media file, the Memory report links to `#ck-<CACHE_KEY>` **only when that key is
present in `cache_controller.db`** (`all_cache_keys`). The key is the one used to locate the file:
either `SHA-256(url token)[:16]` or the `cache_controller` `EXTERNAL_KEY` target.

Claims are looked up by the Memory's `ZSNAPID` **and** by the media-object ids its row references
(`m["media_refs"]` — `ZMEDIAID`, `ZDUPLICATEDFROMSNAPID`), which is the mirror of fallback 3 above:
a claim can name the media object rather than the snap, and a Memory moved into My Eyes Only is
exactly that case. The Memory's MemData identifiers are looked up the same way (`index_claim_uuids`), the
mirror of rule 4, under the same rule: an id another Memory records too is not used. Both are used, not one instead of the other — a Memory has several cached files
and only some of the claims name it by `ZSNAPID`. Every hit is still confirmed by the file
decrypting, so the id match selects candidates rather than asserting the association.

A Memory's page also lists the cached assets of the filters its overlay record lists — found by
`collect_media` through `index_claim_urls`, with the whole-URL rule above — in a section of its own
after Media files, *Cached assets of filters listed with this Memory — not its media*, each linking to
`#ck-<CACHE_KEY>`. They are not media files of the Memory, nothing is decrypted for them, and they are
not among the cache keys a selection names the Memory by (`_cache_tokens`). On a page several Memories
share, each asset is given under the Memory whose record lists it.

Note that this list also feeds `carve_deleted_memories`: a claimed UUID with **no** `ZGALLERYSNAP`
row is a candidate deleted Memory. Indexing a shape that is not a Memory claim therefore does not
merely add a bad link — it sends the carver hunting through files that were never Memories.

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
 "by_message": {"<conversation id>|<server message id>": [ …the same records… ]},
 "messages":   {"<conversation id>": {"title": "…", "href": "Conversations/pages/<key>.html",
                                      "anchors": {"12.0": "msg-12.0", "13.0": "msg-13.0"}}},
 "by_content_id": {"<id>": [{"conversation_id": …, "server_message_id": "12.0",
                             "rule": "media|share|sticker|sticker-name"}]}}
```

`by_key` and `by_message` cover the messages that have an attachment. `messages` lists **every**
message, compactly (one title and page per conversation, an anchor per message): a claim's key can
name a message whose file the chat join did not attach — a kind it does not display, or a file it did
not choose — and `load_chat_links` turns these into the same records. `by_content_id` holds the ids
each message names its media by (`arroyo_content.content_ids`, read from arroyo.db by
`conversations_report.load_content_ids`): the media id of its `local_message_references` (`media`), a
shared item's id at `4.4.5.5.1` (`share`), a sticker's id at `4.4.14.2.6` in base64 (`sticker`) or its
name at `4.4.4.1.2` (`sticker-name`).

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
   When no row of that part is listed, the claim links to the message by its **number**
   (`ChatIdIndex.message`): the report lists message 12 as `12.0` unless a claim of another part was
   joined onto it, and a part of message 12 is still message 12 (e.g. `animationmedia~1:…:12:2:0`).
   The "?" spells out that such a link points at the *message*, not at that exact file.
3. **`by_content_id`.** A claim whose `EXTERNAL_KEY` contains one of the ids a message names its media
   by — the whole id, a UUID-based one in any letter case, base64 exactly — links to that message
   (`ChatIdIndex.content_links`): `content~<MEDIA_ID>`, `thumbnail~<MEDIA_ID>`,
   `SnapVideoFilterState-<MEDIA_ID>` and the like are other cached files of the media the message's
   `local_message_references` names; a shared item's or a sticker's other claims, likewise. The "?"
   names the id and the field it was read from.

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
  `media_by_cache_key.json`), and reads each `scdb-27.sqlite3` directly for the Memory index (its
  overlay records included).

* the **CacheMedia** report (everything under `Library/Caches` that `cache_controller.db` does
  *not* index) runs before cache_controller and writes `CacheMedia/by_cache_key.json`, which
  is what lets a cache_controller entry link forward to a copy of its bytes found under
  `Library/Caches`. The two reports are disjoint by construction — see
  [report_cache_media.md](report_cache_media.md).

So there is no circular dependency, and the back-links from the chat/Memories reports are static
URLs that resolve to anchors the cache_controller report emits. Running cache_controller alone
still produces the full index; only the cross-links are missing.
