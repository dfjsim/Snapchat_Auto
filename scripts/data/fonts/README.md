# NotoColorEmoji.woff2

The emoji font every report page uses (`report_ui.emoji_font_css`), so an emoji is drawn the same on
every examiner's workstation instead of by whichever emoji font that copy of Windows has. Why, and the
rules the reports follow when using it, are in [docs/report_ui.md](../../../docs/report_ui.md)
("Emoji font").

## Licence

**Noto Color Emoji**, Copyright 2022 Google Inc., licensed under the **SIL Open Font License 1.1**
([OFL.txt](OFL.txt), as published with the font). Noto is a trademark of Google Inc. The licence and
copyright notice also travel inside the font itself (its `name` table), which is what accompanies the
font into a report.

## Where it comes from

`2D/fonts/Noto-COLRv1.ttf` of <https://github.com/googlefonts/noto-emoji>, release
**v2026-09-24-unicode18_0** (font version 2.057, Unicode / Emoji 18.0), SHA-256
`b8e25ea68db82f9e4d0aee921f4420be2be39887bd5c893a2ad98710531f9d0c`.

Converted to WOFF2 and nothing else — no glyph, table or name was changed or removed:

```
python -c "from fontTools.ttLib import TTFont; f = TTFont('Noto-COLRv1.ttf'); f.flavor = 'woff2'; f.save('NotoColorEmoji.woff2')"
```

with fontTools 4.65.0 and `brotli`. Result: 1 997 104 bytes, SHA-256
`21e8daa3bb80176b721e5ef6c0936bd84e1bde46497a1d5de74b8e6cb1fc68cb`.

It is a COLRv1 font: Chrome and Edge draw it from version 98, Firefox from 107 (both 2022). Safari
does not, which costs nothing — on a Mac the reports use Apple Color Emoji first.

## Updating it

Take the same file from a newer release, convert it the same way, and update the version, both hashes
and `report_ui`'s CSS comment. Check the result in a browser (`docs/report_ui.md` says how the
coverage was measured).
