# The selection file — normative format

A **selection** names the rows a partial Snapchat Auto report should contain. An examiner normally
produces one by ticking rows in the reports and pressing *Save selections*; this page is for the other
route — **another tool** producing one from identifiers it already has.

It is written for an integrator who cannot be updated in lockstep with Snapchat Auto, so it states what
is guaranteed, what is optional, and where to ask rather than assume.

Related: [report_partial.md](report_partial.md) (what the tool does with a selection),
[cross_report_linking.md](cross_report_linking.md) (where the anchors come from),
[report_ui.md](report_ui.md#what-a-selection-file-records--schema-2) (the browser side).

---

## Do not hand-build the ids

Install the dependency-free distribution and use it:

```
pip install "snapchat-auto-selection @ git+https://github.com/dfjs1m/Snapchat_Auto.git@sel-v1.0.0#subdirectory=packages/snapchat_auto_selection"
```

```python
from snapchat_auto_selection import SelectionBuilder, validate

sel = SelectionBuilder()
sel.add_conversation("<client conversation id>")
sel.add_message("<client conversation id>", "12.0", ts=1700000000, sender="<user id>")
sel.add_memory("<ZSNAPID>", media_id="<ZMEDIAID>")
sel.add_cache_entry("<CACHE_KEY>")
sel.add_cached_file(sha256="<sha256 of the recovered content>", raw_sha256=["<per-copy raw sha256>"])
sel.set_relations("recommended")

assert validate(sel.to_payload()) == []
sel.write_json("selection.json")
```

If importing Python is not possible, the executable will build one from a flat list of items:

```
Snapchat_Auto --make-selection selection.json --items items.json [--relations <spec>]
```

where `items.json` is `[{"kind": "mem", "snap_id": "…"}, {"kind": "conv", "conversation_id": "…"}, …]`
— the same identifier names `add_*` takes. Then:

```
Snapchat_Auto --zip <extraction.zip> --keychain <file> --workdir <dir> --selection selection.json
```

Writing the JSON by hand is supported by this document, but the ids are not guessable from a single
example — see the message rule below — so `anchor_for()` and `validate()` exist to remove the guessing.

---

## Ask the installed build first

```
Snapchat_Auto --describe-selection-api
```

**Run this before writing a file.** Pinning a version of the package does not remove version mismatch,
it relocates it: the examiner's installed Snapchat Auto may read an older schema than the pinned package
writes. `describe()` on the executable reports what *that build* supports:

| field | meaning |
|---|---|
| `tool_version` | the installed build, `+build.<N>` included |
| `api_version` | the Python surface's version, independent of the file format |
| `schema_write` / `schema_min` / `schema_max` | the schema it writes, and the range it reads |
| `kinds` | each kind's report, id prefix, primary identifiers, alternates and a note |
| `relations`, `relation_presets`, `relation_switches`, `containment` | the `--relations` vocabulary |

Compare your `SCHEMA` against `schema_min`/`schema_max`. The relation vocabulary is only known to the
executable, which is why it is reported there and not by the package.

---

## The file

Two forms of one payload, and the difference matters:

| form | what it is |
|---|---|
| `selection.json` | plain JSON. **Use this one.** It is what the reports save by default and what the tool consumes. |
| `selection.js` | the same payload wrapped in `SCSel.preload(…)` — the drop-in a report auto-loads with `<script src>`. |

**A `.json` renamed to `.js` does not work and fails silently**: JSON at statement position is a syntax
error the browser discards without a word, leaving the reports open with nothing selected. Use
`write_js()`, or let the tool convert: `Snapchat_Auto --install-selection selection.json`.

Readers tolerate a UTF-8 BOM, CRLF, a leading comment block, and the `SCSel.preload(` wrapper.

### Top-level fields

```json
{"tool": "Snapchat_Auto", "schema": 2, "api_version": 1,
 "tool_version": "", "run_id": "", "sources": null, "exported": "",
 "relations": "recommended", "note": "",
 "selections": { … }}
```

| field | required | meaning |
|---|---|---|
| `tool` | recommended | must be `"Snapchat_Auto"` if present |
| `schema` | **yes** | the format version. Write `2`. |
| `selections` | **yes** | the rows, by kind — see below |
| `tool_version` | no | the build that produced the file. **Absent for an external tool**, and that is correct. |
| `run_id` | no | which report folder it was made in. Absent for an external tool. |
| `sources` | no | the source fingerprints of that run. Absent for an external tool. |
| `exported` | no | when it was saved, ISO 8601 |
| `relations` | no | a `--relations` spec recorded for the reader; the command line still wins |
| `note` | no | free text |
| `api_version` | no | informational |

**Absent provenance is not an error.** An external tool has no access to Snapchat Auto's fingerprints, so
a partial run built from such a file reports *"source verification not possible"* — never *"verified"* —
and states that in the report's provenance. It also means **nothing from an earlier run is reused**;
everything is re-derived from the evidence. That is the honest cost of an externally built selection, and
it is recorded rather than hidden.

### `selections`

`{kind: {row id: key record}}`. A key record is an object of the identifiers the row can be found by, or
the bare number `1` meaning "none were recorded" — in which case the row can only be matched on its
primary id.

| kind | report | id | key record fields |
|---|---|---|---|
| `conv` | Conversations | `conv-<client conversation id>` | `conv`, `server` |
| `msg` | Conversations | `conv-<client conversation id>\|msg-<server message id>` | `conv`, `smid`, `ts`, `sender` |
| `ct` | Contacts | `ct-<user id>` | `uid`, `user`, `conv` |
| `mem` | Memories | `mem-<ZSNAPID>` | `snap`, `mediaid`, `entry` |
| `cc` | cache_controller | `ck-<CACHE_KEY>` | `key`, `sha` |
| `cm` | Library/Caches | `cm-<sha256 of the recovered content>` | `sha`, `raw` (a **list**), `rel` |

#### The message rule

**A message id is qualified with its conversation.** `server_message_id` is a *per-conversation ordinal*
— `12.0` is message 12, part 0, and message 12 exists in nearly every chat — so a bare `msg-12.0` names a
different message in every conversation. `validate()` rejects an unqualified `msg` id for that reason.

Note the asymmetry: the anchor *inside a conversation page* is page-local (`#msg-12.0`), while the
**selection id** is qualified. Do not copy an anchor out of a URL.

#### Contacts

`ct` uses the first available of user id → username → conversation id, then `ct-unknown`, with every
character outside `[0-9A-Za-z_.:-]` replaced by `_`. `anchor_for("ct", …)` applies the same chain and the
same substitution. Supply every identifier you have: which one the report used depends on what it
resolved, and the alternates are what bridge the difference.

#### Library/Caches files

`cm-<sha256>` is hashed over the **recovered** content — after decoding or decrypting — not over the file
on disk, and rows are **merged by that content**. A build that decodes something an earlier one could not
therefore gives the same file a different id, and can merge two rows into one or split one into two. So:

* always supply `raw` — the SHA-256 of **each copy's bytes as stored**;
* supply `rel`, the path under `Library/Caches`, as a last resort;
* if an id now matches several rows the run **refuses** rather than picking one, because the row you
  ticked and the row you would get are not the same bytes.

---

## How a row is found in the run that consumes the file

Primary id first, then the recorded alternates in a fixed order. Every match records *how* it matched,
and that reaches the report's provenance and `partial_manifest.json`.

| kind | order |
|---|---|
| `mem` | snap id → `ZMEDIAID` → `ZENTRYID` |
| `cc` | cache key → SHA-256 of the stored bytes |
| `conv` | client conversation id → server conversation id |
| `msg` | conversation + server message id → conversation + timestamp + sender |
| `ct` | user id → username → conversation id |
| `cm` | recovered SHA-256 → any copy's raw SHA-256 → relative path |

An id matching **nothing** is named and the build **refuses** (`--unresolved drop` leaves it out and
lists it). An id matching **several rows** always refuses. Neither is silent: an extract quietly missing
evidence is worse than one that will not build.

---

## Stability promise

`SCHEMA` (the file) and `API_VERSION` (the Python surface) move independently.

* Within a schema, changes are **additive only**: new optional top-level fields, new kinds, new alternate
  key fields. An existing valid file stays valid.
* A reader ignores fields it does not know. Do the same.
* Anything that would invalidate an existing file bumps `SCHEMA`; `SCHEMA_MIN` stays as low as the build
  still reads, and `read_selection` keeps understanding the older form.
* `validate()` is the check to run before writing. It returns a list of human-readable problems; `[]`
  means valid.

Schema history: **1** — bare `msg-<n>` ids and a bare `1` per row. **2** (current) — message ids
qualified with their conversation, per-row key records, and the `sources` / `tool_version` provenance a
partial run verifies against. A schema-1 file is still read; its `msg` ids are quarantined rather than
guessed at, because promoting one would invent the conversation it never named.
