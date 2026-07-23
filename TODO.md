# Snapchat Communications report
- Add a way to select only specific conversations or parts of conversations and their associated contacts and output them to PDF with attachments.

# Snapchat Memories report
- Add a way to select only specific Memories and their associated media files and output them to PDF with attachments.
- [DONE-v1.4.0 — see DONE.md] Split into a lightweight sortable/filterable index + per-group detail
  sub-pages, with second-level merging by media MD5/SHA-256 (0-byte excluded, cross-user).

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

# Analysis / Reverse engineering
- Figure out how Cellebrite decides which "File Uploads" get the "My Story" flag.

# New report for `cache_controller.db` data. [DONE — see DONE.md]
- Remaining/uncertain: `CACHE_KEY_VIRTUALIZATION` was empty in every test extraction, so the
  `VIRTUAL_CACHE_KEY` ↔ `CACHE_KEY` semantics are unconfirmed — its rows are listed but no linking
  logic depends on them. Revisit once a populated sample is available.
- [DONE-v1.4.0] View unlinked cached files; real DB field names; the "SHA-256" field-8 finding
  (source hash, may not match cached bytes) + actual cached-file hashes; "Expand all" button.

# Add support for offline tile map server
- If a tile server server is provided in the initial GUI (tested right away when specified),
  use it to generate small embedded maps for each Memory (in the detailed Memory pages)
  and include a URL to the tile server at the right coordinates.
