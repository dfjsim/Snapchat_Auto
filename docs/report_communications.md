# Communications report (legacy)

> **Superseded.** The [Conversations](report_conversations.md) and [Contacts](report_contacts.md)
> reports replace this one and are built from the same parsed rows. This report is still produced,
> under a `_legacy` name, until those two have been validated on more extractions — see the removal
> plan in `TODO.md`. **The parsing described below is not legacy**: it is what produces the message
> frame both this report and the Conversations report render.

Built in `scripts/ParseSnapchat_iOS.py` (`main` → `getHtml`) →
`Reports/Communications_legacy/Communications_legacy_report.html`, with recovered attachments in
`Reports/Communications_legacy/cacheFiles/`.

Parses Snapchat chats, contacts and groups and renders one table per conversation, inlining any
cached attachment (image / video / sticker) that can be linked to a message.

## Sources
| Data | Source |
|---|---|
| Messages | `arroyo.db` → `conversation_message` (`getChats`, `getCacheArroyo`) |
| Friends / groups / display names | `group.snapchat.picaboo.plist`, `app_group_plist_storage`, `primary.docobjects` |
| Cache index | `cache_controller.db` → `CACHE_FILE_CLAIM` (`getCache`) |
| Content index | `contentmanagerV3_<userHash>/contentManagerDb.db` → `CONTENT_OBJECT_TABLE` (`getContentmanager`) |
| Cached bytes | `Documents/com.snap.file_manager_*_SCContent_*/<CACHE_KEY>` |

## How a message is linked to its cached file

The join key between a message and the cache is the **`EXTERNAL_KEY`**, which resolves to a
`CACHE_KEY` (the on-disk filename). `getCacheArroyo` fills each message's content with its
`CACHE_KEY` by three routes:

1. **`local_message_references`** — an `NSKeyedArchiver` plist embedded in the row; its `MEDIA_ID`
   (a UUID) is matched against `CACHE_FILE_CLAIM.EXTERNAL_KEY`, yielding the `CACHE_KEY`.
2. **`content_type == 5`** — a protobuf whose `4→4→4→1→2` field is matched *inside* an
   `EXTERNAL_KEY`.
3. **`content_type == 3`** — a share: its `4→4→5→5→1` field (the shared Story's id) is matched
   inside an `EXTERNAL_KEY`. Only a share of that kind can match this way — see
   [the body kinds](#what-a-row-is-its-content-type-and-its-body).

### When a value has several candidates

These joins used to be loops of every message against every friend and every claim, each overwriting
the last: the value a message ended up with came from whichever candidate matched **last**, which the
database's row order decided, and on a phone with hundreds of thousands of messages the loops took
hours. They are lookups now (`_id_key`, `_names_by_id`, dictionaries keyed by user id, by
`(conversation, message)` and by `EXTERNAL_KEY`), and where there is more than one candidate the
choice is stated — and logged with a count when it happens:

* **A sender's name** (`fixSenders`): a user id the friends data gives under several names is shown
  under **all** of them, separated by « / », in the order the data holds them. The friends list wins
  over the Snapchatters the app merely cached, and a sender is matched on the user id only.
* **A share's or a sticker's file** (`getCacheArroyo`, `_share_claim_order`): of the claims whose key
  contains the item's id, the media comes **before its thumbnail**, then the order cache_controller.db
  lists them in. A local message reference already took the *first* claim of its exact key, and still
  does.
* **A message whose content is a claim's `EXTERNAL_KEY`** (`mergeCacheChats`): when several claims share
  the key — two accounts on one phone — **this account's** claim is taken, then the first in
  cache_controller.db's order.
* A message arroyo lists twice (its `-wal` and its checkpointed reading) still takes the later row, as
  before: the two are versions of one message, not two candidates.

On the four test devices none of these cases occurs, and the reports are byte-identical to what the
loops produced.

**Which id a sticker is matched by** (`_sticker_key_text`). A Sticker message (`content_type` 5) names
its sticker in one of two places. A sticker from a pack is named by the text at `4.4.4.1.2`. A sticker
carried as a creative tool item (body `4.4.14`) is named by the bytes at `4.4.14.2.6`, and the
`customSticker…` claim on its cached file holds those bytes **in base64** in its `EXTERNAL_KEY` — so the
join looks for the base64 text. The join used to read `4.4.4.1.2` only, which such a message does not
have, so its file was never attached to it. A sticker message with neither field is not matched.

`getCache` reads claims with `MEDIA_CONTEXT_TYPE IN (2, 3, 19)` (chat-media contexts) for the
logged-in `USER_ID`; `mergeCache` merges in the `contentManagerDb` rows and **copies each matched
`CACHE_KEY` file into `cacheFiles/`**. `path_to_image_html` then renders it (video/image/sticker)
by file type.

**A message sent with several photos or videos** has one `local_message_references` record per item
(`arroyo_content.media_references`): an 8-byte little-endian length, then a keyed archive whose
`MEDIA_ID` names the item. The join used to read the first only; every item's file is now attached
(`getCacheArroyo` adds a row per further file, which the reports fold into the message).

**Chat media kept in pieces** (`scripts/chat_media.py`, both platforms). `mergeCache` copies a claim's
file only when a *whole* file named after its `CACHE_KEY` is media. A chat video is regularly a
**bundle** — the file named after the key is a small descriptor, the video and its overlay are child
files `<CACHE_KEY>_<child>` — and media can also be stored as byte-range shards. Before the join,
`materialize_chat_media` rebuilds those under their `CACHE_KEY` in a folder the join searches first:
shards concatenated in offset order, a bundle's largest media child, and a file that is not plaintext
decrypted with a key / IV pair its message carries. Only bytes that are media by their magic bytes are
written. Without it a message whose only file was a bundle showed *Media (no cached file)* — unless a
saved copy named after its conversation, message and part stood in for it. The attachment's "?"
(`chat_cache_key`) says how the file was put together.

## Attachment files and their names
Two kinds of file end up in `cacheFiles/`:

* **SCContent copies**, named after their `CACHE_KEY` — no extension;
* **`SCPersistentMedia` copies** ("media saved in chat"), named
  `<type>_<conversation>_<message>_<part>_<n>.<ext>`.

Because a browser handles an extensionless `file://` link inconsistently (Chrome downloads it,
Firefox may show it as text, `<video>` refuses it), `namedWithExtension` gives every rendered
attachment a name ending in its real extension — as a **hard link** beside the original (same
bytes, no copy), leaving the original `CACHE_KEY`-named file in place. Files that already carry a
media extension are left alone.

## Two-way link with the cache_controller report
Each rendered attachment:

* gets an `id="cf-<attachment filename>"` anchor (so the cache_controller report can jump to it), and
* shows a 🗄 `cclink` back to `../CacheController/CacheController_report.html#ck-<CACHE_KEY>`, where
  the key comes from `cacheControllerKey`: the filename itself for SCContent copies, and — for
  `SCPersistentMedia` copies, whose name is *not* a cache key — the `CACHE_KEY` of the claim
  carrying the same `<conversation>:<message>:<part>` triple (`mapPersistentMediaToCacheKeys`).
  Before this, saved-media attachments produced a dead `#ck-<filename>` link.

  A message can carry several claims on that triple (`1:` full media, `thumbnail~1:`, `content~1:`),
  so the mapping picks the one matching the file's own type, and runs against **all** claims rather
  than the `mergeCache`-filtered set — otherwise a saved video's full-media claim is missing (its
  `CACHE_KEY` file is a bundle descriptor, not media) and both the thumbnail and the video would
  point at the thumbnail's entry. See
  [cross_report_linking.md](cross_report_linking.md#the-chat-report--cache_controller).

Before the message contents are turned into HTML, `main` writes
`Reports/Communications_legacy/cache_links.json` (version 2) with a `by_key` **and** a `by_message`
index; the cache_controller report uses the second to link back **all** of a message's cache entries
(full media, thumbnail, raw content claim), which is what a message with two attachments needs. It
only reads this manifest when the Conversations report did not write its own (version 3, which
carries the target page as well as the anchor). Format and rules:
[cross_report_linking.md](cross_report_linking.md).

The report also loads the shared `NAV_JS`, so a `#cf-…` link from another report scrolls the
attachment into view, highlights it, and keeps working when the same link is clicked again into the
already-open tab. See [report_ui.md](report_ui.md).

## Reading the text a person actually typed

`proto_to_msg` does not read a message's text field: it walks the whole protobuf and concatenates
**every** string it finds (`protobuf_wire.strings`). Without a schema a length-delimited value is read as
text when it is printable UTF-8 (line breaks and tabs allowed), searched as a nested message when it
parses as one to its last byte (protobuf.dev, *Encoding*), and skipped otherwise — raw ids and packed
numbers are not text. That is what lets the cache join recognise a media id, so
`message_content` still holds it — but it also glues the encryption key, IV, lens name, sticker name
and any typed text into one value, so a reply to a Snap reached the report as `…` buried inside
`<key>=<iv>==<uuid>…`. `getChats` therefore fills `message_text` from the field that holds the text
(`arroyo_content.message_text`):

| Field | Holds |
|---|---|
| `4.4.2.1` | the body of a text message (`content_type` 1) |
| `4.4.7.11.1` | the text of a **reply to a Snap or Story** — see below |
| `4.4.19.1.1` | a Tiny Snap's text |
| `4.4.24.2.1.1` | each part of a bot's response, joined with line breaks |

**A reply is not a caption.** `4.4.7` is a reply: `4.4.7.3` is the Snap being replied to and
`4.4.7.11` the reply (or `.12` / `.15` / `.17` when the reply is media, a voice note or a Snap). The
media such a row carries is therefore the Snap replied to — typically the *other* person's Story —
not something the replier sent, and the text is what they wrote back to it. The report used to call
it a caption on the sender's media, which states the opposite of who made the picture.

Every text message was found to carry `4.4.2.1`, and no text is produced that the concatenated value
did not already contain. Text found anywhere else in these protobufs is not the message — lens and
sticker names, colour codes, advertisement copy, and the overlay text drawn onto a snap.

`protoField` reads those fields **straight off the wire format** rather than through
`blackboxprotobuf`. Without a schema, a decoder has to guess whether a length-delimited field is a
nested message or a string, and it guesses wrong on exactly the values that matter: an ordinary
sentence whose UTF-8 bytes are themselves valid protobuf decodes as a submessage, its letters
reinterpreted as field numbers, and the text becomes unreachable. A field number and a length are
unambiguous; only the caller decides what the bytes mean.

## What a row is: its content type and its body

`scripts/data/arroyo_content.py` reads what a `conversation_message` row *is* from two places:

* **`content_type`.** The same value is stored inside `message_content` at `4.2` (the field is left
  out when it is 0), and the column and the field agree on every row of every test extraction.
  `arroyo_content.CONTENT_TYPES` names every value — 0 Snap, 1 Text, 2 Media, 3 Shared content,
  4 Voice note, 5 Sticker, 6 App event, 8 Location, 9 Saved to camera roll, 10 Screenshot, 11 Screen
  recording, 12 / 13 Missed video / audio call, 14 Group invite link changed, … 37 Poll — and says
  which are media and which are app events. The row detail shows the raw value with its name.
* **The body, `4.4`.** It holds exactly one field, and *which* one is the kind of message: `2` text,
  `3` media sent in the chat, `5` something shared, `6` voice note, `7` reply to a Snap or Story,
  `8` app event, `11` Snap, `19` Tiny Snap, `24` bot response, `25` notification item, `26` poll.

`getChats` writes `arroyo_content.describe` into **`Message Body`**: nothing for plain text, media
and Snaps, and otherwise one line saying what the row carries — the app event, "Shared a Story",
"Shared a map pin at <latitude>, <longitude> “<title>”", "Reply to a Snap or Story — the media is the
Snap replied to, not something the replier sent", "Voice note", "Poll: …". A body or an event it does
not know is named by its field number ("App event 4.4.8.<n> (not described)"), never guessed at.
The people a description names are **user ids**, and `Message Body` keeps them so: a display name is
whatever this device's user typed and a username can change, while the id never does. The
Conversations report shows each id by name (from the Contacts data) and lists every person with
their full id; the legacy report, whose content for an app event is this description, gets
"name (id)" from `fixSenders`.

Shares (`4.4.5.<n>`): `5` a Story (`.1` its id — the one the cache join above matches), `14` a public
profile Snap, `16` a Spotlight Snap, `18` a map pin (`.1` / `.2` latitude / longitude as doubles,
`.6` its title), `24` a saved Story (`.1` who posted it; `.2` its Snap, with the media keys, whose
own `.2.18.1` names the poster as well), `35` a sports game, `37` an event.

A saved Story is described as "Shared a saved Story posted by <name>". The two poster ids are written
independently — one by the share, one inside the Snap — and agree on the test extractions; if they
ever disagree, the description gives both rather than picking one. The share's media joins through
the ordinary `<conversation>:<message>` claims, so the story-id route above is not needed for it.

## App events

Some rows are events the app recorded in the conversation rather than anything a user sent: their
body is **`4.4.8`**, and the field under it names the event. `getChats` used to report the ones that
hold no string as `ERROR - Something went wrong when parsing this message`, which states something
untrue — the protobuf decodes cleanly — and the ones that do (a group's old and new name) as those
strings glued together. Every one is now described from its own fields; the description is the
row's content (and the legacy report's *Message Content*), and a row with no cached file is labelled
**System message**. User ids are the `{1: <16 bytes>}` messages below.

| `4.4.8.<n>` | Event | Fields |
|---|---|---|
| `1` | screenshot / screen recording | `1` who, `2` 0 screenshot · 1 recording, `3` of 0 the chat · 1 the friendship profile · 2 the group profile · 3 the call, `4` = 2: by someone no longer in the group |
| `2` | call | `1` 0 started · 1 ended · 2 left · 3 joined · 4 missed, `2` 0 audio · 1 video, `3` user, `4` duration in **milliseconds**, `5` participants, `6` call id |
| `3` | group membership | `1` each change: {`1` who, `2` 0 added · 1 created the group · 2 left, `3` how they joined (1 invite sticker, 2 invite link, 3 community, 4 public group), `4` 1 left · 2 removed}; `3` by whom |
| `4` | group renamed | `1` by whom, `2` old name, `3` new name |
| `5` | message deleted | `1` by whom, `2` 1 a chat message · 2 a Snap |
| `6` | group created | `1` by whom, `2` participants, `3` group name |
| `7` | media saved to the **camera roll** | `1` by whom, `2` the `server_message_id` whose media was saved, `3` each {`1` 1 photo · 2 video, `2` count} |
| `8` | when this chat's messages delete | `1` by whom, `2.1` {`3` unviewed / `4` viewed retention in seconds, `5` messages are kept} |
| `10` | group invite link | `1` by whom, `2` 1 created · 2 deleted |
| `13` | live location sharing ended | `1` by whom, `2` 1 session expired · 2 stopped |
| `21` | streak | `1` 1 started · 2 ended · 3 restored, `2` days |
| `22` | My AI's welcome message | — |
| `25` | countdown | `2` 1 created · 2 deleted · 3 updated · 4 started, `3` name |
| `28` | friend place alert | `1` 1 home safe · 2 custom place, `2` state, `3` name |

and, described by name only: `9` a game closed, `11` a group invite prompt, `12` an app update,
`14` a contact joined, `15` / `16` Family Center invite accepted / left, `17` a Snapchat-for-web
notice, `18` a reply added to a Story, `19` chat wallpaper changed or remixed, `20` a Snapchat+ gift,
`23` group live location, `24` how Snaps can be viewed after opening, `26` a Snap remixed, `27` a
sticker made from a photo, `29` a brand collaboration intro, `30` / `31` welcome messages, `32` a
location request accepted, `33` an event update.

The content types an event arrives with: a missed call is 12 (video) or 13 (audio) and every other
call event 6; a save to the camera roll is 9, a screenshot 10, a screen recording 11, an invite link
14; events with no type of their own — deleted messages, group changes, retention changes, streaks,
the My AI welcome — are 6.

A save to the camera roll (`4.4.8.7`) used to be reported as "Saved the media of message N **in this
chat**", and only when the target row's `is_saved` agreed; the event is a save to the device's
camera roll, which `is_saved` does not record.

## Messages whose media is no longer cached

`mergeCacheChats` left-joins the cache onto the message frame, so a message that no
`CACHE_FILE_CLAIM` resolves to comes out of the join with a null `TYPE`. Such a row used to be
**dropped from the frame entirely**, which is why both chat reports listed fewer messages than
`conversation_message` holds. On an account whose older media has aged out of the cache this can be
a large share of the conversation, and every one of those messages is one another tool displays.

They are now kept and labelled by `uncachedLabel`:

| Content Type | Meaning |
|---|---|
| `Media (no cached file)` | a media `content_type` (0 Snap, 2 media, 4 voice note, 26 Tiny Snap) — the message carries media, and no surviving cache file backs it |
| `System message` | an app event (6, 9–14, 19, 20, 22, 27, 30–32, 36) — not media at all, so "no cached file" would be the wrong thing to say about it (see [App events](#app-events)) |
| its name | any other `content_type` in `arroyo_content.CONTENT_TYPES` — e.g. `Poll`, `Location`, `Bot response` |
| `Unrecognised (content_type <n>)` | a value not in the table — the number is reported rather than guessed at |

`content_type` 3 and 5 never reach this: `mergeCacheChats` labels them **Shared content** and
**Sticker** whether or not a claim joined (`Shared content` was `Video (Unknown Source)`; a share can
be a photo, or a map pin with no media at all).

The label deliberately does **not** say the media "expired". A missing file may equally have been
evicted from the cache, never cached on this device, or not carried by the extraction, and those
are different statements — the report may only say the file is not here. Everything else about the
message (sender, both timestamps, both ids, direction) comes from `arroyo.db` and is unaffected.

`getChats` also keeps arroyo's numeric `content_type` in its own column (`Content Type (arroyo)`,
shown in the Conversations report's row detail), so the label never loses the value it came from.

## Notes / caveats
* The report renders with pandas `DataFrame.to_html`; per-conversation tables come from
  `groupby('Client Conversation ID')`. Every conversation is in **one document**, which is why it
  is being replaced: the same failure mode the index tables were virtualized for.
* `main` takes a copy of the message frame (`msg_df`) just before this loop turns each
  `Message Content` into HTML, and hands that copy to the Conversations report — so the two
  reports show the same rows and only one of them is responsible for the rendering.
* `getChats` also selects `client_message_id` / `local_message_id` and `server_conversation_id`
  when the app version's `conversation_message` has them, so both this report and the Conversations
  report can show a message's device-side id as well as the server's.
* The HTML file is written as **cp1252**, so anything injected into it (including the shared JS)
  must stay ASCII; emoji are written as HTML entities.
