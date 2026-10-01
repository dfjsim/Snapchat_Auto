"""An emoji in a report is drawn by the font the reports carry, the same on every workstation.

Windows' own emoji font is only as current as that copy of Windows — an early-2026 one has no Emoji
17.0, which current iPhones offer, and no version has flags (Algeria's reads "DZ"). The reports carry
Noto Color Emoji (`scripts/data/fonts/`), in one stylesheet every page links, and end every font stack
with it. What is pinned here is what makes that work and is easy to undo without noticing: the
stylesheet carries the bundled font, every stack ends with it and nothing emoji-capable comes before
it, the reports' own text symbols stay out of it, and every page reaches the stylesheet.

Every input is synthetic. No extraction data is required or used.
"""
import base64
import hashlib
import pathlib
import re

from scripts import report_ui

REPO = pathlib.Path(__file__).resolve().parent.parent
FONTS = REPO / "scripts" / "data" / "fonts"
SANS = "-apple-system,Segoe UI,Roboto,sans-serif"
_DATA_URL = re.compile(r"url\(data:font/woff2;base64,([A-Za-z0-9+/=]+)\)")

# Unicode 18.0 emoji-data.txt: Emoji=Yes, Emoji_Presentation=No, above ASCII — the emoji a browser
# draws as plain text unless U+FE0F follows, and the only ones that change look in a colour font.
TEXT_DEFAULT_EMOJI = (
    "A9,AE,203C,2049,2122,2139,2194-2199,21A9-21AA,2328,23CF,23ED-23EF,23F1-23F2,23F8-23FA,24C2,"
    "25AA-25AB,25B6,25C0,25FB-25FC,2600-2604,260E,2611,2618,261D,2620,2622-2623,2626,262A,262E-262F,"
    "2638-263A,2640,2642,265F-2660,2663,2665-2666,2668,267B,267E,2692,2694-2697,2699,269B-269C,26A0,"
    "26A7,26B0-26B1,26C8,26CF,26D1,26D3,26E9,26F0-26F1,26F4,26F7-26F9,2702,2708-2709,270C-270D,270F,"
    "2712,2714,2716,271D,2721,2733-2734,2744,2747,2763-2764,27A1,2934-2935,2B05-2B07,3030,303D,3297,"
    "3299,1F170-1F171,1F17E-1F17F,1F202,1F237,1F321,1F324-1F32C,1F336,1F37D,1F396-1F397,1F399-1F39B,"
    "1F39E-1F39F,1F3CB-1F3CE,1F3D4-1F3DF,1F3F3,1F3F5,1F3F7,1F43F,1F441,1F4FD,1F549-1F54A,1F56F-1F570,"
    "1F573-1F579,1F587,1F58A-1F58D,1F590,1F5A5,1F5A8,1F5B1-1F5B2,1F5BC,1F5C2-1F5C4,1F5D1-1F5D3,"
    "1F5DC-1F5DE,1F5E1,1F5E3,1F5E8,1F5EF,1F5F3,1F5FA,1F6CB,1F6CD-1F6CF,1F6E0-1F6E5,1F6E9,1F6F0,1F6F3")


def _spans(text, sep=","):
    out = []
    for item in text.split(sep):
        low, _, high = item.strip().removeprefix("U+").partition("-")
        out.append((int(low, 16), int(high or low, 16)))
    return out


def _in(spans, cp):
    return any(low <= cp <= high for low, high in spans)


def _report_modules():
    return sorted((REPO / "scripts").rglob("*.py"))


def test_the_bundled_font_is_the_documented_one():
    """README.md says where the font came from and what its hash is; the licence travels with it."""
    data = pathlib.Path(report_ui._EMOJI_FONT_FILE).read_bytes()
    assert data[:4] == b"wOF2"
    assert hashlib.sha256(data).hexdigest() in (FONTS / "README.md").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in (FONTS / "OFL.txt").read_text(encoding="utf-8")


def test_the_stylesheet_carries_the_bundled_font_inline():
    """Inline, because a file:// page may be refused a font file from a folder above it."""
    css = report_ui.emoji_font_css()
    match = _DATA_URL.search(css)
    assert match, "the font is not inline as a data: URL"
    assert base64.b64decode(match.group(1)) == pathlib.Path(report_ui._EMOJI_FONT_FILE).read_bytes()
    assert f'font-family:"{report_ui.EMOJI_FONT_FAMILY}"' in css
    # the OFL asks for the copyright notice and the licence wherever the font goes
    assert "Copyright 2022 Google Inc." in css and "SIL Open Font License 1.1" in css


def test_the_font_takes_every_emoji_but_the_reports_own_symbols():
    css = report_ui.emoji_font_css()
    spans = _spans(re.search(r"unicode-range:([^;]+);", css).group(1))
    for cp in (0x1F600, 0x2764, 0x1F1E6, 0x1F1FF, 0x200D, 0xFE0F, 0x20E3, 0xE0067, 0xE007F,
               0x1F3FD, 0x1FAEA, 0x1FAEB, 0x30, 0x23):       # incl. digits and # for the keycaps
        assert _in(spans, cp), f"U+{cp:04X} is kept out of the emoji font"
    for symbol in report_ui.UI_SYMBOLS:
        assert not _in(spans, ord(symbol)), f"U+{ord(symbol):04X} would turn into a colour emoji"


def test_the_reports_own_text_symbols_are_all_kept_out_of_the_font():
    """A text-default emoji the reports use as an icon (▶, ⚠, 🗂) turned colour in Noto. A new one
    must join UI_SYMBOLS; one followed by U+FE0F is meant as an emoji and is left to the font."""
    text_default = _spans(TEXT_DEFAULT_EMOJI)
    found = {}
    for path in _report_modules():
        text = path.read_text(encoding="utf-8")
        uses = [(m.group(0), m.end()) for m in re.finditer(r"[^\x00-\x7f]", text)]
        uses += [(chr(int(m.group(1))), m.end()) for m in re.finditer(r"&#(\d+);", text)]
        uses += [(chr(int(m.group(1) or m.group(2), 16)), m.end())
                 for m in re.finditer(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})", text)]
        for char, end in uses:
            tail = text[end:end + 8]
            emoji_form = tail.startswith(("️", "&#65039;")) or tail.lower().startswith("\\ufe0f")
            if _in(text_default, ord(char)) and not emoji_form:
                found.setdefault(char, path.name)
    missing = {f"U+{ord(c):04X} ({where})" for c, where in found.items() if c not in report_ui.UI_SYMBOLS}
    assert not missing, f"text-default emoji used as report symbols, not in UI_SYMBOLS: {missing}"


def test_every_report_font_stack_ends_with_the_emoji_font():
    """Last, after Apple's: text keeps the system fonts, and a Mac keeps Apple's emoji."""
    expected = f"{SANS},{report_ui.EMOJI_FONT_STACK}"
    found = 0
    for path in _report_modules():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(re.escape(SANS), text):
            found += 1
            line = text.count("\n", 0, match.start()) + 1
            assert text.startswith(expected, match.start()), f"{path.name}:{line} lacks the emoji font"
    assert found >= 6                       # the stacks this was written against are still there


def test_no_system_emoji_font_comes_before_the_bundled_one():
    """A Windows or Linux emoji font named first would win, and the report would again depend on it."""
    system = ("Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji")

    def system_first(stack):
        named = [stack.index(name) for name in system if name in stack]
        return named and (report_ui.EMOJI_FONT_FAMILY not in stack
                          or min(named) < stack.index(report_ui.EMOJI_FONT_FAMILY))

    for path in _report_modules():
        for stack in re.findall(r"font-family:([^;}]+)", path.read_text(encoding="utf-8")):
            assert not system_first(stack), f"{path.name}: {stack}"
    assert not system_first(report_ui.LEGACY_FONT_CSS)
    assert report_ui.EMOJI_FONT_FAMILY in report_ui.LEGACY_FONT_CSS


def test_the_stylesheet_is_written_where_the_pages_look(tmp_path):
    report_ui.write_emoji_font(str(tmp_path))
    assert (tmp_path / report_ui.EMOJI_FONT_CSS).read_text(encoding="utf-8") == report_ui.emoji_font_css()
    assert report_ui.emoji_font_link("../") == '<link rel="stylesheet" href="../emoji_font.css">'


def test_a_legacy_report_carries_the_stylesheet_in_its_own_css_folder(tmp_path):
    """The legacy reports are self-contained: their css/ folder is copied next to them."""
    assert report_ui.copy_css(str(tmp_path))
    assert (tmp_path / "css" / report_ui.EMOJI_FONT_CSS).is_file()
    for name in ("ParseSnapchat_iOS.py", "getCacheAndroid.py", "DecryptLocalMemories_iOS.py"):
        text = (REPO / "scripts" / name).read_text(encoding="utf-8")
        assert f'<link href="./css/{report_ui.EMOJI_FONT_CSS}" rel="stylesheet">' in text, name
        assert "report_ui.LEGACY_FONT_CSS" in text, name


def test_a_missing_font_costs_the_emoji_font_not_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(report_ui, "_EMOJI_FONT_FILE", str(tmp_path / "absent.woff2"))
    report_ui.emoji_font_css.cache_clear()
    try:
        assert report_ui.emoji_font_css() == ""
        report_ui.write_emoji_font(str(tmp_path / "Reports"))
        assert not (tmp_path / "Reports" / report_ui.EMOJI_FONT_CSS).exists()
    finally:
        report_ui.emoji_font_css.cache_clear()
