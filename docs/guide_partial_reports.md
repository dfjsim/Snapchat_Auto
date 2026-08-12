# Handing over part of a report — a short guide

A **partial report** is a folder holding only the rows you chose, plus the related items you asked for.
It is built by the same pipeline as the full report, from the same evidence, into its own
`Reports_partial_<stamp>/` folder. **The full reports are never touched.**

This guide is the whole workflow, in order. Developer detail lives in
[report_partial.md](report_partial.md); the file format another tool writes is in
[selection_format.md](selection_format.md).

---

## 1. Run the full report first

```
Snapchat_Auto.py --zip <extraction.zip> --keychain <keychain.plist>
```

You need it for two reasons: it is where you tick rows and review, and a partial run reads the
cross-report links from it.

## 2. Choose the rows

**In the reports.** Tick the checkbox on any row — conversations, messages, contacts, Memories, cached
files. The toolbar shows how many you have chosen. When you are done, press
**💾 Save selections (.json)** and keep `selection.json` with the case.

> Ticks live in your browser until you save them. Use *Save selections*; do not rely on the browser.

**Or from another tool.** A selection can also be produced outside Snapchat Auto — for example from a
Cellebrite PA/Reader case — as long as it follows [selection_format.md](selection_format.md). Such a
file carries no fingerprints of our own, so the extract will state that it could **not** verify that
this is the evidence the selection was made from. That is expected, and it is recorded rather than
hidden.

## 3. Expand and check *before* you build

The relations bring in more than you ticked: the Memories grouped with a selected one, the cache entry
behind a message's media, the sender's contact record. Look at that **before** producing the extract.

*In the GUI:* put the selection file in **Selection file**, choose **Related items…**, tick
**Expand and check first**, and run. Nothing is built.

*On the command line:*

```
Snapchat_Auto.py --zip <extraction.zip> --keychain <keychain.plist> \
    --selection selection.json --links-dir <run>/Reports \
    --relations recommended --expand-selection expanded.json
```

Two things come out of it:

* **a listing in the log** — every row that would be added, and why;
* **`expanded.json`** — the whole extract as a selection file: your own ticks *and* everything that
  came with them.

## 4. Review it in the full report

Load `expanded.json` into the full report — the **Load…** button in any report's toolbar, or:

```
Snapchat_Auto.py --install-selection expanded.json --report-dir <run>/Reports
```

Now every row that would be in the extract is ticked, and the ones **you did not choose** are shaded
with a bar down the left. Hover one to see why it came in. The toolbar counts them separately
(*"12 selected · 47 pulled in"*), and the **Show** dropdown narrows the table to `selected only` or
`pulled in only`.

Untick anything that should not go out, then **💾 Save selections (.json)** again — say, `final.json`.

## 5. Build the extract

*In the GUI:* point **Selection file** at `final.json`, untick **Expand and check first**, add your
**Case / exhibit reference**, and run.

*On the command line:*

```
Snapchat_Auto.py --zip <extraction.zip> --keychain <keychain.plist> \
    --selection final.json --links-dir <run>/Reports --case-ref "<exhibit>"
```

**You do not need to change the relations for this build.** A file that came out of step 3 says so
itself, and the run then adds nothing further — its ticks are already the full list. (If you *do* pass
`--relations`, the run warns you: following the relations again would expand what you just reviewed a
second time, and the extract would hold more than you approved.)

## 6. What the extract says about itself

* a **PARTIAL REPORT** banner on every page, including conversation pages and Memory sub-pages;
* **"3 of 412"** figures next to each report's own counts;
* rows you did not choose, **marked** as such — with the reason;
* every cross-reference to something *not* in the extract shown in place as `⃠ not in this partial
  report`, so a missing association is visible rather than silently absent;
* a **provenance** block: the tool version, your case reference, the selection's identity, whether the
  evidence verified, which relations were followed, and whether the selection was a reviewed expansion;
* `partial_manifest.json` — the same thing as data, including every row's reason.

---

## If it refuses

That is deliberate: an extract quietly missing evidence, or built from the wrong data, is worse than
one that will not build. Each of these prints what it found.

| it says | what to do |
|---|---|
| rows do not exist in this run | Check this is the extraction the selection was made from. `--unresolved drop` leaves them out and lists them. |
| a row is ambiguous | Re-tick it in this run's reports and save again. |
| the source artifacts differ | You may have a better extraction of the same device. `--sources-mismatch proceed` builds it and states the difference on every page. |
| a different build produced the selection | `--version-mismatch resolve` re-derives everything and re-resolves your ticks. |

## Two things worth knowing

**Ticking a conversation does not disclose its messages.** It gives you the conversation, its
participants and its detail page. Messages are ticked individually — or switch on *Every message of a
selected conversation* in **Related items…**.

**Leave *"Keep following the relations"* off unless you mean it.** It follows only the relations you
ticked, but those already form a loop (a message → its cache entry → that entry's Memory → that
Memory's other entries → their messages), so it grows with the case rather than with your selection.
