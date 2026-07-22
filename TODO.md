# Reporting
- Add the paths to source extraction and keychain/keystore files at the top of the index.html.
- `index.html` seems to be generated only after the pause that asks the user to press any key to continue.

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
    - timestamps
    - geolocation
    - link to a separate HTML page for each Memory or group of Memories
  - In the main index table, include sorting and filtering for the table columns and a global search.
    (The thumbnail column should allow sorting/filtering with or without.)

# Keychain auto-detection
- Add logic to locate GK/Cellebrite/XRY keychain files either inside or outside the extraction ZIP.. 

# Android tests/improvements
- Make sure we properly support all the same features on Android than on iOS, for example:
  - Keystore auto-detection.
  - Memories decoding with media/geolcation decryption.

# Code cleanup and optimization
- Fix Pylance/Pyright/Ruff warnings/errors.

# Analysis / Reverse engineering
- Check if we have metadata in `cache_controller.db` for all files in `Documents/com.snap.file_manager_3_SCContent_...`.
- Figure out how Cellebrite decides which "File Uploads" get the "My Story" flag.

## In our test with extraction "D:/shared/1-EXTRACTIONS/TEST iPhone 13 Pro Max GK FFS 20230515/00008110-000C21D92E46801E_files_full.zip"
- Working dir: `C:/Temp/Snapchat_Auto/Snapchat_Auto-20260722_005629`
- The media file "6382911a94286738b6f31e326e2b8dbf" is found in two `com.snap.file_manager_3_SCContent_...` directories (one contains a full copy and the other contains multiple parts):
  - "\Application\84132E3E-CADD-4579-8D7D-534D30E19A8E\Documents\com.snap.file_manager_3_SCContent_3559758e-fefe-4fef-8946-c5e85ce12e53\6382911a94286738b6f31e326e2b8dbf"
  - "\Application\84132E3E-CADD-4579-8D7D-534D30E19A8E\Documents\com.snap.file_manager_3_SCContent_5803ed5b-a28e-4bed-b418-8416323781ad\6382911a94286738b6f31e326e2b8dbf_2127264-2416128"
  - "\Application\84132E3E-CADD-4579-8D7D-534D30E19A8E\Documents\com.snap.file_manager_3_SCContent_5803ed5b-a28e-4bed-b418-8416323781ad\6382911a94286738b6f31e326e2b8dbf_131072-1129680"
  - "\Application\84132E3E-CADD-4579-8D7D-534D30E19A8E\Documents\com.snap.file_manager_3_SCContent_5803ed5b-a28e-4bed-b418-8416323781ad\6382911a94286738b6f31e326e2b8dbf_1129680-2127264"
  - "\Application\84132E3E-CADD-4579-8D7D-534D30E19A8E\Documents\com.snap.file_manager_3_SCContent_5803ed5b-a28e-4bed-b418-8416323781ad\6382911a94286738b6f31e326e2b8dbf_0-1"
- From my understanding, the UUID after `com.snap.file_manager_3_SCContent_` is the user account ID.
- In the report, we show this media file associated to the user ID "5803ed5b-a28e-4bed-b418-8416323781ad" only,
  but the first cache file shown in the table is the one in "\Application\84132E3E-CADD-4579-8D7D-534D30E19A8E\Documents\com.snap.file_manager_3_SCContent_3559758e-fefe-4fef-8946-c5e85ce12e53".
- We don't show the multi-part copy in "\Application\84132E3E-CADD-4579-8D7D-534D30E19A8E\Documents\com.snap.file_manager_3_SCContent_5803ed5b-a28e-4bed-b418-8416323781ad".
- We also have a reconstructed copy of the video: "C:\Temp\Snapchat_Auto\20260720-0136\SnapFixedVideos\6382911a94286738b6f31e326e2b8dbf.mp4"
- When matching the `CACHE_KEY` column to "6382911a94286738b6f31e326e2b8dbf" in the tables in "\Application\84132E3E-CADD-4579-8D7D-534D30E19A8E\Documents\global_scoped\cachecontroller\cache_controller.db",
  we can associate this media to the "EB854B71-49A9-414E-99DF-F79417AC4123" memory.
- "EB854B71-49A9-414E-99DF-F79417AC4123" is only found in the `scdb-27.sqlite3` for user "5803ed5b-a28e-4bed-b418-8416323781ad".

# New report for `cache_controller.db` data.
- The `CACHE_KEY` value in these 4 tables should allow us to link the entries with the files in `Documents/com.snap.file_manager_3_SCContent_...`:
  - CACHE_FILE_CLAIM
  - CACHE_FILE_METADATA
  - CACHE_FILE_SAMPLED_TOMBSTONE
  - CACHE_KEY_VIRTUALIZATION
- Check if we could link each entry with Snapchat cache files extracted from the device.
- In our report output, when an entry can be linked to Snapchat Memories or conversations, we should include links to these items in the other reports.

