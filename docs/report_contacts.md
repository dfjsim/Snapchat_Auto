# Contacts report

`scripts/contacts_report.py` → `Reports/Contacts/Contacts_report.html` (+ `data/index.js`).

One table, one row per contact:

| Column | |
|---|---|
| ▸ | expands the row: every conversation this contact is in, and their identifiers |
| Display name | as recovered, emoji included |
| Username | with a **device owner** badge on the account the extraction came from |
| Legacy username | the username this contact used *before* changing it, when the device recorded one |
| User ID | the permanent UUID, badged again for the device owner |
| Conversations | the first conversation, `+N` for the rest, plus the conversation id |
| Msgs | messages across **all** of them |
| First / Last message | earliest and latest across all of them, in the report's timezone |

It is the shared virtual table (search, per-column sort, paging, row selection —
[report_ui.md](report_ui.md)), so it stays instant on a device that knows thousands of
Snapchatters. This is also where the Conversations report's participant chips and sender links
land, because this row is where a contact's identifiers all appear together.

## A contact is in more than one conversation

The friends artifact records **one** `CONVERSATION_ID` against a contact — their private
conversation with this device. That is not the only conversation they take part in: every group chat
they are a member of is another one, and a report that shows the first was hiding the rest.

`contact_conversations` therefore **inverts** the Conversations report's participant lists (which
travel in `conversation_index` as plain values for this purpose): a contact belongs to every
conversation whose participant list carries their user id. The expanded row lists them all with
their conversation ids, message counts and first/last times, and each says which of the two made
the association:

| Listed because | Meaning |
|---|---|
| from the friends list | the `CONVERSATION_ID` the friends artifact records against this contact |
| participant list carries this user ID | `arroyo.db user_conversation` (or the groups list) carries it |

### Matched on the user id, and nothing else

Only the permanent user id is compared. A display name is set locally by this device's user and two
accounts can share one; a username can be changed and the old one taken by somebody else. Matching
on either would put a conversation on a person's row on the strength of a name — a false
attribution, and the worst kind, because it is indistinguishable from a true one.

The cost is accepted deliberately: a contact whose user id was never recovered is listed **only**
with the conversation the friends artifact names, even if a participant list mentions their
username. An incomplete answer is recoverable by an examiner; a wrong one is not.

The **In several** filter selects contacts with more than one.

The device owner is listed by the same rule as anyone else — membership as the artifacts record it.
No conversation is attributed to them merely because it is on their device: that would be the tool
asserting a fact no artifact states.

## Three or four identifiers, and what each is worth

A Snapchat contact is named in up to four ways, and they are **not** equally reliable — each column
carries a "?" saying so:

| Identifier | Set by | Changes? |
|---|---|---|
| Display name | this device's user (or Snapchat's display metadata) | freely, and only locally — two devices can call the same account different things |
| Username | the contact | occasionally; Snapchat allows a rename |
| Mutable username | stored by the app beside the username | not established — equal to the username on every row of every tested device; shown as stored, flagged when it differs |
| Legacy username | the contact, previously | it *is* the record of a rename |
| User ID | Snapchat, permanently (a UUID) | never — the only identifier safe to correlate on |

The username fields come from **`primary.docobjects`**: `snapchatter` (the `userId`, and the `p`
blob that also carries the names) joined on `rowid` to `index_snapchatterusername` (current),
`index_snapchattermutableUsername` and `index_snapchatterlegacyUsername` (previous) — the four
tables share one rowid per Snapchatter. The column names of the index tables vary between app
versions, so `load_identifiers` looks them up (`PRAGMA table_info`) instead of assuming, and a
missing table only means that column stays empty: the header then says no username history was
available. A legacy username equal to the current one is not a rename and is not shown as one. The
expanded row lists all three username fields, each with its source table (`_username_rows`); the
Username column badges a row whose mutable username differs (`mutable_differs`). What "mutable"
adds to "username" is not established, and `MUTABLE_NOTE` says so — it is shown because the app
stores it.

The **Username changed** filter isolates the contacts with a recorded rename, and the header counts
them — an account named differently in an older report or chat log is exactly the kind of thing that
is easy to miss.

## The owner's account values — `Documents/user.plist`

The device owner's expanded row carries what `user.plist` says about the signed-in account, read
by key from that TSAF container (`scripts/data/tsaf.py`, `ParseSnapchat_iOS.getAccount`): the
username, user id and laguna id under its `User` object, and the client-encryption identifier, key
and IV under its `client_encryption` object, all as stored. What the laguna id names is not
established.

The client-encryption values are a **second** record of that kind, and the report says so: the key
that opens this device's encrypted caches is the one in `ClientEncryptionService.plist`, which
carries a different identifier and a different key, and **nothing in any tested extraction is
encrypted with the `user.plist` one** — every block-aligned file on four devices was tested against
it (see [snapchat_ios_cache_media.md](snapchat_ios_cache_media.md) for the method and the result).
`ACCOUNT_NOTE` carries that caveat, so a reader does not take the row as a key to try.

A signed-out account leaves the three identity keys empty — the run log then says what the file
*does* hold instead of "No user found". Keyed read after iLEAPP's *Snapchat - Account* artifact;
see [related_ileapp.md](related_ileapp.md).

## Which artifact the contacts came from — and why it matters

Snapchat keeps the friends list in different places depending on the app version, and
`ParseSnapchat_iOS` tries them in order. **Whichever one answered is named in a banner at the top of
the report**, with a "?" giving the exact table/key it was read from, because it changes what the
table means:

| Source | What the rows are |
|---|---|
| `group.snapchat.picaboo.plist` → `share_user` (NSKeyedArchiver) | the account's **friends list** |
| `app_group_plist_storage` → `snapchatter_repository` | the account's **friends list** |
| `primary.docobjects` → `snapchatters__displaymetadata` | fallback — **MIGHT** contain users who are not friends |
| `primary.docobjects` → `snapchatter` + `index_snapchatterusername` | last resort — **WILL** contain users who are not friends |

The last two are shown as a red warning banner rather than a neutral note. `SOURCE_NOTES` in
`scripts/contacts_report.py` holds the text; `friends_source` is set in `ParseSnapchat_iOS.main`
next to the call that succeeded. The display names those two fallbacks read out of the
`snapchatters__displaymetadata` documents come from a FlatBuffers root-table read
(`scripts/data/flatbuffers_doc.py`, slot 0 self-checked against the row's user id, slot 1 the name);
the fixed byte-offset carve the parser used before is kept only as the fallback for a document that
fails the self-check, and logged when it is what answered.

## The Snapchatters that are *not* contacts

`primary.docobjects` → `snapchatter` holds every Snapchatter record the app has cached — not only
friends. On the old-schema test device the friends list explained a handful of its rows and **every
other row was a Quick Add / "people you may know" suggestion** the app had shown: strangers, named
in a `snapchatters__displaysuggestion` page. A tool that reads that table as the friends list (iLEAPP's
*Snapchat - Friends* artifact does) reports them all as friends.

The report therefore lists them **apart**, below the contacts table, in a collapsed `<details>`
block (`_snapchatters_section`): display name (from the `snapchatter` document, slot 2 behind the
slot-0 self-check), the three username fields, user id, and **why cached** —

| why cached | decided by |
|---|---|
| Quick Add suggestion | the user id appears, as text, in a `snapchatters__displaysuggestion` document — the one meaning that was verified |
| named in `<table>` | another docobjects table's document names the id; the table is named as stored, with no interpretation |
| cached by the app; no docobjects table names it | nothing else in the store mentions the id |

`load_snapchatters` reads both WAL views of the store, scans every other table's `p` blobs for the
ASCII UUID (the documents embed it as text), and excludes the contacts and the owner — whatever
artifact the contacts came from, since that is what makes the rest "not contacts". A row here has
**no anchor, no selection and no place in a partial report**: nothing may mistake it for a contact.
The header counts them; `SNAPCHATTERS_NOTE` explains the trap in place. The run log states the split
(`Contacts: N Snapchatter record(s) in primary.docobjects — M in the contacts list, K Quick Add
suggestion(s), J other`).

**The run log states the same thing** — one `Contacts source: …` line naming the source that
answered, at WARNING level for the two `primary.docobjects` fallbacks and INFO for the two real
friends lists. A source that does not answer is logged as a step, not as an error: on iOS 13.49
`group.snapchat.picaboo.plist` has no `share_user` key at all, which says where that app version
keeps its friends list, not that anything failed. Only the older `user` format — which the script
cannot read — is a warning.

## Message counts

They come from the Conversations report (`conv_index`), matched on the contact's conversation id. A
contact with a conversation id but **0 messages** means `arroyo.db` held no message for that
conversation in this extraction — the same rows the Conversations index lists with 0 messages. A
contact with no conversation id at all shows "—": the friends artifact recorded no
`CONVERSATION_ID` for them.

## Groups

Groups are *not* in this table — they are conversations, and the
[Conversations report](report_conversations.md) shows each one with its name, participants and
messages. (The legacy Communications report put friends and groups in two tables at the bottom of
the chat page.)

## Normalizers shared with the Conversations report

`normalize_contacts()` and `normalize_groups()` live here and are imported by
`conversations_report.py`. They exist because the friends/groups DataFrames have a different shape
per source: `Display name` vs `Display Name`, a `Conversation ID` that is sometimes a one-item
*list*, participants that are a list or a stringified list, `"Unknown"` / `"$null"` placeholders,
and the logged-in user marked by wrapping their name in `<b>…</b>` (which is how the legacy HTML
report bolds them). The normalizers turn all of that into one shape and keep the owner marking as
data (`is_owner`) instead of markup.

`text_html()` also lives here and is used by both reports: the parser re-encodes text with
`encode('cp1252', 'xmlcharrefreplace')`, so an emoji arrives as the literal characters
`&#128512;`. Plain escaping would show the examiner the entity instead of the emoji, so `&` is
escaped everywhere *except* where it already starts a character reference, while `<` and `>` are
always escaped — report content must never become markup.
