# Communications report

Built in `scripts/ParseSnapchat_iOS.py` (`main` → `getHtml`) →
`Reports/Communications/Communications_report.html`, with recovered attachments in
`Reports/Communications/cacheFiles/`.

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

## Two-way link with the cache_controller report
Because attachments are named by `CACHE_KEY`, each rendered attachment:

* gets an `id="cf-<CACHE_KEY>"` anchor (so the cache_controller report can jump to it), and
* shows a 🗄 `cclink` back to `../CacheController/CacheController_report.html#ck-<CACHE_KEY>`.

Before the message contents are turned into HTML, `main` writes
`Reports/Communications/cache_links.json` — `CACHE_KEY → [{conversation_id, server_message_id}]`
for every file present in `cacheFiles/` — which the cache_controller report reads to link its
entries back to the exact conversation/message. See
[cross_report_linking.md](cross_report_linking.md).

## Notes / caveats
* The report renders with pandas `DataFrame.to_html`; per-conversation tables come from
  `groupby('Client Conversation ID')`.
* `path_to_image_html` uses the module global `platform` to pick the path separator. It is set at
  import (`platform = system()`) and re-set by `main()` at startup, so it is always defined before
  any attachment is rendered.
