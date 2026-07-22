# Documentation
- [DONE-v1.3.3] Make it clear that this fork has not been tested thouroughly with multiple Snapchat versions and is provided AS IS to help analysing artifacts
  in combination with other tools and proper validations. It should probably be mentioned in the README and also with a popup that includes a
  "Don't display again" checkbox when running the app.

# GUI
- [DONE-v1.3.3] Make it remember the directory path between the ZIP extraction, keychain and temp selections.
  (Persisted to ~/.snapchat_auto_gui.json; the report directory prefills, and zip/keychain have
  "Use previous" buttons plus browse dialogs that open in each other's folder.)
- [DONE-v1.3.3] Write a note under the Timestamp timezone that explains that daylight saving will be applied.
- [DONE-v1.3.3] In Snapchat_Auto.py, get the version automatically for the logger instead of hard coding it.
  (get_version() reads pyproject.toml, falling back to installed package metadata.)

# Report structure and directory paths
- [DONE-v1.3.3] Add "/Report" to "Working/Temp" in the GUI.
- [DONE-v1.3.3] Make the Working/Temp/Report directory path selection mandatory.
- [DONE-v1.3.3] Write the LOG file to the Working/Temp/Report directory.
- [DONE-v1.3.3] Put the data extracted from the ZIP file in it's own sub-directory (ExtractedData/) in the Working/Temp/Report directory.
- [DONE-v1.3.3] Rename these output folders/filenames...
  - Snapchat_iOS_report_date_time/Snapchat_report.html --> Report_date_time/Communications/Communications_report.html
  - Snapchat_iOS_report_date_time/Memories/Memories.html --> Report_date_time/Memories/Memories_report.html
  - Snapchat_LocalMemories_report_date_time/Report.html --> Report_date_time/LocalMemories_legacy/LocalMemories_legacy_report.html.
- [DONE-v1.3.3] Add Report_date_time/index.html to help navigate to other reports.

# Snapchat Memories report
- [DONE-v1.3.1] Fix ".pack" files not being decoded and associated to Snapchat Memories anymore.
  (Root cause: extract_zip.py never extracted Library/Caches/caching-media. Now resolves
  Snapchat's app/app-group containers from container metadata plists and extracts within them.)
  (commit 775abb843347a6f6d9c6daf6dcc9b8c97adc4f36)
