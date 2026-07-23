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
