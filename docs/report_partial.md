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

Related: [report_ui.md](report_ui.md) (how rows are selected in the browser, and where a `file://`
page can keep that), [cross_report_linking.md](cross_report_linking.md) (the anchors a selection names
and the links between reports), [sqlite_wal_handling.md](sqlite_wal_handling.md) (why a database's
`-wal` is part of "the same data").

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
* **the fallback branches of `msg` and `ct`.** A message with no server id is anchored on its
  *position* (`msg-row<N>`), so recovering one more message shifts every later one — as the WAL
  free-space carving in `TODO.md` would. `contact_anchor` falls back username → conversation id →
  `ct-unknown`, which is not unique. Those rows carry the conversation, timestamp and sender, or every
  identifier the contact has.

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

### Sidecars get their own verdict

Each database is fingerprinted together with its `-wal` and `-shm`, because
`scripts/data/sqlite_open.py` reads every database **twice** — with the log applied and without it —
so the log is part of what "the same data" means.

They are compared as **separate lines**. A `-wal` is rewritten whenever anything opens the database,
so it can legitimately differ where the database does not; folding that into the database's verdict
would announce "the evidence changed" for a difference that may mean nothing. The examiner is told
which kind of difference they have.

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
