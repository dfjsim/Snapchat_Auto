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
3. **`content_type == 3`** — a protobuf whose `4→4→5→5→1` field is matched inside an `EXTERNAL_KEY`.

`getCache` reads claims with `MEDIA_CONTEXT_TYPE IN (2, 3, 19)` (chat-media contexts) for the
logged-in `USER_ID`; `mergeCache` merges in the `contentManagerDb` rows and **copies each matched
`CACHE_KEY` file into `cacheFiles/`**. `path_to_image_html` then renders it (video/image/sticker)
by file type.

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
**every** string it finds. That is what lets the cache join recognise a media id, so
`message_content` still holds it — but it also glues the encryption key, IV, lens name, sticker name
and the caption into one value, so a caption reached the report as `…` buried inside
`<key>=<iv>==<uuid>…`. `getChats` therefore fills `message_text` from the field that holds the text:

| Field | Holds |
|---|---|
| `4.4.2.1` | the body of a text message (`content_type` 1) |
| `4.4.7.11.1` | the caption typed on a media message (`content_type` 2) |

Every text message was found to carry `4.4.2.1`, media captions appear at `4.4.7.11.1`, and no text
is produced that the concatenated value did not already contain. Text found anywhere else in these
protobufs is not the message — lens and sticker names, colour codes, advertisement copy, and the
overlay text drawn onto a snap. Recovering captions this way is the point of the change: a caption
sent with a photo used to arrive glued to the encryption key and media id, unreadable as a message.

`protoField` reads those fields **straight off the wire format** rather than through
`blackboxprotobuf`. Without a schema, a decoder has to guess whether a length-delimited field is a
nested message or a string, and it guesses wrong on exactly the values that matter: an ordinary
sentence whose UTF-8 bytes are themselves valid protobuf decodes as a submessage, its letters
reinterpreted as field numbers, and the text becomes unreachable. A field number and a length are
unambiguous; only the caller decides what the bytes mean.

## Messages that carry no text at all

Some rows hold no string anywhere: their content lives in the protobuf's **`4.4.8` branch**, and
they are events the app recorded in the conversation rather than anything a user sent. `getChats`
used to report these as `ERROR - Something went wrong when parsing this message`, which states
something untrue — the protobuf decodes cleanly. They are now labelled **System message**
(`content_type` 6, 9, 12 and 13) and described where the event is understood:

| Event field | Meaning |
|---|---|
| `4.4.8.7` | media saved in the chat — `1.1` is the user who saved it, `2` the `server_message_id` whose media it was |
| `4.4.8.2`, `.5`, `.6`, `.8`, `.22` | also occur, not yet identified; those rows are labelled but not described |

`savedMediaEventText` only calls a `4.4.8.7` event a *save* when the message it points at
independently agrees — `conversation_message.is_saved = 1` on that row. Without that, it reports the
event as a reference to that message and nothing more: one decoded protobuf field is a lead, not a
finding. Another tool renders the same row as "You saved a photo from …".

## Messages whose media is no longer cached

`mergeCacheChats` left-joins the cache onto the message frame, so a message that no
`CACHE_FILE_CLAIM` resolves to comes out of the join with a null `TYPE`. Such a row used to be
**dropped from the frame entirely**, which is why both chat reports listed fewer messages than
`conversation_message` holds. On an account whose older media has aged out of the cache this can be
a large share of the conversation, and every one of those messages is one another tool displays.

They are now kept and labelled by `uncachedLabel`:

| Content Type | Meaning |
|---|---|
| `Media (no cached file)` | `content_type` is one of `MEDIA_CONTENT_TYPES` (0, 2) — the message's `message_content` protobuf carries the 32-byte key / 16-byte IV pair a media message needs, so it *is* media, and no surviving cache file backs it |
| `System message` | `content_type` in `CONTENT_TYPE_NAMES` (6, 9, 12, 13) — not media at all, so "no cached file" would be the wrong thing to say about it (see above) |
| `Unrecognised (content_type <n>)` | any other `content_type` — the number is reported rather than guessed at |

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
