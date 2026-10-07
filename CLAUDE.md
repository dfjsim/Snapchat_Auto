# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`Snapchat_Auto` — a forensics tool that extracts and parses Snapchat data from iOS and
Android device extractions, producing HTML reports of chats, contacts, cached media, and
Memories / My Eyes Only.

- Entry point: `Snapchat_Auto.py` (FreeSimpleGUI front end).
- iOS parsing: `scripts/ParseSnapchat_iOS.py` — also still builds the **legacy** single-page
  chats/contacts report (`Reports/Communications_legacy/`), kept until the two below are validated.
- iOS conversations: `scripts/conversations_report.py` (index of every conversation + one detail
  page each) and `scripts/contacts_report.py` (one table of contacts; also owns the contact/group
  normalizers and `text_html`, which the conversations report imports).
- iOS Memories / MEO decryption: `scripts/DecryptLocalMemories_iOS.py`.
- iOS Memories search index: `scripts/gallery_search.py` — the app's own `gallery_search/…/
  search.sqlite3` (plain SQLite, no keychain: place names, a local date, the app's tags per snap),
  joined onto the Memories report by snap id. Everything in it is app-generated and shown as stored.
- iOS `cache_controller.db` report: `scripts/cache_controller_report.py` (one row per cached file,
  linked to on-disk cache files and two-way to the Memories / Conversations reports). Covers the
  `com.snap.file_manager_*_SCContent_*` folders — i.e. exactly what that database indexes.
- iOS `Library/Caches` report: `scripts/cache_media_report.py` (everything under `Library/Caches`
  that `cache_controller.db` does **not** index: story renders, URL-keyed PINCache stores, saved
  chat media, and the cached documents). Disjoint from the cache_controller report by design. It renders
  after the Memories report, so what it links to a Memory reaches the Memory's page as a data file
  (`scripts/memory_backlinks.py`), as the cache_controller report's leads do (`scripts/memory_leads.py`).
- Chat media kept in pieces: `scripts/chat_media.py` — rebuilds a chat claim's media the cache keeps as
  a bundle (descriptor + child files) or as byte-range shards, before the shared join, on both
  platforms; only bytes that are media are written, and `chat_cache_key` explains each attachment.
- Android: `scripts/ParseSnapchat_Android.py` — the Android run. The chat database (`arroyo.db`) and
  the cached-file index (`cache_controller.db` + `com.snap.file_manager_*_SCContent_*`) are the **same
  databases as on iOS**, so the chat parsing is `ParseSnapchat_iOS`'s functions called unchanged and
  the Conversations and cache_controller reports are the iOS ones; what is Android-only is the account
  (`arroyo.db` `required_values`, `shared_prefs`), the contacts (`main.db` `Friend` +
  `CombinedUsername`, carried into the Contacts report as per-row `_extra` fields) and the Memories
  (`scripts/memories_android_report.py`, `memories.db`). `scripts/android_layout.py` finds every
  artifact in an extracted tree and writes `android_survey.json` (structure only, no content).
  `scripts/getCacheAndroid.py` is the legacy single-page Android report. The Android tests run on
  `tests/android_fixture.py` (a synthetic app folder, archived GrayKey- or UFED-style) — see
  [snapchat_android.md](docs/snapchat_android.md) before touching it.
- Android extraction: `extract_zip.android_entry` maps every archive entry to its **canonical device
  path** (`data/data/<pkg>`, `data/user_de/<n>/<pkg>`, `data/media/<n>/Android/data/<pkg>`) and writes
  it once — a GrayKey archive carries an app's private data three times (`/data/data`, `/data/user/0`,
  `/data_mirror/…`), a UFED one its shared-storage folder five times. Each file is checked against the
  archive's own SHA-256 where GrayKey recorded one.
- Shared report UI: `scripts/report_ui.py` (virtualized index tables, paging, row selection,
  cross-report anchor navigation, "?" popovers, page chrome) — used by the Conversations, Contacts,
  Memories and cache_controller reports, with its `NAV_JS`/`NAV_CSS` also injected into the legacy
  Communications report. **Every cross-report link is emitted through `report_ui.xref`** — the one place
  that can mark a link whose target a partial report does not contain; with no closure it returns the
  caller's markup untouched, so full reports are unaffected. It also owns the emoji font
  (`emoji_font.css`, Noto Color Emoji, linked by every page) that makes an emoji look the same on every
  workstation: every report font stack **ends** with `EMOJI_FONT_STACK`, and a symbol the reports draw
  as text belongs in `UI_SYMBOLS` — see [report_ui.md](docs/report_ui.md).
- Search all reports: `scripts/global_search.py` writes `search.html` beside `selection.js` whenever a
  folder's `index.html` is written — each report's own search (its `data/index.js` search text, `|` for
  either) over every report and every conversation's messages at once; every report's search box links
  to it (`report_ui.search_all_link`). See [report_ui.md](docs/report_ui.md#searching-every-report-at-once-searchhtml).
- Progress: `scripts/progress.py` — GUI-free stages (`progress.stage`), steps inside them
  (`progress.step`, `Counter`), a *still working* heartbeat into every 30 s silence of the log, and a
  per-stage timing summary at the end of a run. Bracket a new long stage and step through any loop
  that can run for minutes. `scripts/parallel.py` (`ordered_map`) runs independent per-file work on
  threads and returns it **in order** — whatever names, de-duplicates or publishes stays in the
  caller's loop, so the reports stay byte-identical. `scripts/run_window.py` is the GUI's view of all
  of it: the run on a worker thread behind a window of stages, counts, the log, the retrieval's controls
  and *Skip thumbnails*. **Nothing reachable from `run()` may touch a window** — report through
  `progress`, the log or a queue. See [progress_and_performance.md](docs/progress_and_performance.md).
- Offline maps: `scripts/offline_maps.py` — static map imagery for geolocated Memories, fetched
  **only** from a tile server the examiner configures in the GUI (never the internet by default).
- Display scaling: `scripts/hidpi.py` — claims Windows DPI awareness before the first window exists,
  and supplies the two things that must then follow it: `tk_scaling()` for the point-sized fonts and
  `px()`/`px2()` for every constant the GUI writes in pixels. Without it the window is a 96-dpi bitmap
  that Windows stretches, which is the soft text on a scaled screen or over RDP. The size is the
  GUI's own editable "Text size" control (Auto, the common sizes, or any percentage typed in — 55
  of them render differently between 100% and 200%), which writes the same `dpi_scale` setting the
  command line does; `--dpi-awareness`, `--dpi-scale` and `--dpi-report` are the CLI side, and
  `--dpi-awareness unaware` reproduces the pre-1.6 rendering in the current build, because "is this
  sharper?" cannot be answered without something to compare against. See
  [hidpi_scaling.md](docs/hidpi_scaling.md).
- Shared helpers: `scripts/data/` (`ccl_bplist.py`, `keychain.py` UFED keychain decrypter,
  `Snapchat_pb2.py` protobuf, bundled `sqlcipher3.exe`, `poster_worker.py` — video
  thumbnails, in killable subprocesses (a few at once, frames cached by the video's SHA-256 in the run
  folder, no time limit — the run window skips) because one cached video in six hangs the decoder for
  good —
  `protobuf_wire.py`, the schema-less protobuf reader every decode shares, and `snap_session.py`, the
snap editor's session record in `userPreferences/pref.docobjects` (which CACHE_KEY a context-34 claim's
snap is held in; carved versions kept only when a claim corroborates them) —
`sniff.py`, the shared magic-byte identifier — identify content with `sniff.classify`, never by
  name or extension, and only call something "encrypted" when it says so: it requires high entropy
  **and** AES block alignment, because "we cannot display it" is not the same statement as "it is
  encrypted" — and `media_meta.py`, which reads what a media file says about **itself**: EXIF/XMP,
  PNG text; in an MP4/MOV the movie and track headers, QuickTime user data (incl. 3GPP boxes),
  `moov › meta` and XMP — walked by seeking, never reading the media data. Every timestamp it returns
  says what clock it is on, and only one whose zone the file states becomes an instant; a naive EXIF
  wall clock is handed back as the string it is; the source files an editor lists in XMP (pantry /
  ingredients) come back apart (`xmp_sources`) and never become the file's own times or GPS; and
  `snap_media_tag.py`, the decoder for the Snapchat app's tag — base64 of a protobuf in a
  description field naming app version, device model, OS and lens id — whose one rule is that the
  value is the tag only when all of it parses; and `device_fs.py`, the device filesystem's own record of each
  extracted file — all four timestamps, owner, mode, inode, protection class — read from a UFED
  archive's `metadata.msgpack` (nanoseconds; `msgpack` is a dependency for it) or the ZIP entry's
  `UT` field, into one shape every report renders the same way; `tsaf.py`, the keyed reader for
  Snap's TSAF containers — `user.plist` and `ClientEncryptionService.plist` are not plists — whose
  one rule is that a value must *immediately* follow its key; and `flatbuffers_doc.py`, a root-table
  reader for the `*.docobjects` FlatBuffers documents that hands back a name only when the document's
  slot 0 is the user id the caller already knows (and, one level deeper, a sub-table's slots and a
  `[ubyte]` vector — `table_field`, `bytes_field`, `string_field(…, table=)` — for the creative-tools
  items, behind the same slot-0 self-check); and `arroyo_content.py`, what an arroyo.db
  `conversation_message` row *is* — every `content_type` named, and its `message_content` body (4.4)
  described: app events from their own fields, shares, replies — read straight off the wire, and a
  kind it does not know named by its field number, never guessed; and `keyed_archive.py`, a strict
  NSKeyedArchiver resolver shared by the MemData ids and overlay-record readers, which converts by the
  archive's own class names and gives None, never a partial tree, for anything that does not hold
  together (the older `ccl_bplist`-based readers and `ufed_keychain` still resolve their own); and
  `snap_overlay.py`, a Memory's overlay record (`ZGALLERYSNAPDETAIL.ZOVERLAY`) and the asset URLs of
  the geofilters it lists, with `normalise_url`, the one whole-URL rule a claim key is matched to a URL
  by — a listed filter is never said to be on the Memory, and a file matched this way is never its
  media; and `ctp_items.py`, the creative-tools item store (`primary.docobjects` › `ctp__item_5`, every
  account's, both readings, staged in a temp folder: a FlatBuffers document with a protobuf inside, the
  feed named only from `ctp__feedtree`) and the cached files its items name — exact whole texts only
  (the whole key, the key after `<word>:` / `<word>~`, or the same id bytes), never a query parameter
  or a part of a text, a text two items of a store hold attributed to neither, the `item_id` column
  matched even when the document has another layout; information on the entry, never a link; and
  `base64_text.py`, the one rule for when a text is base64, shared by `--trace-ids`, the survey and
  the item matcher).
- Selection format: `packages/snapchat_auto_selection/` — a **stdlib-only, dependency-free** uv workspace
  member owning the selection file and the `SelectionBuilder` / `anchor_for` / `validate` / `describe`
  API, so another tool can produce a selection without taking on this project's dependencies. The app
  imports the same module (`scripts/selection_file.py` re-exports it) — one implementation. Its
  dependency list must stay empty; a test walks its ASTs to enforce that.
- Run/build: `uv` project (`pyproject.toml`), Nuitka build via `build_nuitka.cmd` (portable onefile
  EXE), MSI via `uv run build` (`dfjsim_shared_tools`). `[project].version` carries a
  `+build.<N>` tag because the optional update check compares it against installer filenames —
  `dfjsim_shared_tools.auto_update` does the checking, `Snapchat_Auto.py` the wiring, see
  [auto_update.md](docs/auto_update.md). The folder it checks is
  configured per examiner in the GUI and stored in `~/.snapchat_auto_gui.json`; it is **never**
  committed or bundled (see the public-repo rule below).
- Headless runs: `Snapchat_Auto.py --zip <file> [--keychain …] [--workdir …] [--run-name …]`
  runs the whole pipeline with no GUI and no pause, which is how the tool is scripted over
  several extractions. `run()` is the shared entry point for both the GUI and the CLI.
- Identifier search: `Snapchat_Auto.py --trace-ids <run folder> <id>…` (`scripts/trace_ids.py`) — where
  each id occurs in a run's `ExtractedData/` (text/UTF-16/hex/raw/LE-UUID/base64; databases row by row in
  both readings; superseded `-wal` frames). Reports locations only, never content — it is how a finding
  on case data is checked without the data leaving the case machine. See [trace_ids.md](docs/trace_ids.md).
- Claim link survey: `Snapchat_Auto.py --survey-claim-links <run folder>` (`scripts/claim_link_survey.py`) —
  every `cache_controller.db` claim's link status and where in `arroyo.db` the ids of its key occur
  (table, column, protobuf field, content_type, rows per id), grouped by key shape. Shapes and counts
  only; it is how a missing chat link rule is found on case data. See
  [claim_link_survey.md](docs/claim_link_survey.md).
- Retrieval from Snapchat's servers (1.9): `scripts/cloud_download.py` (the engine: authority gate,
  host policy, pacing, the hash-chained `CloudDownloads/cloud_manifest.jsonl`), `scripts/cloud_memories.py`
  (candidates, scopes, date rules, `cloud_files` kept apart from `media_files`, the byte-identity proof
  against cached files) and `scripts/cloud_refresh.py` (a run folder's settings, the targeted refresh).
  Off unless asked, never in a partial run, nothing contacted without `Authority.problems()` empty. What
  comes back is not device evidence and every page that shows it says so. Method credited to DFIR-HBG's
  Snapchat_DownloadMemories_iOS (unlicensed: nothing copied). See [cloud_download.md](docs/cloud_download.md).
- Partial reports: adding `--selection <file>` to a normal run renders **only** the rows an examiner
  ticked plus the related items they asked for, into `Reports_partial_<stamp>/` — the full `Reports/`
  is never touched. Same pipeline, in two halves: every report's `index()`, then one closure, then
  every report's `render()`. `scripts/partial_report.py` owns the closure and what the pages say about
  it; see [report_partial.md](docs/report_partial.md).

## Handling forensic data — read this first

- Extractions contain a real person's private data. **Keep decrypted output and extracted
  artifacts local**; never publish them (no Artifacts, no uploads). Work in the scratchpad,
  not the repo.
- **This repository is public.** Nothing that identifies a test device, an account or a case may be
  committed — not in docs, not in code comments, not in commit messages. See below.
- Extraction ZIPs are huge (tens of GB). **Selectively extract** only the files you need
  (see the app-container paths in the docs below) rather than unzipping the whole archive.
- Open SQLite databases through `scripts/data/sqlite_open.py`, never a bare `sqlite3.connect` on an
  evidence path and never a write mode. It reads every database **twice** — with its `-wal` applied
  (the app's current state) and without it (the last checkpointed state) — and marks rows only one
  reading contains, so deleted/superseded rows are recovered instead of lost. Neither reading
  reaches a `-wal` frame that a later frame replaced; `superseded_wal_pages()` exposes those, and
  anything carved from them must be proved independently and badged `CARVED`. See
  [sqlite_wal_handling.md](docs/sqlite_wal_handling.md).

### Referring to test data — the repo is public

Findings are worth writing down; the device they came from is not. **Never commit**, in any file
(docs, code comments, commit messages):

- account, snap, media, conversation or message **UUIDs** — including truncated ones, since a
  truncated id is still a unique handle for a real account;
- **usernames**, display names, or content hashes (MD5/SHA-256) of a device owner's media;
- **extraction dates**, case or exhibit numbers, serial numbers;
- **filesystem paths** from an analyst machine or evidence store (`D:\…`, `C:\Temp\…`);
- **per-device tallies** of what a test extraction contains — "106 of 353 messages", "22 captions",
  "3 conversations". A census of the test devices is more than a reader needs, and it accumulates
  across write-ups into a profile of the corpus.

Refer to a device by the properties that make it technically interesting — OS and app version,
storage schema, keychain class, number of accounts — e.g. "the two-account iOS 26 device", "the
backup-class-keychain device". Byte offsets, field paths, structure sizes and format details are
fine and are the point of the write-up: they describe the *format*, not the device. Say what was
verified and how ("verified on two devices", "every text message carries this field") without
quoting the count. Placeholders such as `<snapId>`, `<userHash>`, `<CACHE_KEY>` belong in path and
format examples.

The corpus itself, and the script that runs it, live outside the repo for the same reason.

### Licences — what may go into `scripts/`

`scripts/` is compiled into the MIT-licensed EXE and MSI. **No copyleft (GPL/AGPL) source goes in
it** — two GPL files had to be rewritten from their formats in 1.9 (`ufed_keychain.py`,
`protobuf_wire.py`). Every third-party file added to the repository needs its notice in
`THIRD_PARTY_NOTICES.md`; a new Python dependency is picked up by `build_tools/collect_licenses.py`
(re-run it, commit `THIRD_PARTY_LICENSES.txt`). `tests/test_third_party_notices.py` enforces all three.

### Commit messages — the co-author trailer

A commit Claude Code contributed to ends with exactly this trailer, and nothing else naming the
assistant (**Never name the model**):

```
Co-authored-by: Claude Code <noreply@anthropic.com>
```

## Research notes / findings

- [The selection file format](docs/selection_format.md) — the **normative** spec another tool writes a
  selection against, plus the `snapchat_auto_selection` API and the
  `--describe-selection-api` handshake to run before writing one. Written for "an external tool"
  generally; no specific product is named.
- [Related work: iLEAPP's Snapchat module](docs/related_ileapp.md) — what this tool adopted from
  iLEAPP (the search index, orphan key rows, the FlatBuffers slot layout, the TSAF keyed read), the
  `snapchatter` ≠ friends trap and how to recognise it, and where the two tools part ways on purpose.
  **Credit iLEAPP** (Alexis Brignoni, MIT) in the module docstring and the report popover for anything
  else taken from it.
- [Partial reports](docs/report_partial.md) — the selection file the examiner saves (schema, the
  `.json`/`.js` forms and why renaming one to the other fails silently, `--install-selection`), the
  source fingerprints + tool-version gate in `scripts/source_fingerprint.py` that decide whether a
  later run is looking at the same evidence and may reuse anything of it, and what an extract must state
  about itself: the banner, the "N of M" figures, the provenance block, `partial_manifest.json`, the
  `xref` marking of links whose target is absent, and the media a partial run has to prune. Also the
  review loop — `--dry-run` listing the rows it would add, `--expand-selection` writing the closure back
  out as a selection file to check in the full report, the `.vr.pulled` marker, and why an already-expanded
  selection is built with containment only.
- [Partial reports — a short guide](docs/guide_partial_reports.md) — the **user-facing** walkthrough of
  the whole workflow: full run, choose rows (in the reports or from another tool), expand and check,
  review the expansion in the full report, build, and what the extract states about itself. Written for
  an examiner, not for us; keep it that way.
- Per-report internals and the cross-report linking scheme:
  [cross_report_linking.md](docs/cross_report_linking.md) (anchors + how every link is derived),
  [report_conversations.md](docs/report_conversations.md),
  [report_contacts.md](docs/report_contacts.md),
  [report_cache_controller.md](docs/report_cache_controller.md),
  [report_cache_media.md](docs/report_cache_media.md) (the Library/Caches report and the
  boundary between the two cache reports),
  [sqlite_wal_handling.md](docs/sqlite_wal_handling.md) (why every database is read twice),
  [report_memories.md](docs/report_memories.md),
  [report_communications.md](docs/report_communications.md) (legacy report + **the chat parsing
  both chat reports rely on**),
  [report_ui.md](docs/report_ui.md) (why the index tables are virtualized, how the data files are
  laid out, and the anchor/named-tab navigation rules — read before touching report HTML/JS).
- [Decrypting & linking Snapchat Memories media](docs/snapchat_ios_memories_decryption.md)
  — full method for recovering Memories media (`SCContent` + `caching-media/**/*.pack`) and
  geolocation, and linking each media file to its `scdb-27.sqlite3` Memory. Covers both storage
  schemas (keys in `ZGALLERYSNAP.ZENCRYPTION` vs. in `gallery.encrypteddb`), the
  keychain-required matrix (geolocation and My Eyes Only always need the FFS keychain;
  new-schema regular-memory imagery does not), multi-user handling, and the decrypt-and-match
  pack linker. Verified on two devices. Implemented by `scripts/memories_media_report.py`.
- [Snapchat for Android](docs/snapchat_android.md) — where the app keeps its data, how an Android
  archive is mapped to one device path per file (GrayKey vs UFED layouts, their per-entry metadata:
  the `IN` inode/device and `S2` SHA-256 fields, UFED's placeholder owner and change time), what each
  Android report reads, and the layout survey. Credits ALEAPP's Android Snapchat module.
- [Snapchat iOS `Library/Caches` media & documents](docs/snapchat_ios_cache_media.md) — what is
  cached outside `cache_controller.db`, how `sccache.gallery-stories-snap.data` is decrypted
  (AES-256-CBC, key + fixed IV in `Documents/ClientEncryptionService.plist`, no keychain), and
  why a root-level filename UUID must never be quoted as a snap id. Implemented by
  `scripts/cache_media_report.py`.
- [The Snapchat app's tag in media files](docs/snapchat_media_tag.md) — the base64 protobuf the app
  writes into a media file's description field (MP4 `dscp`, MOV `com.apple.quicktime.description`,
  ffmpeg `desc`, an image's EXIF `UserComment`): its layout (lens id packed on iOS, unpacked on
  Android), why field 1.2 is the lens id, whose device it names (the writer: a received snap's
  sender), the source files of an edit kept apart from the file's own XMP, and the other MP4/MOV
  metadata the reader now reads. Implemented by `scripts/data/snap_media_tag.py` and
  `scripts/data/media_meta.py`.
- [Blurred text on scaled displays and over RDP](docs/hidpi_scaling.md) — why a Tk front end is DPI
  *unaware* by default and what Windows does about it, the two halves of the fix that have to move
  together (`tk scaling` for fonts in points, `hidpi.px` for constants in pixels), and why
  FreeSimpleGUI's own `set_options(dpi_awareness=True)` is a silent no-op on Windows 11.
- [pandas 3.x / Python 3.14 compatibility notes](docs/pandas3_python314_compat.md) — the strict
  dtype enforcement (`Invalid value 'X' for dtype '…'`), removed `DataFrame.append()`, and the
  per-cell `df.loc[…] = value` pattern that breaks on the current runtime. Read before adding or
  editing DataFrame cell assignments in the parsing scripts.
