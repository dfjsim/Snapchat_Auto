# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`Snapchat_Auto` — a forensics tool that extracts and parses Snapchat data from iOS and
Android device extractions, producing HTML reports of chats, contacts, cached media, and
Memories / My Eyes Only.

- Entry point: `Snapchat_Auto.py` (FreeSimpleGUI front end).
- iOS parsing: `scripts/ParseSnapchat_iOS.py`.
- iOS Memories / MEO decryption: `scripts/DecryptLocalMemories_iOS.py`.
- iOS `cache_controller.db` report: `scripts/cache_controller_report.py` (one row per cached file,
  linked to on-disk cache files and two-way to the Memories / Communications reports).
- Android: `scripts/getCacheAndroid.py`.
- Shared helpers: `scripts/data/` (`ccl_bplist.py`, `keychain.py` UFED keychain decrypter,
  `parse3.py`/`Snapchat_pb2.py` protobuf, bundled `sqlcipher3.exe`).
- Run/build: `uv` project (`pyproject.toml`), Nuitka build via `build_nuitka.cmd`.

## Handling forensic data — read this first

- Extractions contain a real person's private data. **Keep decrypted output and extracted
  artifacts local**; never publish them (no Artifacts, no uploads). Work in the scratchpad,
  not the repo.
- Extraction ZIPs are huge (tens of GB). **Selectively extract** only the files you need
  (see the app-container paths in the docs below) rather than unzipping the whole archive.
- Open SQLite databases read-only and keep their `-wal`/`-shm` siblings alongside so recent
  rows are visible.

## Research notes / findings

- Per-report internals and the cross-report linking scheme:
  [cross_report_linking.md](docs/cross_report_linking.md) (anchors + how every link is derived),
  [report_cache_controller.md](docs/report_cache_controller.md),
  [report_memories.md](docs/report_memories.md),
  [report_communications.md](docs/report_communications.md).
- [Decrypting & linking Snapchat Memories media](docs/snapchat_ios_memories_decryption.md)
  — full method for recovering Memories media (`SCContent` + `caching-media/**/*.pack`) and
  geolocation, and linking each media file to its `scdb-27.sqlite3` Memory. Covers both storage
  schemas (keys in `ZGALLERYSNAP.ZENCRYPTION` vs. in `gallery.encrypteddb`), the
  keychain-required matrix (geolocation and My Eyes Only always need the FFS keychain;
  new-schema regular-memory imagery does not), multi-user handling, and the decrypt-and-match
  pack linker. Verified on two devices. Implemented by `scripts/memories_media_report.py`.
- [pandas 3.x / Python 3.14 compatibility notes](docs/pandas3_python314_compat.md) — the strict
  dtype enforcement (`Invalid value 'X' for dtype '…'`), removed `DataFrame.append()`, and the
  per-cell `df.loc[…] = value` pattern that breaks on the current runtime. Read before adding or
  editing DataFrame cell assignments in the parsing scripts.
