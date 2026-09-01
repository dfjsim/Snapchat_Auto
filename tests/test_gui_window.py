"""The settings window builds, and starts in the state the examiner should find it in.

Everything here needs a real toolkit and a display, so it is skipped where there is none. What it
buys: a mistyped element argument, a binding on a key that no longer exists, a theme that will not
load and a path field left out of the elision are all faults that otherwise appear only when
somebody opens the app — which, for a GUI wrapping a forensic pipeline, tends to be the examiner.

The window is built **once for the whole file**, with a prefill, so one root covers both what the
saved config puts on the form and what a rebuild carries across. A second Tk root in one pytest
process fails here with a Tcl init error; in a real run, closing a window and building the next one
works, which is what the appearance button does.

No run is started and nothing is clicked: the window is built, inspected and destroyed.
"""
import pytest

import Snapchat_Auto as app

pytest.importorskip("FreeSimpleGUI")

LONG_DIR = r"C:\Temp\Snapchat_Auto\runs\HOM-2026-0042\exhibit B\phone 1\working and reports"
SAVED_ZIP = r"D:\cases\HOM-2026-0042\exhibit B\phone 1\EXTRACTION_FFS.zip"
CFG = {"zip": SAVED_ZIP, "keychain": SAVED_ZIP, "workdir": LONG_DIR,
       "installer_dir": r"C:\builds", "tile_server": "", "appearance": "dark"}
# what a rebuild hands back to the new window: the fields as the examiner had left them
PREFILL = {"zip": SAVED_ZIP, "case_ref": "EXHIBIT-9", "expand_only": True}


@pytest.fixture(scope="module")
def window():
    app.apply_theme(CFG["appearance"])
    try:
        built = app.build_settings_window(CFG, prefill=PREFILL)
    except Exception as error:                              # no display, or no Tk on this host
        pytest.skip(f"no GUI available here: {error}")
    yield built
    built[0].close()


def test_the_window_can_be_resized_and_has_a_floor(window):
    """The form is taller than a laptop screen with every optional section on it."""
    win = window[0]

    assert win.Resizable is True
    assert win.TKroot.minsize() == (760, 420)


def test_the_buttons_are_outside_the_scrolling_area(window):
    """Ok scrolled off the bottom of a fixed window is the same as no Ok at all."""
    win = window[0]

    assert "Ok" in win.AllKeysDict and "Cancel" in win.AllKeysDict
    form = win["form"]
    assert form.Scrollable
    keys_in_form = {getattr(el, "Key", None) for row in form.Rows for el in row}
    assert "Ok" not in keys_in_form


def test_a_long_path_starts_elided_with_the_whole_thing_on_hover(window):
    win, real, _relations = window

    assert real["workdir"] == LONG_DIR, "the value is the path, whatever the box shows"
    shown = win["workdir"].get()
    assert shown != LONG_DIR and "\u2026" in shown
    assert shown.startswith("C:\\Temp") and shown.endswith("reports")
    assert win["workdir"].TooltipObject is not None


def test_a_short_path_is_shown_whole(window):
    win, real, _relations = window

    assert win["installer_dir"].get() == real["installer_dir"] == r"C:\builds"


def test_the_extraction_field_is_not_pre_filled_from_the_saved_config(window):
    """«Use previous» offers the last extraction instead of pre-filling it, so a new case cannot
    inherit the previous one's keychain by simply not being looked at. (`zip` is in the prefill
    here, which is a rebuild handing back what was on the form — a different thing.)"""
    win, real, _relations = window

    assert real["keychain"] == "" and win["keychain"].get() == ""
    assert win["keychain_prev"].visible is True


def test_every_path_field_is_wired_for_focus(window):
    """Without both bindings a field would stay abbreviated while it is being edited."""
    win = window[0]

    for key in app.PATH_KEYS:
        bound = str(win[key].TKEntry.bind())
        assert "<FocusIn>" in bound and "<FocusOut>" in bound, key


def test_the_relation_policy_starts_at_the_recommended_set(window):
    _win, _real, relations = window

    assert relations["relations"]["mem_group"] is True
    assert relations["relations"]["conv_messages"] is False
    assert relations["transitive"] is False


def test_the_form_carries_no_walls_of_prose(window):
    """What "too crammed" was: six explanations printed under their fields, each two to four wrapped
    lines. They are behind "?" marks now — same words, on request. This is the guard against the
    next one being pasted straight back onto the form."""
    win = window[0]
    form = win["form"]

    texts = [str(getattr(el, "DisplayText", "") or "")
             for row in form.Rows for el in row if el.__class__.__name__ == "Text"]
    longest = max(texts, key=len)

    assert len(longest) <= 120, f"a paragraph is back on the form: {longest[:90]!r}"
    marks = [k for k in win.AllKeysDict if isinstance(k, str) and k.startswith("help:")]
    assert len(marks) >= 6, "the explanations should be reachable, not deleted"


def test_the_form_fits_the_window_it_opens_at(window):
    """It scrolls when it has to, but opening already scrolled is what made it feel crammed."""
    win = window[0]
    win.TKroot.update_idletasks()
    inner = win["form"].TKColFrame
    inner = getattr(inner, "TKFrame", inner)

    assert inner.winfo_reqheight() <= app._viewport_size()[1]


def test_the_viewport_is_sized_to_the_screen_not_to_a_guess():
    """A fixed height is a guess about somebody else's monitor: too tall puts the buttons off a
    laptop screen, too short opens a form that would have fitted already scrolled."""
    assert app._viewport_size((3440, 1440)) == app._VIEW_MAX
    assert app._viewport_size((1366, 768)) == (1000, 548)    # width hits the cap, height does not
    assert app._viewport_size((800, 600)) == (720, 420)      # the floor, and it scrolls
    assert app._viewport_size((0, 0)) == app._VIEW_MIN


# --------------------------------------------------------------------------- the appearance button

def test_the_button_shows_the_saved_setting(window):
    win = window[0]

    assert win["appearance_toggle"].get_text() == "Theme: dark"


def test_a_rebuild_carries_the_form_across(window):
    """Switching theme rebuilds the window — a toolkit theme cannot be applied to one that exists —
    so anything already filled in has to survive, or the button costs more than it is worth."""
    win, real, _relations = window

    assert real["zip"] == SAVED_ZIP                          # a path, through the elision
    assert win["case_ref"].get() == "EXHIBIT-9"              # plain text
    assert win["expand_only"].get() is True                  # a checkbox


# --------------------------------------------------------------------------- the legacy reports

def test_the_legacy_setting_is_on_the_main_window_and_starts_off(window):
    """One question about the run, asked once. It used to live only in the relations dialog, where
    it decided a partial extract while a full run produced them regardless."""
    win = window[0]

    assert "legacy_reports" in win.AllKeysDict
    assert win["legacy_reports"].get() is False


def test_the_relations_dialog_no_longer_asks_it_again(window):
    """Two controls for one setting, one gating the other, is how an examiner ends up unsure which
    answer the report used. The dialog states it instead — pinned on the source, since building a
    second window in this process is not possible."""
    import inspect

    source = inspect.getsource(app._relations_dialog)

    assert 'key="legacy_reports"' not in source, "the dialog must not offer a second control"
    assert "set on the main window" in source


# --------------------------------------------------------------------------- what Ok actually reads

def _keys_main_reads():
    """Every key `main()` reads out of `values`, taken from the source.

    Both spellings, and the numeric one is the point: the crash was `values[0]`, so a check that only
    looked at quoted keys would have watched it happen. Derived rather than listed, so a field read in
    a new place cannot quietly escape the check below.
    """
    import inspect
    import re

    source = inspect.getsource(app.main)
    named = set(re.findall(r"""values(?:\.get)?\(?\[?["']([a-z_]+)["']""", source))
    positional = {int(n) for n in re.findall(r"values\[(\d+)\]", source)}
    return named | positional


def test_every_field_ok_reads_is_actually_on_the_form(window):
    """The beta.4 crash: the OS radios had no key, so they were numbered by their POSITION among the
    keyless elements — and re-ordering the form moved them from 0/1 to 1/2. `values[0]` was then a
    KeyError on Ok, after the examiner had filled everything in. Nothing here read the window's
    values, so nothing caught it; this does."""
    win = window[0]
    _event, values = win.read(timeout=10)

    missing = sorted(key for key in _keys_main_reads() if key not in values)

    assert not missing, f"main() reads {missing}, which the window does not produce"


def test_the_os_radios_are_keyed_rather_than_numbered(window):
    win, _real, _relations = window
    _event, values = win.read(timeout=10)

    assert values["os_ios"] is True and values["os_android"] is False
    assert not [k for k in values if isinstance(k, int)], \
        "a positional key is a layout change waiting to break the run"
