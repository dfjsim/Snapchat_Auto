"""The build's Nuitka user plugin that extracts the Tcl/Tk 9 libraries a CPython keeps inside its DLLs.

Each "DLL" here is a few bytes of stand-in code with a zip archive appended, which is how the Python
install manager's Tcl 9 DLLs carry ``tcl_library/`` and ``tk_library/``. Every input is synthetic.
"""
import importlib.util
import io
import os
import zipfile

import pytest

pytest.importorskip("nuitka")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _plugin():
    spec = importlib.util.spec_from_file_location(
        "nuitka_tcl_zipfs", os.path.join(ROOT, "build_tools", "nuitka_tcl_zipfs.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dll(path, members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    with open(path, "wb") as fh:
        fh.write(b"MZ" + b"\0" * 62 + buf.getvalue())


@pytest.fixture
def prefix(tmp_path):
    dlls = tmp_path / "python" / "DLLs"
    dlls.mkdir(parents=True)
    _dll(dlls / "tcl90.dll", {"tcl_library/init.tcl": "# init", "tcl_library/encoding/x.enc": "e",
                              "tcl_library/../escape.tcl": "no"})
    _dll(dlls / "tcl9tk90.dll", {"tk_library/dialog.tcl": "# dialog", "tk_library/ttk/a.tcl": "t"})
    (dlls / "tcl90.dll.bak").write_bytes(b"not a dll")
    return str(tmp_path / "python")


def test_the_libraries_inside_the_dlls_are_extracted_and_named(prefix, tmp_path):
    plugin, env = _plugin(), {}
    out = str(tmp_path / "out")
    done = plugin.prepare(prefix, out_dir=out, environ=env)
    assert set(done) == {"TCL_LIBRARY", "TK_LIBRARY"} and done == env
    assert open(os.path.join(env["TCL_LIBRARY"], "init.tcl")).read() == "# init"
    assert os.path.isfile(os.path.join(env["TCL_LIBRARY"], "encoding", "x.enc"))
    assert os.path.isfile(os.path.join(env["TK_LIBRARY"], "ttk", "a.tcl"))
    # a member that would land outside the output folder is not written
    assert not os.path.exists(os.path.join(out, "escape.tcl"))
    assert not os.path.exists(os.path.join(str(tmp_path), "escape.tcl"))


def test_a_stale_file_of_an_earlier_extraction_does_not_survive(prefix, tmp_path):
    plugin, out = _plugin(), str(tmp_path / "out")
    stale = os.path.join(out, "tcl_library", "old.tcl")
    os.makedirs(os.path.dirname(stale))
    open(stale, "w").close()
    plugin.prepare(prefix, out_dir=out, environ={})
    assert not os.path.exists(stale)


def test_nothing_is_done_when_the_variables_are_set_or_the_folders_exist(prefix, tmp_path):
    plugin, out = _plugin(), str(tmp_path / "out")
    given = {"TCL_LIBRARY": "x", "TK_LIBRARY": "y"}
    assert plugin.prepare(prefix, out_dir=out, environ=given) == {} and given == {"TCL_LIBRARY": "x",
                                                                                  "TK_LIBRARY": "y"}
    # one set by the examiner is kept; the other is still supplied
    one = {"TCL_LIBRARY": "x"}
    assert set(plugin.prepare(prefix, out_dir=out, environ=one)) == {"TK_LIBRARY"}
    assert one["TCL_LIBRARY"] == "x"
    # the classic layout (tcl/tcl9.0/init.tcl) is left to tk-inter's own lookup
    classic = os.path.join(prefix, "tcl", "tcl9.0")
    os.makedirs(classic)
    open(os.path.join(classic, "init.tcl"), "w").close()
    assert plugin.prepare(prefix, out_dir=out, environ={}) == {}


def test_an_install_without_such_dlls_gives_nothing(tmp_path):
    plugin = _plugin()
    assert plugin.find_libraries(str(tmp_path / "missing")) == {}
    empty = tmp_path / "DLLs"
    empty.mkdir()
    (empty / "tcl86t.dll").write_bytes(b"MZ plain dll")
    assert plugin.find_libraries(str(empty)) == {}
    assert plugin.prepare(str(tmp_path), out_dir=str(tmp_path / "out"), environ={}) == {}


def test_the_entry_point_names_the_plugin_for_both_builds():
    head = open(os.path.join(ROOT, "Snapchat_Auto.py"), encoding="utf-8").read(2000)
    assert "# nuitka-project: --user-plugin={MAIN_DIRECTORY}/build_tools/nuitka_tcl_zipfs.py" in head
