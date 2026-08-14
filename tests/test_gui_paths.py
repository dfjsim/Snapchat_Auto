"""What the settings window shows for a path, and what the run is given.

A long path does not fit the field, and which end matters depends on what is being checked — the
right case folder, or the right file — so it is shortened from the middle and both ends stay
visible. That makes the box's text an *abbreviation*, and the one thing that must never happen is a
run being handed the abbreviation: "C:/Users/…/x.zip" is not a file, and it would be reported as a
missing extraction rather than as a display artefact.

So the true value lives beside the widget and :func:`reconcile_paths` puts it back into ``values``
once, immediately after every read, rather than at each of the fifteen places downstream that read a
path. These tests hold that: what the examiner types wins, what they did not touch survives, and no
elision ever reaches the caller.

No GUI is created here — both functions are plain string work.
"""
import Snapchat_Auto as app


LONG = r"D:\cases\HOM-2026-0042\exhibit B\phone 1\extraction\full filesystem\EXTRACTION_FFS.zip"
SHORT = r"D:\x.zip"


# --------------------------------------------------------------------------- the abbreviation

def test_a_long_path_keeps_both_ends():
    shown = app.elide_middle(LONG, 40)

    assert len(shown) == 40
    assert shown.startswith("D:\\cases")                    # which case
    assert shown.endswith("EXTRACTION_FFS.zip")             # which file
    assert "\u2026" in shown


def test_a_path_that_fits_is_left_alone():
    assert app.elide_middle(SHORT, 40) == SHORT
    assert app.elide_middle("", 40) == ""
    assert app.elide_middle(None, 40) == ""


def test_a_width_too_small_to_say_anything_is_not_mangled():
    """Better the field scrolls than that it shows two characters and an ellipsis."""
    assert app.elide_middle(LONG, 8) == LONG


# --------------------------------------------------------------------------- what the run gets

def test_the_real_path_is_what_reaches_the_caller():
    real = {"zip": LONG}
    values = {"zip": app.elide_middle(LONG)}                # what the box is showing

    assert app.reconcile_paths(values, real)["zip"] == LONG


def test_typing_over_an_elided_path_replaces_it():
    """The box no longer shows what we put there, so the examiner changed it and it is now the value."""
    real = {"zip": LONG}
    values = {"zip": r"D:\other.zip"}

    out = app.reconcile_paths(values, real)

    assert out["zip"] == r"D:\other.zip" and real["zip"] == r"D:\other.zip"


def test_a_field_that_was_never_elided_still_reads_back():
    real = {}
    values = {"workdir": r"C:\Temp\runs"}

    assert app.reconcile_paths(values, real)["workdir"] == r"C:\Temp\runs"
    assert real["workdir"] == r"C:\Temp\runs"


def test_clearing_a_field_clears_the_value():
    real = {"keychain": LONG}
    values = {"keychain": ""}

    assert app.reconcile_paths(values, real)["keychain"] == ""


def test_a_key_the_window_does_not_have_is_left_out():
    """The relations dialog and the disclaimer read windows with no path fields at all."""
    assert app.reconcile_paths({"other": "x"}, {}) == {"other": "x"}


def test_every_path_field_goes_through_it():
    """A field added to the form but not to PATH_KEYS would show its elision to the run."""
    assert set(app.PATH_KEYS) == {"zip", "keychain", "workdir", "selection", "installer_dir"}


# --------------------------------------------------------------------------- appearance

def test_the_theme_follows_the_os_and_the_hint_stays_readable():
    """A hint is secondary, not invisible: it was near-white because the old theme's mid-blue left
    nothing else legible, which is how the form ended up pale-on-pale."""
    for appearance in ("dark", "light"):
        assert app.apply_theme(appearance) == appearance
        assert app.hint_color() == app._HINT_COLOR[appearance]
    assert app.os_appearance() in ("dark", "light")          # never raises, never anything else
    assert app.apply_theme("os") == app.os_appearance()      # and "os" resolves to whatever it says


def test_the_button_cycles_all_three_and_comes_back():
    """Forcing light or dark is a normal thing to want; following the OS is the one that stays right
    when the OS changes, so it is where the cycle starts."""
    assert app.APPEARANCE_CHOICES[0] == "os"
    seen, setting = [], "os"
    for _ in range(len(app.APPEARANCE_CHOICES)):
        setting = app.next_appearance(setting)
        seen.append(setting)

    assert seen == ["light", "dark", "os"]
    assert app.next_appearance("nonsense-from-a-hand-edited-config") == "os"


def test_each_setting_says_what_it_is():
    assert app.appearance_label("os") == "Theme: follow OS"
    assert app.appearance_label("dark") == "Theme: dark"
    assert app.appearance_label("who knows") == "Theme: follow OS"


def test_a_question_mark_keeps_its_text_and_shows_it_on_demand(monkeypatch):
    """Every explanation used to be printed under its field, which is accurate and unreadable: a form
    of twenty settings became a wall of prose. The text is the same; where it lives is not."""
    shown = {}
    monkeypatch.setattr(app, "_show_help_window",
                        lambda title, body: shown.update(title=title, body=body))

    mark = app._help("The keychain is only needed for iOS.", title="Keychain")

    assert app._handle_help(mark.Key) is True
    assert shown["title"] == "Keychain"
    assert "keychain is only needed" in shown["body"]
    # and on hover, without a click (the live TooltipObject only exists once it is in a window)
    assert "keychain is only needed" in mark.Tooltip


def test_the_mark_is_something_you_can_press():
    """"(?)" set in text read as a footnote. A mark that does something has to look like it does."""
    mark = app._help("anything")

    assert mark.__class__.__name__ == "Button"
    assert mark.ButtonText == "?"


def test_an_ordinary_event_is_not_swallowed():
    """The loops call this first, so anything that is not a "?" has to fall straight through."""
    assert app._handle_help("Ok") is False
    assert app._handle_help(None) is False


def test_the_popup_text_is_wrapped_once_and_keeps_its_paragraphs():
    """The ragged popups were text wrapped twice — here at 76 columns, then again by the label at
    whatever width the window turned out to be. One wrap, and the window is built to that width."""
    text = ("A selection.json an examiner saved from the reports, which names the rows to render.\n\n"
            "The whole workflow is in the guide.")

    body = app.help_body(text)

    assert max(len(line) for line in body.split("\n")) <= app.HELP_WRAP
    assert "\n\n" in body, "the blank line between paragraphs has to survive"
    assert body.count("\n\n") == 1
    # and no line is broken mid-word
    assert all(not line.startswith(" ") for line in body.split("\n") if line)


def test_a_hint_is_a_point_smaller_than_the_body_text():
    """They move together: the size difference is what marks a hint as secondary."""
    assert app.HINT_FONT[1] == app.BASE_FONT[1] - 1


def test_the_disclaimer_is_given_room_for_all_of_itself():
    """It was sized at ten rows and needs eleven, so the last line of an AS-IS disclaimer was cut."""
    rows = app._wrapped_rows(app.DISCLAIMER_TEXT, 78)

    assert rows >= 11
    assert app._wrapped_rows("one line", 78) == 1
    assert app._wrapped_rows("a\n\nb", 78) == 3             # a blank line still takes a row
