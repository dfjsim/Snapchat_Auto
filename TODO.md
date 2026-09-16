# Corpus test-run notes — correct answers, not defects

Kept here because they are the things a future run should NOT re-investigate: on the four-device
corpus these are the correct answers. The fixes that came out of the same pass are in
[CHANGELOG.md](CHANGELOG.md) under 1.5.0.

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

# Snapchat Conversations / Contacts reports
- Output selected conversations, or parts of conversations, and their contacts to **PDF with
  attachments**. The selection half exists (kind `conv` on the index, kind `msg` on a detail page,
  shared through `Reports/selection.js`) and `--selection` builds a partial report of exactly those
  rows. What is missing is the PDF — see "The print / PDF view" under *Planned features*.
- **Both legacy reports are OFF by default** (since 1.6.0-beta.4): the GUI's "Include the legacy
  reports" checkbox on the main window, `--legacy-reports yes` headlessly. That is one step short of
  removal and it already buys the correctness argument below — a run that does not produce them does
  not write into the extracted evidence copy at all.
  - **Measured**: producing them leaves reassembled whole cache files in
    `ExtractedData/.../SCContent_*` beside the device's own shards, and the cache_controller report
    then reads those back as if they were device data — the affected rows pick up an extra token in
    their search text. With the legacy reports off, the extraction folder is left exactly as
    unzipped. Same device, same build, the two runs differing only in that flag.
  - So the remaining removal work is bounded: everything else in a full report is byte-identical
    between the two runs (checked file by file, run identity normalised).
- Validate against the legacy report on more extractions, then remove the legacy one:
  - message counts per conversation, and the rows each report drops (see `_drop_unrenderable`);
  - that every attachment the legacy report inlined is also shown here, with the same bytes;
  - contacts: that no row of the friends artifact is lost by the normalizers.
  - Removal steps: drop the `getHtml` call + `outputDir`/`cacheFiles` handling in
    `ParseSnapchat_iOS.main` (move the attachment copying to the Conversations report), the
    "Communications (legacy)" entry in `write_index`, and the v2 branch of
    `cache_controller_report.load_chat_links`.

# Snapchat Memories report
- Output selected Memories and their media files to **PDF with attachments**. The selection and
  report half is done — `--selection` builds a partial report holding only the ticked Memories and
  whatever related items are asked for, see docs/report_partial.md. The PDF half is the last, gated
  phase of that work (see *Planned features*).
- We need to be able to filter/search by URL.
- In our Memories report, we need to be able to search/filter by IV/KEY.
- My Eyes Only, still open after the 1.5.2 fixes:
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
  - **Measured evidence that writing into `ExtractedData` changes report content.** Deleting the
    extraction folders and re-extracting them changed 30 rows of the cache_controller report on one
    device and 20 on another, in each case a shard file named `<cache_key>_0-1` becoming
    `<cache_key>_PREFETCH` — the name it actually has in the archive. The databases were byte-identical
    across the re-extraction (all ten fingerprinted artifacts, all four devices), so the difference was
    entirely files an earlier run had written into the tree: exactly the 30 cache keys that
    `SnapFixedVideos` holds a reconstructed `.mp4` for. So the report had been quoting a filename the
    device never had, and only a re-extraction revealed it. Two consequences: this is a correctness
    argument for the removal above, not just a tidiness one; and a corpus baseline must be taken from a
    freshly extracted tree or it bakes in the pollution.

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
- Related goal already recorded above: stop writing into `ExtractedData` at all (see the legacy
  Memories / SnapFixedVideos cleanup).
- Why it matters beyond tidiness: `scripts/source_fingerprint.py` deliberately does **not**
  fingerprint the `-shm` because of this. If the tool stopped touching it, the `-shm` could be
  fingerprinted like the `-wal` — but as long as our own run moves it, hashing it would make the tool
  fail its own source verification over a file it modified itself, which is exactly the false alarm
  that teaches an examiner to wave a sidecar difference through.

# Code cleanup, performance and optimization
- Warnings policy for the test suite, after the beta. Today's two warnings are both third-party and
  invisible outside pytest; `-W error::DeprecationWarning` with just those two ignored passes the
  whole suite, so nothing of ours is deprecated. The policy worth adopting is scoped by module —
  a warning blamed on OUR modules fails the suite, one a dependency raises about its own internals
  stays printed — rather than an ignore list that rots on every dependency update:
      [tool.pytest.ini_options]
          filterwarnings = ['error:::Snapchat_Auto', 'error:::scripts\..*',
                            'error:::snapchat_auto_selection.*', 'error:::test_.*',
                            "ignore::ResourceWarning"]
  Note TOML literal (single-quoted) strings for the entries containing `\.`, and that later entries
  win, so the ResourceWarning ignore stays last. Gating ResourceWarning as well needs
  `error::pytest.PytestUnraisableExceptionWarning` too — `-W error::ResourceWarning` alone does not
  fail, because the raise happens in an unraisable finalizer. The ~12 ResourceWarning sites are all
  ours and all benign (refcounting closes each handle at the end of the statement); two are
  production code, `scripts/memories_media_report.py:1073` and `:1222`, worth a `with` block so a
  future real leak is not lost in the noise.
- Fix Pylance/Pyright/Ruff warnings/errors.
- Consider giving the user an option to make the report dependent on the device extraction ZIP archive for unencrypted media files.
  We would not have to keep a copy of so much extracted media files. It might not be worth it depending on the ratio of encrypted/unencrypted files.
- Check whether anything is worth copying from the standalone `keychain_decoder.py` in the
  `bplist_base64_decoder` project.

# cache_controller.db report — open items
- `CACHE_KEY_VIRTUALIZATION` was empty in every test extraction, so the
  `VIRTUAL_CACHE_KEY` ↔ `CACHE_KEY` semantics are unconfirmed — its rows are listed but no linking
  logic depends on them. Revisit once a populated sample is available.
- We need to be able to filter/search by URL.

# Offline tile map server — remaining ideas
- A map on the Memories *index* (the index only shows coordinates + OSM/Google links today).
- One overview map plotting every geolocated Memory of the case.
- Configurable zoom / map size (currently zoom 15, 3x3 tiles).
- Example URL: http://hostname:port/#map=15/40.000000/-70.000000
  - With our OSM tile server, this URL brings us to the Ubuntu Apache2 default page.
    https://github.com/dfjsim/osm-tirex

# Report UI
- Selections: a `file://` page has no storage that survives closing the tab or that two pages of
  the same run can share (measured — see `docs/report_ui.md`), so selections live in
  `Reports/selection.js`, which the examiner saves from the report. Worth revisiting if we ever
  ship a small local server or a desktop shell, which would allow silent persistence.
- Row cells in the virtualized index tables have a fixed height and clip long values (the full
  value is always in the row detail / detail page). Confirm that reads well on other datasets,
  e.g. accounts with many cache tokens per Memory.

# Planned features
- Integrate Snapchat_Download support with guardrails (reminding the user to have proper legal authorization).
- Partial reports — still open after 1.6.0 (the feature itself: `--selection`, `--expand-selection`,
  see [docs/guide_partial_reports.md](docs/guide_partial_reports.md) and
  [docs/report_partial.md](docs/report_partial.md)):
  - **The print / PDF view** (plan phase 8, on ice): one combined `print.html` per extract so the
    examiner can produce a paginated PDF from the browser. Gated on it being clean and its in-document
    links working. This is the export half of the two PDF items above.
  - **Field-level exclusion** (plan phase 5): leaving named fields or whole blocks out of an extract,
    with each stated as withheld rather than silently missing. In the GUI this belongs in an
    "advanced settings" section of the Related-items step, where the user can exclude some details and
    fields from the final report (for example, some of the fields in the `ZGALLERYSNAP values` section
    of the Memory details page) and save the choice as a default config for future reports.
- Chat decoding, still open after the 1.5.2 fixes:
  - The other event kinds under `4.4.8` — fields `2`, `5`, `6`, `8` and `22`, of which `2` is by far
    the most common — are identified as events but not named. They are labelled `System message`
    with no description.
    Naming them needs ground truth: compare a conversation containing them against another tool.
  - `proto_to_msg` still concatenates every string in the protobuf, and `message_content` still
    depends on that for the cache join. Worth reading the media id from its own field too, so the
    concatenation can go.
