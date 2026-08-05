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
| Legacy username | the contact, previously | it *is* the record of a rename |
| User ID | Snapchat, permanently (a UUID) | never — the only identifier safe to correlate on |

The username pair comes from **`primary.docobjects`**: `snapchatter` (the `userId`, and the `p`
blob that also carries the names) joined on `rowid` to `index_snapchatterusername` (current) and
`index_snapchatterlegacyUsername` (previous) — the three tables share one rowid per contact. The
column names of the two index tables vary between app versions, so `load_identifiers` looks them up
(`PRAGMA table_info`) instead of assuming, and a missing table only means that column stays empty:
the header then says no username history was available. A legacy username equal to the current one
is not a rename and is not shown as one.

The **Username changed** filter isolates the contacts with a recorded rename, and the header counts
them — an account named differently in an older report or chat log is exactly the kind of thing that
is easy to miss.

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
next to the call that succeeded.

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
