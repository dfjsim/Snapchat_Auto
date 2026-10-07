"""The Related items dialog: its buttons stay in reach, and the relations a build added are marked.

Building the dialog needs a display and a Tk root of its own (a second root in one pytest process
fails), so it runs in a subprocess and is skipped where there is no GUI. Nothing is clicked: the
window's ``read`` is replaced by one that measures the window and answers Cancel.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("FreeSimpleGUI")


def _build_and_measure():
    code = textwrap.dedent("""
        import json
        import sys
        sys.path.insert(0, ROOT)
        import FreeSimpleGUI as sg
        import Snapchat_Auto as app
        from scripts import partial_report

        app.hidpi.force_dpi(None)          # not the developer's own saved text size
        out = {}

        def placed(root, widget):
            y = widget.winfo_rooty() - root.winfo_rooty()
            return (bool(widget.winfo_ismapped()) and y >= 0
                    and y + widget.winfo_height() <= root.winfo_height())

        def fake_read(self, *args, **kwargs):
            self.refresh()
            root = self.TKroot
            form = self["relations_form"]
            in_form = {str(getattr(el, "Key", None)) for row in form.Rows for el in row}
            out["scrolls"] = bool(form.Scrollable)
            out["buttons_in_list"] = sorted(in_form & {"Ok", "Cancel", "Minimal", "Everything"})
            buttons = [self[key].Widget for key in ("Ok", "Cancel", "Minimal")]
            out["at_open"] = all(placed(root, w) for w in buttons)
            _min_w, min_h = root.minsize()
            root.geometry(f"{root.winfo_width()}x{min_h}")
            root.update()
            out["at_floor"] = all(placed(root, w) for w in buttons)
            out["labels"] = {key: str(self["rel_" + key].Text) for key in ("conv_cache", "msg_cache")}
            return "Cancel", {}

        try:
            app.apply_theme("dark")
            sg.Window.read = fake_read
            state = {"relations": dict(partial_report.PRESETS["recommended"]), "transitive": False,
                     "legacy_reports": False, "added": ["conv_cache"]}
            app._relations_dialog(state)
        except Exception as error:
            if "display" in str(error).lower() or "tcl" in str(error).lower():
                print("NO-GUI"); sys.exit(0)
            raise
        print("RESULT " + json.dumps(out))
    """).replace("ROOT", repr(ROOT))
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         cwd=ROOT)
    if "NO-GUI" in res.stdout:
        pytest.skip("no GUI available here")
    assert res.returncode == 0, res.stderr[-2000:]
    line = next(x for x in res.stdout.splitlines() if x.startswith("RESULT "))
    return json.loads(line[len("RESULT "):])


@pytest.fixture(scope="module")
def measured():
    return _build_and_measure()


def test_the_buttons_are_outside_the_scrolling_list(measured):
    """Ok scrolled off the bottom of a fixed window is the same as no Ok at all — and the list of
    relations is taller than the window it opens in."""
    assert measured["scrolls"] is True
    assert measured["buttons_in_list"] == []


def test_the_buttons_are_in_the_window_when_it_opens_and_at_its_smallest(measured):
    """The list's requested height is a floor the packer honours before the button row: too tall,
    and a window shrunk to its minimum has no buttons at all, beyond the reach of the scrollbar."""
    assert measured["at_open"] is True
    assert measured["at_floor"] is True


def test_a_relation_added_since_the_policy_was_saved_is_marked(measured):
    from Snapchat_Auto import NEW_RELATION_MARK

    assert measured["labels"]["conv_cache"].endswith(NEW_RELATION_MARK)
    assert not measured["labels"]["msg_cache"].endswith(NEW_RELATION_MARK)
