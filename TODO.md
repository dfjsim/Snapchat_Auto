# Snapchat Communications report
- Add a way to select only specific conversations or parts of conversations and their associated contacts and output them to PDF with attachments.

# Snapchat Memories report
- Add a way to select only specific Memories and their associated media files and output them to PDF with attachments.
- When there are a lot of Memories, the HTML becomes too heavy and hard to work with...
  - Create a simpler HTML page with a table that contains these columns:
    - small thumbnail
    - user ID associated with the Memory
    - IDs like the ZSNAPID, ZENTRYID, etc.
    - strings that look like UUIDs or hashes in cache filenames
    - MD5/SHA256 hashes
    - timestamps
    - geolocation
    - link to a separate HTML page for each Memory or group of Memories
  - In the main index table, include sorting and filtering for the table columns and a global search.
    (The thumbnail column should allow sorting/filtering with or without.)
- We should have a second level of merging by matching media file MD5/SHA256 hashes.
  - If there are any 0-byte media files, they should be excluded for matching because they
    would all have the same hash values.
  - We will need to make the MEDIA and SNAP IDs more obvious.
  - This extra merging logic will make it so that matching media files will all end up in the
    same HTML sub-page.

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
