# Documentation
- Make it clear that this fork has not been properly tested with multiple Snapchat versions and it's provided AS IS to help with other tools and proper validation/analysis of the artifacts.

# Snapchat Communications report
- Add a way to select only specific conversations or parts of conversations and their associated contacts and output them to PDF with attachments.

# Snapchat Memories report
- For the geolocations, also include a link to Google Maps on the same line as the OSM link.
- There are often multiple Memories linked to the same cache media files using the same encryption keys.
  We should group them together and put the common metadata details in a single block.
- Add "Dimensions" data for mp4 video files also - the details are in the ZGALLERYSNAP table.
- The Source paths should show the paths in the device extraction ZIP archive and not the temporary extracted files paths.
- Figure out where the files in SnapFixedVideos are coming from and what they link to. Also make sure they show up in reports.
- Show timestamp entries in two tables (one for `ZGALLERYSNAP` and one for `ZGALLERYENTRY`) and include NULL values so that the columns are consistent.
- Add a way to select only specific Memories and their associated media files and output them to PDF with attachments.

# Keychain auto-detection
- Add logic to locate GK/Cellebrite/XRY keychain files either inside or outside the extraction ZIP.. 

# Android tests/improvements
- Make sure we properly support all the same features on Android than on iOS, for example:
  - Keystore auto-detection.
  - Memories decoding with media/geolcation decryption.

# Code cleanup and optimization
- Fix Pylance/Pyright/Ruff warnings/errors.

# Analysis
- Check if we have metadata in `cache_controller.db` for all files in `Documents/com.snap.file_manager_3_SCContent_...`.
- Figure out how Cellebrite decides which "File Uploads" get the "My Story" flag.
- Figure out these values:
  - ZZGALLERYENTRY.ZGALLERYTYPE
  - ZGALLERYSNAP.ZSYNCEDENTRY
  - ...ZENTRYSOURCE
  - ...ZEXTERNALID

## In our test "C:\Temp\Snapchat_Auto\20260720-0136"...
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

# New report for other items in `cache_controller.db` not already covered in other reports (Communications and Memories)
- What else can we figure out from `cache_controller.db` by matching the `CACHE_KEY` values to filesystem filenames?
