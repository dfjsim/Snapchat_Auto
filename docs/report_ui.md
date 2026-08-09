# Shared report UI — virtual tables and cross-report navigation

`scripts/report_ui.py` holds the pieces every HTML report shares: the **virtual index table**, the
**anchor/tab navigation**, the “?” popover (`HINT_JS`/`HINT_CSS`/`info_icon()`) and the page chrome
the newer reports style themselves with (`PAGE_CSS`). It has no dependencies and emits plain ES5-ish
JS, so the reports keep working from `file://` on any modern browser with nothing installed.

## Why the index tables are virtualized

The Memories index and the cache_controller index used to put **every** row — and, for
cache_controller, every row's expanded detail panel — in the HTML document. On the test extraction
(460 cache entries) that was already a 1.9 MB file; a real device with tens of thousands of cached
files produced a document the browser could not lay out in reasonable time (the "major performance
issues" TODO).

Now:

```
Reports/
  run_id.txt                       identifies this set of reports
  selection.js                     the examiner's row selections (see below)
  CacheController/
    CacheController_report.html    ~20 KB, whatever the number of entries
    data/index.js                  one compact array per row (all rows)
    data/detail-<n>.js             row detail HTML, 250 rows per chunk
```

The Conversations report uses the same engine **twice**: once for the conversation index and once
per conversation, for its message table (`pages/data/<key>/index.js` +
`pages/data/<key>/detail-<n>.js`) — one active conversation can hold as many messages as a small
cache index holds files. Because that would inline the ~20 KB of shared JS/CSS into every
conversation page, that report writes it once to `Conversations/assets/ui.{js,css}` and both the
index and the detail pages load it with `<script src>` / `<link href>`.

* **The document is a shell.** It holds the header, the toolbar, the column titles and the scripts.
* **`data/index.js`** carries the rows: `[anchor id, [cell html…], search text, {col: sort key},
  detail chunk, {filter metadata}]`. Cell markup is kept minimal — per-column styling lives in CSS
  (`.vc.c3 {…}`) — because every byte is multiplied by the row count.
* **Only the visible rows exist in the DOM.** A spacer div provides the full scroll height and the
  rendered window (viewport ± 600 px) is re-rendered on scroll.
* **Detail panels are fetched on demand.** Expanding a row loads only the `detail-<n>.js` chunk
  that contains it, once, and caches it.
* **Search runs over the whole index**, not over the DOM: each row carries a pre-built lower-case
  search string (cache key, every `EXTERNAL_KEY`, user ids, hashes, on-disk filenames, linked
  Memory/conversation ids …). Filters, sorting and the "expand all" button all work on the full
  row set, not just what is on screen.
* **A query is OR-ed on `|`.** `a|b` matches a row containing either; a query with no `|` behaves
  exactly as a plain substring search always did. This exists for `#find=` links (below) but is
  usable by hand, and the search box's tooltip says so.

## Links whose target is a set of rows (`#find=`)

One entry in one report is regularly **several** rows in another: the same cached bytes sit under
more than one path, one pack is stored as a numbered series of chunk files, one Memory owns many
cached files. An `#anchor` reaches only the first of them and a chip per row makes the cell
unreadable, so those links carry every target instead:

```
CacheMedia_report.html#find=<CACHE_KEY>
CacheController_report.html#find=<CACHE_KEY>|<CACHE_KEY>|…
```

`report_ui.find_fragment(tokens)` builds the fragment (de-duplicated, percent-encoded, joined with
`|`); `NAV_JS` routes it to `SCV.findAll`, which clears the filters, puts the tokens in the search
box, refilters and **expands every match**. What the examiner lands on is the complete set, with the
query that produced it visible — clearing the box restores the full report. Each token must be
something the target rows carry in their search text (a `CACHE_KEY`, a snap id, a pack item hash).

`reset` (the config callback `findAll` and `goTo` both use) means *stop hiding anything*, not
"restore the defaults" — the Library/Caches report hides app assets by default, and a `reset` that
re-hid them left every link to an app-asset row landing on nothing.

**In a partial report the token set is narrowed first.** `report_ui.narrow(closure, kind, values,
anchor)` drops the tokens whose row is not in the folder, so the link does not open the receiving report
filtered to nothing, and the chip's own label states the true count. When one shared token addresses
several rows and so cannot be narrowed row by row, the count says how many of them are there and the "?"
text says how many are not. Every cross-report link — narrowed or not — is emitted through
`report_ui.xref`; see [report_partial.md](report_partial.md#links-whose-other-end-is-not-here).

Measured on a synthetic 101 200-row cache_controller index (Chrome, `file://`):

| | |
|---|---|
| document | 20 KB |
| first paint (all rows counted, table interactive) | 0.70 s |
| search | 0.18 s |
| sort by size | 0.04 s |
| scroll to the end | 0.21 s (47 rows in the DOM) |
| jump to an anchor in the last row | 0.62 s |
| JS heap | ~180 MB |

> **Keep the `data/` folder next to the report.** If it is missing, the report shows a red banner
> saying so instead of an empty table. Data files are loaded with `<script src=…>` (not `fetch`),
> because `file://` pages are not allowed to `fetch`/`XMLHttpRequest` their own siblings.

## Cross-report navigation (`NAV_JS`)

Every report — including the plain ones (Communications legacy, Memory detail sub-pages) — includes
`NAV_JS`, which owns what happens when an `#anchor` link is followed:

* **Scrolls the target clear of the sticky toolbar and column titles.** The scroll position is
  computed from the sticky block's measured height, so the target row is never hidden underneath it.
* **Highlights the target** (`.schl`), so it is obvious what was jumped to.
* **Works in a virtual table**, where the target row may not exist in the DOM yet: `SCV.goTo()`
  resolves the row's index, clears any active filter that hides it, expands it, and scrolls to its
  computed offset.
* **Works on repeat clicks into an already-open tab.** Reports open each other in *named* tabs
  (`scauto_cache`, `scauto_memories`, `scauto_convs`, `scauto_contacts`,
  `scauto_comms_legacy`), and the detail sub-pages get one named tab each
  (`scauto_memory_page`, `scauto_conv_page`), so a second click on the same link reuses
  the tab that is already open. When the URL — fragment included — is unchanged, the browser fires
  **no** event, which is why "it only worked the first time". `NAV_JS` therefore **consumes the
  fragment** after acting on it (`location.hash = '_'`), so the next click is always a real
  `hashchange`. The `_` sentinel is used rather than an empty fragment because an empty fragment
  makes the browser scroll back to the top. `history.replaceState` is deliberately not used: it
  throws on `file://` documents.

**Every** link out of an index must carry its named target — a bare `<a href>` is a bug, not a
shorthand. Navigating an index away *in place* discards the whole working state of that page: the
scroll position, the filters, which rows are expanded, the page the pager is on, and the ticks that
have not yet been written to `selection.js` (those live in memory, so leaving raises the "leave
site?" prompt and then loses them — see below). The Memories index thumbnail was missing its
`target` and did exactly that, while the `open ▸` button in the same row did not.

Named tabs are also why these stay plain `<a href>` links rather than `window.open` calls. The name
is what makes the tab get *reused* — fifty clicks yield one detail tab, not fifty — and keeping the
href intact preserves the browser's own escape hatches: Ctrl/⌘-click for a separate tab,
Shift-click for a separate window, middle-click, and the context menu. Intercepting clicks in JS to
open a sized window would take all of that away, and `window.open` features are applied only on the
window's *first* open anyway.

## Filter controls — two rules

**Every filter bar ends with a red "✕ Clear all filters"** (`report_ui.clear_filters_button`,
`SCV.clearFilters()`). The machinery already existed: `C.reset()`, which `findAll`/`goTo` call so a
filter cannot hide the row a cross-report link was aimed at. What was missing was a way for the
examiner to ask for it, and without one "the report says 0 rows" is regularly one control left set
three filters ago, on a bar that does not fit on one line at every window width. It clears the
search box, every dropdown and *Selected only* — the ticks themselves are kept, which is why it is
safe to make it the obvious red button.

**A filter with nothing to match is not offered as a choice.** Either the control is left out
(`conversations_report._wal_filter_html` returns `""` when the `-wal` deleted no message) or the
dead option is disabled and shows its count
(`memories_media_report._media_filter_options`: *"partially cached (incomplete) — 0"*). A dropdown
whose only possible outcome is an empty table is indistinguishable from a broken report, and that
is exactly how it was read. Counts in the options are the cheaper half of the rule: they say what a
filter will return *before* it is chosen.

Reading an optional control from `match`/`reset` goes through `scFv(id)` / `scFvReset(id)`, which
tolerate the element being absent, so the same generated JS works whether or not the control was
emitted.

## The "?" popovers

Every explanation icon opens its popover with `position:fixed`, placed next to the icon in viewport
coordinates and nudged back inside the window when it would fall off the right or bottom edge
(`HINT_JS`). An absolutely positioned popover is clipped by the first ancestor that hides its
overflow, which is exactly what a column header does (`.vhdr .vc` clips so long titles can
ellipsize) and what a virtual row does — the popover came out cut off, or invisible. A fixed element
is not clipped by an overflow ancestor. It is closed on any click, and on scroll or resize, since a
fixed popover would otherwise stay put while the page moves under it.

## Media inside an expanded row

An expanded row is measured as soon as it is in the DOM, so media that resizes the row *after* it
loads leaves every offset below it wrong — the symptom is scrolling that jumps. Media in a detail
panel therefore calls `SCV.remeasure()` when it loads, and previews are capped so an expanded row
stays a sensible size. `remeasure()` only measures: re-rendering would rewrite the window's
`innerHTML`, which recreates those media elements, which fire their load event again — an endless
loop. `measure()` rebuilds only when a height really changed, so it settles after one round.

## Clicking inside a row

In the cache_controller table a row toggles its detail when clicked, but clicks on a **link**, a
**“?” icon**, a form control, or anywhere **inside an open detail panel** never toggle it — so
following a cross-report link no longer collapses/expands the row you are leaving behind.

## Paging

Both index tables carry a pager: **rows per page** (100 / 250 / 500 / 1000 / 5000 / all, default
500) and first / previous / page-picker / next / last. Paging is applied *after* filtering and
sorting, so the search box, the filters and "select all shown" always work on the **whole** index —
only what is drawn is paged. Following an `#anchor` turns to the page the target is on before
scrolling to it, and **Expand all** applies to the current page (and refuses more than 500 rows at
once).

## Selecting rows — and where a `file://` report can keep them

The examiner can tick memories and cache entries as relevant to the case, filter to
**Selected only**, and select/unselect everything matching the current filters at once.

Keeping those ticks is the hard part, because a report opened from `file://` has almost nothing to
store state in. Measured in Chrome (and this is the behaviour the design assumes):

| | |
|---|---|
| `localStorage` in the same tab, after a reload | **kept** |
| `localStorage` seen from a second tab on the *same* file | empty |
| `localStorage` seen from another page in the *same folder* | empty |
| `localStorage` bridged through an iframe both pages embed | empty |

Each `file://` document gets its own partitioned, tab-scoped storage. So there is no browser
storage that the Memories index and a Memory detail sub-page can share, and none that survives
closing the tab.

The durable store is therefore **a file the examiner saves**: `Reports/selection.js`.

* Report generation writes it once, empty, and **never overwrites it** afterwards.
* Every page of the run loads it at startup (`<script src="…/selection.js">`) — that is how the
  Memories index, the Memory detail sub-pages and the cache_controller report agree on what is
  selected.
* Ticks are held in memory; a **“unsaved”** marker appears next to the count, and leaving the page
  with unsaved ticks raises the browser's "leave site?" confirmation.
* `localStorage` is still written as a same-tab safety net, so an accidental reload does not lose
  work; a stash newer than the loaded file wins on reload, but an explicit **Load…** always
  replaces what is in memory.

### Two save forms, and why `.json` is the default

Chrome and Edge treat a `.js` download as a dangerous file type: the examiner gets a "keep / discard"
prompt at best, and on some configurations the download is blocked outright. So the toolbar offers
both forms of the same payload:

| button | file | what it is for |
|---|---|---|
| **💾 Save selections (.json)** | `selection.json` | plain JSON. The copy to keep with the case, and the file handed back to the tool. Downloads without a warning. |
| **Save as selection.js** | `selection.js` | the same payload wrapped in `SCSel.preload(…)` — the drop-in form a report auto-loads. |

**A `.json` renamed to `selection.js` does not work, and fails silently.** The reports load that file
as a script, and bare JSON at statement position is a syntax error the browser discards without a
word — the reports open with nothing selected and no indication why. Nothing in the UI suggests the
rename; instead the tool does the conversion itself:

```
Snapchat_Auto --install-selection selection.json [--report-dir …\Reports]
```

`scripts/selection_file.py` owns both directions (`parse_selection_text` reads either form,
`selection_js_text` writes the drop-in one, `install_selection` places it and backs up whatever was
there). It is stdlib-only and imports nothing else from the project, because it is also the surface
an external tool needs in order to produce a selection of its own.

### What a selection file records — schema 2

```json
{"tool": "Snapchat_Auto", "schema": 2, "tool_version": "1.5.2+build.20260808",
 "run_id": "…", "sources": null, "exported": "…Z",
 "selections": {"conv": {"conv-<id>": {"conv": "<id>", "server": "<id>"}},
                "msg":  {"conv-<id>|msg-12.0": {"conv": "<id>", "smid": "12.0"}},
                "mem":  {"mem-<ZSNAPID>": {"snap": "…", "mediaid": "…"}},
                "cc":   {"ck-<CACHE_KEY>": {"key": "…", "sha": "…"}},
                "cm":   {"cm-<sha256>": {"sha": "…", "raw": ["…"], "rel": "…"}},
                "ct":   {"ct-<user id>": {"uid": "…", "user": "…"}}}}
```

Two things changed from schema 1, both because a selection is about to be the *input* to a partial
report rather than only a working note.

**1. A message id is qualified with its conversation.** `server_message_id` is a per-conversation
ordinal (`12.0` = message 12, part 0), so the row anchor `msg-12.0` is unique only on its own page —
and the store is shared by the whole run. Ticking message 12.0 in one chat therefore marked message
12.0 in *every* chat. The **anchor is unchanged** (every cross-report link, `cache_links.json` record
and `SCV.goTo` target depends on it); what changed is the *stored* id, via a new `selPrefix` in the
virtual table's config:

```js
SCV.init({… selKind:"msg", selPrefix:"conv-<conversation id>|" …})
```

`SCV.selId(anchor)` applies it, `selectShown` stores the same prefixed ids, and
`SCSel.count(kind, prefix)` / `SCSel.clear(kind, prefix)` take a prefix so a conversation page's
count and Clear button mean *this* conversation. A table whose anchors are already globally unique
(`conv-`, `ct-`, `mem-`, `ck-`, `cm-`) sets no prefix and behaves exactly as before.

A schema-1 file loads, but its bare `msg-…` ids go to a quarantine bag that is **not** part of the
selection: they are not counted, not saved, and an amber banner explains that they name a message
number with no conversation and must be re-ticked. They are never promoted to whichever conversation
happens to be open — that would invent a fact, and would put a message the examiner never ticked into
a partial report.

**2. Each ticked row records the identifiers it can be found by again.** Four of the six id schemes
are read straight out of the evidence and survive any parsing improvement — `mem-<ZSNAPID>` and
`conv-<client conversation id>` are device-assigned UUIDs, `ck-<CACHE_KEY>` is a key in
`cache_controller.db`, `msg-<server_message_id>` comes from `conversation_message`. Two do not:

* **`cm-<sha256>` is hashed over the *decoded* payload**, and Library/Caches rows are merged by
  decoded content — so a build that decrypts or decodes something an earlier one could not gives the
  same file a different id, and may merge or split rows. Every copy's *raw* hash and the path travel
  with the selection.
* **the fallback branches of `msg` and `ct`.** `msg-row<N>` is positional (recovering one more
  message shifts every later one, as would the WAL free-space carving in `TODO.md`), and
  `contact_anchor` falls back username → conversation id → `ct-unknown`, which is not even unique.

The keys cost nothing per row in the data files: `SCV.init` takes a `selKeys(row)` callback and the
delegated `change` handler looks the row up through the `data-i` attribute `.vr` already carries, so
they are read only when a box is actually ticked. Hand-written checkboxes (a Memory sub-page's member
blocks, a conversation page's own box) carry `data-keys` inline — there are only a handful.
