# Decrypting & linking Snapchat iOS Memories media (`.pack`, `SCContent`, geolocation)

End-to-end artifact-analysis notes for recovering **Snapchat Memories** media from an iOS
extraction and linking every media file back to its Memory row in `scdb-27.sqlite3`,
including geolocation.

Covers both cache locations:

- `Documents/com.snap.file_manager_*_SCContent_*/` — the **SCContent** cache (thumbnails and,
  when present, full-resolution stills). The existing scripts handle part of this.
- `Library/Caches/caching-media/**/*.pack` — the **caching-media** cache. **Not handled by the
  current scripts.** Magnet AXIOM decrypts *and* links these; Cellebrite PA decrypts but does
  not link. The method below reproduces the linking.

…and both Snapchat storage schemas we observed (see [Two schemas](#two-schemas-where-the-keys-live)).

> Other media also lives under `Library/Caches`: plaintext MP4s at the cache root and in
> `Library/Caches/tmp`, plus the URL-keyed `SCCache` / `global_scoped` caches. That media is
> **not** part of the Memories pipeline, needs no keys, and nothing on the device indexes it —
> see [snapchat_ios_cache_media.md](snapchat_ios_cache_media.md).

## Verified against two real iOS extractions

| | Device A | Device B |
|---|---|---|
| Extraction | UFED Full-Filesystem (AFU) | GrayKey Full-Filesystem |
| Schema | **new** — keys in `scdb-27.ZENCRYPTION` | **old** — keys in `gallery.encrypteddb` |
| Keychain in extraction | limited (no `egocipher`) | full (`egocipher` present) |
| User profiles | 1 | 2 |
| Memories (`ZGALLERYSNAP`) total | 80 | 80 (46 + 34) |
| Memories with recovered media | 80 | 79 |
| Videos (`.mp4`) recovered | 2 | 14 |
| Geolocation recovered | 0 (needs its keychain) | **72** |
| My Eyes Only memories | 0 | 1 (needs `persistedkey`, absent) |

Device B is the important addition: with the full keychain we decrypted the SQLCipher
`gallery.encrypteddb`, recovered per-snap keys **and** GPS coordinates, and decrypted the
`.pack` previews, the full-resolution `SCContent` stills, **and the videos** — across **both**
user profiles on the device.

---

## TL;DR recipe

1. **Read the keychain** (`read_keychain_status` in `scripts/DecryptLocalMemories_iOS.py`, or
   `readKeychain` for just the two keys). You may get `egocipher` (Memories DB key) and/or
   `persistedkey` (My Eyes Only master key). See
   [When is the keychain required?](#when-is-the-full-filesystem-keychain-required) and
   [Diagnosing a keychain](#diagnosing-a-keychain).
2. **Get the per-Memory AES `KEY`/`IV`:**
   - **New schema:** decode `ZGALLERYSNAP.ZENCRYPTION` (a `SCMemoriesSnapEncryption`
     NSKeyedArchiver bplist). No keychain needed for regular memories. If `KEY` is 48 bytes and
     `IV` is 32 the memory is My Eyes Only and the pair is wrapped — unwrap it with that account's
     `persistedkey` before use.
   - **Old schema:** decrypt `gallery.encrypteddb` (SQLCipher, key = `egocipher`) and read the
     `snap_key_iv` table. **Keychain required.**
3. **Geolocation:** always from `gallery.encrypteddb` → `snap_location_table`
   (`snap_id, latitude, longitude`). **Keychain required in both schemas.**
4. **Decrypt SCContent media:** file name = `SHA256(token)[:16 bytes]` (32 hex) where `token`
   is the last path segment of the media/overlay/thumbnail CDN URL from `scdb-27`. Decrypt with
   the snap's `KEY`/`IV` (AES-256-CBC).
5. **Decrypt & link caching-media `.pack`:** the pack names are opaque, so **link by
   decrypt-and-match** — try each memory's `KEY`/`IV` against a folder's first item; the key
   that yields valid media magic bytes identifies the Memory. Then decrypt every item in that
   folder and strip the 8-byte header (below).

```python
from Crypto.Cipher import AES
# ciphertext = concatenated <itemHash>-0.pack, <itemHash>-1.pack, ... in order
n = len(ciphertext) - (len(ciphertext) % 16)
plain = AES.new(KEY, AES.MODE_CBC, IV).decrypt(ciphertext[:n])
assert plain[:4] == b"\x01\x00\x00\x00"          # header marker (both schemas)
length = int.from_bytes(plain[4:8], "little")     # payload length, strips CBC padding
media  = plain[8:8+length]                         # FF D8 FF … (JPEG), etc.
```

---

## On-disk layout

App container: `…/Containers/Data/Application/<APP-UUID>/`. Paths seen on both devices:

| What | Path (under the app container) |
|---|---|
| Memory metadata DB | `Documents/gallery_data_object/1/<userHash>/scdb-27.sqlite3` (+ `-wal`,`-shm`,`.mom`) |
| Encrypted gallery DB | `Documents/gallery_encrypted_db/3/<userHash>/gallery.encrypteddb` (SQLCipher; data often in `-wal`) |
| Cache index (SCContent) | `Documents/global_scoped/cachecontroller/cache_controller.db` |
| SCContent media files | `Documents/com.snap.file_manager_3_SCContent_<userId>/<CACHE_KEY>` |
| **caching-media packs** | `Library/Caches/caching-media/<folderHash>/<itemHash>-<chunk>.pack` |
| Logged-in user id | `Documents/user.plist` |

`<userHash>` is `SHA256(userId)`. A device can hold **multiple profiles** (one `<userHash>`
each). Always open SQLite read-only **with the `-wal`/`-shm` siblings present** so recent rows
are visible.

### Multiple Snapchat users — yes, both can be decrypted

A single `egocipher` keychain item (`egocipher.key.avoidkeyderivation`) exists per **app
install**, not per user, and it decrypts **every** profile's `gallery.encrypteddb` on that
device. Verified on Device B, which has two profiles:

| Profile `<userHash>` | userId | Memories | Decrypts with the one egocipher? |
|---|---|---|---|
| profile 1 | the active account | 34 | ✅ 35 keys / 35 locations |
| profile 2 | the second account | 46 | ✅ 51 keys / 43 locations |

Process **each** profile folder under `gallery_data_object/1/*` and `gallery_encrypted_db/3/*`:
pair each `scdb-27` with the `gallery.encrypteddb` of the same `<userHash>`, decrypt both with
the shared egocipher, and merge all profiles' `snap_key_iv` keys into one set. `SCContent`
folders are **per-user** (`com.snap.file_manager_3_SCContent_<userId>`), while `caching-media`
is device-global — the decrypt-and-match linker naturally attributes each pack to whichever
profile's key opens it, so both users' packs resolve from the merged key set.

---

## Two schemas: where the keys live

The Memory media is AES-256-CBC encrypted with a **per-snap** `KEY` (32 bytes) + `IV` (16 bytes).
Where those live changed between Snapchat versions:

### New schema (Device A, 2025) — keys in `scdb-27`

`ZGALLERYSNAP.ZENCRYPTION` is an `NSKeyedArchiver` bplist (`bplist00…`):

```python
from io import BytesIO
from scripts.data import ccl_bplist
root = ccl_bplist.deserialise_NsKeyedArchiver(
           ccl_bplist.load(BytesIO(row["ZENCRYPTION"])), parse_whole_structure=True)["root"]
# root["$class"]["$classname"] == "SCMemoriesSnapEncryption"
KEY, IV, IS_ENCRYPTED = root["KEY"], root["IV"], root["IS_ENCRYPTED"]
```

- `IS_ENCRYPTED == False` → regular Memory; `KEY`/`IV` are **plaintext** and are **32 and 16
  bytes**. **No keychain and no `gallery.encrypteddb` needed** to decrypt the media.
- `IS_ENCRYPTED == True` → My Eyes Only. `KEY`/`IV` are **wrapped**, and their length says so:
  **48 and 32 bytes**, one AES block longer than a usable pair, because they are the real key and
  IV encrypted (CBC + PKCS#7) under the account's MEO master key. The keychain item
  `com.snapchat.keyservice.persistedkey` is required to unwrap them — exactly as on the old
  schema (see [MEO](#my-eyes-only-meo)).

> ⚠️ **Correction (2026-08-02).** This document previously said new-schema MEO keys were plaintext
> and needed no keychain. That was wrong, and `memories_media_report.py` believed it: it assigned
> the 48-byte value straight through, every matcher rejected it for not being 32 bytes, and the
> memory silently ended up with **no media at all** even when the keychain held what was needed.
> Verified on a two-account iOS 26 test device: unwrapping a My Eyes Only snap with that dump's
> `persistedkey` yields a valid pack header followed by a JPEG. Test the length — 48/32 means
> wrapped — rather than trusting `IS_ENCRYPTED` alone.

### Old schema (Device B, 2023) — keys in `gallery.encrypteddb`

`ZGALLERYSNAP` has **no `ZENCRYPTION` column**. Keys come from the SQLCipher
`gallery.encrypteddb`, table `snap_key_iv (snap_id, key, iv, encrypted)`. Decrypting that DB
requires the `egocipher` keychain key — **the keychain is mandatory here.**

> Detecting the schema: `PRAGMA table_info(ZGALLERYSNAP)` — if `ZENCRYPTION` exists, use the new
> path; otherwise fall back to `gallery.encrypteddb`.

---

## Decrypting `gallery.encrypteddb` (SQLCipher)

Needed for old-schema keys and for geolocation in both schemas.

- Cipher: SQLCipher with **`PRAGMA cipher_compatibility = 3`**, `key = x'<egocipher hex>'`.
- Bundled tool: `scripts/data/sqlcipher3.exe` (see `DecryptLocalMemories_iOS.decrypt_sqlcipher`):

```
sqlcipher3.exe gallery.encrypteddb "pragma key=\"x'<egocipher-hex>'\"" \
    "PRAGMA cipher_compatibility = 3" ".output recovery.sql" ".dump"
```

then replay `recovery.sql` into a fresh SQLite file. Keep the `-wal`/`-shm` alongside the
encrypted DB — the main file is often only 1 KB with all rows in the WAL.

Tables of interest in the decrypted DB:

| Table | Columns | Use |
|---|---|---|
| `snap_key_iv` | `snap_id, key, iv, encrypted` | per-snap AES key/IV (old schema) |
| `snap_location_table` | `snap_id, latitude, longitude` | **geolocation** (both schemas) |
| `snap_address_title`, `media_faces` | — | reverse-geocoded label, face index (bonus) |

`encrypted = 1` rows are My Eyes Only (key 48 bytes / iv 32 bytes, wrapped). The **new** schema
wraps them identically in `ZGALLERYSNAP.ZENCRYPTION` — same lengths, same unwrap.

---

## SCContent cache (thumbnails, full-res stills, **and videos**)

SCContent files are addressed **two** ways — you need both:

1. **By CDN URL (downloaded media).** File name `CACHE_KEY` (32 hex) `= SHA256(token)[:16 bytes]`,
   where `token` is the **last path segment** of the CDN URL (`https://cf-st.sc-cdn.net/d/<token>?…`
   → `<token>`). Applies to `ZMEDIADOWNLOADURL`, `ZOVERLAYDOWNLOADURL`, `ZTHUMBNAILDOWNLOADURL`.

2. **By `cache_controller.db` (locally-captured media with no CDN URL).** This is essential for
   **videos recorded on the device**, whose `ZGALLERYSNAP` row has **empty URL fields**.
   `CACHE_FILE_CLAIM.EXTERNAL_KEY` encodes the snap and role, pointing at the SCContent file
   named `CACHE_KEY`:

   | `EXTERNAL_KEY` prefix | `MEDIA_CONTEXT_TYPE` | Role |
   |---|---|---|
   | `snap-media-<ZSNAPID>` | 19 | full media (image **or video**) |
   | `snap-overlay-<ZSNAPID>` | 19 | overlay |
   | `snap-rendered-lowres-<ZSNAPID>` | 26 | rendered low-res still |
   | `g-media-<ZSNAPID>` | 19 | media |

   Parse the UUID out of `EXTERNAL_KEY`, map `CACHE_KEY` → (snap, role), and decrypt.
   (`cache_controller.db` does **not** reference the `caching-media` packs.)

- **Decryption:** AES-256-CBC with the snap's `KEY`/`IV`. The result carries **PKCS#7 padding**;
  stripping it yields the byte-exact original whose MD5/SHA-256 match current tools (verified
  against Cellebrite PA 10.10), while older decryptors kept the padding and produced different
  hashes. The report therefore lists **both** hash pairs by default (a `padding` option can limit
  it to one). Some files are already plaintext; a few carry a leading 8-byte header.
- The full-resolution still (e.g. `1242×2208`) and the **video `.mp4`** (`ftyp mp42/isom`) live
  here. Worked example: `…/SCContent_<userId>/<cacheKey>`, claimed by
  `snap-media-<snapId>`, decrypts to a 1.97 MB MP4.

---

## caching-media `.pack` cache

- Layout `caching-media/<folderHash>/<itemHash>-<chunkIndex>.pack`, all names 64-hex. One folder
  holds **one Memory**; large items are sharded (`-0.pack`, `-1.pack`, … concatenate in order).
- Names are **opaque**: not `SHA256` of the URL/token, snap id, media id, `CACHE_KEY`, the pack
  bytes, or the plaintext — all tested against the full set, zero matches. The cache is
  independent of `cache_controller.db`.
- Ciphertext is 16-byte aligned (AES-CBC). Decrypt with the snap key/IV and strip the 8-byte
  header (`01 00 00 00` + uint32-LE length) — **identical format in both schemas**.
- A folder typically holds **two-plus preview sizes** (~`270×510` and ~`315×623`); on Device B the
  larger item was often the **full-resolution** image (up to `1242×2208`).
- **Video memories:** the `caching-media` packs are JPEG preview frames only (never the video
  track). The playable **video lives in `SCContent`** as `snap-media-<UUID>` (see above) — so a
  video Memory is fully reconstructed by combining the `SCContent` `.mp4` with the `caching-media`
  preview stills. If a snap-media claim is absent (e.g. a purely cloud-stored video never opened),
  only the preview stills will be present. When a video has **no** cached still at all, the report
  extracts a **poster frame** from the decrypted `.mp4` for the thumbnail, clearly labelled as a
  generated (derived) artifact rather than recovered device data.

### Linking algorithm (deterministic result via decrypt-and-match)

```
memKeys = { snap_id: (KEY, IV) }            # from ZENCRYPTION (new) or snap_key_iv (old)

for folder in caching-media/*:
    firstItem = concat chunks of any one item in the folder
    for snap_id, (KEY, IV) in memKeys:       # skip wrapped MEO keys unless unwrapped
        plain = AES-256-CBC(KEY, IV).decrypt(firstItem[: 16-aligned])
        if plain[8:11] == b"\xFF\xD8\xFF":   # JPEG (or ftyp / \x89PNG)
            link folder -> snap_id; decrypt every item in the folder; break
```

Cost is trivial (folders × memories, one AES block each). In both test runs every folder
matched exactly one snap with no collisions.

---

## When is the full-filesystem keychain required?

The `egocipher` / `persistedkey` keychain items are only present in a **full-filesystem-class
keychain dump** (e.g. GrayKey, checkm8, or a UFED FFS that captured the keychain). A limited
extraction (Device A) may include the filesystem but **not** those keys.

| Goal | New schema (keys in `scdb`) | Old schema (keys in `gallery.encrypteddb`) |
|---|---|---|
| Decrypt regular-memory media (`.pack` + SCContent) | **No keychain needed** | **`egocipher` required** |
| Geolocation (`snap_location_table`) | **`egocipher` required** | **`egocipher` required** |
| My Eyes Only memories | **`persistedkey` required** ¹ | `egocipher` + `persistedkey` required ¹ |

So on the new schema the keychain is required for exactly two things: **geolocation** and
**My Eyes Only**. Regular-memory imagery needs nothing.

¹ With one exception, and it is not a rare one: a Memory *moved* into My Eyes Only still decrypts
with the original snap's key, no `persistedkey` involved — see
[A Memory moved into My Eyes Only keeps its original key](#a-memory-moved-into-my-eyes-only-keeps-its-original-key).

### My Eyes Only (MEO)

`snap_key_iv.encrypted = 1` (old) / `ZENCRYPTION.IS_ENCRYPTED = True` (new). **On both schemas the
key is wrapped and `com.snapchat.keyservice.persistedkey` is required to unwrap it.**

The wrapping is the same operation in both places — the real 32-byte key and 16-byte IV, AES-CBC
encrypted with PKCS#7 padding under the MEO master key — so the wrapped values are **48 and 32
bytes**. That length is the reliable test; do not rely on `IS_ENCRYPTED` alone, and never hand a
48-byte value to `AES.new` (it raises `Incorrect AES key length (48 bytes)`, which reads like a
bug rather than a missing key).

```python
persisted = keychain["com.snapchat.keyservice.persistedkey"]     # NSKeyedArchiver plist
obj = ccl_bplist.deserialise_NsKeyedArchiver(ccl_bplist.load(BytesIO(persisted)))
master, master_iv, owner = obj["masterKey"], obj["initializationVector"], obj["userId"]
key = AES.new(master, AES.MODE_CBC, master_iv).decrypt(wrapped_key)[:32]
iv  = AES.new(master, AES.MODE_CBC, master_iv).decrypt(wrapped_iv)[:16]
```

**`persistedkey` is per account.** Its payload carries the `userId` it belongs to, so a phone
signed into two Snapchat accounts has one item per account, and each profile must be handed its
own — `sha256(userId)` is the `userHash` that names the profile directory. A keychain reader that
keeps only the first item of that name (as this one did) silently discards the second account's
MEO master key. On a two-account iOS 26 test device only one of the two accounts has a
`persistedkey`: that account's MEO memory decrypts and the other's does not — which is the correct,
and reportable, outcome.

> **Correction (2026-08-02).** The previous version of this section claimed new-schema MEO keys
> were "directly usable … no unwrap, and no keychain", citing a device where MEO
> imagery appeared without an `egocipher`. That reading does not hold: media can appear for a MEO
> memory because a *sibling* regular memory shares its `ZMEDIAID` group, and the 48/32-byte key
> lengths on every MEO row in the corpus are unambiguous. Treat MEO as keychain-dependent on both
> schemas.

> **Correction (2026-08-07).** This section used to end with "seeing MEO media in the report does
> imply `persistedkey` was read". That does not hold either — see the next subsection. What the
> wrapped key withholds is the *new row's own key*, not necessarily the bytes on disk.

### A Memory **moved** into My Eyes Only keeps its original key

Moving an existing Memory into My Eyes Only does **not** re-encrypt the media already cached on the
device. What the app writes is a *new* `ZGALLERYSNAP` row for the moved Memory, whose own key
(`ZENCRYPTION` / `snap_key_iv` with `encrypted=1`) is wrapped in the MEO master key — but that row
still points at the **original media object**:

| Field on the MEO row | Value |
|---|---|
| `ZMEDIAID` | the original snap's id |
| `ZDUPLICATEDFROMSNAPID` | the original snap's id |
| `ZENTRYID` (entry) | the original snap's id |
| `ZSNAPSHASH` (entry) | the MEO snap's own id |

The original's `ZGALLERYSNAP` row is gone once it has been moved. **Its `snap_key_iv` row is not**:
it survives in `gallery.encrypteddb` with `encrypted=0` — an ordinary unwrapped 32/16 AES-256
key/IV — and the cached ciphertext is still that snap's. So the media of such a Memory decrypts
with **no keychain `persistedkey` at all**:

```python
# the MEO row's own key is 48/32 and stays wrapped; the media object's key is 32/16 and is not
ref  = meo_row["ZMEDIAID"] or meo_row["ZDUPLICATEDFROMSNAPID"]
key, iv, encrypted = gallery["snap_key_iv"][ref]     # encrypted == 0
```

Two consequences for a reader of these reports:

* A tool that only ever looks up `snap_key_iv[<this snap id>]` reports the Memory as "key wrapped,
  media on disk but undecryptable" while another tool decrypts it — the difference is which snap id
  was looked up, not which keys either tool had. Keep rows whose `snap_id` matches **no**
  `ZGALLERYSNAP` row; they are exactly the ones this needs.
* The link is `ZMEDIAID` / `ZDUPLICATEDFROMSNAPID` — a recorded reference, not proximity — and the
  result is still confirmed by the media decrypting to a recognised format. A key that merely sits
  in the same database proves nothing.

This is **not** a general MEO bypass. It recovers only media that was already cached under the
original snap's key; a Memory captured straight into My Eyes Only, and anything else encrypted
under the account's MEO master key, stays locked without `persistedkey`. Verified on an old-schema
device (decrypts to a complete JPEG whose dimensions match the row's `ZWIDTH`×`ZHEIGHT`); a
new-schema MEO memory in the corpus references no such surviving row and stays locked, as it should.

Implemented by `adopt_media_object_keys` in `scripts/memories_media_report.py`, which labels every
key it recovers this way in the report rather than presenting it as the snap's own.

---

## Diagnosing a keychain

Check a keychain on its own, without re-running an extraction:

```
Snapchat_Auto.exe --diag-keychain <keychain file>
python -m scripts.DecryptLocalMemories_iOS --diag-keychain <keychain file>   # from source
```

Both print the format detected, how many items were scanned, how many belong to Snapchat's access
group (`3MY7A92V5W.com.toyopagroup.picaboo`), which of `egocipher`/`persistedkey` were recovered
(sizes only — never the key material), and a verdict. Exit code is 0 only when `egocipher` was
recovered. The same lines are written into every run log at INFO/WARNING, so a report that says
"keychain missing" can always be traced back to one of these causes:

| status | Meaning |
|---|---|
| `ok` | `egocipher` recovered. |
| `no-egocipher` | Parsed fine, but the item is absent. `egocipher.key.avoidkeyderivation` is ThisDeviceOnly, so backup-class dumps never carry it — an FFS keychain is needed. Also covers metadata-only dumps that list the item without its value. |
| `no-snapchat-items` | Parsed fine, but nothing in the Snapchat access group — likely the wrong device or extraction. |
| `unreadable` | Not a recognized format, or it failed to parse/decrypt (the exception and traceback are logged). |
| `missing` / `none` | The path does not exist / no keychain was supplied at all. |

Recognized formats: GrayKey-style keychain plists (XML **or** binary), UFED
`backup_keychain_v2.plist` (decrypted on the fly via `scripts/data/keychain.py`, leaving
`decrypted_keychain.plist` in the run folder), and objection JSON dumps. Item attributes are
matched case-exactly but representation-agnostically — `agrp`/`gena` may be `<data>` (with or
without a trailing NUL) or `<string>`, and the secret may be raw data, hex, or base64. Items are
matched on the **account name**, so dumps that export no `agrp` still work.

---

## Field reference — `scdb-27.sqlite3` → `ZGALLERYSNAP`

| Column | Meaning |
|---|---|
| `ZSNAPID` | Memory snap UUID (join key to `snap_key_iv` / `snap_location_table`) |
| `ZMEDIAID` | usually equals `ZSNAPID` |
| `ZMEDIATYPE` | `0` = image, `1` = video |
| `ZSERVLETMEDIAFORMAT` | `image_jpeg`, `video_hevc`, `video_avc`, … |
| `ZMEDIADOWNLOADURL` / `ZMEDIAREDIRECTURI` | CDN URL → SCContent `CACHE_KEY` via `SHA256(token)[:16]` |
| `ZOVERLAYDOWNLOADURL` / `ZTHUMBNAILDOWNLOADURL` | overlay / thumbnail CDN URLs |
| `ZCREATETIMEUTC` / `ZCAPTURETIMEUTC` | Apple Cocoa time (add `978307200` → Unix seconds) |
| `ZWIDTH` / `ZHEIGHT` | full-media dimensions |
| `ZHASLOCATION` | `1` if geolocation exists (coords live in `gallery.encrypteddb`) |
| `ZENCRYPTION` | **new schema only** — `SCMemoriesSnapEncryption` bplist with `KEY`/`IV` |

---

## Notes for productionizing (see the tool implementation)

- Detect schema by presence of `ZGALLERYSNAP.ZENCRYPTION`; merge keys from whichever source(s)
  are available (both can be used — `gallery.encrypteddb` is still needed for geolocation).
- Read `egocipher`/`persistedkey` once; degrade gracefully: without the keychain, still emit
  regular-memory imagery on the new schema, and mark geolocation / MEO as "keychain required".
- Treat `plain[:4] == 01 00 00 00` as the pack header marker; fall back to scanning for media
  magic bytes if a future version bumps it.
- Full video tracks are generally **not** cached — surface preview stills and label videos
  accordingly rather than expecting a playable file.
