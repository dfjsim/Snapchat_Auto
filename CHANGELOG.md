# Changelog

All notable changes to Snapchat_Auto, newest first. Versions are the `[project].version` of
`pyproject.toml` at the time; a version marked *untagged* was never released on its own and shipped
inside the next one. Entries name the module or function that carries a change where that helps a
reader find it; the format findings behind them live in [docs/](docs/). Open work is in
[TODO.md](TODO.md).

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
