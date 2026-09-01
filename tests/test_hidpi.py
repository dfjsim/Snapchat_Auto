"""DPI awareness, the pixel scaling that has to go with it, and the switches that control both.

The half of this that matters most is not testable here — whether Windows stops stretching the
window is something only a scaled display shows. What *is* testable is the arithmetic that has to
follow the awareness claim, the option parsing an examiner reaches it through, and the promise that
none of it can take a run down: an extraction must not fail because a cosmetic Windows API was
missing or a policy blocked it.
"""
import sys

import pytest

from scripts import hidpi


@pytest.fixture(autouse=True)
def no_forced_scale():
    """The forced DPI is process-wide, so a test that sets one must not leak it into the next."""
    yield
    hidpi.force_dpi(None)


# --------------------------------------------------------------------------- the arithmetic

def test_a_100_percent_display_changes_nothing(monkeypatch):
    """The 96-dpi case has to be the identity, or every constant in the GUI would have moved.

    Pinned rather than assumed: this has to give the same answer on the reviewer's scaled laptop.
    """
    monkeypatch.setattr(hidpi, "dpi", lambda: 96)

    assert hidpi.px(760) == 760
    assert hidpi.px2((1000, 880)) == (1000, 880)
    assert hidpi.scale() == 1.0
    assert hidpi.tk_scaling() == pytest.approx(96 / 72)


def test_a_125_percent_display_scales_pixels_and_points(monkeypatch):
    monkeypatch.setattr(hidpi, "dpi", lambda: 120)

    assert hidpi.scale() == 1.25
    assert hidpi.px(760) == 950
    assert hidpi.px2((720, 420)) == (900, 525)
    # tk scaling is pixels per *point*, not a multiplier: 120/72, which is what makes an 11pt font
    # 21 pixels tall instead of 17. Handing Tk the 1.25 would leave the text a quarter too small.
    assert hidpi.tk_scaling() == pytest.approx(120 / 72)


def test_odd_scales_land_on_whole_pixels(monkeypatch):
    """150% and 175% are ordinary laptop settings; a fractional pixel is not a size Tk accepts."""
    monkeypatch.setattr(hidpi, "dpi", lambda: 168)           # 175%

    assert hidpi.px2((720, 420)) == (1260, 735)
    assert all(isinstance(value, int) for value in hidpi.px2((721, 421)))


# --------------------------------------------------------------------------- --dpi-scale

@pytest.mark.parametrize("given", [125, "125", "125%", " 125 % ", 1.25, "1.25", "1,25"])
def test_every_way_of_writing_125_percent_means_the_same_thing(given):
    """Windows shows a percentage and this module works in factors, so both have to be accepted."""
    assert hidpi.parse_scale(given) == 120


@pytest.mark.parametrize("given", ["", None, "abc", 0, -125, 1250, 45, 401, "%"])
def test_a_scale_that_is_not_one_is_refused_rather_than_applied(given):
    """A typo must not produce a window that cannot be operated, so it is rejected, not clamped."""
    assert hidpi.parse_scale(given) is None


def test_a_forced_scale_overrides_what_the_display_reports():
    hidpi.force_dpi(144)

    assert hidpi.dpi() == 144
    assert hidpi.scale() == 1.5
    assert hidpi.forced() == 144
    assert hidpi.px(760) == 1140


# --------------------------------------------------------------------------- the option parsing

def test_the_options_are_taken_out_of_the_arguments_they_travel_with():
    """They modify whatever else was asked for, and the app dispatches on its first argument only:
    left in place, "--dpi-scale 150" would be read as an unknown command."""
    options, remaining = hidpi.strip_options(
        ["--dpi-scale", "150", "--zip", "E.zip", "--dpi-awareness=system", "--workdir", "D:/w"])

    assert options == {"scale": "150", "awareness": "system"}
    assert remaining == ["--zip", "E.zip", "--workdir", "D:/w"]


def test_a_command_line_with_none_of_them_comes_back_untouched():
    argv = ["--zip", "E.zip", "--relations", "recommended"]

    assert hidpi.strip_options(argv) == ({}, argv)


def test_the_report_flag_takes_no_value():
    options, remaining = hidpi.strip_options(["--dpi-report", "--zip", "E.zip"])

    assert options == {"report": True}
    assert remaining == ["--zip", "E.zip"]                   # the ZIP was not eaten as its value


# --------------------------------------------------------------------------- configure()

@pytest.fixture
def config(tmp_path):
    """A saved GUI config, which is how this reaches an examiner who will never type a flag."""
    def write(**settings):
        import json
        path = tmp_path / "gui.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        return str(path)
    return write


def test_the_command_line_beats_the_environment_beats_the_saved_settings(monkeypatch, config):
    monkeypatch.setenv(hidpi.SCALE_ENV, "150")
    saved = config(dpi_scale=175, dpi_awareness="system")

    hidpi.configure(["--dpi-scale", "125", "--dpi-awareness", "unaware"], saved)
    assert hidpi.dpi() == 120                                # the command line won

    hidpi.force_dpi(None)
    hidpi.configure(["--dpi-awareness", "unaware"], saved)
    assert hidpi.dpi() == 144                                # then the environment

    hidpi.force_dpi(None)
    monkeypatch.delenv(hidpi.SCALE_ENV)
    hidpi.configure(["--dpi-awareness", "unaware"], saved)
    assert hidpi.dpi() == 168                                # then what was saved


def test_a_missing_or_broken_config_is_not_an_error(tmp_path):
    """It is read before the application's own loader exists, on a path an examiner can edit."""
    broken = tmp_path / "gui.json"
    broken.write_text("{ not json", encoding="utf-8")

    for path in (None, str(tmp_path / "absent.json"), str(broken)):
        settings = hidpi.configure(["--dpi-awareness", "unaware"], path)
        assert settings["mode"] == "unaware"
        assert hidpi.forced() is None


def test_an_unknown_mode_falls_back_to_auto_rather_than_refusing_to_start(config):
    settings = hidpi.configure(["--dpi-awareness", "sharper-please"], config())

    assert settings["mode"] == "auto"


def test_the_report_flag_claims_nothing_itself(monkeypatch):
    """--dpi-report has to show the unaware reading, and awareness is a one-shot per process."""
    claimed = []
    monkeypatch.setattr(hidpi, "enable", lambda mode: claimed.append(mode))

    settings = hidpi.configure(["--dpi-report"], None)

    assert settings["report"] is True
    assert claimed == []


def test_the_startup_line_names_the_switch_that_changed_the_size():
    """A surprising text size has to be traceable to the flag that caused it, not to the display."""
    settings = hidpi.configure(["--dpi-scale", "150", "--dpi-awareness", "unaware"], None)
    line = hidpi.describe(settings)

    assert "144 dpi (150%)" in line
    assert "--dpi-scale" in line
    assert "unaware" in line


# --------------------------------------------------------------------------- it cannot fail a run

def test_nothing_here_can_fail_a_run(monkeypatch):
    """Every entry point is called for its side effect on a window nobody has opened yet."""
    monkeypatch.setattr(hidpi, "_on_windows", lambda: False)

    assert hidpi.enable() is None                            # not Windows: nothing to claim
    assert hidpi.awareness() is None
    assert hidpi.dpi() == hidpi.BASE_DPI                     # and so the scaling is the identity
    assert hidpi.print_report() == 0


@pytest.mark.skipif(sys.platform != "win32", reason="the awareness APIs are Windows-only")
def test_the_process_ends_up_dpi_aware_on_windows():
    """0 — unaware — is the state that produces the stretched window, so it is the one failure.

    Snapchat_Auto claims this at import, before any window exists; calling it again is what a
    manifest-set process looks like (every call refused) and must reach the same answer.
    """
    assert hidpi.enable("auto") in (1, 2)
    assert hidpi.awareness() in (1, 2)


@pytest.mark.skipif(sys.platform != "win32", reason="the awareness APIs are Windows-only")
def test_asking_for_unaware_claims_nothing_new():
    """The point of the mode: it must not quietly upgrade the process it was asked to leave alone.

    It cannot *un*-claim what another test already claimed -- awareness is one-shot per process --
    so what is checked is that it made no call of its own.
    """
    before = hidpi.awareness()

    assert hidpi.enable("unaware") == before


@pytest.mark.skipif(sys.platform != "win32", reason="the awareness APIs are Windows-only")
def test_the_display_reports_a_believable_dpi():
    """A DPI of 0 or None would divide the whole GUI by nothing; the fallbacks exist for that."""
    assert hidpi.dpi() >= hidpi.BASE_DPI
    assert 1.0 <= hidpi.scale() <= 4.0
