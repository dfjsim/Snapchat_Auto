# `--trace-ids` — where an identifier occurs in an extraction

```
Snapchat_Auto.exe --trace-ids <run folder> <id> [<id> ...]
Snapchat_Auto.exe --trace-ids <run folder> @ids.txt          # one id per line, '#' starts a comment
```

The reports link artifacts through identifiers they know to look for. When two artifacts plainly belong
together and no report connects them, the open question is whether the device recorded the connection
somewhere nobody reads yet. `--trace-ids` answers it directly: it searches every file of a run's
`ExtractedData/` for each identifier and lists every place it occurs. Implemented by
`scripts/trace_ids.py`.

It is meant to be run on the machine that holds the case. The output names **locations only** — file,
offset, table, column, row number, the encoding and the kind of cell — and never a cell value or file
content, so what comes back can be discussed without the data. It does name the identifiers that were
searched for, so it stays with the case like the rest of the run folder.

## What is searched for

| form | for |
|---|---|
| `text`, `text-utf16le` | the identifier as given, in any letter case (the case found is reported) |
| `… (without the ~N suffix)` | an identifier ending in `~N` — the shape of some `cache_controller.db` claim keys — is also searched without it; a hit the full form already explains is not repeated |
| `hex` | a dashed UUID without its dashes |
| `bytes` | 32 hex digits (a UUID, a `CACHE_KEY`) as the 16 bytes themselves |
| `bytes-uuid-le` | the same UUID in the GUID byte order Windows and .NET use (first three groups little-endian) |
| `base64`, `base64url` | the 16 bytes in base64, without padding (`base64url` only where it differs) |

Forms shorter than six bytes are not searched; they would match by chance.

## Where

* **Every file** under the run's `ExtractedData/` is read raw, in overlapping chunks, so a match that
  straddles a chunk boundary is found once. `-shm` files (a database's shared-memory index) are skipped.
* **Every SQLite database** at most 256 MB — and any larger one the raw pass hit — is also read row by
  row, through `scripts/data/sqlite_open.py`: both readings, with the `-wal` applied and without it, so a
  hit is reported as `table.column row N` and marked `main+wal`, `wal-only` or `main-only` exactly as the
  reports mark rows (see [sqlite_wal_handling.md](sqlite_wal_handling.md)). The row pass is what finds a
  value long enough to spill onto overflow pages, which page headers cut apart in the raw file. A table
  without a rowid is numbered in table order, because its primary key is evidence content.
* A raw hit in a **database file** that no row of either reading accounts for is said to be "in no row
  either reading returns" — a free page, unallocated space or a deleted record.
* A raw hit in a **`-wal`** is placed in its frame and page, and a frame that a later frame for the same
  page superseded is marked **superseded**: deleted prior state that neither reading returns.

## Output

* the run's log (`SnapchatAuto_<stamp>.log`, written into the run folder): one line per row-level hit
  (up to 40 per identifier) and one line per group of raw hits in a file, with a count and the first
  offsets;
* `trace_ids_<stamp>.json` in the run folder, with every hit:

```json
{"tool": "Snapchat_Auto --trace-ids", "version": "…", "created_utc": "…", "run_folder": "…",
 "files_scanned": 0, "bytes_scanned": 0, "elapsed_s": 0.0,
 "ids":  [{"index": 0, "id": "…", "forms": ["text", "text-utf16le", "hex", "bytes", …]}],
 "hits": [{"id": 0, "kind": "sqlite", "file": "…", "device_path": "…", "table": "…", "column": "…",
           "row": 12, "cell": "text|blob|blob: binary plist|…", "form": "text", "case": "upper",
           "reading": "main+wal|wal-only|main-only"},
          {"id": 0, "kind": "wal", "file": "…-wal", "offset": 0, "frame": 0, "page": 0,
           "where": "page image", "superseded": true, "form": "…", "case": "…"},
          {"id": 0, "kind": "sqlite-file|file", "file": "…", "offset": 0, "in_rows": false, …}]}
```

Exit code: **0** when anything was found, **1** when nothing was, **2** for bad arguments.
