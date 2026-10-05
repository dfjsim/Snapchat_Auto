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
