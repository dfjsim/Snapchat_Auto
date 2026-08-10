# Corpus test-run notes — the fixes themselves are DONE (see DONE.md, "Corpus test-run fixes (v1.5.0)")

Kept here because they are the things a future run should NOT re-investigate: on the four-device
corpus these are the correct answers, not defects.

- On the iOS 26 dual-account device, the Memories with no media are a data-availability fact, not
  a matching failure: that extraction holds 26 `caching-media` folders against 87 in an earlier
  extraction of the same phone. Brute-forcing every key against every pack confirms 48 of 50
  unlock; the 2 that do not are the other account's My Eyes Only.
- That device has a single `arroyo.db`, for one of its two accounts, while most chat-media claims
  belong to the other, so those claims are correctly absent from this account's conversations. The
  run says so in the log, and the cache_controller report lists them under the account that
  claimed them.
- A backup-class keychain has no `egocipher`, so "0 geolocated" is expected on that device.
- One device has no `sccache.gallery-stories-snap.data` directory at all, so "0 decoded/decrypted"
  is correct there.
- The "0 bytes" chat-media rows on the iOS 26 devices are evicted content with the claim retained
  — already documented in `docs/snapchat_ios_cache_media.md`.
- `sccache.gallery-stories-snap.data` decryption works (old-schema device: 2 PNG + 1 MP4;
  iOS 26 single-account: 2 PNG).
- `0 file(s) on disk are not referenced by cache_controller.db` is accurate on all four devices —
  the `<cache_key>_z<hex>` files are bundle children and are resolved by `child_ondisk_paths`.

### Still open from that pass

- `ParseSnapchat_iOS.py:1547` takes `arroyo[0]`. Every device in this corpus has exactly one
  `arroyo.db`, so it is not biting yet, but a device with two logged-in accounts would silently
  lose one account's chats. Needs a device with two arroyo databases to fix against.
- The WAL carver only reads superseded `-wal` frames. Free space inside live pages (and in the
  main database file) can hold deleted rows too and is not searched.

# Snapchat Conversations / Contacts reports  [the reports themselves: DONE — see DONE.md]
- Add a way to select only specific conversations or parts of conversations and their associated contacts and output them to PDF with attachments.
  - The selection half exists: conversations are selectable on the index (kind `conv`) and
    individual messages on a detail page (kind `msg`), shared with every other report through
    `Reports/selection.js`. What is missing is the export.
- Validate against the legacy report on more extractions, then remove the legacy one:
  - message counts per conversation, and the rows each report drops (see `_drop_unrenderable`);
  - that every attachment the legacy report inlined is also shown here, with the same bytes;
  - contacts: that no row of the friends artifact is lost by the normalizers.
  - Removal steps: drop the `getHtml` call + `outputDir`/`cacheFiles` handling in
    `ParseSnapchat_iOS.main` (move the attachment copying to the Conversations report), the
    "Communications (legacy)" entry in `write_index`, and the v2 branch of
    `cache_controller_report.load_chat_links`.
- [FIXED-v1.5.0] Text sent *with* media used to be lost (the parser replaced the message content
  with the attachment). The parsed content is now preserved as "Message Text" and the Conversations
  report shows it; the legacy report still shows only the attachment.

# Snapchat Memories report

- ~~Fold a Memory group behind its lead row, and filter by time.~~ **Done** — a group is one row with
  its members inside it, and both the Memories and Conversations reports have the shared date/time
  window (the latter with a conversation / message / both scope selector). See DONE.md,
  [report_memories.md](docs/report_memories.md#a-group-is-one-row-with-its-members-inside-it),
  [report_ui.md](docs/report_ui.md#folded-rows-cfolded-scvfoldhits-openfoldhits) and
  [report_conversations.md](docs/report_conversations.md#the-time-filter-and-its-scope-control).
- Add a way to select only specific Memories and their associated media files and output them to PDF with attachments.
  - The **selection and report half is done** — `--selection` builds a partial report holding only the
    ticked Memories (and whatever related items are asked for), see docs/report_partial.md. The PDF
    half is not: it is the last, gated phase of that work.
- ~~Some published Memory media is never referenced by any page.~~ **Fixed** — identical content is now
  published once and every Memory that recovered it links to that one copy, see DONE.md and
  [report_memories.md](docs/report_memories.md#one-copy-per-distinct-content-in-media).
- We need to be able to filter/search by URL.
- ~~Fix MEO decryption that fails in some cases.~~ **Fixed in v1.5.2** — four separate causes, see
  DONE.md ("Snapchat Memories report"). Still open in this area:
  - A My Eyes Only Memory captured *directly* into MEO (not moved into it) still needs the
    keychain's `persistedkey`, and there is no way around that — it is the correct outcome, not a
    defect. Worth re-confirming on a device that has both kinds.
  - `caching-media` `.pack` files are linked to a Memory **by** decrypting them, so a Memory with
    no usable key can never be matched to one, however its media is stored. Only a keyed path
    exists there.

# Keychain auto-detection
- Add logic to locate GK/Cellebrite/XRY keychain files either inside or outside the extraction ZIP.

# Android tests/improvements
- Make sure we properly support all the same features on Android than on iOS, for example:
  - Keystore auto-detection.
  - Memories decoding with media/geolcation decryption.

# Cleanup: remove legacy Memories report + SnapFixedVideos (AFTER validation)
- Keep the legacy path for now. Only remove it once the new Memories + cache_controller reports
  have been fully tested and cross-validated against the original/legacy output on several
  extractions (multiple OS/app versions and extraction tools).
- Why it is redundant:
  - `scripts/parseSnapvideos_PREFETCH.py` reconstructs split videos from their byte-range parts into
    `SnapFixedVideos/<cache_key>.mp4` (still ENCRYPTED). It is created once from `Snapchat_Auto.main`.
  - It is consumed ONLY by the legacy `scripts/DecryptLocalMemories_iOS.py` report, which copies those
    reconstructed files back INTO the extraction's SCContent folder (renamed to the cache key) just
    to decrypt them.
  - The new reports already reconstruct split files directly from the parts (`index_sccontent` /
    `_resolve_sccontent` / `materialize_ondisk`) and the Memories report decrypts them in place, so
    both `SnapFixedVideos` and the legacy report are dead weight. Verified on a split video in the
    test corpus.
- Removal steps when we get to it:
  - `Snapchat_Auto.py`: drop the `parseSnapvideos_PREFETCH.main()` call + the `SnapFixedVideos`
    existence check, and the import.
  - `ParseSnapchat_iOS.py`: drop the `DecryptLocalMemories_iOS.main()` legacy-report block and its
    import there.
  - `write_index`: drop the "Local Memories (legacy)" entry.
  - KEEP `scripts/DecryptLocalMemories_iOS.py` — the new Memories report reuses its `readKeychain`
    (imported as `_memkeys`). Optionally delete `scripts/parseSnapvideos_PREFETCH.py` (unused after).
  - Benefit: faster runs and no longer writing into `ExtractedData`.

# Evidence hygiene: something still opens a database in place

`sqlite_open` stages **copies** specifically so SQLite never creates or updates a `-shm` beside the
original and never checkpoints it (see that module's docstring, and
[sqlite_wal_handling.md](docs/sqlite_wal_handling.md)). But two runs of the same build over the same
already-extracted folder leave the **Memories databases' `-shm` with a new mtime** — so something on
that path still opens the original read-write, or read-only in a way that lets SQLite rebuild the
shared-memory index. Found while establishing the run-to-run noise floor for the partial-report work.

- The `-shm` **content** is unchanged (identical SHA-256 both runs), and no `-wal` or main database
  file is touched, so nothing recovered is affected. It is the *writing into the evidence copy* that
  should not be happening.
- Likely candidates: the SQLCipher path (`memories_media_report.decrypt_gallery_db` stages a copy, but
  `scdb-27.sqlite3` is also read on that path), or the bundled `sqlcipher3.exe` invocation. Compare
  each database's `-shm` mtime before and after a run to narrow it down.
- Related goal already recorded below: stop writing into `ExtractedData` at all (see the legacy
  Memories / SnapFixedVideos cleanup).
- Why it matters beyond tidiness: `scripts/source_fingerprint.py` deliberately does **not**
  fingerprint the `-shm` because of this. If the tool stopped touching it, the `-shm` could be
  fingerprinted like the `-wal` — but as long as our own run moves it, hashing it would make the tool
  fail its own source verification over a file it modified itself, which is exactly the false alarm
  that teaches an examiner to wave a sidecar difference through.

# Code cleanup, performance and optimization
- Fix Pylance/Pyright/Ruff warnings/errors.
- Consider giving the user an option to make the report dependent on the device extraction ZIP archive for unencrypted media files.
  We would not have to keep a copy of so much extracted media files. It might not be worth it depending on the ratio of encrypted/unencrypted files.
- Check whether anything is worth copying from the standalone `keychain_decoder.py` in the
  `bplist_base64_decoder` project.

# New report for `cache_controller.db` data. [DONE — see DONE.md]
- Remaining/uncertain: `CACHE_KEY_VIRTUALIZATION` was empty in every test extraction, so the
  `VIRTUAL_CACHE_KEY` ↔ `CACHE_KEY` semantics are unconfirmed — its rows are listed but no linking
  logic depends on them. Revisit once a populated sample is available.
- We need to be able to filter/search by URL.

# New report for other cached files in `Library/Caches/*`
- See `docs/snapchat_ios_cache_media.md`
- **The "modified" column is our extraction time, not a device time.** `cache_media_report.py:1102`
  fills it with `os.path.getmtime()` of the **extracted copy on the analyst's disk**, and `:1455` heads
  the column plainly `modified` in the "Copies on disk" table — beside the path, size, producer and
  stored SHA-256, which *are* device facts. Found by the corpus gate: re-extracting the same ZIP moved
  every one of that report's timestamps to the moment of the unzip, which is proof the value carries
  nothing from the device. Two problems in one — a reader takes it as a fact about the evidence, and it
  stamps the processing date onto every row (the public-repo rule about extraction dates rests on the
  same reasoning, and it applies at least as much to what a report asserts).
  Decide between: reading the timestamp from the **ZIP entry** (which may carry the device's, and can
  be checked against the archive), dropping the column, or keeping it under an unambiguous label such
  as *"mtime of the extracted copy — not a device time"* with a "?" saying so. Do not relabel without
  first checking whether the ZIP holds the real value: a column that could carry a device time is worth
  more than one that admits it carries none. `mtime` has exactly two references, so the edit is small
  either way; it is the decision that needs the care.

# Add support for offline tile map server [DONE — see DONE.md]
- Remaining ideas (not done):
  - A map on the Memories *index* (the index only shows coordinates + OSM/Google links today).
  - One overview map plotting every geolocated Memory of the case.
  - Configurable zoom / map size (currently zoom 15, 3x3 tiles).
- Example URL: http://hostname:port/#map=15/40.000000/-70.000000
  - With our OSM tile server, this URL brings us to the Ubuntu Apache2 default page.
    https://github.com/dfjsim/osm-tirex

# UI bugs [DONE — see DONE.md "Report UI bugs (v1.4.2)"]
- The whole list (tab reuse / anchors, extensionless media, small view icons, big-table performance,
  unviewable encrypted + bundled cache files, two attachments in one message) was fixed in v1.4.2.
  Shared UI code now lives in `scripts/report_ui.py`; see `docs/report_ui.md`.
- Selections (v1.4.2): a `file://` page has no storage that survives closing the tab or that two
  pages of the same run can share (measured — see `docs/report_ui.md`), so selections live in
  `Reports/selection.js`, which the examiner saves from the report. Worth revisiting if we ever
  ship a small local server or a desktop shell, which would allow silent persistence.
- Still worth re-checking on other extractions:
  - ~~The 15 cache entries that remain "🔒 encrypted" on the iOS 16 test device — are any of them
    decryptable from a source we already have (chat media keys)?~~ **Answered and fixed in
    v1.5.0** (see DONE.md, "Corpus test-run fixes"). Almost none of them were encrypted at all: of
    the 253 files 2023 used to padlock, 208 are LZC lens bundles and only 7 are genuinely
    encrypted. The one encrypted *Memory* file there is a My Eyes Only snap whose
    key is wrapped and for which that keychain holds no `persistedkey` — so it is correctly
    unrecoverable, and now says so.
  - Row cells in the virtualized index tables have a fixed height and clip long values (the full
    value is always in the row detail / detail page). Confirm that reads well on other datasets,
    e.g. accounts with many cache tokens per Memory.

# Planned features
- Integrate Snapchat_Download support with guardrails (reminding the user to have proper legal authorization).
- Implement feature to recreate a partial report from the selected elements only.
  - Ask the user if we also include all the elements related to the ones selected
    (for example, the cache_controller and/or Libracy/Caches entries associated to a selected Memory).
- ~~Fix messages decoding from arroyo.db... we are currently missing many that are displayed by at
  least one other tool.~~ **Fixed in v1.5.2** — see DONE.md ("Snapchat conversations / contacts
  reports"). Still open in this area:
  - The other event kinds under `4.4.8` — fields `2`, `5`, `6`, `8` and `22`, of which `2` is by far
    the most common — are identified as events but not named. They are labelled `System message`
    with no description.
    Naming them needs ground truth: compare a conversation containing them against another tool.
  - `proto_to_msg` still concatenates every string in the protobuf, and `message_content` still
    depends on that for the cache join. Worth reading the media id from its own field too, so the
    concatenation can go.

- When we ask the user to decide if he wants to include related artifacts in the subset report, we should
  also have an "advanced settings" section where the user can decide to exclude some details and fields
  in the final report (for example, some of the fields in the `ZGALLERYSNAP values` section of the
  Memory details page). The user should also be able to save his settings as a default config for future reports.
