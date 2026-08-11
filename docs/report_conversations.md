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

One row per conversation, in this order: **▸**, type (private / group / unknown), title,
**conversation id**, participants, message count, attachment count, first and last message, and a
link to the detail page. The id sits next to the name it belongs to rather than at the far right:
they are two forms of the same answer to "which conversation is this", and an examiner reads them
together.

The row can only name two participants before it overflows, so **▸ expands it** to a table of every
participant with their permanent user id, username and display name, plus both conversation ids
(client and server). Search covers the title, every participant, their user ids, usernames and
display names, the sender names and the conversation id — so a user id pasted into the box finds
the conversations that account is in, including the ones the row cannot name. The type /
with-messages / with-attachments filters, the pager and the row selection work over the whole set
(`docs/report_ui.md`).

**Conversations with 0 messages are listed.** A conversation id that the friends or groups list
names but that `arroyo.db` holds no message for is a finding — the messages may have been deleted
or simply not captured — so it appears with a 0 rather than being dropped, and the index says how
many such rows there are.

That used to stop at the friends/groups lists, which missed the case where **only `arroyo.db`
knows the conversation**: a `conversation` / `feed_entry` row with no message, no friend and no
group behind it was dropped from the report altogether — the one situation in which "not listed"
and "no messages" are indistinguishable to the reader. Those rows are now listed too. This is not a
corner case: every corpus device has such conversations, and they dominate on the iOS 26 schema,
which has dropped `user_conversation` entirely and leaves `feed_entry` as the only record that the
conversation exists.

### First / Last **activity**, not first / last message

The columns are labelled *activity* because two different records feed them, and conflating the two
would be a claim about message content that the second record does not make.

| the conversation has | the columns show | marked |
|---|---|---|
| messages | first and last `conversation_message.creation_timestamp` | — |
| no message | `feed_entry.display_timestamp` … `feed_entry.last_updated_timestamp`, falling back to `conversation.creation_timestamp` | a muted **feed** tag on the cell, with the reason on hover |

`feed_entry` is the conversation's row in the app's own chat list, so its dates are what the app
displays against that conversation — which is where a commercial tool's date range for an empty
conversation comes from. They say the conversation was *active*; they do not say a message existed
at that moment, and they are not evidence of content. The expanded row's **Dates** section names
the field each value came from.

A conversation can legitimately end up with **no** date: one the friends list names but that has
no `conversation` row, no `feed_entry` row and no message leaves nothing to date it with. The cell
says *no date recorded* rather than being blank, so it reads as a fact about the extraction rather
than as a gap in the report.

The Contacts report shows the same two columns, aggregated across every conversation a contact is
in, and renders them through the same `report_ui.activity_cell` so a feed date is marked there too.
The value travels between the reports as **plain text plus a `date_source`**, never as a ready-made
cell: the Contacts report escapes what it is handed, so markup arrives there as visible tag soup.

**`conversation.creation_timestamp` is not the start of the conversation.** It is when *this
device* created its local row. Verified on the corpus: on a device restored from a backup it is the
restore, and the conversation's own messages can pre-date it by years. It is shown in the expanded
row (always, including when messages supply the range, because the two disagreeing is itself a
finding) and never used as the displayed range while any message time exists.

### The `-wal` filter

A message that survives only in the reading of `arroyo.db` **without** its write-ahead log was
deleted by the app after the last checkpoint: recovered prior state, not part of the live
conversation. It has always been badged on the row, but neither table could filter for it. Both can
now — the index by *conversations holding at least one*, the message table by the message itself.

The control is **emitted only when the database actually has such a row**. A filter whose only
possible outcome is an empty table is not a choice, it is a trap; the same rule disables the empty
options in the Memories media filter. No device in the test corpus has a WAL-deleted chat message,
so `tests/test_report_filters.py` is what holds this behaviour in place.

### The time filter, and its scope control

The shared date/time window (see
[report_ui.md](report_ui.md#the-datetime-window-report_uitime_filter-time_js)) is on both tables, but
only the index needs a **scope**, because only there does a row have two kinds of time:

| scope | matches against | |
|---|---|---|
| *either kind of time* (default) | the union | can only ever return more than either alone |
| *message times only* | every message's own creation time | never falls back to a feed date |
| *first / last activity only* | the conversation's own range | which may itself be a feed date |

The distinction is the one the section above is about. A conversation holding no message still has a
first/last activity, taken from the app's chat feed, and that says the conversation was active then —
**not** that a message existed then. So *message times only* must return nothing for such a
conversation rather than answer with its feed date, which is what `scConvTimes` in `_REPORT_JS`
enforces and `tests/test_fold_and_time_js.py` executes. The union is the default because a filter that
under-includes hides evidence, while one that over-includes only shows more.

On a conversation page every row is a message, so the scope question does not arise and the control
appears without it. A message whose creation time could not be recovered is hidden while a window is
set — it cannot be shown to fall inside one.

## A conversation's detail page

A metadata block (conversation id, type, how the conversation was named, participants and their
user ids, counts, first/last message, content-type breakdown, per-sender message counts) followed
by the **message table**, which is the same virtual table as the index:

| | |
|---|---|
| Created | in the examiner's timezone; the raw UTC value is in the expanded row |
| Direction | **Sent** when the sender is the logged-in account of the extraction, else Received |
| Sender | `sender_id`, replaced with the contact's username by the parser where it could, with a **device owner** badge on the account the extraction came from. The **id itself is kept** — see below |
| Type | the content type(s) of the message (see the "?" on that column) |
| Content | the message text, and a thumbnail / play button per attached file (see below) |
| Msg ID | `server_message_id` + `.` + the part index (e.g. `12.0`), with the device's own `client_message_id` under it |
| Read | the read timestamp, empty when the message was never read |

### The sender's name is shown; the sender's id is kept

`ParseSnapchat_iOS.fixSenders` replaces `conversation_message.sender_id` **in place** with the friend's
username or display name, because that is what a reader of the report wants. It now copies the id into
a `Sender User ID` column first, and `build_messages` carries it as `msg["sender_uid"]`. Nothing shows
it — the name is what the column displays — and the legacy Communications report drops it, exactly as
it drops `Message Text`, so that report's table is unchanged.

It exists because a display name is the wrong thing to *match a message on*. A message with no server
message id yet is anchored on its **position** in the conversation (`msg-row7`), which recovering one
more message shifts, so a saved selection re-finds it by conversation + time + sender
([report_partial.md](report_partial.md)). Keyed on the name, that fallback rested on whatever the
device happened to know at extraction time: it differs between two extractions of one phone, and it is
absent for a sender who is not in the friends artifact at all.
[selection_format.md](selection_format.md) had always told an external tool that `sender` is a user
id — which was true of the documentation and not of the index, so a tool doing exactly what it said
matched nothing, silently.

So the key is the **user id and nothing else**, matched case-insensitively. The display name is
deliberately not a second spelling: a key that cannot be trusted is worse than no key, and a message
whose sender id was not recovered simply has no `ts_sender` key. Nothing needs the old spelling — a
selection older than this change is re-ticked, which takes seconds.

Confirmed on all four corpus devices that `sender_id` really is a user id (every message's is a UUID).
No device in the corpus has a message *without* a server message id, so the fallback itself is held by
`tests/test_message_sender_identity.py` rather than by the corpus. It is still worth having: the parser
explicitly labels unsent rows *"Sending Message"* and gives them no server id (`cell()` maps the
`"None"` it writes to `""`), which is code written because someone met the case — and those are exactly
the rows whose anchor is positional and therefore movable.

Expanding a row shows the full text, **each** attachment as a capped preview (150 px tall, with a
link to open it full size) plus its name, detected type, size, **MD5 and SHA-256**, where it was
published from and the link to its `cache_controller` entry, and every raw row value — including **both** message identifiers, both
conversation identifiers, and both the stored UTC timestamp and the converted one, so the
conversion can be checked. The sender links to that contact's record.

### The Content cell: the file always wins the space

The row is a fixed height (that is what makes the table virtualizable), so its content has to fit
into it. When the message text was long enough to fill the cell it pushed the attachment button
past the bottom edge, and `overflow:hidden` sliced the button through the middle — the examiner saw
half a label and no way to tell there was a file.

The cell is a flex column: the file box (`.atts`) does not shrink, the text does. So the text gives
way and the file is always shown whole, whatever the message length. The text is clamped to two
lines for a tidy cut, and the whole message is in the expanded row.

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

**Messages with no cache claim at all.** These reach the report as `Media (no cached file)` (or
`No cached file (content_type <n>)` for a type the parser cannot name) — see
[report_communications.md](report_communications.md#messages-whose-media-is-no-longer-cached) for
how the label is derived and why it does not say "expired". The parser used to drop these rows,
which is what made the reports list fewer messages than `arroyo.db` holds. The row detail shows
arroyo's own numeric value as `content_type (arroyo, raw)` beside the label.

### Text sent with media
The parser replaces a message's content with its attachment, which used to destroy any text the
message also carried. `getChats` now reads the text out of **the field that holds it** and keeps it
in `Message Text`, so a caption sent with a photo appears next to it in the row and in the expanded
detail. See
[report_communications.md](report_communications.md#reading-the-text-a-person-actually-typed) for
the field paths and why the whole-protobuf scan they replace could not be relied on.

`_own_text` is now only a screen for a value that arrived through the old concatenating path — a
cache key, an `EXTERNAL_KEY`, a bare UUID, a media id, or the attachment's own file name. It
deliberately does **not** decide by content type: a media message can carry a caption the sender
typed, so "only show text for `content_type` 1" would drop real evidence. Nothing is hidden either
way: the expanded row always lists the raw value as `message_content (parsed)`, with the raw
`content_type` beside it.

A message whose protobuf `getChats` could not parse is marked **⚠ not parsed** instead of showing
the parser's error string as if it were the message; the expanded row explains it and gives the ids
to check in `arroyo.db`.

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
