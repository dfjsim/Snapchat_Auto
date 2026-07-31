# Conversations report

`scripts/conversations_report.py` → `Reports/Conversations/Conversations_report.html` plus one
**detail page per conversation**.

Replaces the chat half of the [legacy Communications report](report_communications.md), which is
still produced (as `Communications_legacy/Communications_legacy_report.html`) until this one has
been validated on more extractions. Both render the **same parsed rows** — see
[Where the data comes from](#where-the-data-comes-from).

```
Reports/Conversations/
  Conversations_report.html       the index: one row per conversation (~6 KB)
  assets/ui.css, assets/ui.js     the shared UI, loaded by the index and every detail page
  data/index.js                   the index rows
  pages/<key>.html                one detail page per conversation (~7 KB each)
  pages/data/<key>/index.js       that conversation's message rows
  pages/data/<key>/detail-<n>.js  per-message detail, fetched only when a row is expanded
  media/<name>.<ext>              the chat attachments (hard links, see below)
  conversation_pages.json         conversation id -> detail page
  cache_links.json                the manifest the cache_controller report links back with
```

## The index

One row per conversation: type (private / group / unknown), title, participants, message count,
attachment count, first and last message, conversation id, and a link to the detail page. Search
covers the title, every participant, the participant user ids, the sender names and the
conversation id; the type / with-messages / with-attachments filters, the pager and the row
selection work over the whole set (`docs/report_ui.md`).

**Conversations with 0 messages are listed.** A conversation id that the friends or groups list
names but that `arroyo.db` holds no message for is a finding — the messages may have been deleted
or simply not captured — so it appears with a 0 rather than being dropped, and the index says how
many such rows there are.

## A conversation's detail page

A metadata block (conversation id, type, how the conversation was named, participants and their
user ids, counts, first/last message, content-type breakdown, per-sender message counts) followed
by the **message table**, which is the same virtual table as the index:

| | |
|---|---|
| Created | in the examiner's timezone; the raw UTC value is in the expanded row |
| Direction | **Sent** when the sender is the logged-in account of the extraction, else Received |
| Sender | `sender_id`, replaced with the contact's username by the parser where it could, with a **device owner** badge on the account the extraction came from |
| Type | the content type(s) of the message (see the "?" on that column) |
| Content | the message text, and a thumbnail / play button per attached file |
| Msg ID | `server_message_id` + `.` + the part index (e.g. `12.0`), with the device's own `client_message_id` under it |
| Read | the read timestamp, empty when the message was never read |

Expanding a row shows the full text, **each** attachment full size (image or `<video>`) with its
name, detected type, size, **MD5 and SHA-256**, where it was published from and the link to its
`cache_controller` entry, and every raw row value — including **both** message identifiers, both
conversation identifiers, and both the stored UTC timestamp and the converted one, so the
conversion can be checked. The sender links to that contact's record.

### One row per message, not per parsed row
The message/cache join emits **one row per cache claim**, so a message that carries two files (a
video and its thumbnail, say) arrives as two otherwise identical rows — which reads as two messages
sent in the same second by the same person. Rows sharing a conversation and a server message id are
therefore folded into one message holding a list of attachments (`_merge_rows`): the row shows every
file side by side, the Type column shows the combined types, and the expanded detail lists each file
separately with its own hashes and cache link. Two *parts* of one message (`12.0`, `12.1`) stay
separate — they are separate sends — and rows with no server message id are never folded, since
nothing distinguishes them from each other.

Search / sort / filter (direction, content type, with-attachment) and paging all run over the whole
conversation, not just the page on screen — which is how a conversation with tens of thousands of
messages stays usable.

### Why the message table is virtualized too
The index being small is not enough: a single active conversation can hold tens of thousands of
messages, and putting them all in one document is the same failure the reports were already fixed
for once ([report_ui.md](report_ui.md#why-the-index-tables-are-virtualized)). So a detail page is
also a shell: its rows live in `pages/data/<key>/index.js`, only the visible ones are in the DOM,
and the per-message detail is fetched a chunk at a time. Keep the `pages/data/` and `assets/`
folders next to the HTML when copying the report.

Measured on a synthetic **20 000-message** conversation (Chrome, `file://`):

| | |
|---|---|
| the conversation's document | 9 KB |
| ready (rows counted, table interactive) | 0.23 s |
| rows in the DOM | 7 |
| search across all 20 000 messages | 3 ms |
| sort by sender | 8 ms |
| row data (`pages/data/<key>/index.js`) | 12 MB |
| per-message detail | 80 chunks of ~260 KB; **one** is loaded when a row is expanded |

The per-message detail deliberately carries **no “?” popovers**: their text is identical for every
message, and it is written once per message into those chunks. The columns they explain carry them
in the table header instead, where they are written once per page.

`assets/ui.css` and `assets/ui.js` exist for the same reason: the ~20 KB of shared UI code would
otherwise be inlined into every conversation page. A `file://` page may load a sibling
subresource (it may not `fetch` one), so both the index and the detail pages `<script src>` them.

## Where the data comes from

This report does **not** re-parse the chat database. `ParseSnapchat_iOS.main` hands it the message
frame it has already assembled — `arroyo.db` → `conversation_message`, joined to
`cache_controller.db` → `CACHE_FILE_CLAIM` and to the `SCPersistentMedia` copies (that join is
documented in [report_communications.md](report_communications.md)) — taken **before** the frame's
content is turned into the legacy report's HTML. What this report adds is the structure, the
examiner's timezone, the attachment hashes, and a stated provenance for every derived value.

Conversation identity comes from three sources, and each conversation records (in its "?" icons)
which one applied:

| Value | Preferred source | Fallbacks |
|---|---|---|
| Type | `arroyo.db` `user_conversation.conversation_type` (0 private / 1 group) | the groups list → the friends list → "Unknown" |
| Title | `GROUP_NAME` from the groups list | the contact's display name / username → the first non-owner sender → "(unidentified conversation)" |
| Participants | `user_conversation` user ids, resolved to contacts | `GROUP_PARTICIPANTS_USER_NAMES` → the single contact → the distinct senders of the messages |

`user_conversation` is absent on newer Snapchat schemas; the report degrades to the friends/groups
lists and says so in the "?".

### Participants
Each participant is shown as **display name (username)** and links to that contact's row in the
[Contacts report](report_contacts.md), where all of their identifiers are — display name, current
username, previous username and the permanent user id. The conversation header also lists the
participants' **user IDs**, since that is the only identifier that survives a rename. The device
owner is badged wherever they appear: in the participant list, next to their user id, and on every
message they sent.

### Both identifiers, everywhere
A message has two identities — the id the device gave it (`client_message_id`, present as soon as
it is composed) and the one the server assigned (`server_message_id`, absent while it is still
sending) — and so does a conversation (`client_conversation_id` / `server_conversation_id`). Both
are shown, so a row can be found again in `arroyo.db` either way. The optional columns are selected
from `conversation_message` only when that app version's schema has them (see `getChats`).

## Attachments

Each attachment is published into `media/` as a **hard link** to the file the parser copied out of
the extraction (no bytes duplicated; a real copy only if the filesystem refuses to link), under a
name ending in its **detected** extension — cache files are named after their `CACHE_KEY` with no
extension, which browsers handle inconsistently. Its MD5/SHA-256 are computed from the extracted
bytes so the displayed file can be corroborated.

`mov` / `m4v` / `webm` / `gif` are recognised as well as the `mp4` / `jpg` / `png` / `webp` the
legacy report handles; anything else is still listed with its detected type and a link, rather than
being hidden.

**Duplicate rows.** `mergeCacheChats` produces one row per cache claim of a message, so a message
with three claims arrives as three rows. For the two content types that only ever *are* their
attachment — "Video (Unknown Source)" (`content_type` 3) and "Sticker" (`content_type` 5) — a row
whose claim has no renderable file is one of those duplicates and is dropped, exactly as the legacy
report drops it. Every other row is kept even when its file is missing: a message whose media was
not recovered is a finding, not noise.

**Known limitation (inherited).** When a message has an attachment, the parser replaces its content
with that attachment, so any text the same message carried is not shown. The "?" on the Content
column says so.

## Cross-report links

* Each attachment links to `../../CacheController/CacheController_report.html#ck-<CACHE_KEY>`, with
  a "?" explaining how the key was derived (the filename *is* the key for `SCContent` copies;
  `SCPersistentMedia` copies are matched through the claim carrying the same
  conversation / message / part).
* `cache_links.json` (version 3) is what the cache_controller report links **back** with. It has
  the same two indexes as the legacy manifest plus an `href` per record, because with one page per
  conversation the target is no longer a single document. Only messages that **have a recovered
  attachment** are listed (as in the legacy manifest): a message with no cached file is not
  something a cache entry can point at, and indexing every message would make this file grow with
  the whole chat history. Format and matching rules:
  [cross_report_linking.md](cross_report_linking.md).
* `conversation_pages.json` (conversation id → detail page) is the equivalent of the Memories
  report's `memory_pages.json`, for any other report or tool that needs to resolve a conversation
  to its page. (The Contacts report does not read it — `main` returns the same mapping, with the
  per-conversation counts, straight to it.)

## Selections

Conversations can be ticked on the index and messages on a detail page (kinds `conv` and `msg`),
sharing the run's `Reports/selection.js` with every other report — which is the groundwork for the
"export selected conversations" item in `TODO.md`. Why the selection lives in a file the examiner
saves: [report_ui.md](report_ui.md#selecting-rows--and-where-a-file-report-can-keep-them).
