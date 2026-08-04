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
- iOS `cache_controller.db` report: `scripts/cache_controller_report.py` (one row per cached file,
  linked to on-disk cache files and two-way to the Memories / Conversations reports). Covers the
  `com.snap.file_manager_*_SCContent_*` folders — i.e. exactly what that database indexes.
- iOS `Library/Caches` report: `scripts/cache_media_report.py` (everything under `Library/Caches`
  that `cache_controller.db` does **not** index: story renders, URL-keyed PINCache stores, saved
  chat media, and the cached documents). Disjoint from the cache_controller report by design.
- Android: `scripts/getCacheAndroid.py`.
- Shared report UI: `scripts/report_ui.py` (virtualized index tables, paging, row selection,
  cross-report anchor navigation, "?" popovers, page chrome) — used by the Conversations, Contacts,
  Memories and cache_controller reports, with its `NAV_JS`/`NAV_CSS` also injected into the legacy
  Communications report.
- Offline maps: `scripts/offline_maps.py` — static map imagery for geolocated Memories, fetched
  **only** from a tile server the examiner configures in the GUI (never the internet by default).
- Shared helpers: `scripts/data/` (`ccl_bplist.py`, `keychain.py` UFED keychain decrypter,
  `parse3.py`/`Snapchat_pb2.py` protobuf, bundled `sqlcipher3.exe`, `poster_worker.py` — video
  thumbnails, in a killable subprocess because one cached video in six hangs the decoder for good —
  and `sniff.py`, the shared magic-byte identifier. Identify content with `sniff.classify`, never by
  name or extension, and only call something "encrypted" when it says so: it requires high entropy
  **and** AES block alignment, because "we cannot display it" is not the same statement as "it is
  encrypted").
- Run/build: `uv` project (`pyproject.toml`), Nuitka build via `build_nuitka.cmd`.
- Headless runs: `Snapchat_Auto.py --zip <file> [--keychain …] [--workdir …] [--run-name …]`
  runs the whole pipeline with no GUI and no pause, which is how the tool is scripted over
  several extractions. `run()` is the shared entry point for both the GUI and the CLI.

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
- **filesystem paths** from an analyst machine or evidence store (`D:\…`, `C:\Temp\…`).

Refer to a device by the properties that make it technically interesting — OS and app version,
storage schema, keychain class, number of accounts — e.g. "the two-account iOS 26 device", "the
backup-class-keychain device". Counts, sizes, byte offsets, timings and format details are fine and
are the point of the write-up. Placeholders such as `<snapId>`, `<userHash>`, `<CACHE_KEY>` belong
in path and format examples.

The corpus itself, and the script that runs it, live outside the repo for the same reason.

## Research notes / findings

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
- [Snapchat iOS `Library/Caches` media & documents](docs/snapchat_ios_cache_media.md) — what is
  cached outside `cache_controller.db`, how `sccache.gallery-stories-snap.data` is decrypted
  (AES-256-CBC, key + fixed IV in `Documents/ClientEncryptionService.plist`, no keychain), and
  why a root-level filename UUID must never be quoted as a snap id. Implemented by
  `scripts/cache_media_report.py`.
- [pandas 3.x / Python 3.14 compatibility notes](docs/pandas3_python314_compat.md) — the strict
  dtype enforcement (`Invalid value 'X' for dtype '…'`), removed `DataFrame.append()`, and the
  per-cell `df.loc[…] = value` pattern that breaks on the current runtime. Read before adding or
  editing DataFrame cell assignments in the parsing scripts.
