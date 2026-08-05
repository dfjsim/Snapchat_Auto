# Snapchat_Auto

> ### A fork — original work and all credit: [DFIR-HBG](https://github.com/DFIR-HBG) and [stark4n6](https://github.com/stark4n6)
>
> **Snapchat_Auto** was created by **DFIR-HBG** and **stark4n6**. Upstream repository:
> **<https://github.com/DFIR-HBG/Snapchat_Auto>**.
>
> This is a personal fork that adds the iOS Memories media report and some compatibility fixes
> (see [What this fork adds](#what-this-fork-adds)). Everything else — the tool itself, its
> parsing, decryption and reporting — is their work. This fork is redistributed under the
> original **MIT Licence, © 2022 DFIR-HBG** (see [LICENSE](LICENSE)). Please direct stars,
> issues and credit upstream.

> ### ⚠️ Disclaimer — provided AS IS
>
> This fork is provided **AS IS, with no warranty of any kind**. It has **not been thoroughly
> tested across the many versions of the Snapchat app**, and Snapchat's database schemas differ
> between versions, so some artifacts may be parsed incompletely or potentially incorrectly in some cases.
> Use it as an **aid to analysis, not as a sole authority** — always validate findings against
> the original artifacts and corroborate them with other tools before relying on them.
> (The app shows this notice on startup; it can be dismissed with a *"Don't display again"* box.)

> ### Scope
>
> This is an offline artifact-analysis tool for digital forensic examiners. It reads a device
> extraction that the examiner is **lawfully authorised to examine**, and nothing else:
>
> - it never contacts Snapchat's servers or any other network service — the only optional network
>   access is to a map tile server and to a folder of newer builds for update checks, both
>   configured by the examiner themselves and both off by default;
> - it does not circumvent account authentication and cannot access data that is not already on
>   the device;
> - it decrypts locally stored data using keys recovered **from that same device** (its app
>   container and, where the extraction includes one, its keychain).
>
> The `docs/` notes describe where Snapchat stores data on the device and the formats it uses, so
> that an examiner can verify or reproduce by hand anything the tool reports. Recovered output is
> the device owner's private data: keep it within the case.
>
> This project is not a guide to reverse engineering the Snapchat application. It does not document
> or promote decompiling, disassembling, or instrumenting the app itself; it is limited to
> analyzing artifacts already present in a lawfully obtained extraction and reporting them with
> clear provenance and caveats.

Automatic extraction and parsing of Snapchat for iOS and Android

Install required libraries with 'pip install -r requirements.txt'

To be able to decrypt the iOS memories database you will need to have sqlite3.exe in PATH.
Download https://www.sqlite.org/download.html or https://developer.android.com/studio/releases/platform-tools and add that folder to PATH.

1. Copy Snapchat_Auto_vX.py and scripts folder together (Or Snapchat_Auto_vX.exe)
2. Run Snapchat_Auto_vX.py or Snapchat_Auto_vX.exe
3. Choose iOS/Android
4. Point to your extraction ZIP-file
5. Point to your keychain file (For decryption of cached memories/MEO on iOS)
6. Profit

### Running it without the GUI

Passing `--zip` runs the whole thing headlessly — no window, and no "press any key" at the end —
so it can be driven from a batch/PowerShell/shell script or run over several extractions in a loop:

```
Snapchat_Auto.exe --zip <extraction.zip> [--keychain <file>] [--workdir <dir>]
                  [--os ios|android] [--tz local|utc|<IANA>|<+/-HH:MM>]
                  [--padding both|strip|keep] [--tile-server <url>] [--run-name <name>]
```

Everything for the run still lands in one folder under `--workdir`. `--run-name` pins that
folder's name instead of using a timestamp, so a repeated run overwrites the same place and two
runs stay directly comparable. Exit code is 0 on success, 1 if the run failed, 2 for a bad
argument. `--help` lists everything; `--diag-keychain <file>` still checks a keychain on its own.

### Update checks (optional, off by default)

If you keep new builds of this tool in a folder — a shared drive, say — point the GUI's *"Folder
with newer builds"* field at it and each start will offer a newer build found there. Leave it empty
(the default) and nothing is checked and no folder is touched; there is no update server and
nothing is sent anywhere. Headless runs never check. See
[docs/auto_update.md](docs/auto_update.md) for the filename convention builds have to follow.

## Memories media report (iOS)

The iOS run also produces a **Memories** report (`<output>/Memories/Memories.html`) that links
each Snapchat Memory to *all* of its recovered media — images **and videos** — from both the
`SCContent` files (`Documents/com.snap.file_manager_*`) and the `caching-media/**/*.pack` cache.
For every media file it shows the full **on-device source path** and its **MD5 + SHA-256**; for
every Memory it shows the per-snap **AES key/IV**, all **timestamps** from `scdb-27` — both
snap-level (`ZGALLERYSNAP`) and entry/album-level (`ZGALLERYENTRY`) — the CDN **URLs**,
dimensions, duration, camera, IDs, and **geolocation** (coordinates + reverse-geocoded place,
with a map link). When a device has **multiple Snapchat users**, the report is split into
per-user sections with a navigation bar linking to each.

- **Hashes:** SCContent media is AES-CBC with PKCS#7 padding, and different tools emit the media
  with or without that padding. By default the report lists **both** MD5/SHA-256 pairs (padding
  stripped — matching e.g. Cellebrite PA 10.10 — *and* padding kept) so you can match either
  tool. Choose *Without padding only* / *With padding only* in the GUI (or `--padding
  strip|keep`) to show just one. The media file saved to disk is the byte-exact (stripped) media
  unless you pick "with padding only".
- **Timestamps:** shown in **local time** by default (DST-aware). Choose **UTC** or a specific
  timezone in the GUI (or `--tz utc|America/Toronto|-04:00`); IANA zone names apply daylight
  saving correctly per date.
- **Video posters:** when a video Memory has a recovered `.mp4` but no cached still image, a
  poster frame is extracted from the video for the thumbnail. It is clearly labelled *poster
  (generated)* / source *generated* — a derived artifact, not original device data.
- **Incomplete media is flagged.** The device caches only the byte ranges it actually streamed, so
  recovered media is often *part* of the original — a video that plays for a few seconds and stops,
  or breaks up part way through. Those files carry a red **⚠ incomplete — partially cached** badge
  stating exactly what is missing (missing tail, gaps between byte ranges, or missing pack chunks),
  the index shows a **PART** chip with an *incomplete only* filter, and the header counts them. The
  bytes are still genuine and correctly decrypted — they are just not the whole media, which is
  something to state before drawing anything from what such a file does or does not show.

- Works for both key storage schemas and for multiple Snapchat users on one device.
- On newer Snapchat, **regular-memory** imagery decrypts **without** a keychain — those keys live
  in `scdb`. A full-filesystem keychain is required for **geolocation** (`egocipher`), for older
  extractions where the keys live in `gallery.encrypteddb` (`egocipher`), and for **My Eyes Only**
  on *either* schema (`persistedkey`): a MEO memory never stores a usable key beside itself, it
  stores one wrapped in that account's master key. `persistedkey` is per account, so on a phone
  signed into two accounts each needs its own. Memories whose key stays wrapped are reported as
  such rather than silently appearing to have no media.

Run it standalone on an already-unzipped extraction:

```
python -m scripts.memories_media_report <extraction_root_or_app_container> [keychain.plist] [output_dir]
```

Check a keychain by itself, without running an extraction (prints the format, the item counts and
which keys were recovered; exit code 0 only when `egocipher` was found):

```
Snapchat_Auto.exe --diag-keychain <keychain file>
python -m scripts.DecryptLocalMemories_iOS --diag-keychain <keychain file>   # from source
```

See [docs/snapchat_ios_memories_decryption.md](docs/snapchat_ios_memories_decryption.md) for
how the decryption and linking work, and exactly when the keychain is required.

## What this fork adds

Relative to [upstream](https://github.com/DFIR-HBG/Snapchat_Auto):

- The **Memories media report** described above (`scripts/memories_media_report.py`), which links
  each Memory to all of its recovered media, including the `caching-media/**/*.pack` cache, and
  adds geolocation, hashes and source paths.
- A **Conversations report** (`scripts/conversations_report.py`) — an index of every conversation
  (type, participants, message and attachment counts, first/last message) plus **one detail page
  per conversation** holding its full message table: sender, direction, both the report-timezone
  and the raw UTC timestamps, the content, and each attachment with its MD5/SHA-256 and a link to
  its `cache_controller` entry. Both tables are searchable, sortable and paged, and neither the
  index nor a detail page grows with the data (see
  [docs/report_conversations.md](docs/report_conversations.md)).
- A **Contacts report** (`scripts/contacts_report.py`) — one table of every contact, naming the
  artifact the contact list came from (and warning when that source includes non-friends), linked
  to each contact's conversation.
- The original single-page chats/contacts/groups report is kept as
  `Communications_legacy/Communications_legacy_report.html` until the two reports above have been
  validated on more extractions.
- A **`cache_controller.db` report** (`scripts/cache_controller_report.py`) — a sortable/filterable
  index of every file Snapchat cached on the device (one row per `CACHE_KEY`), joining the claim,
  metadata, children and deletion tables, resolving each entry to its on-disk cache file(s), and
  cross-linking two-way to the Memories and Conversations reports. Run standalone with
  `python -m scripts.cache_controller_report <extraction_root_or_app_container> [output_dir]`.
- **Compatibility fixes** for pandas 3.x / Python 3.14 and for newer Snapchat iOS schemas — see
  [docs/pandas3_python314_compat.md](docs/pandas3_python314_compat.md).
- `uv` project setup (`pyproject.toml`) and a Nuitka build script.

## Credits and licence

- **Original authors:** [DFIR-HBG](https://github.com/DFIR-HBG) and
  [stark4n6](https://github.com/stark4n6) — <https://github.com/DFIR-HBG/Snapchat_Auto>.
- **Licence:** MIT, © 2022 DFIR-HBG. The original [LICENSE](LICENSE) is retained unmodified and
  covers this fork, including all modifications made here.
- Fork maintained by [dfjs1m](https://github.com/dfjs1m). Bugs in the original tool
  should be reported upstream; only fork-specific issues belong here.
- **Development note:** the fork-specific features and fixes listed under
  [What this fork adds](#what-this-fork-adds) — including the Memories media report, the
  artifact-analysis notes in `docs/`, and the compatibility fixes — were developed with the
  assistance of [Claude Code](https://claude.com/claude-code) (Anthropic). The findings were
  verified against real device extractions.
