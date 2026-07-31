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

# Keychain auto-detection
- Add logic to locate GK/Cellebrite/XRY keychain files either inside or outside the extraction ZIP.. 

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
    both `SnapFixedVideos` and the legacy report are dead weight. Verified on the `6382911a…` split
    video.
- Removal steps when we get to it:
  - `Snapchat_Auto.py`: drop the `parseSnapvideos_PREFETCH.main()` call + the `SnapFixedVideos`
    existence check, and the import.
  - `ParseSnapchat_iOS.py`: drop the `DecryptLocalMemories_iOS.main()` legacy-report block and its
    import there.
  - `write_index`: drop the "Local Memories (legacy)" entry.
  - KEEP `scripts/DecryptLocalMemories_iOS.py` — the new Memories report reuses its `readKeychain`
    (imported as `_memkeys`). Optionally delete `scripts/parseSnapvideos_PREFETCH.py` (unused after).
  - Benefit: faster runs and no longer writing into `ExtractedData`.

# Code cleanup and optimization
- Fix Pylance/Pyright/Ruff warnings/errors.

# New report for `cache_controller.db` data. [DONE — see DONE.md]
- Remaining/uncertain: `CACHE_KEY_VIRTUALIZATION` was empty in every test extraction, so the
  `VIRTUAL_CACHE_KEY` ↔ `CACHE_KEY` semantics are unconfirmed — its rows are listed but no linking
  logic depends on them. Revisit once a populated sample is available.

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
  - The 15 cache entries that remain "🔒 encrypted" on the 2023 test device — are any of them
    decryptable from a source we already have (chat media keys)?
  - Row cells in the virtualized index tables have a fixed height and clip long values (the full
    value is always in the row detail / detail page). Confirm that reads well on other datasets,
    e.g. accounts with many cache tokens per Memory.

