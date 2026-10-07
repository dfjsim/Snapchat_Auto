# `--trace-ids` — where an identifier occurs in an extraction

```
Snapchat_Auto.exe --trace-ids <run folder> <id> [<id> ...]
Snapchat_Auto.exe --trace-ids <run folder> @ids.txt          # one id per line, '#' starts a comment
```

The reports link artifacts through identifiers they know to look for. When two artifacts plainly belong
together and no report connects them, the open question is whether the device recorded the connection
somewhere nobody reads yet. `--trace-ids` answers it directly: it searches every file of a run's
`ExtractedData/` for each identifier and lists every place it occurs. Implemented by
`scripts/trace_ids.py`. To ask the same of every cache claim at once — which cached files a
chat message holds an id of — see [`--survey-claim-links`](claim_link_survey.md).

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
| `bytes (base64-decoded)`, `hex (base64-decoded)` | an identifier that is base64 — padded with `=`, using `+` or `/`, or mixing upper case, lower case and digits, and decoding cleanly — as the bytes it encodes, and their hex. An app that keeps an id as base64 text in one store can keep it as raw bytes in another. Hex (a UUID, a `CACHE_KEY`, a number) is not read as base64 |
| `base64 without padding`, `base64url` / `base64` | the same base64 identifier without its `=` padding, and in the other base64 alphabet (`-_` for `+/`, or the reverse), where that differs |

Forms shorter than six bytes are not searched; they would match by chance.

Where a shorter form is part of a longer one found at the same place — `ABC` inside `ABC~1`, `QUJD`
inside `QUJD==` — only the longer one is reported; the shorter form is reported where it stands on its
own.

## Where

* **Every file** under the run's `ExtractedData/` is read raw, in overlapping chunks, so a match that
  straddles a chunk boundary is found once. `-shm` files (a database's shared-memory index) are skipped.
* **Every SQLite database** at most 256 MB — and any larger one the raw pass hit — is also read row by
  row, through `scripts/data/sqlite_open.py`: both readings, with the `-wal` applied and without it, so a
  hit is reported as `table.column row N` and marked `main+wal`, `wal-only` or `main-only` exactly as the
  reports mark rows (see [sqlite_wal_handling.md](sqlite_wal_handling.md)). The row pass is what finds a
  value long enough to spill onto overflow pages, which page headers cut apart in the raw file. A table
  without a rowid is numbered in table order, because its primary key is evidence content.
* A row-level hit inside a blob that is a **protobuf message** also names the **field** it lies in, as
  the dotted path of field numbers from the outermost message in (`4.4.14.1`). The path is read from
  the wire alone, the way every schema-less decode in the project reads (`protobuf_wire.field_path`):
  it goes down while the match lies inside one length-delimited value that is not printable text and
  parses as a message to its last byte. It is a location, like the row number, and what a field
  *means* is for the report that reads it. No field is named for a text cell, for a blob that is not a
  message, or for a match that crosses a field boundary.
* A raw hit in a **database file** that no row of either reading accounts for is said to be "in no row
  either reading returns" — a free page, unallocated space or a deleted record. Hit by hit, the file's
  own page structure says which where it can: a hit on a page the **freelist** holds (`free page`),
  between a b-tree page's cell pointers and its first cell (`unallocated space`), or in a released cell
  (`freeblock`) is in no row, even when some other row holds the same identifier — a deleted record
  of the same kind as a live one is exactly that case. The file is read as it stands (the last
  checkpointed state); a page the `-wal` rewrote is read from the log by the merged reading, so what
  sits at such an offset is returned by neither reading.
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
           "reading": "main+wal|wal-only|main-only", "field": "4.4.14.1"},
          {"id": 0, "kind": "wal", "file": "…-wal", "offset": 0, "frame": 0, "page": 0,
           "where": "page image", "superseded": true, "form": "…", "case": "…"},
          {"id": 0, "kind": "sqlite-file|file", "file": "…", "offset": 0, "in_rows": false,
           "where": "free page|unallocated space|freeblock", …}]}
```

`field` is present only on a row-level hit inside a protobuf blob; `where` on a database-file hit
only when it lies outside every live record.

Exit code: **0** when anything was found, **1** when nothing was, **2** for bad arguments.
