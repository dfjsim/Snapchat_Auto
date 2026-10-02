# Retrieving Memories media from Snapchat's servers

A Memory's row in `scdb-27.sqlite3` records where its media is stored: `ZGALLERYSNAP.ZMEDIADOWNLOADURL`
(and `ZOVERLAYDOWNLOADURL` for its overlay), with `ZMEDIAREDIRECTURI` / `ZOVERLAYREDIRECTURI` as an
alternative address. Requesting that address returns the media encrypted with the Memory's own AES
key — the key the device holds for it, which the Memories report already reads to decrypt the cached
copies. So for a Memory the device no longer holds, or holds only part of, the tool can ask for the
copy the device itself pointed at and decrypt it the same way.

That is the only thing it ever does with the network, and only when the examiner asks for it.

**Credit.** The method — request the recorded URL, decrypt with the Memory's key — is the one of
DFIR-HBG's [Snapchat_DownloadMemories_iOS](https://github.com/DFIR-HBG/Snapchat_DownloadMemories_iOS),
where John Hyla ([snoop168](https://github.com/snoop168)) added the overlay retrieval. That repository
carries no licence, so none of its code is used; the implementation here (`scripts/cloud_download.py`,
`scripts/cloud_memories.py`, `scripts/cloud_refresh.py`) is this project's own.

## The legal gate

Nothing is requested until the examiner has, for **that** request:

1. confirmed that they hold the legal authority to retrieve this data from Snapchat's servers, and
2. typed what that authority is — e.g. *Search warrant #… issued by … on …*, *Consent of <name>,
   <date>*. At least eight characters.

Neither is remembered between runs, by design: an authority belongs to one case. The engine
(`cloud_download.Engine`) refuses to start without both, whichever front end built the request. The note
is recorded with every request, shown on every page that shows a retrieved file, on the index of the
Memories report and on `index.html`, and — in a partial extract — in the banner on every page and in
`partial_manifest.json`.

## What can be retrieved

`cloud_memories.candidate` says for every Memory whether a copy from the servers could add anything:

| State | When |
|---|---|
| media missing | no full copy of the media was recovered from the device (at most a thumbnail, a low-resolution render or a generated poster) |
| local copy incomplete | the device holds only part of it: a partially cached file, a video with only a still, only the app's transcoded backup, or an overlay the row records but the device does not hold |
| no download address | one of those, but the row records no https address to ask for |
| retrieved | a copy was retrieved into this run folder |

The Memories index filters on it (*Snapchat's servers*), so an examiner sees what a retrieval would be
for before asking for one. What to retrieve is then chosen as **scopes**, any combination of:

* `missing` and `incomplete` — the states above;
* `selection` — the Memories in a selection file saved from the reports;
* `snaps` — snap ids pasted in. Every Memory page with something to gain has a **☁ Get from Snapchat's
  servers…** button that copies its snap id, and the index has **📋 Copy snap IDs** for every ticked
  Memory, one per line. The reports are static pages and cannot start a retrieval themselves.

A Memory with no usable key is left out (what came back could not be decrypted), and the count is
logged.

### The date filter

Date rules apply to **every** scope, pasted snap ids included — a warrant's date range is a limit, not a
suggestion. Each rule is a range on one or more timestamps of the Memory, with either end open, and is
either *include* or *exclude*. A Memory is retrieved when at least one include rule matches it (or there
is none) **and** no exclude rule does. A rule whose timestamps a Memory does not have does not match it;
the plan says, per rule, how many Memories in scope that was.

Timestamps a rule can name: every `ZGALLERYSNAP` and `ZGALLERYENTRY` time column the report lists
(`ZCAPTURETIMEUTC`, `ZCREATETIMEUTC`, `ZGALLERYENTRY.ZCREATETIMEUTC`, …), the MemData identifiers'
creation time (`MEMDATA`), or `*` for every one. Dates are entered in the run's report timezone, DST
included; a date alone is the start of that day for *from* and its last second for *to*. One range can be
applied to several timestamps at once (in the window: *Add for checked*, or *Copy range to…* on a row;
on the command line: a comma-separated list of timestamps, or `*`). The rules exactly as entered and
their UTC bounds are recorded with the request.

## How requests are made

* **Only the recorded addresses**: https only; the first request must go to a host under `sc-cdn.net`,
  the CDN every Memories URL column of the test extractions points at (`--cloud-allow-host` adds one);
  a redirect may lead elsewhere, but never to a private, loopback, link-local or reserved address — the
  URLs come from evidence, which could have been tampered with. `ZMEDIADOWNLOADURL` is tried first,
  then `ZMEDIAREDIRECTURI`.
* **Plainly**: a GET with no cookies, no credentials and no account token; nothing signs in. The
  User-Agent is `Snapchat_Auto/<version>`. A proxy set in the environment is used and recorded.
* **Paced**: one request at a time; a delay with random jitter between requests (default 4 s ± 2 s); at
  most so many requests in any minute (default 10); on 429 or 5xx, `Retry-After` when the server sends
  one (seconds or an HTTP date), else back-off doubling from 30 s up to 15 minutes, up to 3 attempts;
  403/404/410 move on to the next recorded address. The run stops after 5 transient failures in a row,
  or 25 refused addresses in a row. Every one of these can be changed while it runs — the window's
  pace fields, and Pause / Resume / Stop.

## What is kept, and where

```
<run folder>/CloudDownloads/
  README.txt
  cloud_manifest.jsonl          one JSON record per line, hash-chained
  encrypted/<snap id>/<role>-<n>.bin       the bytes exactly as received
  decrypted/<snap id>/<role>-<n>.<ext>     the same bytes decrypted with the Memory's key
```

Nothing there is ever overwritten; asking again writes `<role>-2…`. A file already retrieved is not
requested again unless the examiner asks (`--cloud-redownload yes`). Each line of the manifest carries
the SHA-256 of the line before it (`prev`), so an edit anywhere breaks the chain, and the report says so.

* `session` — when, the tool version, the authority (note, attested, when), the scopes and plan, the date
  rules, the pace, the User-Agent and proxy.
* `request` — per attempt: the snap id and role, why it was asked for, the URL column and URL, the final
  URL and every redirect, start and end (UTC), the HTTP status, the headers `Date`, `Content-Type`,
  `Content-Length`, `ETag`, `Last-Modified` (and a few cache headers), the size, MD5 and SHA-256 of what
  was received, and of what it decrypted to, with the decryption's result.
* `end` — the totals and why it stopped (*completed*, *examiner*, *failures*, *refused*).

## What the reports show

What comes back is **not device evidence**: it is what the server returned when asked. The Memories
report keeps it apart (`m["cloud_files"]`, never `media_files`, so the groups, hashes and states derived
from the device cannot change because of it) and publishes it under `media/cloud/`:

* the index: a **☁ CLOUD** badge, the filter, and a notice naming the authority;
* the Memory's page: a section *☁ Retrieved from Snapchat's servers — NOT device evidence* with each
  file, its hashes as received and decrypted, when and from which column it came, the HTTP status, the
  authority, and every **cache file on the device that is byte-identical to it**;
* `index.html`: a notice and a Sources row with the counts, dates and authorities.

The comparison is the point of a retrieval for linking: a plaintext cache file that no claim connects
to its Memory — a snap editor's working copy, say — is proven to be that Memory's media when its SHA-256
equals the decrypted server copy's. `cloud_memories.find_identical` compares sizes first and hashes only
same-size files: decrypted bytes against plaintext SCContent files (whole, or rebuilt from their
byte-range parts), and the bytes as received against the raw files. Matches go into
`Memories/media_by_content.json`, which the cache reports read to show the link from their side.

## Running it

**During a run** — the GUI's *Snapchat's servers* section, or on the command line:

```
Snapchat_Auto.exe --zip <extraction.zip> --keychain <file> \
    --cloud missing,incomplete --attest yes --authority "Search warrant 2026-1234, Court of …" \
    --cloud-dates "ZCAPTURETIMEUTC|2024-03-01|2024-03-31|include"
```

The retrieval happens while the Memories report is read, before anything is rendered.

**On a run folder that already exists** — *Cloud download for an existing run…* in the GUI, or:

```
Snapchat_Auto.exe --cloud-download <run folder> --cloud-snaps @ids.txt \
    --attest yes --authority "Consent of …, 2026-10-01"
```

Nothing is unzipped again. The reports the retrieval changes are refreshed in place — Memories, then
Library/Caches, then cache_controller (`--refresh targeted`, the default) — when this build wrote them,
and the whole pipeline re-runs otherwise (`--refresh full`): a targeted refresh would leave the folder
holding two builds' reports. The run's own settings come from `Reports/run_settings.json`. Saved
selections (`Reports/selection.js`) and the run id are kept; ticks not yet saved from an open report
are not — save first.

**Partial reports never contact the network.** A partial extract carries the retrieved media of the
Memories it contains, prunes the rest, and states the authority in its banner and provenance (the
`cloud` key of `partial_report.PROVENANCE_KEYS`).

## Failure modes

* An address that has expired or was revoked answers 403/404; the next recorded address is tried, and
  then the Memory is reported as failed. Nothing about the evidence changes.
* A body shorter than its `Content-Length` is retried; a body over 1 GB is refused.
* A server copy that does not decrypt to media is kept as received, with `decrypt.result` saying so.
* A second retrieval into the same run folder while one runs is refused (`CloudDownloads/.lock`).
