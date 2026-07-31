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
  `scauto_comms_legacy`), so a second click on the same link reuses
  the tab that is already open. When the URL — fragment included — is unchanged, the browser fires
  **no** event, which is why "it only worked the first time". `NAV_JS` therefore **consumes the
  fragment** after acting on it (`location.hash = '_'`), so the next click is always a real
  `hashchange`. The `_` sentinel is used rather than an empty fragment because an empty fragment
  makes the browser scroll back to the top. `history.replaceState` is deliberately not used: it
  throws on `file://` documents.

## The "?" popovers

Every explanation icon opens its popover with `position:fixed`, placed next to the icon in viewport
coordinates and nudged back inside the window when it would fall off the right or bottom edge
(`HINT_JS`). An absolutely positioned popover is clipped by the first ancestor that hides its
overflow, which is exactly what a column header does (`.vhdr .vc` clips so long titles can
ellipsize) and what a virtual row does — the popover came out cut off, or invisible. A fixed element
is not clipped by an overflow ancestor. It is closed on any click, and on scroll or resize, since a
fixed popover would otherwise stay put while the page moves under it.

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
* **💾 Save selections** downloads a new `selection.js` (a small, human-readable
  `SCSel.preload({…})` file, ids in plain text). Dropping it back next to the reports makes the
  selection load automatically from then on — and it is a record that can be filed with the case.
  **Load…** reads one back explicitly.
* `localStorage` is still written as a same-tab safety net, so an accidental reload does not lose
  work; a stash newer than the loaded file wins on reload, but an explicit **Load…** always
  replaces what is in memory.
