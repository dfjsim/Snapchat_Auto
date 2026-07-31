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
