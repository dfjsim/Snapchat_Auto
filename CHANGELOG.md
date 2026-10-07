# Changelog

All notable changes to Snapchat_Auto, newest first. Versions are the `[project].version` of
`pyproject.toml` at the time; a version marked *untagged* was never released on its own and shipped
inside the next one. Entries name the module or function that carries a change where that helps a
reader find it; the format findings behind them live in [docs/](docs/). Open work is in
[TODO.md](TODO.md).

## [Unreleased]

### Added
- **`--survey-claim-links <run folder>`** — `scripts/claim_link_survey.py`. For every
  `cache_controller.db` claim: whether the reports tie it to a chat message (by the attached file, by
  the message its key names), and where in `arroyo.db` the ids its key carries occur — table, column,
  protobuf field, content_type, reading, how many rows hold each id — grouped by the key's shape
  (`customSticker~<b64:13>`). Shapes, counts and field paths only, in the log and
  `claim_link_survey_<stamp>.json`. It finds, in one run on a case, the claim kinds a link rule is
  missing for. A second section does the same for every file under `Library/Caches` whose path carries
  a UUID (`filtered-<UUID>.mp4` …), looked up in every database of the extraction.
  `protobuf_wire.values_with_paths` lists every value of a message with its field path.
  See [docs/claim_link_survey.md](docs/claim_link_survey.md).
- **Expand all / Collapse all in the Library/Caches report**, as in the cache_controller and
  Conversations reports.
- **Search by date from the run's index page** — `global_search.index_form`. The index's search box
  now carries the same date/time window as `search.html` (between two moments, or within ± N of one,
  and *incl. inode changed*), alone or with the words; it was only on the search page itself. The
  window travels in the fragment (`#q=…&mode=…`) and the search page sets its controls from it. The
  index page also links the reports' emoji font, as every other page does.
- **The survey says what arroyo.db holds of the message an untied key names**
  (`untied_named_message`): the message, only its conversation (a message of it, or a `conversation`,
  `feed_entry` or `user_conversation` row), or neither — and then whether the claim's `USER_ID` is the
  `required_values` `USERID` of an `arroyo.db` or another account's (`claim_link_survey._arroyo_facts`,
  `_named_label`). A message it holds is a missing rule. A message it does not hold, in a conversation
  it holds or of its own account, is one only a recovery of deleted records could give a rule. A
  conversation it does not hold, claimed by another account, is one no rule written from that
  `arroyo.db` can tie — said only when every `arroyo.db` names its account. Every `arroyo.db` of the
  extraction is read, a table that will not read in either reading makes it unread rather than empty
  (`_rows`), and a message the server never numbered counts as held by its `client_message_id`. A
  claim's snap id is looked up in the Memories of the app folder the reports read its
  `cache_controller.db` from (`_apps`), which on Android is not four folders above it.
- **The survey looks untied claims up in every database**, not only arroyo.db (`found_in_any_database`):
  a Story, a preference or a Memory can hold the id a claim carries. A hit in a message the server never
  numbered says so, and an owner username in a key's `<USERNAME>~` position is masked out of the shapes.

### Fixed
- **A custom sticker sent in a chat is linked to its cached file** — `ParseSnapchat_iOS.getCacheArroyo`
  (`_sticker_key_text`). A Sticker message whose body is a creative tool item (`4.4.14`) names its
  sticker by the bytes at `4.4.14.2.6`, and the `customSticker…` claim on the cached file holds them in
  base64; the join read only the pack sticker's text id (`4.4.4.1.2`), so these messages never got
  their file — not in the chat reports, and not as a chat link on the claim in the cache_controller
  report. See [docs/report_communications.md](docs/report_communications.md).
- **A cached file links to the chat message it belongs to even when it is not the file the chat
  report shows** — `cache_controller_report._chat_links_for`, `ChatIdIndex`. Two kinds of claim were
  left without a chat link: a key naming a message (`animationmedia~1:<conversation>:<message>:<part>`)
  whose file the chat join had not attached, or under a part the report does not list — the manifest
  listed only messages with an attachment; and a key carrying an id the message names its media by —
  `content~`, `thumbnail~` or `SnapVideoFilterState-` with the media id of the message's
  `local_message_references`, or a shared item's or a sticker's other files. The Conversations
  manifest now lists every message and those ids (`conversations_report.load_content_ids`,
  `arroyo_content.content_ids`); each link's "?" names the id and the field it came from. See
  [docs/cross_report_linking.md](docs/cross_report_linking.md). A message the server never numbered
  (not sent, or still sending) is found by its `client_message_id`, and a `local_message_references`
  media id is read in any letter case.
- **Every photo or video of a message sent with several is attached** — `arroyo_content.media_references`,
  `ParseSnapchat_iOS.getCacheArroyo`. `local_message_references` holds one record per media item (an
  8-byte length, then a keyed archive); only the first was read, so the others' cached files were never
  attached and their claims linked to nothing.
- **A full-media claim keyed by a Memory's own snap id links to that Memory, in both reports** —
  `cache_controller_report` (fallback after `ZMEDIAID`) and `memories_media_report.collect_media`. A
  context-19 claim keyed `<snapId>~1` carries the Memory's `ZSNAPID` exactly, but only the Memory-scoped
  key shapes were read for one; the Memories report now locates and decrypts that file for the Memory.
- **A chat video kept as a bundle is shown with its message on iOS** — `scripts/chat_media.py`. The
  file named after a bundle's CACHE_KEY is a descriptor and the video is a child file, so the join, which
  copies only a whole file that is media, left such a message showing *Media (no cached file)* unless a
  saved copy named after its conversation, message and part stood in. The rebuild the Android run used
  (shards concatenated, a bundle's largest media child, a file decrypted with its message's key) moved
  out of `ParseSnapchat_Android` into a shared module and now runs on iOS too. Where the saved copy and
  the cached file are the same bytes, the Conversations report shows them as one attachment and lists
  the other name (`same_as`).
- **A Library/Caches file byte-identical to a bundle's child or a byte-range part links to its cache
  entry** — `cache_media_report.sccontent_key`. The link named the piece's file name
  (`<CACHE_KEY>_<child>`), which is a cache_controller row only when no claimed bundle lists the child,
  so it usually opened the report without landing anywhere. And `thumbnail~<UUID>` /
  `profilethumbnail~<UUID>` no longer report "thumbnail" as the claim's owner username (`claim_owner`).
- **A relation a later build adds starts at its default in the GUI** — `Snapchat_Auto._saved_relations`.
  The relation policy the GUI remembers names only the relations of the build that saved it, and the
  dialog read an absent one as off, so a new relation that is on by default stayed off for every
  examiner who had saved a policy. The saved choices are now laid over the recommended set, and a key
  this build no longer has is dropped instead of making the run refuse its own `--relations` spec.

### Changed
- **Python 3.14.8 and Nuitka 4.2.2.** `.python-version` pins 3.14.8, and `[tool.uv]
  python-preference = "system"` has uv build `.venv` on the python.org runtime instead of its own older
  3.14 copy, which a rebuilt `.venv` would otherwise have used — and the MSI bundled. Nuitka is required
  at 4.2.2 or later.
- **`--trace-ids` finds a base64 identifier where it is kept as bytes, and names the protobuf field a
  hit is in** — `scripts/trace_ids.py`. An identifier that is base64 (padded, using `+` or `/`, or
  mixing cases and digits) is also searched as the bytes it encodes, their hex, without its padding
  and in the other base64 alphabet; it used to be searched as the text given only, so the same id
  stored as raw bytes in another database was reported as not there. A row-level hit inside a protobuf
  blob now carries `field` — the dotted path of the field it lies in, read from the wire alone
  (`protobuf_wire.field_path`) — so the trace says where in a `message_content` an id sits, not only
  which row. A shorter form found inside a longer one at the same place is reported once, as the
  longer one. A raw hit in a database file on a free page, in a page's unallocated space or in a
  freeblock now says so (`where`) and is marked as in no row, hit by hit — it used to count as "in
  rows" whenever some other row held the same identifier. See [docs/trace_ids.md](docs/trace_ids.md).
- **The build no longer warns that FreeSimpleGUI imports pydoc.** `Snapchat_Auto.py` carries
  `# nuitka-project: --noinclude-pydoc-mode=allow`, which Nuitka reads for the portable EXE and the
  MSI alike: pydoc stays in the build (leaving it out breaks `import FreeSimpleGUI`) and the warning
  goes.

## [1.9.0-beta.2] — 2026-10-05

### Added
- **Search all reports by date** — `scripts/global_search.py`. A **date / time window** (between two
  moments, or within ± N of one) finds rows by when, alone or with the words, which a row must then
  match as well. Every report's rows now carry the times they show — the Contacts, cache_controller and
  Library/Caches rows newly (claims, last read, the device's created / modified / read times, a media
  file's own zoned times). The device's **inode-change** times can be left out with *incl. inode
  changed* — ticked by default — here and in the Memories index's own Time filter: copying or acquiring
  a file can set that time.
- **A run says where it is** — `scripts/progress.py`. The pipeline reports its stages and the count
  inside each one (*decrypting SCContent media, 340 of 1,320*); whenever nothing has been logged for 30
  seconds a *still working* line names the stage, the step and the count, so a long stage no longer
  looks like a crash; and the end of every run — one that failed included — logs how long each stage
  took. A retrieval from Snapchat's servers logs its long waits, and its item numbers start at 1.
  See [docs/progress_and_performance.md](docs/progress_and_performance.md).
- **The same reports in less time** — `scripts/parallel.py`. Reading, hashing and decoding cached
  files (Library/Caches, cache_controller) and decrypting Memories run on several threads, with every
  naming and de-duplication decision still taken in the original order, so the reports are
  byte-identical; the SCContent listing is taken once per run. **Thumbnails** are cut by several
  workers at once, kept in the run folder's `.thumbnail_cache/` by the video's SHA-256 (the same video
  is decoded once per run folder), and no longer stop after ten minutes: the run window can skip them,
  and a headless run can set `--thumbnail-minutes`. A packaged build starts a thumbnail worker without
  loading the GUI and the parsers.
- **The run window** — `scripts/run_window.py`. A run started from the GUI happens behind a window
  showing the stages done with their times, the step and count of the one running, how long since
  anything was logged, the log with its warnings and errors, and at the end *Open report* / *Open
  folder*; *Skip thumbnails* while thumbnails are cut. A retrieval from Snapchat's servers during the
  run shows its progress and controls (pace, Pause, Stop) in the same window instead of its own, and
  no longer waits for that window to be closed before the run goes on.

### Fixed
- **Chat parsing took the last match wherever there were several, and took hours on a large phone.**
  `fixSenders`, `getCacheArroyo` and `mergeCacheChats` compared every message with every friend and
  every cache claim, each match overwriting the one before. They are lookups now, and each choice is
  stated: a user id with several names shows all of them (« / »); a share or sticker takes its media
  before its thumbnail; a message naming a key claimed twice takes this account's claim; each is
  logged with a count when it happens. `getSCPersistentMedia` no longer rebuilds its table after every
  file. Identical output on the test devices, where none of these cases occurs.
  See [docs/report_communications.md](docs/report_communications.md#when-a-value-has-several-candidates).

## [1.9.0-beta.1] — 2026-10-02

### Added
- **`--trace-ids <run folder> <id> …`** — `scripts/trace_ids.py`. Searches every file a run extracted
  for each identifier — as text in any case, UTF-16, dashless hex, the raw and little-endian UUID
  bytes and base64 — reads every database row by row in both readings (with and without its
  `-wal`), and places `-wal` hits in their frame, marking superseded frames. It reports where each
  identifier occurs — file, offset, table, column, row — and never the content, in the log and in
  `trace_ids_<stamp>.json`. For asking whether the device recorded a connection no report makes yet,
  on the machine that holds the case. See [docs/trace_ids.md](docs/trace_ids.md).
- **The MemData identifiers are read** — `memories_media_report.decode_memdata`.
  `ZGALLERYSNAP.ZMEMDATAIDS` and `ZGALLERYENTRY.ZMEMDATAID` (newer app versions) showed as
  `<blob N bytes>`; they are NSKeyedArchiver records of a uuid, a creation time and an entry type, and
  the Memory page now shows them as such. The uuids are searchable and the creation times join the
  Memory's timestamps.
- **A cache claim carrying a Memory's MemData identifier links to that Memory**, in both reports
  (`cache_controller_report._memdata_link`, `memories_media_report.index_claim_uuids`): a recorded
  identifier, like a `ZMEDIAID`, used only when nothing stronger matched and only when exactly one
  Memory records it.
- **Context-34 claims are the snap editor's working copy** — `scripts/data/snap_session.py`. They are
  categorised *Snap editor*, and the app's own session record (`userPreferences/pref.docobjects`,
  `SnapEditor-SnapSessionContext`), which names the file's CACHE_KEY with the claim key and context
  and dates the editing, is shown under the claims — the live row, and earlier versions carved from
  superseded `-wal` frames when a claim corroborates them. `pref.docobjects` joins the source
  fingerprint. `scripts/data/protobuf_wire.py` is the protobuf reader `arroyo_content` already used,
  now shared.
- **Retrieving Memories media from Snapchat's servers** — `scripts/cloud_download.py`,
  `scripts/cloud_memories.py`, `scripts/cloud_refresh.py`; [docs/cloud_download.md](docs/cloud_download.md).
  Off unless asked. It will not start until the examiner confirms holding the legal authority and types
  what it is; it requests only the addresses a Memory's row records (`ZMEDIADOWNLOADURL`, then
  `ZMEDIAREDIRECTURI`; the overlay columns), https to the recorded CDN only, no cookies or credentials,
  one at a time with a delay, a per-minute cap, `Retry-After` and back-off, all adjustable while it runs;
  it decrypts with the Memory's own key and keeps the bytes as received and decrypted in
  `CloudDownloads/`, with a hash-chained record of every request. What to retrieve: Memories whose media
  is **missing** or whose device copy is **incomplete** (the Memories index now filters on that), a
  selection file, or snap ids — copied from a Memory page's *Get from Snapchat's servers…* or from
  *Copy snap IDs* for the ticked Memories — narrowed by include/exclude **date rules** on any of the
  Memory's timestamps. During a run (`--cloud …`) or on an existing run folder (`--cloud-download`),
  which refreshes the affected reports without unzipping again. Retrieved media is marked ☁, never mixed
  with device media, and the authority is stated beside it, on `index.html` and in partial extracts.
  In the GUI: a *Snapchat's servers* section on the main window, a Cloud download window (the
  authority, what to retrieve with counts, the date-rule table with *Add for checked* and *Copy
  range to…*, the pace) and a progress window — progress, the request in flight, a log, and the
  pace, Pause and Stop while it runs (`scripts/cloud_gui.py`).
  Method after DFIR-HBG's Snapchat_DownloadMemories_iOS (overlay retrieval there by John Hyla); that
  repository has no licence, so none of its code is used.
- **Proven by content** — a cached file byte-identical to a Memory's media links to that Memory in the
  cache_controller and Library/Caches reports: first to the media as this run **recovered it from the
  device** (marked ≡ — no retrieval needed), then to a copy retrieved from the servers (decrypted, or as
  received; marked ☁, with the retrieval and its authority). It is how a file no identifier connects to
  its Memory — the snap editor's working copy of a snap later saved to Memories — is proven to be its
  media; on a test device the working copies that are a Memory's media now link without a retrieval.
  The device's copies are never compared with a file some Memory's media was recovered from, and every
  recorded identifier still wins. The Memory's page names those files beside the media they match.
- **Possible Memory — not proven** — `scripts/memory_leads.py`. A cached media file nothing connects to
  a Memory (a snap editor's working copy, a file no claim names, a Memory-shaped claim whose row is gone,
  or an unrecognised one the app claimed as Memories media) lists the Memories of the same kind whose
  creation or capture time falls within ten minutes of the file's claim or filesystem times — ranked,
  with every difference and how many Memories fell in the window, never as a link. Its *Copy snap IDs*
  feeds the Cloud download, which proves or rules the lead out. Shown on **both** sides: on the file's
  row, and on the Memory's — a *≈ possible file* badge and a *Possible cached file* filter in the
  Memories index, and a panel on the Memory's page with every difference and where the Memory ranks
  among the file's leads. The cache_controller report writes them once, as `data/memory_leads.js`, and
  the Memories pages load it (`SCV.annotate` adds it to the index rows). Differences under ten seconds
  are shown to the tenth.
- **Search all reports** — `scripts/global_search.py`, `search.html` beside the reports. One search over
  every report and every conversation's messages at once, from the run's `index.html` or the *🔎 All
  reports* link beside each report's search box. It is each report's own search (its rows' search text,
  `|` for either) on its own `data/index.js`, so the counts agree; each hit opens its row, *Open all*
  opens the report filtered to the same search. Written with every `index.html`, partial extracts
  included.

### Changed
- **The two GPL-licensed files are gone; both were compiled into the MIT-labelled EXE and MSI.**
  `scripts/data/keychain.py` (GPL-3.0-or-later) is replaced by `scripts/data/ufed_keychain.py`, written
  from [docs/ufed_keychain_format.md](docs/ufed_keychain_format.md); on every UFED keychain in the test
  corpus it yields the same items and the same keychain status. `scripts/data/parse3.py` (GPL-2.0) is
  replaced by `protobuf_wire.strings`: the reports are unchanged on the corpus; the legacy
  Communications report's concatenated value for a media message can differ — a stray control
  character is gone, and a printable CDN token the old parser took for a nested message is now listed.
  `requests`, `urllib3` and `pyasn1` are no longer dependencies.
- **The licences travel with the application.** `THIRD_PARTY_NOTICES.md` names every file in the
  repository that is not this project's own (CCL Forensics' `ccl_bplist.py`, the SQLCipher shell with
  its SQLite and OpenSSL, Bootstrap, the emoji font) and what the build carries (FreeSimpleGUI,
  opencv's FFmpeg, CPython and Tcl/Tk, the Nuitka runtime, the update helper); `THIRD_PARTY_LICENSES.txt`
  holds the licence of every Python package the build carries, generated from `uv.lock` by
  `build_tools/collect_licenses.py`. The EXE and the MSI now ship both, and LICENSE, which neither
  shipped before. The README no longer says LICENSE is unmodified.

### Fixed
- **The cache_controller report read `scdb-27` in place** (`load_memory_index`), which gives a WAL
  database a `-shm` beside the evidence file, and only with its `-wal` applied. It now reads both
  readings from staged copies through `sqlite_open`, so a cache file named through a URL the `-wal`
  has since replaced still links to its Memory.
- **A Memory's page named only the first cache file its media came from.** The file table shows one row
  per distinct content and described only the first record of it, so the same bytes recovered again —
  a thumbnail from SCContent and from a caching-media pack, a pack from two folders, a file under two
  members of a group — lost every other source: the Library/Caches report linked a pack (*2 copies*)
  to the Memory while its page named none. Each row now lists every source, with its link and paths
  (`memories_media_report._media_sources`).
- **Library/Caches files linked to a Memory were not listed on the Memory's page** unless they were a
  pack it had decrypted itself. That report renders after the Memories report, so it now writes its
  Memory links to `CacheMedia/data/memory_links.js` and the page lists every one — pack, CDN URL key,
  identical content — with how it was linked (`scripts/memory_backlinks.py`).

## [1.8.0-beta.4] — 2026-10-01

### Fixed
- **An emoji looked different — or was missing — depending on the workstation.** The reports drew
  emoji with Windows' own font, which is only as current as that copy of Windows: no version draws a
  flag (🇩🇿 read "DZ" in Chrome and Edge, an England flag was a bare black flag), and a Windows 11
  font from early 2026 has none of Emoji 17.0, which current iPhones offer — a box, or a sequence
  drawn in pieces. Every report page now links `Reports/emoji_font.css`, which carries **Noto Color
  Emoji** 2.057 (Google's, SIL OFL 1.1, `scripts/data/fonts/`); it draws all 3,972 sequences of
  Unicode 18.0. It comes last in each font stack, after Apple's, so a Mac keeps Apple's emoji, and
  the reports' own text symbols (▶ ⚠ 🗂 …) stay out of it (`report_ui.UI_SYMBOLS`). The legacy
  reports get it too. Emoji now look like Android's rather than Windows'.

## [1.8.0-beta.3] — 2026-09-29

### Added
- **What each chat message is** — `scripts/data/arroyo_content.py`. Every `conversation_message`
  row gets a *Message Body* read from `message_content`: app events are described from their own
  fields (calls with their duration, deleted messages and Snaps, saves to the camera roll,
  screenshots, members added, removed or leaving, groups created or renamed, changes to when
  messages delete, streaks, the My AI welcome…), a share says what was shared (a Story, a Spotlight
  Snap, a map pin with its coordinates, a saved Story with who posted it…), and a reply to a Snap or
  Story says so. The Conversations report shows it in italics, apart from anything typed, and
  searches it; each user id in it is shown by name, and the expanded message lists every person
  with their full, permanent user id. The raw `content_type` is shown with its name, and a bot's
  response and a Tiny Snap's text are read as the message's text.
- The expanded message shows the **sender's permanent user id** beside the name
  (`sender_id (user id, as stored)`), and a conversation's messages can be searched by it.
- A save to the camera roll links to the message whose media was saved ("About: message N ▸").

### Changed
- `content_type` 3 is labelled **Shared content** (was *Video (Unknown Source)*): it is anything
  shared into the chat, and its media may be a photo. Every `content_type` is named; an app event
  with no cached file is a *System message* whatever its type (was 6, 9, 12 and 13 only).

### Fixed
- **A save to the camera roll was reported as a save in the chat.** `4.4.8.7` (`content_type` 9)
  records that someone saved a message's media to the device's camera roll; it read "Saved the media
  of message N in this chat", and only when the target's `is_saved` flag agreed.
- **A reply to a Snap or Story was presented as a caption.** `4.4.7.11.1` is the reply's text, and
  the media on that row is the Snap replied to — typically the other person's Story — not something
  the replier sent.
- **Shared content and stickers disappeared from the Conversations report** when a message's only
  row had no *video* file — a shared photo, a map pin (which has no media), a sticker whose file was
  gone. Such a row is now dropped only beside another row of the same message. The legacy report
  dropped **every** such row: its test looked for media at the start of a cell that starts with the
  attachment's anchor.
- An app event that carries text (a group's old and new name) was shown as the strings glued
  together.

## [1.8.0-beta.2] — 2026-09-24

### Added
- **The Snapchat app's tag in media files is decoded** — `scripts/data/snap_media_tag.py`. The app
  writes base64 of a protobuf into a file's description field (an MP4's 3GPP `dscp` box, a MOV's
  `com.apple.quicktime.description`, the `desc` item of the Android app's ffmpeg-muxed MP4, an image's
  EXIF `UserComment`); decoded, it names the app version, device model and operating system of the
  app that **wrote** the file — for a received snap, the sender's — and the lens id (packed on iOS,
  unpacked on Android; the id matches the app's own lens records). It is shown only when all of it
  decodes, in a block of its own inside every *Embedded metadata* block, with the field and byte
  offset it was read from; the encoded text stays under *all fields*, as stored. See
  [docs/snapchat_media_tag.md](docs/snapchat_media_tag.md). The tag was found by **Keban
  Bronsario**.
- The Memories index has an **APP TAG** chip and a *with the Snapchat app's tag* option in its
  *Embedded metadata* filter; the tag's user agent, lens id and encoded text are searchable in every
  index where the file appears.
- **Chat attachments show what they say about themselves**: an expanded message has the same
  *Embedded metadata* block per image or video (`conversations_report.publish_attachment`), with
  its explanations once in the *Content* header; a conversation is found by the tags of the files
  sent in it.
- **The Android Memories report shows embedded metadata**: it was read but never rendered, and a
  file de-duplicated by content had none.
- **The source files of an edit** — the clips, images and music an editing program lists in a
  video's XMP (`xmpMM:Ingredients`, `xmpMM:Pantry`) — are shown in a collapsed table of their own,
  labelled as theirs: their paths, programs, dates, durations, GPS and any Snapchat tag. None of it
  enters the file's own timestamps, GPS or date search.

### Changed
- `media_meta` reads more of an MP4 / MOV: QuickTime metadata in `moov › meta` (where iOS keeps a
  MOV's `com.apple.quicktime.make` / `model` / `creationdate` / `location.ISO6709` — never reached
  before), 3GPP asset boxes (`titl`, `dscp`, …), XMP (a top-level `uuid` box or `udta › XMP_`), and
  the track headers (`tkhd`, `mdhd`): their times are noted on the `mvhd` row where they agree and
  listed where they differ. `data` atoms are read by their type — numbers as numbers, an image by its
  size — instead of as text. XMP in an image is now parsed (entity declarations refused), so its own
  properties are listed under *all fields* and its edit history is shown as timestamps.
- An ISO base media file is walked by seeking from box to box: its media data is never read, and a
  `udta` after more than 4 MB of sample tables is no longer lost.
- The *read from* line names QuickTime user data whenever a field came from it; it said *the file
  header* when every such field was also a key field.
- `media_meta.STRUCTURAL` lists the XMP spellings of pixel size, orientation and stream layout, so an
  XMP that only restates them does not make a file *notable*.

### Fixed
- **A UFED iOS archive's access time was shown as the device's modification time.** A UFED / CLBX
  archive's `UT` extra field is flagged mtime / atime / ctime but holds the access time in all three
  slots; the real times are in its `metadata.msgpack`. `extract_zip` read the field as the
  modification time for the manifest's `mtimes`, for a file the stat table has no record of (or
  every file, when the table cannot be read) and for the extracted copy's mtime. It now reads it as
  the access time only (`device_fs.from_zip_entry(ut_access_only=True)`), takes `mtimes` from the
  table, and never sets a time from it. The archive is recognised by its stat table, on iOS and
  Android alike.
  **Reports made by 1.6.0-beta.2 to 1.6.1-beta.1 from a UFED iOS extraction** show the access time
  as *modified* wherever it differs from the modification time, and so does any later report built
  from an extraction folder those versions wrote. Reports made since 1.6.2-beta.1 read the stat
  table first and are right wherever it has a record of the file.
- The manifest records which reading applied (`archive`: `clbx`, `graykey` or `zip`). A folder written
  before that is read through `device_fs.manifest_times`: shown as the access time when it holds
  stat-table records or its containers were read from under `filesystem<N>/`, with both readings when
  it cannot say, and the run log advises re-extracting.
- Wording: the popover no longer says a GrayKey acquisition *commonly* sets the access and change
  times (it *can*); GrayKey's fourth `UT` value is the birth time because it matched UFED's named
  birth time for the same device, not because it precedes the modification time; the claim that two
  tools' agreeing stamps proved `UT` to be the modification time is withdrawn, and the extraction
  dates quoted with it are removed from the docs. `_DEVICE_MTIME_BASIS` (unused) is gone.
- The Android test archives carry the `UT` field each tool really writes — GrayKey modified /
  accessed / changed plus its `S2` SHA-256, UFED modified / accessed / 0 — instead of a four-value
  one no Android archive has.

## [1.8.0-beta.1] — 2026-09-22

The Android side, rebuilt to produce the same reports as iOS — see
[docs/snapchat_android.md](docs/snapchat_android.md).

### Added
- **Conversations, Contacts, Memories and cache_controller reports for Android**
  (`scripts/ParseSnapchat_Android.py`). The chat database and the cached-file index are the same on
  both platforms, so the chat parsing and the cache_controller report are the iOS ones; the Android
  run adds the account (`arroyo.db` `required_values`, `shared_prefs`), the contacts (`main.db`
  `Friend` + `CombinedUsername`, every row, each stating what the table records about the link and
  quoting the column comments the table's own schema carries) and the Memories
  (`scripts/memories_android_report.py`: `memories.db`, location without a keychain; media found in
  the app's own cache folders by the MD5 of a request string built from the snap's ids, in the
  native cache by claim or URL token, and decrypted with the snap's key, the My Eyes Only key
  unwrapped with the master key `memories_meo_confidential` stores, or a key pair inside the
  `snapdoc` — only bytes that are media are ever accepted; thumbnail packages split into their
  images; `snap_ids` read as the FlatBuffers vector they are).
- Chat media the cache holds only in pieces — a video stored as a bundle, a file stored as byte-range
  shards — is rebuilt for the Conversations report (`materialize_chat_media`) instead of being
  listed as having no cached file; the attachment says how it was put together.
- `scripts/android_layout.py` — where each artifact is in an extracted Android tree, logged as an
  inventory, and `android_survey.json` beside the run log: tables, columns, row counts, preference key
  names and folder sizes, with no content and every UUID replaced.
- The Android extraction checks every file against the SHA-256 a GrayKey archive records for it
  (`S2`), and records the inode / device number (`IN`) and permissions.

### Fixed
- The Android run logged a traceback for every run (`logger.info("…", database)`, a print-style
  call the logging module rejects) — the error in the report that prompted this rewrite.
- **Android extraction** (`extract_zip.android_entry`): the package name was matched anywhere in the
  path, so a GrayKey archive's three copies of each private file were written onto one another and the
  app's shared-storage folder was merged into its private one. Each entry is now mapped to its
  canonical device path and written once (the copy read through `/data/data` wins, and differing
  copies are counted); the whole app folder is taken instead of three sub-folders; symbolic links are
  not written; an extraction without the app raises a clear error instead of pausing and crashing.
- A time of 0 in a ZIP entry's `UT` field is left out rather than shown as 1970 (a UFED archive of an
  Android phone writes 0 as every file's change time), and an archive that records owner 0/0 for
  every file of the app is not reported as root-owned.
- The legacy Android report skipped every other file after one it rejected, and failed on a missing
  cache folder; it now writes into `Reports/Communications_legacy/` and is produced only with the
  legacy reports, like the iOS ones.

## [1.7.0-beta.1] — 2026-09-20

Four methods adopted from iLEAPP's iOS Snapchat module (Alexis Brignoni, MIT), credited where
implemented and in the report popovers; the comparison, and where the two tools part ways on
purpose, is in [docs/related_ileapp.md](docs/related_ileapp.md).

### Added
- **The app's Memories search index** (`scripts/gallery_search.py`; `Documents/gallery_search`
  is now extracted and fingerprinted). Plain SQLite, no keychain: per snap, a local calendar
  date, time words, the app's reverse geocoding **down to street and postal code**, a place
  cluster, `Image` / `Video`, the caption and the app's visual labels with their confidence. Joined
  onto the Memories report by snap id and shown as stored — its own section on the detail page,
  the index row's collapsed block and search tokens, a *Search index date* row (a string, never an
  instant), and a fourth Geolocation state, **place name only (search index)**, for a Memory with
  no coordinates. On a device whose keychain is backup-class it is the only location the report
  can give. Read with and without its `-wal`; a snap indexed since the checkpoint is marked.
- **Key rows with no Memory row** (`orphan_key_memories`). A `gallery.encrypteddb` `snap_key_iv`
  row whose snap id no reading of `scdb-27` lists is a Memory the app no longer shows; it is now a
  **RECOVERED** row with its key (unwrapped when the persistedkey is at hand), coordinates and
  address title, its own `-wal` filter option and a detail page that says why its panels are empty.
  A row whose key a listed Memory already holds is the same media object and is not listed twice;
  one a Memory references under a *different* key is, and names the referrer.
  `ZDUPLICATEDFROMSNAPID` joins the index's search tokens so either id finds the other.
- **The Snapchatters that are not contacts** (`load_snapchatters`, `_snapchatters_section`). The
  `snapchatter` table of `primary.docobjects` is the app's cache of every Snapchatter it has
  rendered; on the old-schema test device all but a handful of its rows were Quick Add suggestions.
  The Contacts report lists them apart in a collapsed table — display name from the FlatBuffers
  document, the three username fields, user id and *why cached* (Quick Add suggestion when a
  `snapchatters__displaysuggestion` page names the id; otherwise the docobjects table that does, as
  stored) — with no anchors, no selection and no place in a partial report. The run log states the
  split.
- **All three username fields** on every contact — username, mutable username, legacy username,
  each with its source table — and a badge when the mutable username differs from the username
  (`MUTABLE_NOTE`: what it adds is not established; the two were equal on every tested row).
- **The owner's `user.plist` values** on the device owner's row: username, user id, laguna id and
  the client-encryption identifier / key / IV, as stored. A device carries **two** client-encryption
  records, and only the `ClientEncryptionService.plist` one opens anything: every block-aligned file
  of all four test extractions was tested against the `user.plist` key by the padding of its last
  block — which identifies a CBC key whatever the IV or framing — and no store matched, while the
  same test picks out exactly the `sccache.gallery-stories-snap.data` entries under the
  `ClientEncryptionService` key. `ACCOUNT_NOTE` carries that caveat so the row is not read as a key
  to try; the method is in `docs/snapchat_ios_cache_media.md`.
- `scripts/data/tsaf.py` — one keyed reader for Snap's TSAF containers (`user.plist`,
  `ClientEncryptionService.plist`), whose one rule is that a value must *immediately* follow its
  key: a signed-out account's `user.plist` keeps its identity keys with an empty value, and "the next
  string" there is the following key's name. `getUserID` reads the keyed `user_id` first and, when
  it is absent, logs what the file does hold instead of "No user found".
- `scripts/data/flatbuffers_doc.py` — a root-table reader for the `*.docobjects` FlatBuffers
  documents, handing back a name only when the document's slot 0 is the user id already known.

### Changed
- The display names the two `primary.docobjects` contact fallbacks read are FlatBuffers fields
  behind that self-check; the fixed byte-offset carve is kept only as the fallback for a document
  that fails it, and logged when it answers.

## [1.6.2-beta.1] — 2026-09-16

### Added
- **The device filesystem's whole record of every cache file, from the extraction archive**
  (`scripts/data/device_fs.py`; `msgpack` becomes a dependency). A Cellebrite UFED archive carries
  `metadata<N>/metadata.msgpack`, a stat record for every path on the volume — created (birth),
  modified, accessed and inode-changed times at **nanosecond** precision, owner, mode, inode, data-
  protection class and xattrs; a GrayKey archive writes four timestamps into each entry's `UT` extra
  field, the non-standard fourth being the birth time. `extract_zip` reads the richest record the
  archive has (streaming the UFED table, keeping only the extracted paths) into
  `extraction_manifest.json` under `fs`, keeps `mtimes` for older readers, and gives the extracted
  copies their sub-second mtime. Every report shows the record under the source path it belongs
  to, through one renderer: identical instants merged onto one line, a split file's parts bounded,
  the source and precision named, with the caveat that *accessed* and *inode changed* can be the
  acquisition's own. The Memories index lists each recorded timestamp with its source and the time
  filter matches on all of them.

## [1.6.1-beta.1] — 2026-09-16

### Added
- **Every timestamp in the Memories report says where it was read from.** A Memory's times come
  from three different things that need not agree — the app's database, the media file's own
  header, and the device's filesystem — and the report now tags each value with its source instead
  of folding them into one column: `scdb-27 › ZGALLERYSNAP.<column>` / `ZGALLERYENTRY.<column>`
  (Cocoa seconds, converted to the run's timezone), `inside <file>` (EXIF, XMP, PNG text, an MP4's
  `mvhd`, QuickTime `creationdate` — converted only when the file *states* its zone, marked
  *UTC assumed* where only the format defines it so, otherwise shown *as written*), and
  `extraction archive › <path>` (the cache file's mtime **on the device**, from the archive entry's
  `UT` field via `extraction_manifest.json`, never the extracted copy's own). The index row's
  expanded area draws the source as a third column with a `?` explaining the tags; the *Created*
  column header names its field; on the detail page the two database tables say which store and
  encoding they came from, a file's own timestamps sit under its *Embedded metadata* block (as
  written / in the report's timezone / why), and the device mtime sits on the line of the path it
  dates in the *Media files* table. All of them are keys for the time filter. A poster frame this
  tool generated is never read.
- **Embedded metadata (EXIF and the like) is read from every recovered media file** —
  `scripts/data/media_meta.py`, Pillow and the standard library only: EXIF and XMP in a JPEG or
  WebP, text chunks in a PNG, the `mvhd` header and QuickTime user data (`©xyz` location, `©mak`,
  `keys`/`ilst`) in an MP4 or MOV. The detail page gets an **Embedded metadata** section beside the
  thumbnail: per file, the container, pixel size, the fields that identify a device or place (make,
  model, software, lens, serials, the GPS fix as a map link labelled as the file's own), the file's
  timestamps, and everything else behind *all fields — N more*. A file with nothing says so, since
  "no EXIF" is itself a finding. The index gets an **EXIF** chip and an **Embedded metadata** filter
  (with / none found, counted), and the fields join the row's search text. HEIF/HEIC is not read
  in this build and the page says so rather than reporting nothing. The same block, through the
  same renderer (`report_ui.embedded_meta_html`), appears in the **cache_controller** report for
  every published cache file and bundle child, and in the **Library/Caches** report for every
  recovered media file — where it replaces that report's own `mvhd` grid and keeps its caveat. The
  cache_controller report also dates every on-disk path with the file's mtime on the device.
- **Search the Memories index by AES key / IV and by what the file says about itself.** The key
  and IV in hex, camera make and model, software and GPS join the CDN URLs (already searchable) in
  each row's search text. A new collapsed block in the row's expanded area — *CDN URLs, AES key /
  IV, embedded metadata — also matched by Search* — lists them all, each with its source, so a hit
  can be confirmed.
- **A "Text size" control on the main window**, beside the appearance button: *Auto* (follow the
  display) or any percentage from 50 to 400, typed or picked from the common sizes, applied on
  Enter. It writes the same `dpi_scale` setting as `--dpi-scale`, so there is one setting rather
  than two, and it is saved before the window is rebuilt, so the choice survives a Cancel. The list
  is editable on purpose: walking `tk scaling` across 100–200% gives 55 distinct renderings of the
  form's fonts, most of them between the presets.
- **The update folder says on the form when it could not be read.** A share that is not connected
  at startup meant no update would ever be offered, and the only trace was a warning in the log.
  The startup check now reports what it found (`dfjsim_shared_tools` 0.4.0,
  `check_for_update()` returns an `UpdateCheck` with a status), and when the folder could not be
  read — or the running version cannot be compared — a one-line note under the folder field says so
  and points at **Check**, which clears it. Deliberately not a dialog: a share that is
  down every morning must not greet every start. A folder with no build in it yet, or an update
  the examiner declined, gets no note. `Snapchat_Auto.startup_update_check`.

### Fixed
- **A `<details>` opened inside an expanded index row closed itself a frame later.** The virtual
  table redraws a row from its static detail string on every re-measure, which reset the element;
  the table now remembers which `<details>` are open per row and restores them after each redraw
  (`report_ui.VTABLE_JS`).
- **Rebuilding the window (theme or text size) blanked the two Browse buttons.** `window.read()`
  reports a `FolderBrowse` in `values` with an empty string, and the rebuild passed every value to
  `Element.update()` — whose first argument, for a button, is its label. Buttons are skipped now and
  the browse buttons carry explicit keys.

## [1.6.0-beta.5] — 2026-09-01

### Added
- **The GUI draws at the display's DPI** (`scripts/hidpi.py`). The process claimed no DPI awareness,
  so Windows drew it at 96 dpi and stretched the bitmap to fit a scaled laptop screen or an RDP
  session — the soft, smeared text. Per-Monitor v2 awareness is claimed before the first window
  exists, and the two halves that must follow it move together: `tk scaling` for point-sized fonts
  and `px()`/`px2()` for every constant written in pixels. FreeSimpleGUI's own
  `set_options(dpi_awareness=True)` is a silent no-op on Windows 11 (it tests `platform.release()`
  against `'7'`/`'8'`/`'10'`). `--dpi-awareness unaware` reproduces the old rendering in the same
  build, `--dpi-scale` forces a size, `--dpi-report` prints the diagnosis. See
  [hidpi_scaling.md](docs/hidpi_scaling.md).

### Fixed
- **`KeyError: 0` on Ok in the packaged beta.4.** The OS radios had no key, so they were numbered by
  position among the keyless elements, and re-ordering the form moved them. They are keyed and read
  by name; a GUI test now asserts that every key `main()` reads is present in the values it gets.
- **Text and file paths are written as UTF-8, not in the locale encoding** (cp1252 on Windows
  examiner workstations). Three faults, one cause: the legacy Memories report was written with the
  locale encoding and a Memory caption holding an emoji raised `UnicodeEncodeError` and lost the
  report; the legacy Communications report said `encoding="cp1252"` outright and failed the same way
  (both are UTF-8 now and both documents declare it); and the poster worker's pipes carried file
  paths under the locale default with `errors="replace"`, so a path with a character outside cp1252
  reached the worker as `?`, it opened nothing, and the video was recorded as undecodable. With the
  path intact a fourth surfaced: `cv2.imwrite` goes through the ANSI API on Windows and silently
  returned False; the frame is encoded with `cv2.imencode` and written by Python. A test guards that
  no shipped module opens a text file for writing without an encoding.

### Documentation
- Recorded what the two pytest warnings are (both third-party, neither ours) and the scoped
  `filterwarnings` policy worth adopting after the beta.
- Recorded beside the Nuitka flags why the pydoc anti-bloat warning stays: FreeSimpleGUI imports
  `pydoc` at module scope, so excluding it gives a build whose GUI dies on import.

## [1.6.0-beta.4] — 2026-08-14

### Added
- **The legacy reports are off by default.** One checkbox on the main window, remembered;
  `--legacy-reports yes` headlessly, which also overrides the `legacy_reports` token a selection
  file may record. The Related-items dialog states the setting instead of offering a second control.
  Off because they are superseded by the Conversations, Contacts and Memories reports, the legacy
  Memories report decrypts every Memory on the device whatever the run was for, and neither has row
  selection — and because producing them writes reassembled whole cache files into `ExtractedData`
  beside the device's own shards, which the cache_controller report then reads back as device data.
  With them off, the extraction folder is left exactly as unzipped; everything else in a full report
  is byte-identical between the two settings.
- **A theme button** cycling *Follow OS* / *light* / *dark*, remembered in
  `~/.snapchat_auto_gui.json`. The window is rebuilt with the form carried across, and the choice is
  saved before the rebuild.

### Changed
- **The main window follows the OS appearance, is resizable, and shows both ends of a long path.**
  The old `DarkBlue3` theme left nothing legible on it. The form scrolls inside the window with
  Ok/Cancel outside the scroll area; path fields grow with the window; a long path is shortened from
  the middle so the case folder and the file name are both visible, with the true value restored
  into `values` once per read (`reconcile_paths`) and shown whole while the field has focus.
- **Explanations moved behind "?" buttons.** Every setting had two to four lines of prose printed
  under it; the text is now shown on hover and in full in a popup on click, as the reports already
  do. The settings form went from 912 px to 596 px of content and opens unscrolled. Popups are
  wrapped once, at the width the window is built to, instead of twice.
- **Settings are grouped** — Evidence, Report options, Partial report, Updates — and the case /
  exhibit reference comes first: it is stamped on every page handed over and is the one field
  deliberately not remembered between runs. The viewport is sized from the screen rather than a
  fixed 640 px.
- Base and hint fonts one point larger; the AS-IS disclaimer is laid out from its text (its last
  line had been clipped).
- README records that FreeSimpleGUI is LGPLv3+ and what that obliges on distribution.

## [1.6.0-beta.3] — 2026-08-12

### Added
- **Geolocation filter on the Memories index, in three states**: coordinates recovered / on the
  device but none recovered / the app recorded no location. Three rather than two on purpose:
  geolocation lives in the encrypted gallery database, so a run without the FFS keychain recovers
  none of it, and folding "not recovered" into "no location" would report this tool's own gap as a
  fact about the device. Options carry their counts and grey out when empty; `_geo_state` is read by
  the filter and the index cell alike so the two cannot disagree.

### Fixed
- **One unreadable poster frame cost the whole Memories report** (`FileNotFoundError` on
  `<snapId>_poster.jpg`). Two videos can yield byte-identical frames; the duplicate was correctly
  removed, but the frame was read once per Memory, so a further Memory sharing that video opened a
  file just deleted. Frames are published once per video before any Memory is handed one, and the
  poster stage is guarded as a whole: a poster is a derived artifact and every Memory works without
  one. Decryption and publishing are deliberately *not* guarded that way.
- **A selection marked `expanded` with no row that says it was expanded is no longer trusted.** The
  marker tells a run the ticks are already a closure, so a false one produced an extract *smaller*
  than asked for, silently. `is_expanded()` now requires at least one row recording the relation
  that put it there; `validate()` names the mistake for an integrator. An external tool cannot
  produce an expansion — it is derived from the evidence — and should set `relations` instead.

## [1.6.0-beta.2] — 2026-08-11

### Added
- **Expand, check and adjust a selection before building the extract.** `--dry-run` lists every row
  it would add with the reason; `--expand-selection` writes the closure back out *as a selection
  file* and builds nothing. Loaded into the full report it ticks every row the extract would hold,
  shading the rows a relation pulled in and counting them apart, so the examiner unticks what should
  not go out and builds from that. The file is stamped with the full report's `run_id`, records
  `relations: minimal` and says it is an expansion, so building it does not add a second hop. The
  workflow is [guide_partial_reports.md](docs/guide_partial_reports.md).
- **A Memory can be named by its cache key** (`mem` gains a `cachekeys` alternate; the index
  registers every cache key and pack item hash of each Memory's media) — the only `mem` identifier a
  tool that never read `scdb-27` can have. A cache key naming every member of one group resolves to
  all of them, since the group *is* the snap rows sharing that media; one naming rows of different
  groups still refuses. A server message id resolves in either spelling (`<message>` or
  `<message>.<part>`), case-folded.
- Two relation defaults changed at the examiner's request: `conv_messages` off (ticking a
  conversation asks for the conversation, not every message in it), and the new `msg_sender` on,
  which is what makes that safe.

### Fixed
- **A message's sender is matched by user id, not by display name.** `fixSenders` replaced
  `sender_id` in place with the friend's name, so the `ts_sender` alternate — documented as a user
  id — was built from a name and a tool doing what the format says matched nothing. The id is kept
  in a `Sender User ID` column and is the only key; a message whose sender id was not recovered gets
  no key. The comparison was also case-sensitive against a case-folded index.
- **A message's position is never an identifier.** A message with no server message id was anchored
  on its position, so recovering one more message made the same string name a different message. It
  is now anchored on arroyo's `client_message_id`; a positional id must be proved by an alternate or
  is reported as not found.
- **The Library/Caches "modified" column showed when *we* unzipped the file** — `os.path.getmtime()`
  of the extracted copy, beside path, size and stored hash, which are facts about the device. An
  extraction ZIP does record the real mtime, in the entry's `UT` extra field (UTC seconds; the DOS
  date/time is a zoneless wall clock and is not used). `extract_zip` records it in
  `extraction_manifest.json` and stamps the extracted copies; the report reads the manifest, because
  a file always *has* an mtime, so from the file alone "the device recorded this" cannot be told
  from "the archive recorded nothing". Absent, the column says *not recorded*.
- **A timestamp alternate is whole seconds on both sides.** `created_unix` came from
  `datetime.timestamp()` as a float, so the index held `…|1700000000.0|…` while a tool exporting the
  documented integer looked up `…|1700000000|…`. One spelling now (`ts_sender_key`); `validate()`
  flags a value that still looks like milliseconds.

### Documentation
- A media id is not a substitute for a snap id: of the three `scdb-27` identifiers only `ZSNAPID` is
  unique to one Memory row; `ZMEDIAID` is shared across a group by design and `ZENTRYID` names an
  album entry. `cache_keys` is the answer for a tool that cannot read the database.

## [1.6.0-beta.1] — 2026-08-11

The first build carrying **partial reports**: a normal run with `--selection <file>` renders only
the rows an examiner ticked plus the related items they asked for, into `Reports_partial_<stamp>/`,
and the full `Reports/` is never touched. See [report_partial.md](docs/report_partial.md).

### Added
- **Selection file schema 2.** A message selection was stored as its page anchor (`msg-12.0`), but
  `server_message_id` is a per-conversation ordinal, so that id named a different message in every
  chat. The stored id is now qualified by conversation; the anchor is unchanged. A schema-1 file
  still loads, but its bare message ids go to a quarantine bag that is not counted, not saved,
  explained in a banner, and never promoted to whichever conversation is open. The file records the
  tool version and, per ticked row, the identifiers it can be found by again. `.json` is the default
  save (browsers flag a `.js` download); a `.json` renamed to `selection.js` fails silently, so
  `--install-selection` does the conversion.
- **Source fingerprints and a tool-version gate** (`scripts/source_fingerprint.py`). Every run
  records the artifacts it read — role, path, size, MD5, SHA-256 — into `Reports/sources.json`, on
  `index.html`, and into each page so a saved selection carries a copy. A later run checks that copy
  against the extraction it is handed. Databases are fingerprinted with their `-wal`, because the
  log is where recovered deleted rows come from: a differing `-wal` is a real difference, never a
  cosmetic one. The `-shm` is *not* fingerprinted, since our own run still moves its mtime (see
  TODO.md). `verify()` asks "same evidence?", `check_version()` "same build, `+build` tag
  included?", and `reuse_allowed()` needs both; no provenance yields "cannot be verified", never
  "verified".
- **Every report generator is split into `index()` then `render()`** — index works out which rows
  exist and what each can be found by again, render writes the report from that, optionally filtered
  by a closure; `main()` is the two in one call. A partial run runs every `index()`, decides one
  closure from all of them, then every `render()`, because the relations run in both directions
  across a report order fixed by manifest dependencies. `tests/test_index_render_split.py` pins that
  rendering with a closure holding everything is byte-for-byte a full render.
- **What an extract states about itself.** Every cross-report link goes through `report_ui.xref`,
  which marks a link whose target the extract does not contain — keeping the label and identifier,
  because a link that goes nowhere misleads and one deleted hides that the association exists. Each
  page carries a PARTIAL banner, "N of M in the extraction" figures and a provenance block;
  `partial_manifest.json` records every row's reason and every cut cross-reference. Message-derived
  figures are recomputed from the messages kept, a group page rendered in part states the group's
  real size, and the cache reports say when the Memory holding their plaintext is absent. Three
  things an extract must not inherit are stated rather than silently missing: the legacy reports,
  the legacy `cache_links.json`, and the staged `sqlite_views` copies.
- **Wired into the CLI and the GUI.** `check_evidence` runs before any report is built, version
  first; exit codes distinguish wrong evidence from an unresolvable selection. Both cache reports
  take a `links_dir` pointing at the full report folder the selection was made in, and
  `assign_groups` derives a group's page key from the whole group so the name does not depend on
  which members were rendered.
- **A dependency-free selection package** — `packages/snapchat_auto_selection/`, a stdlib-only uv
  workspace member the app itself imports, so another tool can write a selection without taking on
  pandas, OpenCV or a GUI toolkit. `anchor_for()` derives every id from the identifiers a tool
  actually has, each `add_*` records the alternates, `validate()` names the unqualified-message
  mistake, and `--describe-selection-api` is the handshake answered by the installed executable.
  Spec: [selection_format.md](docs/selection_format.md).
- **A selection file's own `relations` spec is the run's default** — the run's settings win, then
  the file's spec, then the built-in defaults; whichever applied is named in the log, the provenance
  and the manifest. A spec this build cannot read is refused rather than silently replaced.
- **A Memory group is one index row.** A group is drawn as its earliest member's row with every
  member rendered in its expanded area, each with its own ids, timestamps, selection box and Details
  button. Every Memory keeps its own data row, so anchors, selection ids and cross-report links are
  untouched. A group is found by any member's values; a lead reached through a member opens with
  that member highlighted; "Select all shown" ticks the members the filters match and no others;
  `C.reset()` unfolds. A **Fold groups** control turns it off. The lead row also carries a second,
  three-state checkbox for the whole group — separate from the row's own box, whose `data-id` *is*
  its Memory's store id.
- **A time window** on the Memories index, the Conversations index and every conversation page —
  *between* two points or *within ± N* of one — matching every timestamp a row carries, including
  those only its detail shows. Keys are read back from the displayed `YYYY-MM-DD HH:MM:SS` strings,
  so no timezone arithmetic happens in the browser and what a row is filtered on cannot disagree
  with what is on screen. A row with no readable timestamp is hidden while a window is set, never
  included by it. A conversation gets a scope control — its own activity, its messages' times, or
  both — and *message times only* never falls back to a feed date. `tests/test_fold_and_time_js.py`
  executes the JS under node with a DOM stub.

### Fixed
- **Identical Memory media is published once**, and every Memory that recovered it links to that
  copy. Members of a group routinely recover the same bytes from the same cache file; each got its
  own copy in `media/` while the group's file table listed one row per distinct content, so the
  other copies were linked by no page. `_save_media` keeps a per-run `{md5: file}` map; posters go
  the same way. Only the copy on disk is shared — each Memory keeps its own role, cache key, source
  paths and basis — and the group's file table names which Memories recovered each row. Zero-byte
  files are excluded, matching `assign_groups`. Two runs over one extraction produce byte-identical
  `Reports`.
- **A grouped Memory could not be resolved** by a selection: `ZMEDIAID` matched both members of its
  group and the build called the selection ambiguous. An exact id match is now the answer, and an
  alternate matching several rows is *not discriminating* rather than ambiguous — skipped for the
  next.
- **The cross-report manifests were looked for in the run's own `Reports/`**, which the GUI
  recreates per run, so every association in a partial report degraded to nothing; the folder is now
  derived from where the selection file sits. A refused partial run raised out of the GUI leaving
  nothing in the log. The `beforeunload` guard prompted on close in every tab opened after anything
  had been ticked.
- **An empty report accused itself of a missing data folder**: the banner fired whenever there were
  no rows, so a partial extract legitimately holding nothing reported a fault. It fires only when
  the data script really did not run, and the empty-table text says "This extract contains no …"
  instead of "nothing matches the current filters" when no filter is set.
- **Report pages are reproducible.** The page copy of the sources manifest carried `collected` —
  when the run happened — so every page differed run to run; it carries identity only now.
- Two selection bugs exposed by folded members: the delegated `change` handler read the *row's* key
  record for a hand-written checkbox inside a virtual row (an inline `data-keys` now wins), and
  `scSyncBoxes` skipped every box inside a `.vr`.

### Changed
- The run `index.html` leads with the links to the reports; the source artifacts sit under them in a
  block that starts closed (plain `<details>`). The provenance block's long sub-sections are folded,
  each summary carrying its answer.
- Memories index: the snap count is its own line and the button says "Details".
- The `-wal` wording in the fingerprint verdicts no longer suggests a log difference can be waved
  through.

## [1.5.2] — 2026-08-08

### Fixed
- **The chat reports now carry every row `conversation_message` holds.** `mergeCacheChats` dropped
  every message whose media is no longer cached — on an account whose older media has aged out, a
  large share of the conversation. Those rows are kept as "Media (no cached file)". Three defects
  from the same pass: `getChats` skipped a message identified only by `server_conversation_id`
  (`reportExcludedMessages` now warns about any row left out); a media message's protobuf **media id
  was shown as the message text** — `getChats` reads the text from the field that holds it
  (`4.4.2.1` for a text message, `4.4.7.11.1` for a media caption) instead of concatenating every
  string, which recovered captions buried inside `<key>=<iv>==<uuid>…`, with the last gaps needing
  the wire format read directly because a body whose bytes are also valid protobuf is mis-decoded as
  a submessage; and a message whose protobuf holds no string was "ERROR - Something went wrong" —
  those are app events (the `4.4.8` branch), now **System message**, with `4.4.8.7` called a save
  only after corroborating `conversation_message.is_saved`.
- **My Eyes Only media reported as undecryptable while another tool decrypted it** — four causes.
  The locked-MEO notice named the account by `userHash` alone, an identifier appearing nowhere else
  the examiner looks; it now names the userId with the hash beside it and distinguishes *no
  `persistedkey` at all* / *one for another account* / *this account's, unwrap failed*. **A Memory
  *moved* into MEO is not re-encrypted**: the new snap row's key is wrapped but it still points at
  the original media object whose `snap_key_iv` row survives unwrapped, and those rows were dropped
  for having no snap — `adopt_media_object_keys` adopts such a key, labelled as the media object's
  and counted apart from keychain unwraps (media captured straight into MEO still needs
  `persistedkey`). **A Memory with no key was skipped by the media phase entirely**, so it showed no
  media while the cache_controller report played the very same shards; keyless Memories now go
  through the same addressing and `decrypt_sccontent` recognises plaintext from the magic bytes
  before it looks at a key. And **`cache_controller.db` claims were matched by substring**, dropping
  `<snapid>_memories_backup_transcoded` (the UUID first, so the tested prefix was empty — full media
  that, in the test corpus, is the only copy of some minute-long videos) while matching
  `…/previewmedia/<UUID>` on "media". One canonical shape list (`SNAP_CLAIM_PREFIXES` /
  `classify_snap_claim`) is read by both cache reports and matched exactly.
- **Every conversation had every participant** in the database: the arroyo record was a module-level
  template copied with `dict()`, so all of them aliased one `user_ids` list — every contact matched
  every conversation and showed the extraction's whole message total.
- **Poster frames were never extracted in a packaged build.** `_worker_command` decided on
  `sys.executable`, which a Nuitka standalone sets to a path that does not exist; the build is
  detected with `__compiled__`. A video that was never attempted is no longer said to have "not
  decoded", and one that yields no frame says why (it did not decode, typically because only part of
  it was cached; or it has no video track).
- The Contacts report escaped the Conversations index's marked-up activity cell into visible tag
  soup; first/last now travel as text plus a `date_source` through `report_ui.activity_cell`, and a
  conversation nothing can date says "no date recorded". The Library/Caches *Recovered* filter filed
  owned-elsewhere and app-asset rows under "not recovered"; four states now, with counts.

### Changed
- Every index bar ends with a red **Clear all filters**; `-wal` filters on the conversation index
  and the message table; a filter with nothing to match is omitted or disabled with its count. The
  Memories "Media" filter is "Recovered media" with four states.
- *First/Last message* became *First/Last activity*: a conversation with no message shows the range
  from its own `feed_entry` row tagged "feed"; conversations only arroyo.db knows about are listed;
  `conversation.creation_timestamp` is exposed, with the caveat that on a restored device it can
  post-date its own messages.
- The Memories index thumbnail opens the detail page in its named tab; the Links cell stays on one
  line; "(index)" dropped from the Memory chip in both cache reports.

## [1.5.1] — 2026-08-07

### Added
- **Optional update check**: `Snapchat_Auto.py` compares `[project].version` (now carrying a
  `+build.<N>` tag) against installer filenames in a folder the examiner configures in the GUI,
  stored only in `~/.snapchat_auto_gui.json` and never bundled. Uses
  `dfjsim_shared_tools.auto_update`; see [auto_update.md](docs/auto_update.md). First shipped in a
  1.5.0 rebuild on 2026-08-05.

### Changed
- GUI hint text wraps (`_hint`).
- `dfjsim_shared_tools` is depended on at a tag instead of a local path; the MSI build gets a
  console and ships `pyproject.toml`.

## [1.5.0] — 2026-08-04

### Added
- **Conversations report** (`scripts/conversations_report.py`, `Reports/Conversations/`): an index
  with one row per conversation — type, title, participants, message and attachment counts,
  first/last message, conversation id — plus **one detail page per conversation** with its full
  message table. Both are the shared virtual table, so a conversation with tens of thousands of
  messages opens as fast as an empty one. A message row expands to the full text, the attachment at
  full size with its MD5/SHA-256, and every raw value including both the stored UTC timestamp and
  the converted one. Attachments are hard-linked into `media/` under their detected extension;
  `mov`/`m4v`/`webm`/`gif` are recognised. Conversations with 0 messages are listed rather than
  dropped. Every derived value states its source in a "?".
  [report_conversations.md](docs/report_conversations.md).
- **Contacts report** (`scripts/contacts_report.py`, `Reports/Contacts/`): one table of every
  contact — display name, username, **legacy username** (from `primary.docobjects`, the index
  tables' column names looked up rather than assumed), user id, conversations, message count,
  first/last — with a device-owner badge and a banner naming the artifact the contacts came from,
  including the warning that the `primary.docobjects` fallbacks are not the friends list. A "?" per
  identifier column says what each is worth (display name free to change, username changeable, user
  id permanent), plus a "Username changed" filter. Membership comes from participant lists matched
  on user id, so group chats appear, not only the private conversation.
  [report_contacts.md](docs/report_contacts.md).
- The original chats/contacts report is `Reports/Communications_legacy/`, rendered from the **same**
  parsed rows as the Conversations report.
- **The device owner is marked wherever named** — sender, participant list, participant user id, and
  both Contacts identifiers. Participants show display name + username and link to the contact's
  record; the conversation header lists their user ids, the one identifier that survives a rename.
- Both message and conversation identifiers are reported: `client_message_id` under the server id,
  and `client_conversation_id` / `server_conversation_id` on the conversation page, selected only
  when the app version's schema has them.
- `cache_links.json` **version 3** adds an `href` per record, since with one page per conversation
  an anchor no longer says which document to open; `load_chat_links` falls back to v2/v1.
- **Memories deleted from `scdb` are recovered from superseded `-wal` frames.** Reading each
  database twice gives the two states SQLite can produce; neither reaches a page image a later frame
  in the same log replaced. `sqlite_open.superseded_wal_pages()` exposes those frames,
  `carve_deleted_memories` carves `SCMemoriesSnapEncryption` archives out of them and tests each
  carved key against the cache file claimed for a snap with no Memory row. A key is accepted
  **only** when it decrypts the claimed file into valid media; an already-plaintext file is skipped
  because it would "decrypt" under any key. Badged `CARVED` everywhere, with a banner explaining
  that no database row survives. [sqlite_wal_handling.md](docs/sqlite_wal_handling.md).
- **Partially cached Memories media is detected and flagged.** The cache holds only the byte ranges
  the device streamed, so truncated and gap-riddled `.mp4`s were written as if complete. Three
  checks: a missing PKCS#7 tail, holes between `<start>-<end>` shards (`_part_coverage`), and a pack
  shorter than its declared payload. Incomplete files get a red badge stating what is missing, a
  **PART** chip and filter, and a header count; plaintext files are *completeness not verified*
  rather than guessed at. A ciphertext whose length is not a block multiple keeps its block-aligned
  prefix.
- **Cache files the index does not know about are listed** in the cache_controller report
  (`orphan_entries`, category "Not in the index") — only when no indexed entry resolved to the file,
  so bundle children and byte-range parts stay under their parent.
- **Text sent with media is kept** (`Message Text`): `mergeCacheChats` had overwritten the parsed
  content with the attachment's cache key. Values that are not text are not shown as a message, and
  a message whose protobuf could not be parsed is "⚠ not parsed" instead of the parser's error
  string.
- `collect_media` logs each phase's size, progress and elapsed time — a long run on a large gallery
  was indistinguishable from a hang.
- The two `gallery.encrypteddb` log lines say which reading each is (with and without the `-wal`).

### Changed
- **Content is identified by sniffing, never by name** — `scripts/data/sniff.py`, shared by every
  report. "Encrypted" requires high entropy **and** AES block alignment; everything else is named
  for what it is (LZC lens bundles, protobuf, WEBVTT, ZIP, JSON, HTML, fonts, bplists). Across the
  corpus ~97% of the files the cache_controller report had padlocked were not encrypted at all. The
  header reports how many entries hold encrypted bytes, how many the Memories report can open and
  how many have no key. `_hash_stream` keeps 8 KB of head instead of 16 bytes. An ISO base media
  file is typed by its **brand**, so audio recordings and HEIC photographs are no longer presented
  as `.mp4`.
- **`caching-media` pack matching went from hours to minutes**: `pack_matches` probes the first 32
  bytes instead of AES-decrypting the whole item — every acceptance test in `decrypt_pack` reads
  within the first 24 plaintext bytes, and CBC decrypts a prefix independently, so the verdict is
  identical.
- **Poster extraction runs in a killable subprocess** (`scripts/data/poster_worker.py`). About one
  cached video in six blocks the decoder for good; abandoned threads piled up, corrupted the fd-2
  redirect, and the run exited 120. A per-file bound and a per-pass budget; two case extractions
  went from 1244 s and 4933 s to 128 s and 300 s. FFmpeg's decoder output is silenced by redirecting
  fd 2 — the `OPENCV_FFMPEG_*` variables reach only the demuxer.
- A message with **several cached files is one row** (`_merge_rows`), folding rows sharing a
  conversation + server message id; two *parts* of one message stay separate.
- "**open**" is a chip that opens in its own tab in the Conversations, Contacts and Memories
  indexes; expanded attachment previews are capped at 150 px with a link to the full file, and media
  tells the virtual table to re-measure when it loads (`SCV.remeasure`).
- A link with several targets opens the other report filtered to all of them (`#find=`) rather than
  reaching only the first; the two cache reports share one column order; cached video gets a poster
  frame; `caching-media` rows show the copy the Memories report decrypted.
- The keychain and `primary.docobjects` are read once per run, so a finding is not logged twice.

### Fixed
- **`mergeCache` crashed on an empty frame and the legacy report lost every chat attachment.**
  `pd.DataFrame({"CACHE_KEY": []})` types the column float64 and `Series.apply` short-circuits on an
  empty Series; `empty_cache_frame()` / `normalize_cache_keys()` now build and type empty frames
  explicitly. [pandas3_python314_compat.md](docs/pandas3_python314_compat.md) §2b.
- **`getCache` only ever read one account's cache claims.** On a two-account device the detected
  account can own none of the relevant claims. It reads every account, logs the per-account
  breakdown, and a claim belonging to a different account stays visible in the cache_controller
  report under the `USER_ID` that made it rather than becoming a synthetic message in this account's
  report.
- **New-schema My Eyes Only memories were never decrypted, even with the key in hand.**
  `ZGALLERYSNAP.ZENCRYPTION` for `IS_ENCRYPTED=1` holds the key **wrapped** (48-byte `KEY`, 32-byte
  `IV`) and `unwrap_meo_key` was wired only into the old-schema path. `persistedkey` is **per
  account** and the keychain reader kept only the first item of each name; it now hands each profile
  its own. The keychain secret is no longer written to a `temp_meo.plist` in the working directory.
  Memories with no usable key say "key still wrapped, no persistedkey for this account"; the docs
  and banner that claimed new-schema MEO needed no keychain are corrected.
- **Every SCContent cache folder is extracted and searched.** `extract_zip` matched the literal
  `com.snap.file_manager_3_SCContent_`, leaving other generations (e.g. `_4_`, no user-id suffix) in
  the archive; it takes glob patterns now (`wanted()`), `ParseSnapchat_iOS` resolves all folders
  (`sccontent_folders`), and `mergeCache`'s multi-folder branch actually resolves a file.
- **The Library/Caches report counted files it deliberately does not decode as failures**:
  `caching-media` packs the Memories report decrypts in full were "not recovered". Rows owned by
  another report and app assets are counted apart (`↗ decoded in the Memories report`), and packs
  link to their Memory through `media_by_pack.json`, which the Memories report writes from its
  decrypt-and-match result.
- **The friends-source fall-through no longer reads as a failure**: `Can not find key 'share_user'`
  was ERROR on every newer-iOS run although it only means the friends list is elsewhere. INFO now,
  each fall-through says where it is moving, and one `Contacts source:` line names the answering
  source — WARNING for the two `primary.docobjects` fallbacks. Both parsers also wrote a
  `test.plist` scratch file into the run folder; they parse in memory now.
- **"Could not copy the CSS folder" fired on every re-run** of an existing run folder — `copytree`
  raised `FileExistsError` under a bare `except`. One shared `report_ui.copy_css()` with
  `dirs_exist_ok=True`.
- The legacy Memories report's log line counted Memories *listed*, not files written; it reports
  both.
- "?" popovers are no longer clipped by the sticky header or a virtual row (`position:fixed`, nudged
  inside the window). Double-clicking a Snap ID in the Memories report no longer selects the label's
  last word with it.
- Ids rendered without the `.0` pandas leaves on an integer column that also holds NULLs.

### Documentation
- `docs/report_conversations.md`, `docs/report_contacts.md`, `docs/sqlite_wal_handling.md`; the
  CLAUDE.md rules on referring to test data (no per-device tallies in committed text).

## [1.4.2] — 2026-07-29 *(untagged; shipped in 1.5.0)*

### Added
- **Big index tables are virtualized** (`scripts/report_ui.py`). Rows live in `data/index.js`, each
  cache_controller row's detail in a `data/detail-<n>.js` chunk fetched on expand, and only the rows
  in the viewport are in the DOM. A synthetic 101 200-row index opens in 0.70 s (search 0.18 s, sort
  0.04 s) where the one-big-table layout was unusable; search, sort and filters still cover the full
  index. Data files load with `<script src>` because `file://` pages may not `fetch` siblings; a red
  banner appears if `data/` was left behind. [report_ui.md](docs/report_ui.md).
- **Paging** on both index tables (100–5000 rows or all, default 500), applied after filtering and
  sorting so search and "select all shown" cover the whole index; following an `#anchor` turns to
  the page holding the target.
- **Row selection** in both indexes and on the Memory detail pages — a checkbox per row, "Selected
  only", select/unselect all matching, a count. Measured in Chrome: `localStorage` on a `file://`
  page is partitioned per browsing context, so no browser storage can be shared by two pages of one
  run or outlive the tab. Selections therefore live in **`Reports/selection.js`**, written empty at
  generation and loaded by every page; **💾 Save selections** downloads a new one to drop next to the
  reports and **Load…** reads one back; `localStorage` is a same-tab safety net only.
- **My Eyes Only indicator** on the Memories index: a red MEO badge, matched by a search for "meo",
  plus an any / only / exclude filter.
- **Offline map tile server** (`scripts/offline_maps.py`). A tile server URL — server root or
  `{z}/{x}/{y}` template — set in the GUI with a **Test** button. Each geolocated Memory's detail
  page gets a stitched 3×3-tile map with a marker and a link opening the server at the same place.
  Nothing is fetched when it is empty; tiles are cached and Memories at the same coordinates share
  one image; the map is labelled a derived artifact with a "?" recording server, zoom and tile
  count; a server that stops answering degrades to a warning.
- **Bundle children are resolved and viewable.** For `TYPE=3` the `<CACHE_KEY>` file is only the
  `CHILDREN` descriptor; children stored as `<CACHE_KEY>_<child name>` are hashed, typed and
  published, which is what makes a chat video (an `.mp4` plus a `.webp` overlay) viewable.
- **Encrypted cache files link to their decrypted copy**: the Memories report writes
  `media_by_cache_key.json` and the cache_controller report shows the plaintext copy, labelled
  derived, with both files' hashes and the Memory's `ZSNAPID`.
- A preview thumbnail / ▶ play button per cache_controller row, replacing the "on disk" glyphs.

### Fixed
- **Anchors work on repeat clicks into an already-open tab.** A click whose URL equals the tab's
  current URL fires no event; `NAV_JS` consumes the fragment (`location.hash='_'`, since
  `history.replaceState` throws on `file://`) so the next click is a real `hashchange`. Targets
  scroll clear of the sticky toolbar and are highlighted, in every report.
- Clicking a link or "?" inside a cache_controller row no longer toggles the row.
- **Chat → cache_controller links from saved media resolved**: `SCPersistentMedia` attachments are
  not named after a cache key and produced a dead link; they are matched to the claim carrying the
  same `<conversation>:<message>:<part>` triple (`mapPersistentMediaToCacheKeys`). **A message with
  two attachments links back from both** (`cache_links.json` v2 with `by_message`), and **each
  attachment links to its own entry** — the mapping runs against the raw `CACHE_FILE_CLAIM` rows and
  `_rank_claim` prefers an exact type match, verified byte for byte against the linked entry or one
  of its bundle children.
- **Extensionless media no longer breaks the browser**: every viewable file is published under a
  name ending in its real extension, as a **hard link**, in both the cache_controller `files/` and
  the Communications `cacheFiles/` folders.

## [1.4.1] — 2026-07-22

### Added
- **The Memories report is a lightweight index plus one detail page per group**
  (`Memories_report.html` + `pages/<key>.html`), so it stays usable with many Memories. The index is
  sortable and filterable — thumbnail, kind, user, ids, cache tokens, hashes, created, geolocation.
  Second-level grouping (`assign_groups`, union-find) merges Memories by `ZMEDIAID` **and** by
  identical non-zero media MD5 across users. `memory_pages.json` lets the cache_controller report
  link to both the index row and the detail page.
- The cache_controller report computes and shows the **actual cached file's** MD5/SHA-256
  (`materialize_ondisk`); recognizable plaintext media ≤ 30 MB is copied to
  `files/<CACHE_KEY>.<ext>` and **viewable even when unlinked** (👁). Detail panels use the real DB
  column names; an **Expand all** / Collapse all button.
- Memories index: `ZMEDIAID`/`ZSNAPID`/`ZENTRYID` in one labelled column; OSM **and** Google links;
  the Detail column shows each group's snap count.

### Fixed
- Field 8 of `CONTENT_RETRIEVAL_METADATA` was mislabelled "Content SHA-256": it is usually a CDN
  media token, sometimes a 64-hex hash, sometimes the `CACHE_KEY` — and even the hash is a
  **source-side** hash that need not match the cached bytes. Labelled by real column name, value
  type and a "?" caveat.
- Hashing streams instead of reading whole files; published files are hard-linked instead of copied;
  non-name `CHILDREN` descriptors are handled.
- The GUI shows the real version in frozen builds.

## [1.4.0] — 2026-07-22

### Added
- **cache_controller.db report** (`scripts/cache_controller_report.py`, `Reports/CacheController/`):
  one row per physical cache file (`CACHE_KEY`), aggregating its `CACHE_FILE_CLAIM` rows and joining
  `CACHE_FILE_METADATA` (size/type/shard, the `CHILDREN` protobuf of byte-range parts or bundle
  children, `CONTENT_RETRIEVAL_METADATA` = CDN URL + content ref). Each entry is resolved to its
  on-disk file(s) under `com.snap.file_manager_*_SCContent_*` (whole / parts / bundle children).
  Sortable, filterable, per-row detail. `CACHE_FILE_SAMPLED_TOMBSTONE` deletion records fold into
  their entry; `CACHE_KEY_VIRTUALIZATION` is listed with its semantics marked unconfirmed. Columns
  are read dynamically, since the schema varies by app version.
  [report_cache_controller.md](docs/report_cache_controller.md).
- **Two-way cross-report links.** cache → Memory via `snap-*-<UUID>` / `g-media-<UUID>` →
  `#mem-<snapid>`; Memory → cache per media file via `#ck-<cache_key>`; cache → chat via the
  `cache_links.json` manifest the Communications report writes, with a `#cf-<cache_key>` anchor and
  a back-link. Two dormant-but-validated **fallbacks** after the primary snap-UUID match:
  `SHA-256(memory URL token)[:16] == CACHE_KEY`, and a `ZMEDIAID` inside an `EXTERNAL_KEY`. Each
  link records *how* it was made. [cross_report_linking.md](docs/cross_report_linking.md).
- **A round "?" on every media file and cross-report link** whose popover explains, in plain
  language, how the association was derived — matched identifier, primary vs fallback, how the bytes
  were located and decrypted (`_info`, `how`, `memory_basis`).
- **Cross-scope on-disk copies are flagged** in both the cache_controller and Memories reports: a
  physical copy in a different account's `SCContent_<userId>` folder than the account(s) claiming
  it. A ⚠ chip, detail paths grouped by scope, a "cross-scope only" filter and a "?"; the claim's
  `USER_ID` stays authoritative (`_scope_user`, `_resolve_on_disk`, `_cross_scope_basis`).
- `index.html` carries a "Sources" block with the extraction ZIP and keychain paths.

### Fixed
- `index.html` was written only after the "press any key" pause; the pause moved to
  `Snapchat_Auto.main`, after `write_index`.
- `path_to_image_html` no longer depends on `main()` setting the `platform` global first
  (module-level default).

### Documentation
- `docs/cross_report_linking.md`, `docs/report_cache_controller.md`, `docs/report_memories.md`,
  `docs/report_communications.md`, `docs/forensics_tool_guidelines.md`.
- Verified that `cache_controller.db` holds metadata for every file in the `SCContent` folders.

## [1.3.3] — 2026-07-22 *(untagged)*

### Added
- The GUI remembers the ZIP, keychain and report-directory paths between runs
  (`~/.snapchat_auto_gui.json`), with "Use previous" buttons and browse dialogs opening in each
  other's folder. A note under the timezone says daylight saving is applied. The logger reads the
  version from `pyproject.toml` (`get_version()`), falling back to package metadata.
- An **AS IS** disclaimer in the README and as a dialog with a "Don't display again" checkbox: this
  fork has not been tested against many Snapchat versions and is meant to be used alongside other
  tools and proper validation.
- The report directory is mandatory; the log and `ExtractedData/` live inside it; the outputs are
  `Report_<stamp>/Communications/`, `Report_<stamp>/Memories/` and
  `Report_<stamp>/LocalMemories_legacy/`, with a `Report_<stamp>/index.html` linking them.
- Memories report: Memories sharing the same cache media and AES key/IV are grouped, with media,
  encryption and timestamps shown once per group (`_render_group`); a Google Maps link beside the
  OSM link; "Dimensions" falls back to `ZWIDTH×ZHEIGHT` for mp4; source paths shown as their device
  path (anchored on `/private/var/mobile/` or `/Application/`); timestamps as two NULL-filled
  `ZGALLERYSNAP` / `ZGALLERYENTRY` tables with a fixed column set; extra fields surfaced
  (`SNAP_OTHER_LABELS` / `ENTRY_OTHER_LABELS`).
- The cache lookup treats the `CACHE_KEY` as the *start* of the on-disk filename: media split into
  `<cache_key>_<start>-<end>` parts is discovered, concatenated in offset order, decrypted and
  hash-verified (`index_sccontent` / `_resolve_sccontent`).

### Fixed
- The cache-merge crash in the Memories report.

## [1.3.1] — 2026-07-19

### Fixed
- `.pack` files were no longer decoded and associated to Memories: `extract_zip.py` never extracted
  `Library/Caches/caching-media`. It now resolves Snapchat's app and app-group containers from the
  container metadata plists and extracts within them.
- SQLCipher integration for gallery decryption updated (bundled `sqlcipher3.exe`).
