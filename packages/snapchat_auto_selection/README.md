# snapchat-auto-selection

A **selection** names the rows of a [Snapchat Auto](https://github.com/dfjs1m/Snapchat_Auto) report that
a partial report should contain: particular conversations, messages, contacts, Memories and cached
files. An examiner normally produces one by ticking rows in the reports and pressing *Save selections*.
This distribution is for the other route — another tool producing one from identifiers it already has.

Stdlib only, no dependencies, its own version and tag namespace (`sel-v<N>`), so it can be pinned
without pinning the application.

```python
from snapchat_auto_selection import SelectionBuilder, validate

sel = SelectionBuilder()
sel.add_conversation("aaaa0000-0000-4000-8000-000000000001")
sel.add_message("aaaa0000-0000-4000-8000-000000000001", "12.0", ts=1700000000, sender="u-0001")
sel.add_memory("SNAP-0001", media_id="MEDIA-1")
sel.add_cache_entry("00000000000000000000000000000001")
sel.set_relations("recommended")

assert validate(sel.to_payload()) == []
sel.write_json("selection.json")
```

Then, on the machine that holds the evidence:

```
Snapchat_Auto --zip <extraction.zip> --keychain <file> --workdir <dir> --selection selection.json
```

## Do not hand-build the ids

`anchor_for()` exists because the spelling is not guessable. A message id is qualified with its
conversation — `conv-<id>|msg-12.0` — because a server message id is a *per-conversation ordinal*, so a
bare `msg-12.0` names a different message in every chat. Each `add_*` also records the alternate
identifiers that let a row be found again when the primary one moves between builds, which is what makes
a selection built here resolve exactly like one saved from the reports.

## Check before you write

```
Snapchat_Auto --describe-selection-api
```

Pinning a version of this package does not remove version mismatch, it relocates it: the examiner's
installed build may read an older schema than this one writes. `describe()` on the **executable**
reports what that build actually supports — the schema range, the kinds and their identifiers, the
relation vocabulary — so compare it against this package's `SCHEMA` first.

`SCHEMA` (the file format) and `API_VERSION` (this Python surface) move independently. Within a schema,
changes are additive only.

## Provenance is optional

A selection saved from the reports carries the source fingerprints of the run it was made in, and a
partial run verifies them. An external tool has no access to those, so `sources`, `run_id` and
`tool_version` may be absent — the partial run then reports *"source verification not possible"* rather
than refusing, and says so in the report's provenance. That difference is deliberate: it is recorded,
not hidden.

The normative description of the file is `docs/selection_format.md` in the main repository.
