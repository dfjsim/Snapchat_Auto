# Progress and performance

A run on a large extraction takes a long time in places that used to say nothing while they worked:
extracting from a tens-of-GB ZIP, hashing it (`--hash-zip`), decrypting thousands of cached Memory
files, reading and decoding everything under `Library/Caches`, hashing every cached file, and cutting
thumbnails out of every cached video. The console then looks exactly like a crash. This page is about
how a run says where it is, and about the work done to make it shorter without changing what it
produces.

## Where a run is (`scripts/progress.py`)

The pipeline reports to one GUI-free module, which does not know who listens:

* **Stages.** `progress.stage(label)` brackets a stage, and stages nest: the run's own (*Extraction*,
  *SnapFixedVideos*, *Hashing the sources*, *Chats*, *Conversations*, *Contacts*, *Memories*,
  *Library/Caches*, *cache_controller*, *Index and search page*), and in a partial run each report's
  *index* and *render*.
* **Steps.** `progress.step(text, done, total)` says where inside a stage the run is — *decrypting
  SCContent media* 340 of 1,320, *hashing the extraction ZIP (MB)*, *retrieving from Snapchat's
  servers* 12 of 44. It sets three attributes and nothing more, so it is cheap enough for every
  iteration of a loop. `progress.Counter` does the same for work that several threads advance.
* **The heartbeat.** Between `start_run()` and `finish_run()` a thread watches the log: when nothing
  has been logged for `HEARTBEAT_S` (30) seconds it logs *… still working — Memories: decrypting
  SCContent media (340 of 1,320) — 2 min 10 s in this stage*, once per silence. Every log record
  counts as activity, so a talkative stage never hears from it.
* **The timing summary.** `finish_run()` logs how long each stage took, nested stages indented, and
  marks one an error stopped. It runs from `run()`'s `finally` too, so a run that failed still says
  where its time went — which is what an examiner's log from a real case needs to show for the slow
  part to be found.

With no run started the calls still work (they update the state nobody reads), so a report module
used on its own, and the tests, are unaffected. Nothing here touches a report.

## The run window (`scripts/run_window.py`)

A run started from the GUI no longer leaves the examiner with only the console. The window closes the
settings as before, and the run happens on a worker thread behind a window that shows, four times a
second:

* the **stages** done, each with its time (✗ for one an error stopped), and the one running;
* the **step** inside it with its count and a bar when the count has a total, and — once nothing has
  been logged for ten seconds — how long it has been, so a slow step reads as slow rather than dead;
* the **log** as it is written, with a count of warnings and errors;
* while a retrieval from Snapchat's servers runs, **its** progress, the request in flight or the wait
  and why, the pace with *Apply*, and *Pause / Resume / Stop*. The retrieval no longer opens a window
  of its own (`run_window.cloud_runner`): the engine runs on the run's thread, its events reach this
  window through a queue, and this window drives its `Control`. Its old window waited to be closed
  before the run went on; this one does not wait;
* **Skip thumbnails** while thumbnails are being cut (it stops every thumbnail pass left in the run);
* at the end, **Open report** and **Open folder**. Closing the window while the run is going asks
  first, then ends the program: a pipeline cannot be stopped half-way into a state worth continuing.

Only the window's thread touches the window. The pipeline never does — it reports to
`scripts/progress.py`, to the log, and to the retrieval's event queue — which is why the retrieval's
own window had to go: it was drawn by the thread that ran it. Whatever the run raised is handed back
to the caller (`run_in_window` returns it), `SystemExit` included, so a refused partial run or an
extraction without the app is still explained in a dialog and in the log. A retrieval for an existing
run folder (*Cloud download for an existing run…*) still uses its own progress window: no pipeline
runs there.

## The same work in less time

Measured first: a warm re-run of the test devices spent most of its time reading, hashing and
decoding cached files and cutting thumbnails, and a fresh run on Windows several times longer on the
same files — the first read of a file the extraction has just written waits on the antivirus. Both
overlap well when several files are in flight. What may not change is the report, so every change
below keeps the output **byte-identical** (checked on all four test devices with the byte-diff gate),
by one rule: the independent per-file work runs in parallel, and everything that decides a name,
de-duplicates or publishes stays in the original loop, in the original order.

* **`scripts/parallel.py` — `ordered_map`.** Runs a function over items on several threads and yields
  the results strictly in input order, with a bounded window so one slow early item never lets the
  rest pile up in memory. An exception surfaces at its item's place, as the loop would have raised it.
  Used for:
  * Library/Caches: reading, hashing, decoding and reading the metadata of every file
    (`cache_media_report._read_cache_file`); merging by content stays in walk order;
  * cache_controller: hashing every on-disk file and bundle child before the publishing loop, and
    reading the embedded metadata of what was published after it (`materialize_ondisk`);
  * Memories: locating, decrypting and hashing each Memory's SCContent media (`collect_media`'s
    `decode`); `_save_media`, which names a file after the first Memory to recover it, stays in the
    loop, in `addressed` order.
* **The SCContent listing** (`index_sccontent`) is taken once per run and handed out again while no
  folder of it has changed (a folder's modification time moves when a file is added, removed or
  renamed), and is read with `scandir`, which tells files from folders without a second system call
  per file.

### Thumbnails

Cutting a frame out of every cached video was the longest silence of all, and the slowest part on a
large case: one process, one video at a time, and roughly one video in six hangs the decoder until it
is killed (`scripts/data/poster_worker.py`). Now:

* **Several workers** (`default_workers`: one per core but one, at most four) each take a video at a
  time and are each still killed when theirs hangs. A video's result does not depend on which worker
  took it.
* **A thumbnail cache** in the run folder (`.thumbnail_cache/`, outside `Reports/`): a frame is kept
  under the SHA-256 of the video it came from (and whether the video is complete, which decides the
  frame), so the same bytes are decoded once per run folder — the Library/Caches and cache_controller
  reports often hold the same video, and a partial run or a refresh after a retrieval decodes nothing
  it already has. Only frames are kept; whether a video hangs is decided afresh by each run.
* **No time limit.** The fixed ten-minute cap per pass is gone: a pass runs to the end, the run
  window's *Skip thumbnails* stops it between files (`progress.request_skip("thumbnails")`), and a
  headless run can set `--thumbnail-minutes`. A video not reached is listed as *not attempted*, never
  as undecodable.
* **A lighter worker.** The worker imports `scripts/data/poster_frame.py` (OpenCV only) instead of a
  report module, and a packaged build answers `--poster-worker` before importing the GUI and the
  parsers — a worker restarts after every video that hangs, and each restart used to pay for both.

Not done: hashing the extraction ZIP (`--hash-zip`) alongside the rest. Its digest is part of the
source fingerprint that every report page embeds for the selection file, so the pages could not be
written until it is known; it now at least reports its progress in MB.
