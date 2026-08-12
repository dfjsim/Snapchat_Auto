# Partial reports — a run of only the selected elements

An examiner regularly needs to hand over **part** of a run: the conversations, messages, Memories and
cached files relevant to one case, not the whole extraction. They tick rows in the reports, save the
selection, and the tool then does a run that renders **only** those rows plus whichever related
elements they ask for.

This page is the reference for the pieces that make that possible. It is written as they land, so
sections appear here as the feature is built.

* [The selection file](#the-selection-file) — what the examiner saves, and how it gets back in.
* [Source fingerprints and the version gate](#source-fingerprints-and-the-version-gate) — how a
  partial run establishes it is looking at the same evidence, read by the same build.
* [What a partial report says about itself](#what-a-partial-report-says-about-itself) — the banner, the
  "N of M" figures, the provenance block and `partial_manifest.json`.
* [Links whose other end is not here](#links-whose-other-end-is-not-here) — `xref`, `narrow`, and why
  a full report is byte-identical to one built before any of this existed.
* [What must not be left behind](#what-must-not-be-left-behind) — pruning media the filter orphaned.
* [Memory groups rendered in part](#memory-groups-rendered-in-part) — stating a group's real size.
* [Running one](#running-one) — the CLI, the GUI, the exit codes, and where the mismatch questions
  get asked.
* [The index pass](#the-index-pass-and-the-cross-report-manifests) — why every report is indexed
  before any of them renders, and where the cross-report manifests come from.
* [What a partial run leaves out](#what-a-partial-run-leaves-out-beyond-the-unselected-rows) beyond
  the unselected rows.

Related: [selection_format.md](selection_format.md) (the **normative** file format, for another tool
producing a selection), [report_ui.md](report_ui.md) (how rows are selected in the browser, and where a
`file://` page can keep that), [cross_report_linking.md](cross_report_linking.md) (the anchors a
selection names and the links between reports), [sqlite_wal_handling.md](sqlite_wal_handling.md) (why a
database's `-wal` is part of "the same data").

---

## The selection file

`Reports/selection.js` is written once, empty, by report generation and **never overwritten**. The
examiner ticks rows, then saves — in one of two forms, for a reason worth stating:

| button | file | what it is for |
|---|---|---|
| **💾 Save selections (.json)** | `selection.json` | plain JSON. The copy to keep with the case, and the file handed back to the tool. Downloads without a browser warning. |
| **Save as selection.js** | `selection.js` | the same payload wrapped in `SCSel.preload(…)` — the drop-in form a report auto-loads. |

Chrome and Edge treat a `.js` download as a dangerous file type: a "keep / discard" prompt at best,
and on some configurations blocked outright. Hence `.json` as the default.

**A `.json` renamed to `selection.js` does not work, and fails silently.** A report loads that file as
a script, and bare JSON at statement position is a syntax error the browser discards without a word —
the reports open with nothing selected and no indication why. So the tool converts it instead:

```
Snapchat_Auto --install-selection selection.json [--report-dir …\Reports] [--force yes]
```

That reads either form, migrates it to the current schema, backs up any existing `selection.js` as
`selection.js.bak-<stamp>`, and **refuses a selection saved for a different run** unless `--force yes`
— installing another case's selection into a report folder is worth blocking rather than reporting
afterwards.

`scripts/selection_file.py` owns both directions. It is **stdlib-only and imports nothing else from
this project**, because it is also the surface an external tool needs in order to produce a selection
of its own.

### Schema 2

```json
{"tool": "Snapchat_Auto", "schema": 2, "tool_version": "1.5.2+build.20260808",
 "run_id": "…", "sources": {…}, "exported": "…Z",
 "selections": {"conv": {"conv-<id>":        {"conv": "<id>", "server": "<id>"}},
                "msg":  {"conv-<id>|msg-12.0": {"conv": "<id>", "smid": "12.0"}},
                "mem":  {"mem-<ZSNAPID>":    {"snap": "…", "mediaid": "…"}},
                "cc":   {"ck-<CACHE_KEY>":   {"key": "…", "sha": "…"}},
                "cm":   {"cm-<sha256>":      {"sha": "…", "raw": ["…"], "rel": "…"}},
                "ct":   {"ct-<user id>":     {"uid": "…", "user": "…"}}}}
```

A row's value is its **key record** — the identifiers it can be found by again — or a bare `1` when
none were recorded. See [report_ui.md](report_ui.md#what-a-selection-file-records--schema-2) for the
browser side: why message ids are qualified with their conversation, why a schema-1 file's bare
`msg-…` ids are quarantined rather than guessed at, and how the key records reach the store without
costing anything per row.

**Which ids survive a change to the parser**, and which need those key records:

| kind | id | derived from | survives? |
|---|---|---|---|
| `mem` | `mem-<ZSNAPID>` | `ZGALLERYSNAP.ZSNAPID`, a device-assigned UUID | **yes** |
| `cc` | `ck-<CACHE_KEY>` | a key in `cache_controller.db` | **yes** |
| `conv` | `conv-<client conversation id>` | `arroyo.db`, a device-assigned UUID | **yes** |
| `msg` | `conv-<id>\|msg-<server_message_id>` | `conversation_message` | yes — but see below |
| `ct` | `ct-<user id>` | `primary.docobjects` / the friends artifact | yes for the user id |
| `cm` | `cm-<sha256>` | the **decoded** payload's hash | **no** |

Two need care:

* **`cm-<sha256>` is hashed over the recovered content, not the file on disk**
  (`cache_media_report`), and its rows are **merged by decoded content**. A build that decrypts or
  decodes something an earlier one could not therefore gives the same file a different id, and may
  merge two rows into one or split one into two. Every copy's raw SHA-256 and the relative path travel
  with the selection.
* **the fallback branches of `msg` and `ct`.** A message with no server id is anchored on the
  device's own (`msg-c<client_message_id>`, which is evidence and needs no special handling); one with
  **neither** id falls back to its *position*, `msg-row<N>`, and recovering one more message shifts
  every later one — as the WAL free-space carving in `TODO.md` would. `contact_anchor` falls back
  username → conversation id → `ct-unknown`, which is not unique. Those rows carry the conversation,
  timestamp and the sender's user id, or every identifier the contact has.

  **A positional id is never matched on.** `_resolve_one` normally takes an exact id match as the
  answer, and that rule rests on a row id being a fact about the evidence — which every id here is but
  this one. A shifted position *still exists*, so an exact match would hand over a different message
  than the one ticked, silently, reporting the confident "its own id" as its reason. So it has to be
  proved by an alternate (conversation + time + sender's user id), and if nothing does, it is named as
  not found with that explanation. Refusing to name a message is a bad outcome; naming the wrong one is
  worse.

  That triple is spelled by **one** function, `partial_report.ts_sender_key`, called by the index that
  registers it and the lookup that resolves it. The two have disagreed twice — once over the case of the
  sender, once over `created_unix` being a float where an external tool sends the integer the format
  documents — and each time the result was a key that matched nothing, which is indistinguishable from a
  key that was never sent. A key spelled in two places is a key that will differ again.

### Several rows of one group are the group, not a doubt

An alternate that names several rows normally means the run refuses, because picking one would hand over
a row the examiner did not tick. There is one exception, and it is a statement rather than a guess: when
**every** row a key names belongs to one **group**, the key has identified which rows that media file
belongs to — a group *is* the snap rows that share one media object. All of them become seeds, and each
carries the reason *"…, which 2 Memories of one group share — all 2 included"*.

It is a **last resort**: `_resolve_one` tries every alternate for a single-row match first, so a
selection carrying both a group-wide identifier and a row-specific one resolves on the specific one.
Rows that are not one group still refuse.

This matters for one caller in particular. A tool that never read `scdb-27` has no snap id, so a cache
key is its only `mem` identifier — and `ZMEDIAID` would not have rescued it, being shared by a group's
members by design (which is what made every grouped Memory resolve as ambiguous before `0568f39`).
Refusing there stopped the whole build over a row the key had actually found. With `mem_group` on — the
default — the closure would have pulled the siblings in anyway, so in the usual case this changes the
record rather than the output. `Index.groups`, filled in by the Memories index from `assign_groups`, is
what tells the two cases apart.

---

## Source fingerprints and the version gate

A partial report is a subset of a report the examiner ticked rows in, and the two are only comparable
if they were built from the same evidence, by the same build. `scripts/source_fingerprint.py` is what
establishes that.

### What a run records

Every run writes `Reports/sources.json` and shows the same values on its `index.html`:

* per artifact — role, path in the extraction, size, MD5, SHA-256;
* the **tool version**, `+build.<N>` tag included;
* a **source digest**, one value over the sorted per-artifact hashes;
* the extraction ZIP's path, size and mtime — hashed only with `--hash-zip yes`, because tens of GB is
  a long sequential read and it is the database hashes that actually bind what the reports contain.

The artifacts are the databases, plists and keychain — the things that decide *what the reports
contain*: `arroyo.db`, `scdb-27.sqlite3`, `gallery.encrypteddb`, `cache_controller.db`,
`contentManagerDb.db`, `primary.docobjects`, `user.plist`, `ClientEncryptionService.plist`,
`group.snapchat.picaboo.plist`, and the keychain/keystore. **Cached media files are not fingerprinted
here** — every one of them already carries its MD5 and SHA-256 in the cache reports — and
`sources.json` says so rather than leaving the scope to be inferred.

`collect()` is **handed the paths the parser already resolved** rather than globbing for them itself.
One place decides where an artifact lives, and it is `ParseSnapchat_iOS.main` that has to find it
anyway; a second set of globs would drift from the first.

A **missing** artifact is recorded as missing, not omitted. "This run had no `gallery.encrypteddb`" is
a finding, and a later run that suddenly has one is a difference worth showing.

### Sidecars get their own verdict — and a differing log is a real difference

Each database is fingerprinted together with its `-wal` and `-shm`, because
`scripts/data/sqlite_open.py` reads every database **twice** — with the log applied and without it —
and marks the rows only one reading contains. The log is where the **deleted and superseded rows**
come from, so it is not incidental to what the reports say.

They are compared as **separate lines**, so the examiner is told precisely *what* differs instead of
getting one verdict for a whole database. That is a diagnosis, not a discount: **a differing `-wal`
fails the verdict exactly as a differing database does, and nothing may be reused.** Two logs can
agree on every current row and still recover a different set of deleted rows, so "the live data looks
the same" is not a reason to trust it.

### The two verdicts

| | question | on a difference |
|---|---|---|
| `verify(expected, found)` | is this the same evidence? | worth stopping for — but the examiner may have a better extraction of the same device and choose to go on |
| `check_version(expected)` | was it read by the same build? | nothing an earlier run produced may be reused |

Neither reports success when it had nothing to compare. A selection with no `sources` block (one from
an external tool, or from before this existed) yields *"cannot be verified"*, never *"verified"* —
"we could not check" and "it matched" must never read the same.

### Reuse, and why it is gated on both

`reuse_allowed()` requires an **identical build and byte-identical artifacts**. Given both,
re-deriving would be deterministic, which is what makes reusing an earlier run's output equivalent to
re-deriving it rather than a shortcut. Given either in doubt, everything is re-derived from the
evidence.

The version half is the important one: a newer Snapchat_Auto may extract more paths from the ZIP
(`extract_zip.ios_files` grows), decrypt media an older build could not, or classify bytes
differently. Inheriting the older output would hide exactly those improvements — which is why the
comparison is exact, `+build.<N>` included, and why two builds of the same version count as different.

---

## What a partial report says about itself

A subset of a report is only usable if the reader can tell that it *is* a subset, of what, and what was
cut out. Four things carry that, and a partial run emits all four.

**The banner** (`partial_report.banner_html`) sits under the header of **every** page, not only the
index — including each conversation page and each Memory detail sub-page, because a page handed on by
itself has to say what it is. It names the extract as partial, points at `partial_manifest.json`, and
carries the examiner's case reference when one was given.

It is also where the **mismatches** appear. If the examiner chose to proceed through a source or version
mismatch (see above), that decision is on the face of every page — not only in a JSON file next to it.
The same goes for the case where a selection cannot be checked at all: a file with no `sources` block
says *"not possible to verify"*, never nothing.

**The figures** (`figures_html`) are appended to each report's own `.sum` line rather than replacing it.
The line already counts the rows that were rendered, which is the truth about the folder; what it cannot
say on its own is how much of the extraction that is, and *a subset presented without its denominator
reads as the whole*. So each report adds `3 of 412 conversation(s) in the extraction · 2 selected, 1
pulled in by a relation`. A conversation page states its own denominator instead — how much of *that
chat* is here — since that is the question a reader of one conversation has.

Two rules follow, worth stating separately because both were bugs waiting to happen:

* **No figure may describe rows that are not there.** `conversations_report._narrowed` recomputes every
  message-derived count from the messages it kept (`n_messages`, `n_attachments`, `n_files`, the sender
  and type tallies) and keeps the conversation's real size as `n_messages_full` for the "of M" figure. A
  page listing four messages under a header saying 137 is worse than one that says nothing: the header
  is what gets quoted.
* **It returns a copy.** The model itself stays intact — the closure was decided from it, and the other
  reports still read it.
* **A sub-report holding nothing is a normal outcome, and must read as one.** An extract of two Memories
  legitimately contains no conversation, no contact and no Library/Caches file. Both of the messages a
  reader would otherwise get there were wrong: the shared table raised the *"row data could not be
  loaded — keep the data folder next to the HTML"* banner on any empty table, and the empty-table text
  said *"nothing matches the current filters"* with no filter set. Neither is true of an extract that
  simply holds none of that kind, and both send the examiner after a fault instead of at the figures
  above, which already say `0 of 246`. See
  [report_ui.md](report_ui.md#an-empty-report-is-not-a-broken-one).

**The provenance block** (`provenance_html`) is a collapsed `<details>` on each report and expanded on
`index.html`: the tool version, the selection's identity (name, SHA-256 as supplied, `selection_digest`,
its `exported` stamp and schema), the source verification verdict with its per-artifact table, the
per-report *selected / pulled in / total*, and every relation with a check or cross and its basis. The
relations that were **not** followed are listed too — an omission the reader cannot see is one they will
not account for. So is the fact that the legacy reports have no row selection and were left out whole.

Its four long sub-sections — the source verdicts, the per-report counts, the relation policy, and what
is and is not in the folder — are themselves **folded** (`_prov_section`), because expanded on
`index.html` they push the links to the reports off the first screen, and the links are what the page is
for. Folding a statement out of sight is only acceptable if the statement's *answer* stays visible, so
each summary carries it — *"12 of 12 identical to the run this selection was made in"*, *"41 of 848
row(s), across 6 report(s)"*, *"9 of 11 relation(s) followed — one hop from each selected row"* — and the
table behind it is the working. What stays unfolded is the identity table plus the one-line statements
that have no table to hide: the tool-version verdict, the count of cross-references pointing outside the
extract, any withheld fields, and how the selection resolved.

**`partial_manifest.json`** at the extract root is the machine-readable form, and the file to read when
the question is *"what was left out"*, which no amount of on-page marking answers in aggregate: the
closure with a reason per row, every cross-reference that was cut and how many places pointed at it, the
relation policy, how each ticked row resolved, and the two verdicts.

A partial run writes into `Reports_partial_<stamp>/` (`partial_report.partial_dir`), never the folder it
is a subset of. Overwriting the reports the examiner ticked rows in would destroy the thing the extract
is a subset of.

### The `prov` mapping

Every function above takes an optional `prov` mapping whose keys are listed in
`partial_report.PROVENANCE_KEYS`. All of them are optional by design: a partial run built from an
externally produced selection knows some and not others, and the report states which rather than
implying it checked something it could not.

---

## Links whose other end is not here

A partial folder is full of cross-report links whose target it does not contain. The two dishonest
options are a link that goes nowhere and a link that was silently deleted — the first misleads the
reader, the second hides that an association exists at all. So every cross-report link goes through one
helper:

```python
report_ui.xref(link_html, targets, *, closure=None, label=None, brief=False, hint="")
```

It **wraps**, it does not build. Each call site keeps its own classes, target window, emoji and attribute
order, and passes the anchor it would have emitted anyway together with the `(kind, row id)` pairs the
link reaches. That choice is deliberate:

* `closure=None` returns the argument untouched, so **a full report is byte-identical by construction**
  rather than by inspection. Rebuilding fifteen heterogeneous chip markups from one signature would have
  rewritten the markup of every full report for no gain, and would have destroyed the corpus byte-diff
  that is the only thing catching a filtered code path drifting from the full one.
* An excluded target becomes a `.xout` marker that keeps **the visible label and the identifier**: the
  examiner can still see *what* is missing. Markup that is not a recognised anchor is stripped rather
  than re-emitted, so a live `<a>` can never survive inside the marker.
* `brief=True` is for a fixed-height index cell — the marker alone, the sentence in its tooltip. A
  collapsed virtual row is exactly one row tall, so a second line of text there is not shown short, it is
  sliced through the middle.
* Every marked target is recorded on the closure (`note_excluded`), which is what fills the manifest's
  `excluded_refs`.

The marker glyph is U+2298 (circled division slash), not the U+20E0 combining mark: a combining
character has nothing to combine with here and renders unpredictably on its own.

A link whose target is a **set** of rows (a `#find=` fragment) is narrowed first with
`report_ui.narrow(closure, kind, values, anchor)`, so the receiving report does not open filtered to
nothing and the label states the true count — `cache (1)`, not `cache (3)`. When one shared token
addresses several rows and so cannot be narrowed row by row (a CACHE_KEY that several Library/Caches rows
carry), the count states how many of them the extract holds and the basis text says how many it does not.

Some links reach rows by a token rather than by an id, so the caller cannot tell what is on the other
end. `Closure.reaches(edge, kind, id, dst_kind)` answers that from the edges the generators already
derived — all of them known before the first page is written. That is how the Memories report's
`Library/Caches` pack link knows which chunk rows it addresses.

### A preview is not a link

Two reports show *another report's* file inline: the Library/Caches and cache_controller reports display
the plaintext the Memories report decrypted, because those cached bytes are encrypted and the copy is
sitting in the next folder. In a partial extract that Memory may be absent and its plaintext pruned. So
both filter those copies to the ones actually present (`_decrypted_here`, `_absent_memory_copy`) and,
when none is, **say so** — falling through to the "not recovered" branch would be worse than a broken
image, because these bytes *were* recovered and the row would be denying it.

---

## What must not be left behind

Two stages publish files **before** the filter runs, each for a reason given in its own module:
conversation attachments (hard links out of a folder the parser already filled — deferring them would buy
nothing) and Memory media (grouping depends on the decrypted bytes, so the decryption cannot be deferred
at all).

That means a partial run's `media/` folders start out holding files no included row references. A
disclosure folder carrying media nothing in it points at is a defect — the file is there, an examiner can
open it, and no page says where it came from. In the Memories case it is worse than a defect: it would
put decrypted media of unselected Memories into a bundle, which is the one thing this feature exists to
avoid.

So both prune, and only ever inside the run's own output folder:
`conversations_report._prune_media` to the attachments the rendered messages reference,
`memories_media_report._prune_media` to the media files of the included Memories. These are links, so
removing them cannot touch the extracted copy behind them. Maps need no pruning: they are rendered after
the filter, from the included Memories only.

---

## Memory groups rendered in part

A group exists precisely because its members share a `ZMEDIAID` and/or identical media bytes — a sibling
is the same media under another snap row, and all members share one detail page. With the `mem_group`
relation on, selecting one member brings the others; each is badged as included-but-not-selected
(`partial_report.sibling_badge`) so it cannot be mistaken for something the examiner chose.

With it off, the page renders only the selected members — and then it has to state the group's true size,
because "Group of 1" would deny the relationship the grouping asserts. `render()` derives the whole
extraction's grouping (`assign_groups` over the unfiltered model — union-find over records already in
memory, no media touched) and hands `_render_group_detail` a `group_of` map. The page then says *"Showing
1 of 2 memories grouped here"*, marks each omitted snap id, and — because the page is re-rendered rather
than copied — every shared block below follows the members it was given, so the file table, the timestamp
columns and the encryption columns describe nobody who is absent.

### A kept media file can be named after a Memory that is not here

Identical content is published once, under the name of whichever Memory it was written for **first**
(see [report_memories.md](report_memories.md#one-copy-per-distinct-content-in-media)). In a partial
extract that first writer may be one of the group's omitted members, so the surviving file in `media/`
can carry a snap id the report does not contain. Three things make that sound rather than a leak:

* the name is a **name**, not an attribution. The file table's per-file line
  (`memories_media_report._media_refs`) is built from the members actually rendered, so it names only
  Memories the extract contains — an absent snap is never listed as having recovered anything;
* the omitted snap ids are **already on the page** by design: the sharebar names every one of them, and
  `_render_group_detail` marks each with `xout`. A group page is required to state its real size, so a
  filename cannot disclose an id the page does not;
* the alternative — renaming a shared file per partial run — would make two reports built from the same
  evidence disagree about what the same bytes are called, for no gain over the sentence above.

Pruning is unaffected: the keep-set is built from each included Memory's own `f["out"]`, which is the
shared name, so a file an included Memory recovered is kept however it is named.

---

## Running one

A partial report is a **normal run with a selection**, not a separate mode of the tool. Same
extraction, same parsing, same generators; only what gets rendered differs.

```
Snapchat_Auto --zip <extraction.zip> [--keychain <file>] --workdir <dir> [--run-name <name>]
              --selection <selection.json>
              [--relations recommended|minimal|all|<a,b,-c>] [--case-ref <text>]
              [--dry-run yes] [--unresolved refuse|drop]
              [--sources-mismatch refuse|proceed] [--version-mismatch refuse|resolve]
              [--no-reuse yes] [--max-rows <n>] [--links-dir <dir>]
```

It writes `Reports_partial_<stamp>/` **inside the run folder**, with its own `index.html`,
`partial_manifest.json` and provenance, so the folder can be handed over as it stands. The full
`Reports/` is never touched. Pointing a partial run at the same `--workdir`/`--run-name` as the full one
is the normal case and the fast one: `ExtractedData/` and `SnapFixedVideos/` are already there, so
nothing is unzipped again.

`--dry-run yes` does everything up to the closure — verifies the evidence, indexes every report,
resolves the selection, expands the relations — prints what the extract *would* hold, and writes
nothing.

**Exit codes** are distinct, because a script driving several extractions needs to tell these apart:

| code | meaning |
|---|---|
| 0 | built (or dry-run completed) |
| 2 | bad usage — an unknown option, a missing value, an unknown relation name |
| 3 | this is not the evidence, or not the build, the selection was made with |
| 4 | the selection could not be resolved: a ticked row this run has no match for, or an ambiguous one |
| 1 | anything else |

In the **GUI** the same thing is one optional field: *Selection file*, a **Related items…** dialog
(one checkbox per relation with its basis, the transitive and legacy switches, and Minimal /
Recommended / Everything), and a *Case / exhibit reference*. Choosing a file reports what it holds and
**offers back the paths it was made from** — a recorded path that no longer exists is shown as a hint
rather than filled in, because a pre-filled path that does not resolve is worse than an empty field.
The relation policy is remembered between runs; the selection file and the case reference are not, since
both belong to one case and carrying a case reference onto the next one is a real error.

The GUI converts its dialog into the CLI's own `--relations` spec and parses it back with the same
code (`partial_report.parse_relations` / `parse_policy`). That round trip is a test: two front ends that
can drift are two different tools.

### What `recommended` follows, and two defaults worth explaining

`Relation.default` in `partial_report.RELATIONS` is the single source of the `recommended` preset, so the
CLI, the GUI dialog and `describe()` cannot disagree about it. Two are set the way they are on purpose:

* **`conv_messages` is off.** A conversation can hold thousands of messages, and ticking the conversation
  asks for the conversation — its detail page, its participants, its activity — not for every message in
  it to be disclosed. Messages are individually tickable in the same report, which is the finer
  instrument, so the default is the narrower reading of the click.
* **`msg_sender` is on**, and it is what makes the above safe: a message whose sender has no Contacts row
  in the extract shows a display name the reader can learn nothing else about. It is keyed on the
  sender's permanent user id and takes the anchor from `contact_link_index`, the same row every other
  report links that person by — a message whose sender id was not recovered links to no contact rather
  than to one matched on a name.

`transitive` follows **only the relations that are switched on** — the enabled list is computed once and
each pass reuses it. That does not make it a safe default, because the recommended set already contains a
cycle: a message reaches its cache entry (`msg_cache`), that entry's Memory (`cache_memory`), that
Memory's other entries (`mem_cache`), and their messages (`cache_message`), and round again. So it grows
with the size of the connected component in the evidence rather than with the selection, which is why one
hop is the default and the dialog says so.

### Where the relation policy comes from

Three sources, most specific first: **the run's own settings** (`--relations`, or the GUI dialog, which
always supplies one), then **the spec the selection file records**, then **the built-in defaults**
(`recommended`). A tool that builds selections can therefore record the policy its selection is meant to
be read with, and the report is built with one flag instead of two — while the examiner running the report
keeps the last word, which is the right way round.

Whichever it was is named in the run log, in the provenance (*"Policy taken from the selection file"*) and
in `partial_manifest.json` as `relations_from`. That attribution is not decoration: the provenance lists
which relations were followed, and a reader cannot check a choice they cannot trace to whoever made it.

A spec in the file that this build cannot read is **refused**, naming the file and pointing at
`--relations` as the way to say what to follow instead. Falling back to the defaults would build the
extract under a policy nobody asked for, and the provenance would then attribute it to the defaults —
true, but not what was requested.

### Where the mismatch questions get asked

`partial_report.check_evidence` runs inside the pipeline, immediately after the run records its source
fingerprints and **before any report is built**: the tool version first (a difference there invalidates
reuse whatever the hashes say), then every artifact. It refuses unless the options say to proceed, and
either way both verdicts land in the provenance — and on a mismatch, in the banner of every page.

The GUI asks the same question *twice*, deliberately. Before starting it compares the selection's
fingerprints against the **full report folder's** `sources.json`, which answers "does this selection
belong to that run" in a few milliseconds — so the examiner settles it before waiting through an
extraction rather than after. Whether the ZIP about to be processed is that same evidence can only be
answered once it is unpacked, and the pipeline answers that too.

## The index pass, and the cross-report manifests

In partial mode the parser runs the reports in two halves rather than one (`ParseSnapchat_iOS`):

```
_index_stages()   every report's index() -- the rows each would render, and the edges it derived
resolve()         every ticked id -> a row of this run
expand()          + the related rows the examiner asked for -> one Closure
_render_partial() every report's render(), in the order the manifests depend on
write_manifest()  partial_manifest.json
```

A report whose `index()` fails is left out with one logged line, exactly as a failing report is in a
full run — and its ticked rows then come back from `resolve()` as *not produced by this run*, which
refuses the build. That is the honest outcome: an extract quietly missing evidence is worse than one
that will not build.

**The one ordering problem, and its answer.** Several `index()` steps read manifests that an *earlier
report's* `render()` writes: `cache_media_report.index` needs `memory_pages.json`, `cache_links.json`
and `memory_packs.json`; `cache_controller_report.index` needs `media_by_cache_key.json`. In an
index-all-then-render-all pass nothing has written them yet. So both take a **`links_dir`**, and a
partial run points it at the full report folder the selection was made in — same evidence, same build,
already proved by the gate above. Interleaving index and render per report is not an option: that is
exactly what makes a backward relation (a ticked cache entry pulling in its Memory) impossible, which is
the whole reason the index pass exists.

Reading a full run's manifests only works if the page names they contain are still right in the extract,
and one of them is not free: a Memory group's detail page is named after a hash of its members' snap
ids. Rendered from part of a group that hash changes, so every `pages/<key>.html` in the full run's
manifest would point at nothing. `assign_groups` therefore takes the same `group_of` map the page uses
to state the group's real size, and derives the key from **the whole group** — so the page name is a
property of the group, not of which members happened to be rendered. Without a `group_of` (a full run)
the group *is* its members and the key is unchanged.

## What a partial run leaves out beyond the unselected rows

Three things are removed or skipped because they are whole-database or whole-device by construction, and
each is *stated* in the provenance rather than silently missing:

* **The two legacy reports** (`Communications_legacy`, `LocalMemories_legacy`). Neither has row
  selection, so both are all-or-nothing; the legacy Memories report decrypts every Memory on the
  device. Left out by default, `legacy_reports` opts in. The legacy folder is also the parser's staging
  area for chat attachments, so it is removed *after* the Conversations report has linked out what it
  needs — those are hard links, so the bytes survive in `Conversations/media/`.
* **`Communications_legacy/cache_links.json`**, which names every message of every conversation.
* **`CacheController/sqlite_views/`** — the staged copies of `cache_controller.db`, with its write-ahead
  log applied and without. In a full report those are transparency: any figure can be read back from the
  database it came from. In an extract of selected rows they are the entire database, every row of it.

## Still to build

The two **reuse caches** from the source-fingerprint design are not implemented: the `Library/Caches`
index cache and hard-linking already-decrypted Memory media out of the full run. `reuse_allowed()`
gates them and the provenance reports what it decided, but today a partial run re-derives both. Neither
is a correctness gap — the `ExtractedData/` and `SnapFixedVideos/` short-circuits already make a partial
run into the same run folder skip the expensive unzip — and both are worth having, because the
`Library/Caches` walk and the Memory decryption are what a partial run cannot otherwise defer.
