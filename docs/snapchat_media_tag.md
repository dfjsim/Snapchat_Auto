# The Snapchat app's tag in media files

The Snapchat app writes a short base64 string into the *description* field of media it encodes.
Decoded, it is a protobuf that names the **app version, the device model and the operating system
of the app that wrote the file**, and the **lens** used. This note describes where the tag is, its
layout, what each field is known to mean and how that is known, and how the reports show it.

Implemented by `scripts/data/snap_media_tag.py` (the decoder) and `scripts/data/media_meta.py`
(which offers every text field a media file carries to the decoder). Shown by
`report_ui.snap_tag_html` wherever a report shows a file's *Embedded metadata*.

**Credit:** the tag — a base64 protobuf in the description field of some media files — was found by
**Keban Bronsario**.

## Where it is

| File | Field | Form of the field |
|---|---|---|
| MP4 written by the iOS app | `moov › udta › dscp` | 3GPP asset box: a FullBox (version/flags `00 00 00 00`), a packed ISO-639-2/T language (`0x55C4` = `und`), then NUL-terminated UTF-8 |
| MOV written by the iOS app | `moov › meta`, key `com.apple.quicktime.description` | QuickTime metadata, handler `mdta`. This `meta` is **not** a FullBox — its `hdlr` is its first child — unlike the `meta` inside `udta`. The value is an `ilst` item's `data` atom, type 1 (UTF-8) |
| MP4 written by the Android app | `moov › udta › meta › ilst › desc` | the iTunes-style list an ffmpeg muxer writes (`©too` = `Lavf…` beside it); `meta` is an ISO FullBox with handler `mdir`; `data` atom type 1 |
| An image the app saved | EXIF `UserComment` | met as XMP `exif:UserComment` where an editing program copied such an image's EXIF into the list of files a video was edited from (see *Source files of an edit* below) |

An MP4 written by the iOS app has its `moov` — and so the tag — after the media data, at the end of
the file; a MOV has it before, at the start. Either way the reader finds it by walking the top-level
boxes by seeking, without reading the media data.

The decoder is offered **every** text field a file carries — EXIF text fields, XMP properties, PNG
text chunks, every QuickTime user-data and metadata value — so a carrier not listed here is found
without a change to the code.

## Layout

The field holds standard base64 (with padding) of this protobuf:

| Path | Wire type | Content |
|---|---|---|
| `1` | 2 | a message holding the fields below |
| `1.1` | 2 | `Snapchat/<appVersion> (<deviceModel>; <operatingSystem>; gzip)` — the shape of an HTTP user agent |
| `1.2` | 2 or 0 | the lens id, a repeated int64. The iOS app writes it **packed** (wire type 2, the varints inside); the Android app **unpacked** (wire type 0). Both are valid encodings of a repeated field |
| `1.4` | 0 | a varint (the iOS app writes `1`); shown as stored, without a meaning attached |

On iOS `<deviceModel>` is the model identifier (`iPhone<n>,<m>`) and `<operatingSystem>` is
`iOS <version>`. The Android app writes the manufacturer's model number, an operating system of the
form `Android <version>#…#…` (further `#`-separated values) and appends ` V/<name>` after the closing
parenthesis. Everything in the user agent is **shown as written**; the model identifier is not
translated into a marketing name and the `#`-separated values are not interpreted.

A synthetic example (not from any device):

```
CjkKLlNuYXBjaGF0LzEyLjAuMC4xIChpUGhvbmUxNSwyOyBpT1MgMTcuMDsgZ3ppcCkSBbW48P4tIAE=

0a 39                                   field 1, message, 57 bytes
   0a 2e  "Snapchat/12.0.0.1 (iPhone15,2; iOS 17.0; gzip)"      field 1.1
   12 05  b5 b8 f0 fe 2d                field 1.2, packed: one varint = 12345678901
   20 01                                field 1.4 = 1
```

### The one rule

**The value is the tag only when all of it parses:** the whole field is base64 (strict alphabet and
padding), every byte of both protobuf levels is accounted for (no trailing bytes, no truncated
varint, no group wire types, no field number 0), and field 1.1 is UTF-8 text starting
`Snapchat/<version> (`. Anything else is the text it is, shown as stored. Ordinary prose,
another program's description or a random base64 blob cannot pass all of these at once.

A field that decodes is shown twice, on purpose: decoded, in its own block, and **as stored**, at the
front of the file's *all fields*, so the examiner can see exactly what was decoded. The block's
*Read from* line names the field and its byte offset in the file.

## What field 1.2 is: the lens id

Each value checked also appears in the app's own lens records on the device that holds the file:

- for a Memory saved by the iOS app: a document key `<prefix>_<lensId>_lens_central` in the global
  `*.docobjects` store, and a row next to `LENSES` in `rtus.db`;
- for a story snap written by the Android app and received on an iOS device: an
  `SCStoriesSnapLens` archive in `content_feed_database`, next to `HAS_SNAPPABLES_METADATA`.

Verified on two devices. That is why the report labels the value *Lens id*. A lens id from another
person's device need not appear in the extraction at all — the report shows it either way. Resolving
an id to a lens *name* is not done yet (see TODO).

## Whose device it names

The tag names the app instance that **wrote** the file:

- **a received snap or story:** the sender's app, device model and operating system;
- **a Memory:** the device that saved it. Memories follow the account, so a Memory can have been
  saved on a phone other than the one extracted — a model that differs from the extracted device's is
  itself a finding;
- **a source file of an edit:** the tag found in an editing program's record of an image it used
  belongs to *that image*, not to the video the program exported, and is shown with the image.

A file **without** the tag says nothing: older versions of the app write none, and anything that
re-encodes the file drops it.

## Checking a tag by hand

1. Open the file at the offset the report's *Read from* line gives, and copy the base64 text (it ends
   at the NUL or at the end of the field).
2. `base64 -d | protoc --decode_raw` prints the fields above; field `1.2` of an iOS tag comes out as
   bytes, which are the packed varint(s).

## Source files of an edit (XMP pantry)

A video exported from an editing program (Adobe Premiere Pro, After Effects, …) carries in its XMP
two kinds of data, which the reader keeps apart:

- **about the file itself** — `xmp:CreateDate` / `ModifyDate` / `MetadataDate`, `xmp:CreatorTool`,
  its own `xmpMM:History` (each event is shown as a timestamp, labelled with its action and program);
- **about every file that went into the edit** — `xmpMM:Ingredients` (their paths on the editing
  computer, and which part of each was used) and `xmpMM:Pantry` (what the program knew about each:
  its own dates, the program that made or saved it, its duration, sometimes a GPS fix, and for an
  image the app saved, the tag). Pantry items are joined to ingredients by
  `xmpMM:InstanceID` ↔ `stRef:instanceID`; nested pantries are flattened.

The second kind is stored **in** the file but describes **those** files — a clip was recorded, dated
and located on its own device and clock, often long before the edit. So it is returned apart
(`media_meta` → `xmp_sources`), shown in a collapsed table of its own labelled as the source files'
data, and never enters the file's timestamps, its GPS, the date search, or anything computed from
them. Their file names, programs and tags are searchable; their dates and places are not. A date of
`1904-01-01T00:00:00Z` in such an entry is the zero value of a QuickTime time and is shown as
*not set*.

The XMP is parsed as XML (`xml.etree`), after refusing any packet that contains a document type or
an entity declaration. A packet that is not well-formed XML falls back to reading its dates as text,
and stops at the first `<xmpMM:Pantry` / `<xmpMM:Ingredients` so that a source file's date is never
read as the file's own.

## Other things the reader now reads in MP4 / MOV

The tag's carriers needed these, and each is useful on its own:

- **QuickTime metadata in `moov › meta`** — where iOS keeps a MOV's `com.apple.quicktime.make`,
  `model`, `software`, `creationdate` and `location.ISO6709`. `data` atoms are read by their type
  (text, big-endian integers, floats; anything else — a cover image — is shown by its size).
- **3GPP asset boxes** in `udta` (`titl`, `dscp`, `cprt`, `perf`, `auth`, `gnre`, `albm`), in UTF-8 or
  UTF-16. A box of the same name in QuickTime's own text form is still read that way.
- **Track headers** (`tkhd`, `mdhd`): where every track states the same creation / modification
  time as the movie header, the `mvhd` time says so; a track time that differs is listed as its own
  timestamp, labelled with the tracks that carry it.
- **XMP** in a top-level `uuid` box (`BE7ACFCB-97A9-42E8-9C71-999491E3AFAC`) or in `udta › XMP_`.
- The walk **seeks** from box to box and reads only metadata boxes (each at most 4 MB), so a
  `udta` after large sample tables is no longer lost and the media data is never read.

## Where the reports show it

- **Embedded metadata** block — the Memories detail pages, the cache_controller and Library/Caches
  rows, chat attachments in the Conversations detail pages (without the «?», whose text is in the
  *Content* column header), and the Android Memories detail pages. The tag is a block of its own
  under the file's key fields; the source files of an edit are a collapsed table under its
  timestamps.
- **Memories index** — an `APP TAG` chip on a Memory whose media carries the tag (its tooltip names
  the app, model, OS and lens), and a *with the Snapchat app's tag* option in the *Embedded
  metadata* filter. The row's *CDN URLs, AES key / IV, …* block lists the tag and lens id.
- **Search**, in every index where the file appears: the user agent (so the app version, the model
  and the OS each find it), every lens id, the encoded text as stored, and the phrase
  `snapchat app tag lens`; in the Conversations index a conversation is found by the tags of the
  files sent in it.
