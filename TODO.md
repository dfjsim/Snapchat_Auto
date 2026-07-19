# GUI
- Make it remember the directory path between the ZIP extraction, keychain and temp selections.
- Write a note under the Timestamp timezone that explains that daylight saving will be applied.

# Report structure and directory paths
- Add "/Report" to "Working/Temp".
- Make the Working/Temp/Report directory path selection mandatory.
- Write the LOG file to the Working/Temp/Report directory.
- Put the data extracted from the ZIP file in it's own sub-directory in the Working/Temp/Report directory.
- Rename these output folders/filenames...
  - Snapchat_iOS_report_date_time/Snapchat_report.html --> Report_date_time/Communications/Communications_report.html
  - Snapchat_iOS_report_date_time/Memories/Memories.html --> Report_date_time/Memories/Memories_report.html
  - Snapchat_LocalMemories_report_date_time/Report.html --> Report_date_time/LocalMemories_legacy/LocalMemories_legacy_report.html.
- Add Report_date_time/index.html to help navigate to other reports.

# Snapchat Communications report
- Add a way to select only specific conversations or parts of conversations and their associated contacts and output them to PDF with attachments.

# Snapchat Memories report
- [DONE] Fix ".pack" files not being decoded and associated to Snapchat Memories anymore.
  (Root cause: extract_zip.py never extracted Library/Caches/caching-media. Now resolves
  Snapchat's app/app-group containers from container metadata plists and extracts within them.)
- For the geolocations, also include a link to Google Maps on the same line as the OSM link.
- There are often multiple Memories linked to the same cache media files. It would be nice to group them together
  and put the common metadata details (timestamps, Key/IV, etc.) together.
- Add a way to select only specific Memories and their associated media files and output them to PDF with attachments.
- Figure out where the files in SnapFixedVideos are coming from and what they link to. Also make sure they show up in reports.

# Keychain auto-detection
- Add logic to locate GK/Cellebrite/XRY keychain files either inside or outside the extraction ZIP.. 

# Android tests/improvements
- Make sure we properly support all the same features on Android than on iOS, for example:
  - Keystore auto-detection.
  - Memories decoding with media/geolcation decryption.

# Code cleanup and optimization
- Fix Pylance/Pyright/Ruff warnings/errors.

# Analysis
- Check if we have metadata in cache_controller.db for all files in Documents/com.snap.file_manager_3_SCContent_*
- Figure out how Cellebrite decides which "File Uploads" get the "My Story" flag.


"C:\Temp\Snapchat_Auto\20260716-1503\Application\84132E3E-CADD-4579-8D7D-534D30E19A8E\Documents\global_scoped\cachecontroller\cache_controller.db"
"C:\Temp\Snapchat_Auto\20260716-1503\Application\84132E3E-CADD-4579-8D7D-534D30E19A8E\Documents\gallery_data_object\1\650c8c96bef03ebd3a6683b275ac35178d3fc41f0752d96f0b607b80d5b73742\scdb-27.sqlite3"
"C:\Temp\Snapchat_Auto\20260716-1503\Application\84132E3E-CADD-4579-8D7D-534D30E19A8E\Documents\gallery_data_object\1\09e676dd458b7196a9c0aa2a90ff50136158da725927a880ca8d680d41420163\scdb-27.sqlite3"
