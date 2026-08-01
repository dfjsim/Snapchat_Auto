# Documentation
- [DONE-v1.3.3] Make it clear that this fork has not been tested thouroughly with multiple Snapchat versions and is provided AS IS to help analysing artifacts
  in combination with other tools and proper validations. It should probably be mentioned in the README and also with a popup that includes a
  "Don't display again" checkbox when running the app.

# GUI
- [DONE-v1.3.3] Make it remember the directory path between the ZIP extraction, keychain and temp selections.
  (Persisted to ~/.snapchat_auto_gui.json; the report directory prefills, and zip/keychain have
  "Use previous" buttons plus browse dialogs that open in each other's folder.)
- [DONE-v1.3.3] Write a note under the Timestamp timezone that explains that daylight saving will be applied.
- [DONE-v1.3.3] In Snapchat_Auto.py, get the version automatically for the logger instead of hard coding it.
  (get_version() reads pyproject.toml, falling back to installed package metadata.)

# Reporting
- [DONE-v1.4.0] For media artifacts, display a small interrogation symbol icon that the user can click to get details on
  how the link was made between the media and artifact shown.
- [RESOLVED-v1.4.0] `index.html` seems to be generated only after the pause that asks the user to press any key to continue.
  (Moved `os.system("pause")` out of `ParseSnapchat_iOS.main` and into `Snapchat_Auto.main`, after
  `write_index`, so the index is written before the prompt appears.)
- [DONE-v1.4.0] Added the source extraction ZIP and keychain/keystore paths at the top of `index.html`
  (a "Sources" block; `write_index` now takes `zip_path`/`keychain_path`).
- [DONE-v1.4.0] cache_controller report flags **cross-scope on-disk copies** — a physical copy sitting
  in a different account's `SCContent_<userId>` folder than the account(s) that claim the file (an
  untracked/materialized duplicate). Shows a ⚠ chip + on-disk marker, groups the detail paths by
  account scope, adds a "cross-scope only" filter and a summary count, and the "?" explains it. The
  claim's USER_ID stays authoritative. Verified on the 2023 GK device (4 such files, incl. the
  `6382911a…` memory whose full copy lives in the active account's scope). See `_scope_user` /
  `_resolve_on_disk` / `_cross_scope_basis`; documented in `docs/report_cache_controller.md`.
- [DONE-v1.4.0] Mirrored the cross-scope flag in the **Memories report**: each media file's source
  paths are grouped by SCContent account scope, a ⚠ "cross-scope copy" badge + "?" appears when a
  copy lives in a different account's scope than the Memory owner (`map_userids` owner lookup).
  Shared `_scope_user` helper (defined in `memories_media_report`, imported by the cache report).

# Snapchat conversations / contacts reports
- [DONE-v1.5.0] New **Conversations report** (`scripts/conversations_report.py`) —
  `Reports/Conversations/`: an index with one row per conversation (type, title, participants,
  message + attachment counts, first/last message, conversation id) plus **one detail page per
  conversation** carrying its full message table. Both tables are the shared virtual table, so a
  conversation with tens of thousands of messages opens as fast as an empty one and search / sort /
  filter / paging still cover all of it. A message row expands to the full text, the attachment at
  full size, the attachment's MD5/SHA-256 and every raw row value (including **both** the stored
  UTC timestamp and the one converted to the report's timezone). Attachments are hard-linked into
  `media/` under a name ending in their detected extension, and `mov`/`m4v`/`webm`/`gif` are
  recognised in addition to the four types the legacy report handled.
  - Conversations with **0 messages** are listed (a friend/group whose conversation id the app
    knows but for which arroyo.db holds nothing), rather than dropped.
  - Every derived value states its source in a "?": conversation type from
    `user_conversation.conversation_type` (falling back to the groups/friends lists), the title from
    `GROUP_NAME` / the contact's name / the first non-owner sender, participants from
    `user_conversation` user ids / `GROUP_PARTICIPANTS_USER_NAMES` / the distinct senders.
  - Documented in `docs/report_conversations.md`.
- [DONE-v1.5.0] New **Contacts report** (`scripts/contacts_report.py`) — `Reports/Contacts/`: one
  virtualized table of every contact (display name, username, user id, conversation link, message
  count, first/last message), with a **device owner** badge and a banner naming the artifact the
  contacts came from — including the warning that the `primary.docobjects` fallbacks are not the
  friends list and contain users who are not friends. Documented in `docs/report_contacts.md`.
- [DONE-v1.5.0] The original chats/contacts/groups report is now
  `Reports/Communications_legacy/Communications_legacy_report.html` (same output, `_legacy` name,
  like `LocalMemories_legacy`). Both chat reports render the **same** parsed rows: `main` copies the
  message frame just before it turns each `Message Content` into the legacy report's HTML.
- [DONE-v1.5.0] A message with **several cached files** is now one row, not one row per file. The
  message/cache join emits one row per claim, so a video and its thumbnail read as two messages sent
  in the same second; rows sharing a conversation + server message id are folded into one message
  holding a list of attachments (`_merge_rows`). The row shows every file, the Type column shows the
  combined types, and the expanded detail lists each file with its own hashes and cache link. Two
  *parts* of one message (12.0 / 12.1) stay separate, and rows with no server message id are never
  folded.
- [DONE-v1.5.0] **The device owner is marked wherever they are named**: next to the sender of every
  message they sent, in the participant list, next to their participant user id, and in the Contacts
  table next to both their username and their user id.
- [DONE-v1.5.0] **Contacts carry all three-or-four identifiers.** New *Legacy username* column (the
  username a contact used before renaming), read from primary.docobjects — `snapchatter` joined on
  rowid to `index_snapchatterusername` and `index_snapchatterlegacyUsername`, with the index tables'
  column names looked up rather than assumed. Each identifier column has a "?" explaining what it is
  worth (display name = local and free to change, username = changeable, user id = permanent), plus
  a "Username changed" filter and a count in the header. Missing tables degrade to an empty column
  and a note.
- [DONE-v1.5.0] **Conversation participants show display name + username** and link to that
  contact's record in the Contacts report, where all of their identifiers are together; the sender
  of an expanded message links there too. The conversation header also lists the participants' user
  ids, the only identifier that survives a rename.
- [DONE-v1.5.0] **Both message and conversation identifiers are reported**: `client_message_id`
  under the server message id in the table and in the raw values, and
  `client_conversation_id` / `server_conversation_id` on the conversation page. `getChats` selects
  those columns only when the app version's `conversation_message` has them, and the ids are
  rendered without the `.0` pandas leaves on an integer column that also holds NULLs.
- [DONE-v1.5.0] **"?" popovers are no longer clipped.** They are placed with `position:fixed` next
  to their icon and nudged back inside the window at the edges, because an absolutely positioned
  popover is clipped by the sticky column header (which hides overflow so titles can ellipsize) and
  by a virtual row. Verified in a column header, in the rightmost header (where it flips) and inside
  an expanded row: fully in the viewport and the topmost element at its centre. Fixes the two older
  reports too (their tips also inherited `white-space:nowrap` from the header).
- [DONE-v1.5.0] `cache_links.json` **version 3** (written by the Conversations report) adds an
  `href` per record, because with one page per conversation an anchor no longer says which document
  to open. `cache_controller_report.load_chat_links` prefers it, falls back to the legacy v2/v1
  manifests, and stamps those with the document they belong to. Chat chips now open the message's
  conversation page in the `scauto_convs` tab.

# Coverage and report UI fixes (v1.5.0)
- [DONE-v1.5.0] **Every SCContent cache folder is extracted and searched.** `extract_zip` filtered
  ZIP entries on the literal string `com.snap.file_manager_3_SCContent_`, so a device with e.g.
  `com.snap.file_manager_4_SCContent_` (a different generation, and no user id suffix) had those
  files left in the archive — their cache_controller entries then looked like files that were not on
  the device. The extraction list now takes glob patterns (`wanted()`), covering
  `Documents/` **and** `Library/Caches/com.snap.file_manager_*_SCContent_*`. `ParseSnapchat_iOS`
  resolves all of them too (`sccontent_folders`, logged-in account first) instead of pinning
  `_3_<uuid>`, and `mergeCache` looks a CACHE_KEY up across all of them — its multi-folder branch
  never actually resolved a file. `parseSnapvideos_PREFETCH` matches all folders as well.
- [DONE-v1.5.0] **The friends-source fall-through no longer reads as a failure, and leaves no
  scratch file.** `Can not find key 'share_user' in Group plist` was logged at ERROR on every
  newer-iOS run, although it only means this app version keeps its friends list somewhere else and
  `main` should try the next source — which on 13.49 it does, successfully, from
  `app_group_plist_storage`. It is now an INFO statement of that version fact; the older `user`
  format (which the script genuinely cannot read) is a WARNING; each fall-through says which source
  it is moving to; and one `Contacts source: …` line names the source that answered — at WARNING
  for the two `primary.docobjects` fallbacks, which are **not** the friends list. Both friends
  parsers also round-tripped the embedded NSKeyedArchiver blob through a `test.plist` file written
  into the run folder next to the reports; they now parse it in memory (`BytesIO`), so no scratch
  artifact is produced at all.
- [DONE-v1.5.0] **Partially cached Memories media is detected and flagged instead of silently
  presented as the whole media.** The cache holds only the byte ranges the device actually
  streamed, so `collect_media` was writing truncated and gap-riddled `.mp4`s that looked like
  complete recoveries — and whose broken frames made OpenCV's FFmpeg print thousands of
  `Invalid NAL unit size` / `Error splitting the input into NAL units` lines into the run log.
  Three checks now classify every recovered file: a missing **PKCS#7** tail (a complete CBC file
  always ends in padding), **holes between `<start>-<end>` shards** (`_part_coverage`, measured
  from each shard's real size on disk so either end convention works), and a **pack shorter than
  the payload length its header declares**. Incomplete files get a red badge stating exactly what
  is missing, a tinted row, a banner on the detail page, a **PART** chip and an
  *incomplete only / complete only* filter on the index, and a count in the header; plaintext
  files are marked *completeness not verified* rather than guessed at. A ciphertext whose length
  is not a block multiple is no longer discarded outright — the block-aligned prefix is recovered.
- [DONE-v1.5.0] **caching-media pack matching went from hours to minutes.** Each pack folder was
  tried against every Memory's key by AES-decrypting the *whole* item, so cost grew with
  gallery size × item size (~5 h on a 22 k-Memory gallery). `pack_matches` probes the first
  32 bytes instead — every acceptance test in `decrypt_pack` reads within the first 24 plaintext
  bytes, and CBC decrypts a prefix independently, so the verdict is identical (~50× faster per
  trial, and the item is no longer read off disk to reject it).
- [DONE-v1.5.0] **`collect_media` reports progress.** It does all the per-file work of the
  Memories report and logged nothing from start to finish, so a long run on a large gallery was
  indistinguishable from a hang. Each phase now logs its size, periodic progress and elapsed time,
  and a closing line counts the partially cached files. FFmpeg's decoder output is silenced by
  redirecting **fd 2** (`_quiet_stderr`) — the `OPENCV_FFMPEG_*` environment variables never
  suppressed it, because the capture options reach only the demuxer while those messages come from
  the decoder context. Poster frames are still extracted from partial video: the cached bytes start
  at the beginning of the file, so `generate_poster` skips the seek and takes the first frame that
  decodes.
- [DONE-v1.5.0] **Cache files the index does not know about are listed.** The cache_controller
  report was built only from `cache_controller.db`, so a file on disk that no claim / metadata /
  tombstone row leads to had no row at all. `orphan_entries` adds one per such file, in the category
  **"Not in the index"**, counted in the header and explained in a "?": no EXTERNAL_KEY, no owner, no
  timestamps — only the bytes and their hashes. A file counts as an orphan only when no indexed
  entry resolved to it, so bundle children and byte-range parts stay under their parent.
- [DONE-v1.5.0] **Text sent with media is no longer lost.** `mergeCacheChats` overwrote the parsed
  message content with the attachment's cache key; it now keeps a copy first (`Message Text`), and
  the Conversations report shows a real caption next to its media. Values that are not text (a cache
  key, an EXTERNAL_KEY, a bare media id, the attachment's own name) are not shown as a message, and
  the raw parsed value is always in the expanded row as `message_content (parsed)`. A message whose
  protobuf could not be parsed is marked "⚠ not parsed" instead of showing the parser's error string
  as the message body.
- [DONE-v1.5.0] **Expanded messages no longer take over the page, and scrolling stopped jumping.**
  Attachment previews are capped (150 px) with a link to the full-size file, and media tells the
  virtual table to re-measure when it loads (`SCV.remeasure`) — a row measured while its media was
  still blank left every offset below it wrong. `remeasure()` only measures: re-rendering would
  recreate the media and re-fire its load event forever.
- [DONE-v1.5.0] "**open**" is a chip that opens in its own tab (`scauto_conv_page` /
  `scauto_memory_page`) in the Conversations, Contacts and Memories indexes, instead of a plain link
  that replaced the index.
- [DONE-v1.5.0] Double-clicking the start of a **Snap ID / Media ID** in the Memories report no
  longer selects the label's last word with it: a label and its value were adjacent inline spans, so
  the text ran together ("Snap ID8BA4C50C…"). Verified in Chrome with Selection.modify: before
  "ID8BA4C50C", after "8BA4C50C".

# Report structure and directory paths
- [DONE-v1.3.3] Add "/Report" to "Working/Temp" in the GUI.
- [DONE-v1.3.3] Make the Working/Temp/Report directory path selection mandatory.
- [DONE-v1.3.3] Write the LOG file to the Working/Temp/Report directory.
- [DONE-v1.3.3] Put the data extracted from the ZIP file in it's own sub-directory (ExtractedData/) in the Working/Temp/Report directory.
- [DONE-v1.3.3] Rename these output folders/filenames...
  - Snapchat_iOS_report_date_time/Snapchat_report.html --> Report_date_time/Communications/Communications_report.html
  - Snapchat_iOS_report_date_time/Memories/Memories.html --> Report_date_time/Memories/Memories_report.html
  - Snapchat_LocalMemories_report_date_time/Report.html --> Report_date_time/LocalMemories_legacy/LocalMemories_legacy_report.html.
- [DONE-v1.3.3] Add Report_date_time/index.html to help navigate to other reports.

# Snapchat Memories report
- [DONE-v1.4.0] Split the Memories report into a **lightweight index** (`Memories_report.html`) plus
  one **detail sub-page per group** (`pages/<key>.html`), so it stays usable with many Memories.
  The index is a sortable/filterable table (global search, with/without-thumbnail filter, user
  filter), one row per memory: thumbnail, kind, user, ZSNAPID/ZENTRYID/ZMEDIAID, cache tokens,
  media MD5/SHA-256, created, geolocation, detail link. Sub-pages hold the full detail with MEDIA/
  SNAP IDs prominent and a back-to-index link. Second-level grouping (`assign_groups`, union-find)
  merges memories by ZMEDIAID **and** by identical non-zero media MD5 **across users** (0-byte
  excluded). Writes `memory_pages.json` (snap_id -> sub-page) so the cache_controller report links
  to both the index row and the detail page. Verified on the 2023 GK device: 80 memories -> 66
  groups, and 80/80 index<->subpage + 77/77 cache->index + 77/77 cache->detail links resolve.
- [DONE-v1.3.3] Geolocations now include a Google Maps link on the same line as the OSM link.
- [DONE-v1.3.3] Memories sharing the same cache media + AES key/IV are grouped; media, encryption and
  timestamps are shown once per group (see `_render_group`).
- [DONE-v1.3.3] "Dimensions" now falls back to the ZGALLERYSNAP ZWIDTH×ZHEIGHT for mp4 video files.
- [DONE-v1.3.3] Source paths are shown as their in-extraction/device path (anchored on `/private/var/mobile/`
  or `/Application/`) instead of the temporary extracted path. NOTE: heuristic — revisit if an
  extraction tool uses a different root layout.
- [DONE-v1.3.3] Timestamps render as two NULL-filled tables (ZGALLERYSNAP / ZGALLERYENTRY) with a fixed
  column set across all Memories artifacts.
- [DONE-v1.3.3] Surfaced extra ZGALLERYSNAP / ZGALLERYENTRY fields (`SNAP_OTHER_LABELS` / `ENTRY_OTHER_LABELS`),
  kept in separate sections so a column name present in both tables shows both values.
- [DONE-v1.3.1] Fix ".pack" files not being decoded and associated to Snapchat Memories anymore.
  (Root cause: extract_zip.py never extracted Library/Caches/caching-media. Now resolves
  Snapchat's app/app-group containers from container metadata plists and extracts within them.)
  (commit 775abb843347a6f6d9c6daf6dcc9b8c97adc4f36)

# cache_controller.db report
- [DONE-v1.3.3] cache_controller.db lookup now treats the `CACHE_KEY` as the *start* of the on-disk filename:
  media stored split into `<cache_key>_<start>-<end>` parts is discovered, concatenated in offset
  order, and decrypted (same reconstruction as `SnapFixedVideos`, but decrypted from the parts and
  hash-verified). All full copies + parts show as source paths. See `index_sccontent` /
  `_resolve_sccontent` in `scripts/memories_media_report.py`.
- [DONE-v1.3.3] New `Reports/CacheController/CacheController_report.html` (`scripts/cache_controller_report.py`).
  One row per physical cache file (`CACHE_KEY`), aggregating all of its `CACHE_FILE_CLAIM` rows and
  joining `CACHE_FILE_METADATA` (size/type/shard, the `CHILDREN` protobuf = byte-range parts or
  bundle child keys, and `CONTENT_RETRIEVAL_METADATA` = CDN URL + content ref, the latter labelled
  by value: a CDN media token, a 64-hex content SHA-256, or the CACHE_KEY). Each entry is
  resolved to its on-disk file(s) under `com.snap.file_manager_*_SCContent_*` (whole / parts /
  bundle children). Sortable/filterable table with a global search, category / on-disk / linked
  filters, and per-row expandable detail. `CACHE_FILE_SAMPLED_TOMBSTONE` deletion records are
  folded into their entry; `CACHE_KEY_VIRTUALIZATION` is listed but its semantics are marked
  unconfirmed (empty in all test data). Columns are read dynamically (schema varies by app version).
- [DONE-v1.3.3] Two-way cross-report links. cache→Memory via `snap-*-<UUID>` / `g-media-<UUID>` →
  `#mem-<snapid>` anchors added to the Memories report; the Memories report links back per media
  file to `#ck-<cache_key>` (only when that key is present in cache_controller). cache→chat via a
  `cache_links.json` manifest the Communications report now writes; `path_to_image_html` adds a
  `#cf-<cache_key>` anchor and a back-link to the cache entry. Verified on the 2023 GK FFS
  extraction (2 users): 77/77 cache-to-memory and 98/98 memory-to-cache anchors resolve.
- [DONE-v1.3.3] cache-to-Memory linking has two **fallbacks** after the primary snap-UUID-in-EXTERNAL_KEY
  match: (a) `SHA-256(memory URL token)[:16] == CACHE_KEY` for CDN-downloaded media with only a URL
  claim, and (b) a `ZMEDIAID` UUID inside an EXTERNAL_KEY. Each link records *how* it was made.
  Measured on both test extractions: the fallbacks add 0 links (the primary already resolves every
  linkable entry), so they are dormant-but-validated robustness for other app versions / cloud-only
  memories. See `load_memory_index` / `build_entries`.
- [DONE-v1.3.3] Every media file and cross-report link carries a clickable round **"?"** icon whose
  popover explains, in plain language, how that association was derived (matched identifier, primary
  vs fallback, how the bytes were located/decrypted). Added to both the cache_controller report and
  the Memories report (`_info` + `how`/`memory_basis` strings).
- [DONE-v1.3.3] Documented the reports and their linking logic under `docs/`:
  `cross_report_linking.md` (the anchor scheme + every link basis), `report_cache_controller.md`,
  `report_memories.md`, `report_communications.md`.

# cache_controller.db report — follow-up improvements
- [DONE-v1.4.0] Field-8 of `CONTENT_RETRIEVAL_METADATA` was mislabelled "Content SHA-256". It is
  usually a CDN media token, sometimes a 64-hex hash, sometimes the CACHE_KEY — and even the 64-hex
  form is a **source-side** hash that need not match the cached bytes (proven on `f1cd5e24…`, an
  app_install_screenshot whose field 8 matched neither the cached file nor the download). Now
  labelled by real column name + value-type + a "?" caveat, and the report additionally computes and
  shows the **actual cached file's** MD5/SHA-256 (`materialize_ondisk`).
- [DONE-v1.4.0] Cached media files are now **viewable even when unlinked** to a Memory/chat:
  recognizable plaintext media (≤30 MB) is copied to `files/<CACHE_KEY>.<ext>` and embedded/linked
  (👁 marker in the table). Encrypted bytes are hashed but not copied.
- [DONE-v1.4.0] Detail panels now use the **real DB column names** (description in parentheses).
- [DONE-v1.4.0] Added an **Expand all** / Collapse all button.
- [DONE-v1.4.0] Memories index: ZMEDIAID/ZSNAPID/ZENTRYID combined into one labelled column;
  geolocation shows OSM **and** Google links; the Detail column shows each group's snap count.
- [DONE-v1.4.0] View unlinked cached files; real DB field names; the "SHA-256" field-8 finding
  (source hash, may not match cached bytes) + actual cached-file hashes; "Expand all" button.

# Report UI bugs (v1.4.2)
- [DONE-v1.4.2] **Big index tables are now virtualized** (new `scripts/report_ui.py`). Rows live in
  `data/index.js`, each cache_controller row's detail HTML in a `data/detail-<n>.js` chunk fetched
  only when that row is expanded, and only the rows in the viewport are put in the DOM. The
  cache_controller document went from 1.9 MB to 20 KB on the test extraction; a synthetic
  101 200-row index opens in **0.70 s** (search 0.18 s, sort 0.04 s, ~180 MB heap) where the old
  one-big-table layout was unusable. Search/sort/filters still cover the full index (each row
  carries a pre-built search string). Data files are loaded with `<script src>` because `file://`
  pages may not `fetch` their siblings; a red banner appears if `data/` was left behind.
- [DONE-v1.4.2] **Anchors work on repeat clicks into an already-open tab.** Reports open each other
  in named tabs, and a click whose URL (fragment included) equals the tab's current URL fires no
  event at all — which is why a cache_controller link only expanded its target the first time.
  `NAV_JS` now consumes the fragment (`location.hash='_'`, a sentinel that does not scroll to top;
  `history.replaceState` throws on `file://`) so the next click is always a real `hashchange`.
- [DONE-v1.4.2] Anchor targets are scrolled **clear of the sticky toolbar + column titles** (the
  Memories index bug) and highlighted, in every report — including the plain Communications report
  and the Memory detail sub-pages.
- [DONE-v1.4.2] Clicking a **link or "?" inside a cache_controller row no longer toggles the row**,
  so following a cross-report link no longer leaves the row you left behind expanded/collapsed.
- [DONE-v1.4.2] **Chat → cache_controller links from saved media resolved.** `SCPersistentMedia`
  attachments are not named after a cache key, so they produced a dead `#ck-<filename>` link; they
  are now matched to the claim carrying the same `<conversation>:<message>:<part>` triple
  (`mapPersistentMediaToCacheKeys`). Verified: 19/19 links in the test report resolve to a row.
- [DONE-v1.4.2] **A message with two attachments links back from both.** `cache_links.json` is now
  version 2 with a `by_message` index, and cache_controller matches claims by the conversation +
  message in their `EXTERNAL_KEY`, so a message's full-media, thumbnail and content claims all link
  back (chat-linked entries went from 17 to 33 on the test extraction).
- [DONE-v1.4.2] **Extensionless media no longer breaks the browser.** Every viewable file is
  published under a name ending in its real extension — as a **hard link** (same bytes, no data
  duplicated; 113/113 published files on the test extraction are links) — in both the
  cache_controller `files/` folder and the Communications `cacheFiles/` folder.
- [DONE-v1.4.2] **Bundle children are resolved and viewable.** For `TYPE=3` the `<CACHE_KEY>` file
  is only the CHILDREN descriptor; children are stored as `<CACHE_KEY>_<child name>` and were never
  located. They are now hashed, typed and published individually — this is what makes the chat video
  of message 12.0 (a 219 KB `.mp4` + 40 KB `.webp` overlay) viewable, and the descriptor's
  "detected type" now says so instead of reading as unrecognized.
- [DONE-v1.4.2] **Encrypted cache files link to their decrypted copy.** The Memories report writes
  `media_by_cache_key.json`, and the cache_controller report shows/links that plaintext copy
  (labelled derived, with both files' hashes and the Memory's ZSNAPID). Openable on-disk entries:
  109 → 199 of 228, with 14 zero-byte and 15 still-encrypted entries now labelled as such.
- [DONE-v1.4.2] The cache_controller "on disk" glyphs were replaced by a real **preview thumbnail /
  ▶ play button** per row.
- [DONE-v1.4.2] **Each attachment of a message links to its own cache entry** (follow-up: the first
  fix made both attachments of message 12.0 point at the thumbnail's entry). Two causes:
  - the saved-media mapping ran against the `mergeCache`-filtered frame, which keeps only claims
    whose `CACHE_KEY` file is directly recognizable media — that drops exactly the full-media claim
    of a chat video, whose `CACHE_KEY` file is the 90-byte bundle descriptor. It now uses the raw
    `CACHE_FILE_CLAIM` rows;
  - the claim preference ignored the file's own type. `_rank_claim` now prefers an exact type
    match, sends `thumbnail…` files to `thumbnail~1:` and everything else to the full media `1:`.
  Verified byte for byte: every one of the 19 attachments' SHA-256 equals the linked entry's bytes
  or one of its bundle children's — message 12.0's PNG → `thumbnail~1:…12:0:0`, its .mov →
  `1:…12:0:0` whose child `z2a132f1f…` *is* that video.

# Report UI: paging, selection, MEO indicator, offline maps (v1.4.2)
- [DONE-v1.4.2] **My Eyes Only indicator** in the Memories index: a red MEO badge in the Kind
  column, matched by a search for "meo"/"my eyes only", plus a My Eyes Only filter
  (any / only / exclude).
- [DONE-v1.4.2] **Paging** on both index tables — rows per page (100/250/500/1000/5000/all,
  default 500) and first/prev/page-picker/next/last. Paging applies after filtering and sorting, so
  search, filters and "select all shown" still cover the whole index; following an `#anchor` turns
  to the page holding the target first.
- [DONE-v1.4.2] **Row selection** in both index tables and on the Memory detail sub-pages: a
  checkbox per row/memory, a "Selected only" filter, select/unselect everything matching the current
  filters, and a selected count.
  - The storage problem, measured in Chrome: `localStorage` on a `file://` page is partitioned per
    browsing context — a second tab on the *same* file starts empty, another page in the same folder
    starts empty, and an iframe bridge is partitioned too; it only survives a reload of that tab.
    There is therefore **no browser storage the index and a sub-page can share, and none that
    outlives the tab**.
  - So selections live in **`Reports/selection.js`**, written empty at generation (and never
    overwritten afterwards) and loaded by every page of the run. Ticks are held in memory, flagged
    "unsaved" (with a leave-page confirmation), and **💾 Save selections** downloads a new
    `selection.js` to drop next to the reports and file with the case; **Load…** reads one back.
    `localStorage` is kept only as a same-tab safety net for accidental reloads.
- [DONE-v1.4.2] **Offline map tile server support** (`scripts/offline_maps.py`). A tile server URL
  (server root or a `{z}/{x}/{y}` template) can be set in the GUI, with a **Test** button that
  fetches a tile immediately and a re-test before the run starts. When set, each geolocated Memory's
  detail page gets a stitched 3x3-tile map with a marker at the recovered coordinates and a link
  that opens the tile server at the same place. Nothing is ever fetched when it is empty — the
  reports never reach the network on their own. Tiles are cached and memories at the same
  coordinates share one image (22 geolocated memories → 3 images / 27 requests on the test set); the
  map is labelled a derived artifact, with a "?" recording the server, zoom and tile count; a server
  that stops answering degrades to a warning instead of failing the run.

# Analysis / Reverse engineering
- [DONE-v1.4.0] Check if we have metadata in `cache_controller.db` for all files in `Documents/com.snap.file_manager_3_SCContent_...`.

# Other
- [RESOLVED-v1.4.0] Earlier note about `path_to_image_html` reading `platform` as a global: on closer
  inspection this was NOT a bug — `main()` declares `global platform` (ParseSnapchat_iOS.py:1342)
  and sets it before any attachment is rendered, so it always works. Hardened anyway with a
  module-level `platform = system()` default so it no longer depends on `main()` running first.
