# Reading every SQLite database twice: with and without its `-wal`

`scripts/data/sqlite_open.py` is how this project opens **every** SQLite database. It reads each one
twice and reports rows that only one of the two readings contains.

## Why

A write-ahead log (`<db>-wal`) holds committed pages that have not yet been checkpointed into the
main database file. Which file a row lives in is evidence:

| Reading | What it is | What a normal SQLite client does |
|---|---|---|
| database **+ `-wal`** | the app's current state | this, and only this |
| database **alone** | the state as of the last checkpoint | never |

Rows can exist in only one of the two:

* **`wal-only`** — written recently, not yet checkpointed. Part of the app's current state; a tool
  that ignores the `-wal` misses it entirely.
* **`main-only`** — the `-wal` **updated or deleted** it after the last checkpoint. This is
  recoverable **prior state**: a deleted message, a deleted Memory, a superseded metadata row.
  It is *not* current, and the reports say so explicitly.

Before this, the project only ever produced the merged view, so the whole `main-only` class was
invisible.

## What it finds in practice

Measured on the iOS 16 test device (`cache_controller.db` 29 MB + 2.7 MB `-wal`; `scdb-27.sqlite3`
35 MB + 1.0 MB `-wal`; `arroyo.db`):

| Report | Recovered by the second reading |
|---|---|
| cache_controller | **111** `CACHE_FILE_METADATA` rows rewritten since the checkpoint (both versions kept) + 1 `wal-only` claim + 8 `wal-only` tombstones |
| Memories | **17** Memory rows deleted since the checkpoint (11 of them with recoverable media), 13 rewritten |
| Conversations | **2** messages deleted since the checkpoint |

A worked example of why the superseded version matters — one cache file's metadata row:

```
column                     before the last checkpoint    current (-wal applied)
FILE_SIZE_BYTES            4172                          0
TOTAL_DISK_USED_BYTES      4172                          0
DELETED_TIMESTAMP_MILLIS   0                             1678609256808
```

Read normally, that entry is just "0 bytes, deleted" and the original size is gone. Both readings
together show the file *was* 4172 bytes and when it was deleted.

## How it works

```python
from scripts.data import sqlite_open

views = sqlite_open.open_views(db_path, workdir)
rows, markers = sqlite_open.read_table(views, "CACHE_FILE_CLAIM")
views.close()
```

* **Both readings come from copies** staged under `workdir` (or a temp dir that `close()` removes):
  `withwal/` gets the database plus its `-wal`/`-shm`, `nowal/` gets the database file alone. Both
  are opened `mode=ro`.
* **The evidence is only ever read.** Copying rather than opening in place is the point: a
  read-only open can still create a `-shm` beside the original and can checkpoint it. Verified on a
  full pipeline run — all 60 database/`-wal`/`-shm` files were byte-identical afterwards.
* **Rows are matched on the declared primary key**, falling back to the whole row when a table has
  none (and always, for `read_sql`/`query_both`, since an arbitrary query has no key). A row whose
  key is in both readings but whose *values* differ is returned **twice**: the current version
  marked `wal-only` and the checkpointed one `main-only`.
* **No `-wal` means no second copy.** The readings are identical by construction, every row is
  `main+wal`, and the source block says so.

### The API

| Function | Use |
|---|---|
| `open_views(db, workdir)` | both connections + `info`; use `views.merged` where a live connection is needed |
| `read_table(views, table)` | `(rows, markers)` for a whole table, keyed on its primary key |
| `read_sql(db, query)` | a DataFrame with a `_wal` column — the drop-in for `pd.read_sql_query` |
| `query_both(views, query)` | `(rows, markers)` as plain tuples, for non-pandas callers |
| `table_columns(db, table)` | column names, for queries built from a version-varying schema |
| `describe(info)` | the one-line `-wal` summary a report puts in its source block |

## How it appears in the reports

Every report states, in its source block, each database's `-wal`/`-shm` size and whether the two
readings differ — including when there is no `-wal`, since that means the readings are identical.

| Report | Marking |
|---|---|
| cache_controller | `-wal only` / `no -wal only` badge on the row, a `(read from)` column in the claim and tombstone tables, a **CACHE_FILE_METADATA — superseded version** diff table, and a `-wal` filter |
| Memories | `DELETED` / `EDITED` badge on the index row and a `-wal` filter |
| Conversations | a `⚠ deleted since checkpoint` badge on the message |

**Counts in report headers stay the merged figure** — the app's current state. `main-only` rows are
*additions*, always badged, so an existing number never silently changes meaning. Verified: on the
same extraction the cache_controller totals (17 856 / 5 148 / 8 749 / 2 422) and the Contacts
totals are unchanged from before this work; Memories went 6 148 → 6 165 and Conversations 164 → 166
purely through badged additions.

Every badge carries a **“?”** whose text (`sqlite_open.MARKER_HELP`) explains, in plain language,
what the marker means and warns that `main-only` rows must not be reported as current — see
`docs/forensics_tool_guidelines.md`.

## Coverage

| Database | Where |
|---|---|
| `cache_controller.db` | `cache_controller_report`, `memories_media_report` (`index_cache_controller`, `all_cache_keys`), `ParseSnapchat_iOS` |
| `arroyo.db` | `ParseSnapchat_iOS` (7 sites), `conversations_report` |
| `*primary.docobjects` | `ParseSnapchat_iOS` (4 sites), `contacts_report` |
| `scdb-27.sqlite3` | `memories_media_report.load_memories` |
| `gallery.encrypteddb` (SQLCipher) | `decrypt_gallery_db(..., with_wal=)` + `gallery_rows` |
| `contentManagerDb.db` | `ParseSnapchat_iOS` (3 sites) |
| Android `core.db` / `main.db` / `arroyo.db` | `getCacheAndroid` |

**One exception:** `scripts/DecryptLocalMemories_iOS.py` still opens databases `mode=rwc` and shells
out to `sqlite3 .dump`. It backs the **legacy** Memories report that is slated for removal, so it
was deliberately left alone rather than reworked.

## The gap the two readings do not cover: superseded frames

Reading twice gives the two states **SQLite itself** can produce. Neither reaches a page image that
a *later frame in the same log* has already replaced: applying a WAL keeps only the newest frame
per page, so a row written and then overwritten mid-log is invisible to both readings. That is not
a corner case — it is where a record deleted between two checkpoints ends up.

Measured on one test device: `scdb-27.sqlite3-wal` holds 624 frames, **507 of them superseded**.
Two Memories exist in no reading at all, yet `cache_controller.db` still claims their media and the
files are still on disk — they showed as unrecoverable encrypted blobs.

`sqlite_open.wal_page_images()` walks every frame in file order, and
`superseded_wal_pages()` returns just the stale ones. `memories_media_report.carve_deleted_memories`
uses them: it carves `SCMemoriesSnapEncryption` archives out of those page images and tests each
carved key against the file `cache_controller.db` claims for a snap with no Memory row. Both
Memories were recovered that way — a JPEG plus its thumbnail, and an MP4 plus its thumbnail.

Two rules make this safe to report:

* **Carved data is proved, never inferred.** A key is accepted only when it decrypts the claimed
  file into bytes with a valid media signature. Proximity in the page is not used, so a wrong key
  cannot produce a row. A cache file that is *already* plaintext is skipped for this purpose —
  it would "decrypt" under any key and prove nothing.
* **It is badged apart.** These rows carry `sqlite_open.CARVED` (`wal-carved`), a distinct
  `CARVED` badge and filter in the Memories report, and a banner on the detail page saying that no
  database row survives, so the empty metadata panels are not read as blank fields on the device.
  Only the identifier, the key and the media are recovered; capture time, dimensions and
  geolocation are gone with the row.

## If you add a database

Use `sqlite_open`, never a bare `sqlite3.connect` on an evidence path, and never a write mode.
Surface the marker on the row — an unmarked `main-only` row presented as current data is worse than
not recovering it at all.
