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
- Add a way to select only specific Memories and their associated media files and output them to PDF with attachments.
- We need to be able to filter/search by URL.
- Fix MEO decryption that fails in some cases.

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

# Big parts to fix...
- Integrate Snapchat_Download support with guardrails (reminding the user to have proper legal authorization).
- Implement feature to recreate a partial report from the selected elements only.
  - Ask the user if we include all the elements related to the ones selected.
- Fix messages decoding from arroyo.db... we are currently missing many that are displayed by at least one other tool.
