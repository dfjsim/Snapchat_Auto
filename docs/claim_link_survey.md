# `--survey-claim-links` — which cached files could be tied to a chat message

```
Snapchat_Auto.exe --survey-claim-links <run folder>
```

The cache_controller report ties a cached file to a chat message in two ways: the chat join attached
that file to the message, or the claim's `EXTERNAL_KEY` names the conversation and the message
(`<type>:<conversation>:<message>:<part>`). Every other claim is shown with no chat link — and a kind of
claim the join does not know yet looks exactly like one that belongs to no message at all.
[`--trace-ids`](trace_ids.md) answers "is this id recorded anywhere?" for one id; this asks it of every
claim at once, so one run on the case says which link rules are missing. Implemented by
`scripts/claim_link_survey.py`.

Run it on a run folder the full pipeline produced: the reports' own links are read from
`Reports/Conversations/cache_links.json`. Without it every claim counts as untied, and the log says so.

## What it does

* **Claims.** Every row of `CACHE_FILE_CLAIM` in each `cache_controller.db` of `ExtractedData/`, both
  readings (a claim the two readings hold in different versions is counted once). Its `EXTERNAL_KEY` is
  cut into the ids it carries and what lies between them:

  | placeholder | id |
  |---|---|
  | `<uuid>` | a dashed UUID |
  | `<hexN>` | N hex digits (16 or more) |
  | `<b64:N>` | base64 of N bytes (6 or more) — padded or not, either alphabet (as `--trace-ids` reads it) |
  | `<id>` | any other token of 8 or more characters mixing letters and digits |
  | `<num>` | a number of 8 digits or more — looked up |
  | `<n>` | a shorter number — not looked up |
  | the word itself | a word of letters (and `-` `_`) of 10 or more characters, outside a host name — a sticker's name is one |

  A URL is read part by part: the host name's words are never ids, a path is cut at its `/`, and each
  query value is read whole (base64 there may hold `/`), percent-escapes undone.
* **Link status**, computed by the cache_controller report's own `_chat_links_for`, so the two
  cannot disagree: `message: attached file`, `message: named in the key`, `message: id in the key`
  (an id the message names its media by), `Memory-scoped key`, `Memory: its snap id in the key` (a
  full-media claim whose key carries a Memory's `ZSNAPID` — a Memory of the app folder the reports read
  that `cache_controller.db` from, an iOS container or an Android app folder), or `none`.
* **The databases.** Every row of every table of every SQLite database of `ExtractedData/` (at most
  256 MB each — larger ones are listed as skipped), both readings. `arroyo.db` is what a link to a
  message reads; the others say what else holds an id — a Story, a preference, a Memory. For a claim,
  `cache_controller.db` itself is set aside: its ids are there by definition — its own row, its
  siblings' keys, its file's retrieval metadata.
  A hit in `conversation_message` also says whether the row has a `server_message_id` — a message the
  server never numbered (not sent, or still sending) is linked by its `client_message_id`.
  Each value is looked at: a text column, a blob
  whole, every length-delimited value of a blob that is a protobuf message (with its field path —
  `protobuf_wire.values_with_paths`), and the printable strings of any other blob (a binary plist). A
  value is matched whole against the bytes a claim id stands for (and a UUID's little-endian bytes);
  a text is cut into ids exactly as a key is, and each matched by identity — so a UUID in a key and its
  16 bytes in a blob, or base64 in a key and the bytes in a message, are the same id.

* **Library/Caches files.** Every file under a `Library/Caches` folder whose path carries a UUID
  (`filtered-<UUID>.mp4`, `tmp/<UUID>~thumbnail-generation.mp4`, `sccache.*/<UUID>` …). The Library/Caches
  report links such a file by its claim or by byte-identical content; a file it links to nothing may
  still be named somewhere. Its UUIDs are looked up in the same databases, the same way.

## What it reports

Grouped by context (`MEDIA_CONTEXT_TYPE`) and key **shape** — the key with each id replaced by its
placeholder (`customSticker~<b64:13>`, `content~<n>:<uuid>:<n>:<n>:<n>`):

* how many claims have the shape, and how many of each link status;
* how many of them carry an id `arroyo.db` holds (`found_in_arroyo`) and an id any database holds
  (`found_in_any_database`), by status;
* for the untied claims whose key names a conversation and a message
  (`<type>:<conversation>:<message>:<part>`): what `arroyo.db` holds of what the key names, in either
  reading, and whose claim it is (`untied_named_message`) — see [below](#a-key-that-names-a-message);
* for each id position in the shape: every place the same id was found — table, column, protobuf
  field, `content_type` (for `conversation_message`), reading, and how the row holds it (`a whole value,
  as bytes`, `a whole text, as <b64:13>`, `inside a text, as <uuid>`) — with the number of claims;
* **rows per id**: in how many rows each claim's id occurs — `1`, `2-5`, `6-50`, `more than 50`.

And for the Library/Caches files, grouped by **path shape** (the path under `Library/Caches` with its
UUIDs, long hex runs and numbers replaced): how many files, how many have a UUID some database holds,
and for each UUID position the database, table, column, field, reading and rows per id. A shape no
database holds is listed as such — on the test devices that is `filtered-<UUID>.mp4`, whose UUID is
minted when the file is written (see [snapchat_ios_cache_media.md](snapchat_ios_cache_media.md)).

The shapes whose untied claims carry an id `arroyo.db` holds come first: they are what a link rule is
written for. The log then lists the shapes whose untied claims carry an id only another database holds. Rows per id is what tells a rule from a coincidence of meaning: a sticker sent a few times
is in a few messages; a conversation's own id is in every message of the conversation, and ties a file to
the conversation, not to a message.

Output: a summary in the run's log, and `claim_link_survey_<stamp>.json` in the run folder:

```json
{"tool": "Snapchat_Auto --survey-claim-links", "version": "…", "created_utc": "…", "run_folder": "…",
 "elapsed_s": 0.0, "claims": 0, "arroyo_rows": 0,
 "sources": {"cache_controller": ["<device path>"], "arroyo": ["<device path>"],
             "chat_links": "Reports/Conversations/cache_links.json"},
 "cache_files": 0, "database_rows": 0,
 "shapes": [{"context": 2, "shape": "customSticker~<b64:13>", "claims": 3,
             "status": {"none": 3}, "found_in_arroyo": {"none": 2},
             "found_in_any_database": {"none": 2},
             "ids": [{"position": 2, "id": "<b64:13>",
                      "found": [{"claims": 2, "claim_status": "none", "database": "arroyo.db",
                                 "table": "conversation_message", "column": "message_content",
                                 "field": "4.4.14.2.6", "content_type": 5, "reading": "main+wal",
                                 "held_as": "a whole value, as bytes"}],
                      "rows_per_id": {"1": 1, "2-5": 1}}]}],
 "file_shapes": [{"shape": "tmp/<uuid>~thumbnail-generation.mp4", "files": 4, "found_in_databases": 4,
                  "ids": [{"position": 1, "id": "<uuid>",
                           "found": [{"files": 4, "database": "cache_controller.db",
                                      "table": "CACHE_FILE_CLAIM", "column": "EXTERNAL_KEY", "field": "",
                                      "content_type": null, "reading": "main+wal",
                                      "held_as": "inside a text, as <uuid>"}],
                           "rows_per_id": {"2-5": 4}}]}]}
```

No id, key or cell value is written — shapes, counts, field paths and device paths of the databases. A
word of letters only is kept in a shape, because words are what tell one kind of key from another
(`thumbnail`, `customSticker`). An owner username a key carries in the `<USERNAME>~<snapId>` position —
upper case, right before a `~` — is replaced by `<NAME>` (`mask_names`); a UUID or hex id in that
position, as a snap id is written, stays an id. A name in any other position
or spelling would still be carried into a shape, so the output stays with the case like the rest of
the run folder.

Exit code: **0** when the run was surveyed, **1** when it holds no claim or no `arroyo.db`, **2** for
bad arguments.

### A key that names a message

An untied claim whose key names a conversation and a message gets one label, decided in this order:

| `untied_named_message` | what it means for a link rule |
|---|---|
| `names a message arroyo.db holds` | The message is there and nothing ties the file to it: **a rule is missing.** |
| `names a message not found (arroyo.db not read)` | An `arroyo.db` could not be read in full — it has no `conversation_message` table, or a table its messages, conversations or account are read from will not read, in either reading — so nothing is said to be absent: a damaged table is not one that holds nothing. |
| `names a message arroyo.db does not hold, in a conversation it holds` | The conversation is there — a message of it, or a `conversation`, `feed_entry` or `user_conversation` row — but not that message. There is no row to tie the file to; a recovery of deleted records is what could bring one back. |
| `names a conversation arroyo.db does not hold — claimed by arroyo.db's own account` | None of `conversation_message`, `conversation`, `feed_entry` or `user_conversation` holds the conversation, in either reading, and the claim's `USER_ID` is the `required_values` `USERID` of an `arroyo.db`: the account's own chat database holds no message of it and none of those rows. As above, only a recovery of deleted records could give a rule something to tie to. |
| `names a conversation arroyo.db does not hold — claimed by another account` | The claim's `USER_ID` is the account of no `arroyo.db` in the extraction, and every one of them named its account — a second account on the phone, whose chat database is not in the extraction. **No rule is possible from the extraction's `arroyo.db`.** |
| `names a conversation arroyo.db does not hold` | The same absence, with no account to compare: the claim has no `USER_ID`, or it is none of the accounts read while an `arroyo.db` named none (no `required_values` `USERID`) — so the claim may be that database's own. |

A message is held by its `server_message_id`, or — one the server never numbered (not sent, or still
sending) — by its `client_message_id`, so a key naming an unsent message is never called absent. A
conversation is held when one of those four tables has a row of it; other tables that carry a
conversation id are not read for this. Letter case is ignored in every id. The survey reads **every**
`arroyo.db` of `ExtractedData/`, and "does not hold" means none of them does; a run reads one, so on a
phone with more than one account a message the survey finds held may be in an `arroyo.db` the run's
reports were not built from.

The status of such a claim stays `none` whatever its label: the label says why nothing ties it, it does
not tie it.

## Then

A shape worth a rule names the field the id is in. The rule is written from that field — as the chat
join reads a custom sticker's id from `4.4.14.2.6` (see
[report_communications.md](report_communications.md)) — and checked with `--trace-ids` on one of the
ids, which lists the rows themselves.
