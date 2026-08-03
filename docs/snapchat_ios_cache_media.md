# Snapchat iOS `Library/Caches` media — what is there, and what indexes it

Findings for the media that lives under the Snapchat app container's
`Library/Caches`, separate from the Memories pipeline documented in
[snapchat_ios_memories_decryption.md](snapchat_ios_memories_decryption.md).

Short version: **the media at the `Library/Caches` root and in `Library/Caches/tmp` is
plaintext, needs no key, and attributes exactly** — these are **story snaps**, and
`cache_controller.db` maps them to plaintext copies in `SCContent`, yielding the snap id
*and* the owner username. The `tmp` filename UUID **is** the snap id; the root
`filtered-<UUID>` name is ephemeral but its content is byte-identical to the `tmp` copy, so
it attributes by hash. Verified 7/7 on the iOS 16 extraction.

`sccache.gallery-stories-snap.data` **is** encrypted, and is now solved: AES-256-CBC with a
key and fixed IV read from `Documents/ClientEncryptionService.plist` — **no keychain
required**. See [the solution](#solved--how-to-decrypt-sccachegallery-stories-snapdata).
That matters most on newer versions, where `SCContent` keeps only a JPEG still and the full
media exists solely inside the encrypted cache.

> **Implemented by `scripts/cache_media_report.py`** → `Reports/CacheMedia/`. See
> [report_cache_media.md](report_cache_media.md) for the report's internals and for the
> boundary with the cache_controller report.

## Verified against two extractions

| | Extraction A | Extraction B |
|---|---|---|
| Source | GrayKey full filesystem (iOS 16.2, Snapchat 12.19.1) | UFED full filesystem (iOS 26.2.1, Snapchat 14.14.0) |
| Media at `Caches` root | **7** `filtered-<UUID>.mp4` | **none** (only `backup.did`) |
| `Caches/tmp` | **7** `<UUID>~thumbnail-generation.mp4` | absent |
| `caching-media` packs | 97 | 195 |
| `com.toyopagroup.picaboo/Cache.db` | present, **0 rows** | **absent** |
| `SCPersistentMedia` | empty | 2 plaintext media files |

The two extractions run very different Snapchat versions, and the root-level
`filtered-*` artifact only appears in the older one. Treat its presence as
version-dependent and never assume it.

---

## Inventory by naming scheme

What matters for tooling is not the folder but **how the filename is formed**, because
that determines whether an index is needed at all.

| Location | Filename form | Self-describing? |
|---|---|---|
| `Caches/` root | `filtered-<UUID>.mp4` (prefix varies, may be absent) | no — UUID is ephemeral |
| `Caches/tmp/` | `<UUID>~thumbnail-generation.mp4` | no — UUID is ephemeral |
| `Caches/SCCache/com.pinterest.PINDiskCache.*` | URL-encoded CDN URL | **yes** — the name *is* the record |
| `Caches/global_scoped/sccache.*.data/` | URL-encoded CDN URL | **yes** |
| `Caches/user_scoped/` | mixed; some URL-encoded | partly |
| `Caches/caching-media/<hex64>/<hex64>-<n>.pack` | SHA-256-looking, opaque | no — use decrypt-and-match |
| `Caches/com.toyopagroup.picaboo/Cache.db` | standard `NSURLCache` SQLite | would be, but it is **empty/absent** |

`SCCache` uses PINCache (`com.pinterest.PINDiskCache.*`), whose disk keys are the
URL-encoded request keys — so for those caches the CDN URL is recoverable straight
from the filename, and it can be joined to `ZGALLERYSNAP.Z*DOWNLOADURL` the same way
`SCContent` cache keys are.

---

## The root / `tmp` media: no index exists, and none could

### The files

All seven at the root are **plaintext `ftypmp42` MP4** — directly playable, no key, no
decryption step.

### Root UUIDs are referenced nowhere; `tmp` UUIDs are

This distinction matters and is easy to get wrong.

- **Root `filtered-<UUID>.mp4`** — each UUID was searched across every file in the app
  container (2,670 files), **as ASCII text and as its 16-byte binary form**. Zero hits.
  None matches any of the 64 `ZGALLERYSNAP.ZSNAPID` values. These UUIDs are ephemeral.
- **`tmp/<UUID>~thumbnail-generation.mp4`** — every one of these UUIDs **does** appear
  elsewhere, as `sccache.unencrypted.stories.thumbnail/{carousel,large,small}-thumbnail-v2-<UUID>`
  and, for one, as `sccache.gallery-stories-snap.data/<UUID>`. So the `tmp` UUID is a
  real **story-snap identifier**, not scratch.

Practical consequence: `tmp` media *can* be tied to the stories caches by UUID; root
media cannot be tied to anything by name.

### Why the root UUID is per-copy, not per-snap

The seven root files and the seven `tmp` files are the **same seven videos**, byte for
byte, under **completely different UUIDs**:

| # | size | `Caches/` root | `Caches/tmp/` |
|---|---|---|---|
| 1 | 805,554 | `filtered-<UUID-A>.mp4` | `<UUID-B>~thumbnail-generation.mp4` |
| 2 | 3,950,904 | `filtered-<UUID-C>.mp4` | `<UUID-D>~thumbnail-generation.mp4` |
| 3 | 805,554 | … | … |
| 4 | 4,054,433 | … | … |
| 5 | 3,950,904 | … | … |
| 6 | 904,556 | … | … |
| 7 | 1,431,913 | … | … |

Each row is one distinct sha256; every root UUID differs from its `tmp` counterpart. Two rows share
a size (805,554) and two more share another (3,950,904) — the same clip re-encoded in a second
render pass, which is itself the behavioural signal described below.

Identical content, different name, in two places. The root UUID is a scratch identifier
minted when the file is written. The **prefix/suffix** identifies the producer
(`filtered-` = filter/render pass, `~thumbnail-generation` = thumbnail pass).

**Implication:** never present a *root* UUID as a snap id — it looks exactly like one and
will silently mislead. A `tmp` UUID may legitimately be joined to the stories caches.

---

## They are Stories, not Memories — and they attribute exactly

These are **story snaps**, not Memories, which is why the Memories key set never matched
them. `cache_controller.db` indexes them, and the same media is stored **in plaintext** in
`SCContent`. Attribution is therefore exact and deterministic — no heuristics needed.

**7 of 7** distinct videos in `Caches` root + `tmp` resolve to a snap id *and* an owner. (The
per-file table of hashes, snap ids and the owner username is deliberately not reproduced here — see
"Referring to test data" in `CLAUDE.md`. Each row was: one sha256, one size, the root
`filtered-<UUID>` name, the `tmp` `<UUID>~thumbnail-generation` name, the snap id the `tmp` UUID
resolves to, the owner username from the `<USERNAME>~<snapId>` claim, and the two byte-identical
plaintext `SCContent` copies.)

### Verified on the iOS 26 extraction too

The attribution link holds across the version gap: **12/12** UUID-named cache files resolve
to `cache_controller` claims. What differs is what sits on the other end:

| | 2023 (Snapchat 12.19.1) | 2026 (Snapchat 14.14.0) |
|---|---|---|
| Cache-side artifact | full-size MP4 in root + `tmp` | `bplist` thumbnails + encrypted `sccache` video |
| `SCContent` copy | **plaintext MP4**, byte-identical | **plaintext JPEG still** (142,560 / 96,958 B) |
| Video recoverable in plaintext? | **yes** | **no — only the encrypted `sccache` copy** |

That last row matters: on the iOS 26 device the story **video exists only inside
`sccache.gallery-stories-snap.data`**. `SCContent` holds a still image for those snaps.
So cracking that cache is not cosmetic for newer versions — it is the difference between
recovering a still and recovering the video.

Two further notes from the 2026 run:

- Many claims resolve to **zero-byte** `SCContent` files (evicted content, claim retained).
  Treat size 0 as "claimed but no longer on disk", not as an unreadable/encrypted file.
- `SCPersistentMedia`'s `cm-chat-media-video-1_<uuid>_…` matched **20 claims** for a single
  file, because the embedded UUID is a *conversation*-level identifier that many claims
  reference. UUID matching alone is too coarse there — join on the full `EXTERNAL_KEY`, not
  just an embedded UUID.

### The linking method

```
Caches/tmp/<UUID>~thumbnail-generation.mp4
    UUID  ->  cache_controller.CACHE_FILE_CLAIM.EXTERNAL_KEY
              (either "<USERNAME>~<snapId>"  [MEDIA_CONTEXT_TYPE 3]
                   or "<snapId>"             [MEDIA_CONTEXT_TYPE 4])
    ->  CACHE_KEY  ->  Documents/com.snap.file_manager_*_SCContent_*/<CACHE_KEY>
    ->  PLAINTEXT mp4/jpeg, byte-identical to the cache copy
```

The root `filtered-<UUID>.mp4` has **no claim of its own** — its UUID is ephemeral — but it
is byte-identical to the `tmp` copy, so it attributes transitively **by content hash**.

Two useful consequences:

- **The owner username is recoverable** (`EXTERNAL_KEY` type 3 carries `<USERNAME>~<snapId>`),
  which the filename alone never gives.
- **No decryption is required** for any of this media.

### Correction to an earlier result

An earlier pass reported "0 exact matches" against Memories. That was an artefact of the
method, not a fact: the scan only kept `SCContent` files that *decrypted* with a per-snap
Memories key, so files already stored in plaintext were skipped before comparison. Once
plaintext files are included, the match rate is **14 byte-identical copies / 7 of 7 videos**.
Any future linker must hash `SCContent` files **as-is** in addition to attempting decryption.

### Are unprefixed filenames an exact match?

Where testable, a bare filename is not automatically the original:
`user_scoped/<userHash>/recorded_videos/recorded-<id>.mp4` is plaintext and matches decrypted
Memories snap `F51E43F8` at **exactly** 2,025,242 bytes, yet differs from **offset 30 onward**
(2,017,190 bytes differ) — same length, same `ftyp` header, different payload. The bare-UUID
files in `sccache.gallery-stories-snap.data/` are encrypted, so a raw hash cannot match by
construction; but their content is recoverable in plaintext from `SCContent` anyway.

---

## The `sccache.*` caches: which are encrypted, and how

`sccache.*` directories are **PINCache** disk caches, created through the app's cache-utility
class in both an unscoped and a per-user-scoped form.

That utility's default serializer/deserializer pair does **no encryption** — the deserializer is a
plain `NSKeyedUnarchiver`. That matches what is on disk:

| Cache | On-disk head | Format |
|---|---|---|
| `sccache.unencrypted.stories.thumbnail/*` | `bplist00` | `NSKeyedArchiver` plist wrapping the image — **plaintext**, the name is accurate |
| `sccache.nyc-impala/*` | `TSAF` | Snap's TSAF container — plaintext |
| `sccache.dynamic-caption.data/*` | `0x00010000` (sfnt) | TrueType **fonts**, not media |
| `sccache.gallery-stories-snap.data/*` | random-looking | **encrypted** |

> Sniff by magic bytes, not by directory name. The thumbnails are `bplist00`-wrapped, so a
> naive `ftyp`/JPEG check misses them entirely — they must be unarchived first.

### SOLVED — how to decrypt `sccache.gallery-stories-snap.data`

```python
# key material: Documents/ClientEncryptionService.plist  (Snap "TSAF" container)
#   SCClientEncryption { identifier, encryption_key (base64, 32 B),
#                        initialization_vector (base64, 16 B) }
key = base64.b64decode(encryption_key)        # AES-256
iv  = base64.b64decode(initialization_vector) # FIXED per install

plain = unpad_pkcs7(AES.new(key, AES.MODE_CBC, iv).decrypt(blob[:len(blob)//16*16]))
# plain is an NSKeyedArchiver bplist; the media is the largest data blob in $objects
media = max((o for o in plistlib.loads(plain)["$objects"]
             if isinstance(o, (bytes, bytearray))), key=len)
```

Ciphertext starts at **offset 0** — there is no header. The IV is fixed per install, which
is exactly why sibling files shared a constant prefix; the 80 identical bytes are simply the
common `bplist00` header of the archived object, **not** wrapped key material as first
suspected.

**Verified end-to-end on both extractions** (5/5 entries, valid magic bytes):

| Extraction | recovered |
|---|---|
| iOS 16 | PNG 2,339,946 B |
| iOS 16 | PNG 3,057,783 B |
| iOS 16 | **MP4 2,300,560 B** |
| iOS 26 | PNG 1,880,663 B |
| iOS 26 | PNG 2,647,012 B |

Key shape observed, on both extractions:

```
key  <32 bytes, hex>      # AES-256
iv   <16 bytes, hex>
```

The values are **per install** and are deliberately not recorded here: they are live decryption
keys for a real device's cached media, they are never reused across devices, and the finding — a
32-byte key plus a 16-byte IV, held in the app container — is what reproduces the method. Read them
from the container on the extraction at hand.

**No keychain required** — the key lives in the app container, so this works on any
extraction that captured `Documents/`, including ones without a full-filesystem keychain.

#### How the key was located

Static analysis of the app binary shows this cache is written through a story-media cache class
that hands its payload to an *encryptor* object, and that the encryptor's `encryptionKey` and
`initializationVector` properties are the values used. Those two property names are exactly the
fields persisted in `ClientEncryptionService.plist`, which is what makes the file readable without
a keychain.

Function addresses are deliberately not recorded here: they are valid only for one specific app
build and are useless — or misleading — against any other. The check that matters is reproducible
from the artifact alone: read the two fields, decrypt, and confirm the result carries valid media
magic bytes.

#### Scope of the key — the sibling caches are not encrypted at all

Only `sccache.gallery-stories-snap.data` is encrypted. The others each have their own
plaintext format, so sniff the magic bytes rather than assuming:

| Cache | Format | Handling |
|---|---|---|
| `sccache.gallery-stories-snap.data` | AES-256-CBC → `bplist00` | decrypt (above), then unarchive |
| `sccache.unencrypted.stories.thumbnail` | `bplist00` | unarchive only — the name is accurate |
| `sccache.dynamic-caption.data` | **TrueType fonts** (`sfnt`, `0x00010000`) | not media — see below |
| `sccache.nyc-impala/*` | Snap `TSAF` container | plaintext |

`dynamic-caption.data` looked encrypted at a glance but is a **font cache**: each entry is a
valid TrueType file (tables `GDEF/GPOS/GSUB/cmap/glyf/name`) downloaded per caption style and
keyed by its CDN URL. On the iOS 16 device: *Teko* (282,904 B), *Staatliches* (61,400 B),
*Pirata One* (54,276 B), *Special Elite* (151,068 B). Read the `name` table to identify one.

These are not evidentiary media and should be excluded from a media report — though the
URL-keyed filename is still a record that the style was fetched, if that ever matters.

### Measured properties of the ciphertext

These were the observations that led to the solution, kept because they are the fingerprint
to look for if the scheme changes:

| Property | Value | Implication |
|---|---|---|
| Length | always a multiple of **16** | block cipher, no stream mode |
| Entropy | 7.999 bits/byte | genuinely encrypted, not merely wrapped |
| Duplicate 16-byte blocks | **0** across multi-MB files | **not ECB** |
| Shared prefix between siblings | **exactly 80 bytes**, in *both* installs | fixed 80-byte header, not a coincidental plaintext overlap |
| Header value | constant per install, differs across installs | header is derived from a per-install secret |

The constant prefix was the decisive clue, though it initially pointed the wrong way: it is
the shared `bplist00` header of the archived object under a **fixed IV**, not a fixed-size
header. Both readings predict a constant prefix; only the fixed-IV one is correct.

Why the obvious key candidates all failed: **the key is not in the keychain.** Every
16/24/32-byte value in the picaboo access group (plus SHA-256 and hex-decoded variants), all
86 per-snap `snap_key_iv` keys, `egocipher`, `SCShareAuthKey`, `notificationEncryptionKey`
and `EncryptedDiskCacheKey` were tested at offsets 0/16/32/48/64/80/96 in CBC and ECB — 56
candidates, no hits. The key lives in `Documents/ClientEncryptionService.plist` instead.

The cache utility's default serializer/deserializer pair is the **plain** `NSKeyedArchiver` one —
not the encrypted path.

### Do not confuse this with `EncryptedDiskCacheKey`

There is a separate Valdi subsystem whose key **is** in the keychain:

| | |
|---|---|
| Keychain item | `acct=EncryptedDiskCacheKey`, `svce=VALDI`, group `<TEAMID>.com.toyopagroup.picaboo` |
| Key | 16 bytes (AES-128), generated and persisted on first use if absent |
| Wire format | first **12 bytes** are a header consumed by the decryptor; ciphertext follows (`len - 12`) |
| Error string | `"Failed to derypt data"` (Snapchat's typo — useful to grep for) |
| Present in | iOS 26 extraction only; **absent** from the iOS 16 device's keychain |

This key does **not** decrypt `sccache.gallery-stories-snap.data` in any tested layout
(GCM with tag at either end, CTR, CBC, ECB). It is a different cache subsystem.

**Next step if this is pursued:** identify the encrypted serializer variant installed for this
cache — the counterpart to the plain archiver pair — and establish where its key comes from.

---

## Independent value: these files date themselves

With no database, the file's own container metadata is the evidence. Every one of these
MP4s carries an `mvhd` atom with creation/modification time and duration:

```
20:24:50  9.74s     20:24:51  1.97s        <- first render pass
20:25:12  9.74s     20:25:13  1.97s        <- second pass, same two clips, re-encoded
20:36:08  9.98s     20:36:09  2.21s
20:38:45  3.54s
```

The 22-second offset between two passes over the same pair of clips is a usable
behavioural signal (preview render followed by send/export render). `mvhd` times are
specified as UTC; Apple encoders are not always faithful to that, so corroborate
against filesystem timestamps before stating a timezone in a report.

Note these files can survive independently of Memories: they are renders produced during
editing/sending, so they may exist for content that was **never saved to Memories** —
which is precisely what makes them worth reporting.

---

## Report scope — avoid double-reporting

Several of these locations are **already covered by existing reports**. Decide the
boundary before implementing, or the same media will appear two or three times with
different identifiers.

| Location | Already covered by | Action for the new cache report |
|---|---|---|
| `Documents/com.snap.file_manager_*_SCContent_*` | **cache_controller report** (indexed in `cache_controller.db` via `CACHE_FILE_CLAIM.EXTERNAL_KEY`) | **exclude** — or extend the cache_controller report instead |
| `Library/Caches/SCPersistentMedia` (`cm-*`) | **conversations report** (media are linked there) | **include** — not covered by `cache_controller.db` |
| `Library/Caches/` root, `Caches/tmp` | nothing | **include** |
| `Library/Caches/caching-media` | Memories report (decrypt-and-match) | exclude, or cross-reference |
| `sccache.*` plaintext caches | nothing | include |

Two workable designs:

1. **Extend the cache_controller report** to enumerate every cache location, keeping one
   place that answers "what media is cached on this device"; the new report then only
   adds locations that index cannot reach.
2. **Keep the new report strictly complementary** — everything under `Library/Caches`
   *except* what `cache_controller.db` already claims, plus `SCPersistentMedia`.

Either is fine, but pick one explicitly and state it in the report header, because
"cached media" otherwise means something different in each report.

`SCPersistentMedia` is the important gap: its `cm-chat-media-video-1_<uuid>_<n>_0_0.mov`
files are linked from the conversations report but appear in **no** cache index. Note the
embedded UUID is a **v5 (name-based)** UUID, i.e. deterministic — which is why those files
link cleanly, unlike the random v4 UUIDs at the cache root.

---

## Recommendations for `Snapchat_Auto`

A new report for cache media, kept separate from the Memories report.

1. **Collect, do not join.** Enumerate media under `Library/Caches` by content sniffing
   (`ftyp` / `\xFF\xD8\xFF` / `\x89PNG` / `bplist00`), not by extension or name. Recover
   everything plaintext; never present a root-level filename UUID as an identifier.
2. **Record the producer tag.** Parse the prefix/suffix (`filtered-`,
   `~thumbnail-generation`, none) into a `producer` column — it is the only meaningful
   part of the name, and it explains what the file is.
3. **Deduplicate by sha256** across the root and `tmp` (and any future location). In
   Extraction A this collapses 14 files to 7 distinct videos; reporting 14 would
   overstate the content.
4. **Extract `mvhd`** creation/modification time and duration for every MP4; these are
   the only timestamps that are not filesystem-derived.
5. **Correlate, and label it as correlation.** Offer an optional pass matching cache
   media to Memories snaps on (duration ±0.05 s, `mvhd` time within a few minutes,
   size within ~1%). Emit a confidence and the evidence used. Do **not** merge these
   rows into the Memories report.
6. **Handle the self-describing caches separately.** For `SCCache` and
   `global_scoped`/`user_scoped`, URL-decode the filename to recover the CDN URL, then
   reuse the existing `SHA256(token)[:16]` logic to join to `ZGALLERYSNAP` — those
   *can* be linked properly, unlike the root media.
7. **Expect absence.** Extraction B has no root media at all. Empty results are the
   normal case on newer versions and must not be reported as an error.
8. **Attribute through `cache_controller.db`, exactly.** For every UUID-named cache file,
   look the UUID up in `CACHE_FILE_CLAIM.EXTERNAL_KEY` and follow `CACHE_KEY` to the
   `SCContent` file. Verify by sha256 and report the snap id and the **owner username**
   (from the `<USERNAME>~<snapId>` form). Fall back to content-hash equality for files
   whose own UUID has no claim (the root `filtered-*` set). This is exact — prefer it over
   the duration/size correlation described above, which is only a last resort.
9. **Hash `SCContent` files as-is, too.** Some are stored plaintext. A linker that only
   hashes successfully-decrypted output will silently miss every story snap.
10. **Decrypt `sccache.gallery-stories-snap.data`** with the key/IV from
    `Documents/ClientEncryptionService.plist`, then unarchive the resulting `bplist00` and
    take the largest data blob as the media. This needs no keychain, so it works on
    filesystem-only extractions — and on newer versions it is the *only* way to recover the
    full-resolution media. Read the key per extraction; it is per-install.

### Known limits / open items

- `sccache.gallery-stories-snap.data` is **solved** (see above) and should be decrypted in
  the report, not listed as opaque. It is the only source for the full media on newer
  versions, where `SCContent` keeps just a still.
- Sibling caches are **not** covered by that key, and none of them are encrypted:
  `sccache.dynamic-caption.data` is a **TrueType font cache**, `sccache.nyc-impala/*` is
  plaintext `TSAF`, and `sccache.unencrypted.stories.thumbnail` is `bplist00`. Treat each
  `sccache.*` directory as its own format; sniff before assuming.
- `caching-media` remains opaque; the existing decrypt-and-match linker is still the
  only way in, and it needs the keychain.
- The iOS 16 device's keychain has **no** `EncryptedDiskCacheKey` at all, so any Valdi encrypted
  cache on that device is out of reach regardless.
- Findings are from two devices. The `filtered-` prefix, the `~thumbnail-generation`
  suffix and the root location are all version-dependent and were **not** observed on
  the iOS 26 extraction.

---

## Reproducing

- Root/`tmp` media: sniff magic bytes, hash, read `mvhd`. No keys required.
- Attribution: open `Documents/global_scoped/cachecontroller/cache_controller.db` **with its
  `-wal`**, read `CACHE_FILE_CLAIM (CACHE_KEY, EXTERNAL_KEY, MEDIA_CONTEXT_TYPE)`, extract
  the UUID from `EXTERNAL_KEY`, and resolve `CACHE_KEY` against the `SCContent` folders.
  Confirm with sha256 against the cache copy.
- Memories side (for correlation): recover `egocipher` from the keychain, decrypt
  `gallery.encrypteddb` (SQLCipher, `cipher_compatibility = 3`), read `snap_key_iv`,
  then AES-256-CBC decrypt `SCContent` files with each snap's key/iv and strip PKCS#7 —
  as in [snapchat_ios_memories_decryption.md](snapchat_ios_memories_decryption.md).
- The scan that proved nothing references the root UUIDs: walk every file in the container
  and search for each UUID both as ASCII and as `uuid.UUID(u).bytes`.
- `sccache` decryption: read `Documents/ClientEncryptionService.plist`, base64-decode the
  32-byte `encryption_key` and 16-byte `initialization_vector`, AES-256-CBC from offset 0,
  strip PKCS#7, `plistlib.loads` the result and take the largest `$objects` data blob.

---

## Addendum — findings from implementing this (2026-08-01)

Corrections and additions from building `scripts/cache_media_report.py` and running it against
every Snapchat-bearing extraction in the corpus.

### The corpus is smaller than it looks

Of the 9 iOS ZIPs in the test corpus, **only 4 contain Snapchat**: two GrayKey full-filesystem, one
UFED full-filesystem and one **UFED AFU** (which this document did not previously cover). A `7z l -r <zip> "*SCContent*"` test that checks `$LASTEXITCODE` reports **every**
archive as a hit — `7z l` exits 0 whether or not the filter matched, including on Android keystore
ZIPs. Test the listing output, not the exit code.

### A third root naming scheme: bare UUIDs

The AFU device has root media named with a **bare UUID and no prefix at all** — 5 `.mp4`
and 6 `.jpg`. So the root is not always video, and "producer: none" is a normal result rather than a
parse failure. The five MP4s share one MD5, which is why the report collapses them to a single row
with six copies.

### `SCPersistentMedia` names are colon-separated on the device

`cm-chat-media-video-1:19e0693c-…:12:0:0.mov`. The underscore spelling exists only because
extraction rewrites `:` (Windows cannot hold it in a filename). Any code matching these names must
accept **both** separators, or a run over a folder it did not extract itself silently produces zero
saved-media links.

### The URL-keyed caches were never extracted on Windows

Their filenames carry the CDN query string's `?`, which Windows rejects; the write failed inside a
bare `except: pass`, silently dropping exactly the files whose name *is* their provenance (14 on the
iOS 16 device). They are now percent-encoded on disk, with `extraction_manifest.json` recording the
exact on-device name.

### `cronet` — a store this document missed

Snapchat embeds Chromium's network stack. Two artifacts, neither indexed by any Snapchat database:

* `cronet/prefs/local_prefs.json` — `net.host_cache`: **hostname → resolved IPs** with expiries
  (Chromium's 1601 epoch, microseconds). Present on every 2025/iOS 26 device, in **two** copies
  (`Library/Caches/cronet/` and `Documents/user_scoped/<hash>/cronet/`).
* `cronet/disk_cache/` — a Chromium **blockfile HTTP cache** (on the AFU device: 48 entries, 32 MB).
  Its `EntryStore` records join each cached request URL to the `f_XXXXXX` file holding the response
  body. Bodies seen: JPEG, zstd-compressed **Snapchat resource bundles** (which decompress with
  Python 3.14's stdlib `compression.zstd` and hold named members such as
  `res/theme_lightpurple_background.png` — 105 distinct member names on that device),
  Snapchat **protobuf API responses** (one referencing `memories_tagging`) and `LZC` lens
  bundles up to 7.4 MB. The URLs include
  `cf-st.sc-cdn.net/d/<token>`, the same media token that joins to a Memory's download URL.
  **Note the key offset is 100, not the classic 96, in this Cronet build.**

### `Cache.db` is present but empty everywhere

`Library/Caches/com.toyopagroup.picaboo/Cache.db` is a standard `NSURLCache`
(`cfurl_cache_response` / `cfurl_cache_blob_data` / `cfurl_cache_receiver_data`). It exists on the
iOS 16 and one iOS 26 device — the latter with a 105 KB `-wal` — but holds **zero entries** on both.
Report it as "present, 0 entries" so an empty result is distinguishable from one never looked at.

### Reliably empty, so they need no re-investigation

`gallery/1/<userHash>` (an empty directory on every device), `SCMediaCache/*` (a 42-byte empty
bplist), `backup.did` (10 bytes), `KSCrash/…/Reports` and `ConsoleLog.txt`.

### Also worth knowing

`KSCrash/Snapchat/Data/CrashState.json` carries app-session history (launches and sessions since the
last crash, time active, the previous session id). And **outside `Library/Caches`**,
`Library/Application Support/com.toyopagroup.picaboo/.mapbox/cache.db` is a Mapbox **map-tile
cache** — tiles Snap Map fetched — which no report currently touches.
