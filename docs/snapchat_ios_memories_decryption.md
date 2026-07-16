# Decrypting & linking Snapchat iOS Memories media (`.pack`, `SCContent`, geolocation)

End-to-end reverse-engineering notes for recovering **Snapchat Memories** media from an iOS
extraction and linking every media file back to its Memory row in `scdb-27.sqlite3`,
including geolocation.

Covers both cache locations:

- `Documents/com.snap.file_manager_*_SCContent_*/` — the **SCContent** cache (thumbnails and,
  when present, full-resolution stills). The existing scripts handle part of this.
- `Library/Caches/caching-media/**/*.pack` — the **caching-media** cache. **Not handled by the
  current scripts.** Magnet AXIOM decrypts *and* links these; Cellebrite PA decrypts but does
  not link. The method below reproduces the linking.

…and both Snapchat storage schemas we observed (see [Two schemas](#two-schemas-where-the-keys-live)).

> ⚠️ Extractions contain a device owner's private media. Keep all decrypted output **local** —
> never upload or publish it.

## Verified against two real iPhone 13 Pro Max extractions

| | Device A | Device B |
|---|---|---|
| Extraction | UFED Full-Filesystem (AFU), 2025 | GrayKey Full-Filesystem, 2023-05-15 |
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

1. **Read the keychain** (`readKeychain` in `scripts/DecryptLocalMemories_iOS.py`). You may get
   `egocipher` (Memories DB key) and/or `persistedkey` (My Eyes Only master key). See
   [When is the keychain required?](#when-is-the-full-filesystem-keychain-required).
2. **Get the per-Memory AES `KEY`/`IV`:**
   - **New schema:** decode `ZGALLERYSNAP.ZENCRYPTION` (a `SCMemoriesSnapEncryption`
     NSKeyedArchiver bplist). No keychain needed for regular memories.
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
| `650c8c96…` | `3559758e-…` (active) | 34 | ✅ 35 keys / 35 locations |
| `09e676dd…` | `5803ed5b-…` | 46 | ✅ 51 keys / 43 locations |

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

- `IS_ENCRYPTED == False` → regular Memory; `KEY`/`IV` are **plaintext**. **No keychain and no
  `gallery.encrypteddb` needed** to decrypt the media.
- `IS_ENCRYPTED == True` → My Eyes Only; `KEY`/`IV` are wrapped (see [MEO](#my-eyes-only-meo)).

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

`encrypted = 1` rows are My Eyes Only (key 48 bytes / iv 32 bytes, wrapped).

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
| My Eyes Only memories | `persistedkey` required | `egocipher` + `persistedkey` required |

So even on the new schema, **geolocation and My Eyes Only still need the FFS keychain.** Regular
memory *imagery* is the only thing recoverable without it.

### My Eyes Only (MEO)

`snap_key_iv.encrypted = 1` (old) / `ZENCRYPTION.IS_ENCRYPTED = True` (new). The `KEY`/`IV` are
themselves AES-CBC-encrypted with the MEO master key/IV, which come from the keychain item
`com.snapchat.keyservice.persistedkey` (an NSKeyedArchiver plist with `masterKey` /
`initializationVector`). Unwrap them first — see `DecryptLocalMemories_iOS.fixMEOkeys` — then
decrypt media as usual. Device B has 1 MEO memory, but its `persistedkey` was not in the
keychain, so it could not be decrypted (a clean illustration of the dependency).

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
