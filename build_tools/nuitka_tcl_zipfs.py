"""Nuitka user plugin: hand tk-inter the Tcl/Tk script libraries that a Tcl 9 build keeps inside its DLLs.

The Python install manager's CPython (3.14 and later on Windows) ships Tcl/Tk 9, whose script
libraries are not folders under the install: each one is a zip archive appended to its DLL, which
Tcl mounts at run time (``info library`` answers ``//zipfs:/lib/tcl/tcl_library``). Nuitka's tk-inter
plugin looks only for folders (``<prefix>/tcl/tcl9.0``) or a separate ``.zip``, finds neither, and
stops the build with "Could not find Tcl". This plugin, named in ``Snapchat_Auto.py`` with
``# nuitka-project: --user-plugin=…`` so that ``uv run build`` and ``build_nuitka.cmd`` both load it,
extracts the two libraries from those DLLs into ``build/tcl_zipfs/`` and points tk-inter at them
through ``TCL_LIBRARY`` / ``TK_LIBRARY`` — the first places it looks. The libraries come out of the
very DLLs the build bundles, so their version is the one the application runs with.

It does nothing when ``TCL_LIBRARY`` / ``TK_LIBRARY`` are already set, when the interpreter has the
classic folders, or when no DLL carries a library — tk-inter's own lookup then decides, as before.
Standard library plus Nuitka's plugin base; it lives outside ``scripts/``, which is bundled into the
application.
"""
import os
import shutil
import sys
import zipfile

from nuitka.plugins.PluginBase import NuitkaPluginBase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Where the libraries are extracted to; ``build/`` is ignored by git.
OUT_DIR = os.path.join(ROOT, "build", "tcl_zipfs")
#: (library, the folder it sits in inside its DLL's archive, a file every copy of it has)
LIBRARIES = (("TCL_LIBRARY", "tcl_library", "init.tcl"), ("TK_LIBRARY", "tk_library", "dialog.tcl"))


def _has_classic_folders(prefix):
    """Whether the install has the Tcl library as a folder (``tcl/tcl8.6`` or ``tcl/tcl9.0``)."""
    tcl = os.path.join(prefix, "tcl")
    return os.path.isdir(tcl) and any(
        os.path.isfile(os.path.join(tcl, name, "init.tcl")) for name in os.listdir(tcl))


def find_libraries(dll_dir):
    """``{env var: (dll path, folder inside its archive)}`` for each library a DLL there carries."""
    found = {}
    if not os.path.isdir(dll_dir):
        return found
    for name in sorted(os.listdir(dll_dir)):
        path = os.path.join(dll_dir, name)
        if not (name.lower().endswith(".dll") and name.lower().startswith("tcl")):
            continue
        try:
            if not zipfile.is_zipfile(path):
                continue
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            continue
        for env, folder, marker in LIBRARIES:
            if env not in found and f"{folder}/{marker}" in names:
                found[env] = (path, folder)
    return found


def extract(dll_path, folder, out_dir):
    """Extract ``folder/`` of the DLL's archive into ``out_dir/folder``; that directory's path.

    The copy is made afresh each time (a few hundred small files), so a Python update can never
    leave an older library beside a newer DLL. A member that would land outside that folder is skipped.
    """
    target = os.path.join(out_dir, folder)
    shutil.rmtree(target, ignore_errors=True)
    root = os.path.realpath(target)
    with zipfile.ZipFile(dll_path) as archive:
        for member in archive.infolist():
            if not member.filename.startswith(folder + "/") or member.is_dir():
                continue
            dest = os.path.realpath(os.path.join(out_dir, *member.filename.split("/")))
            if os.path.commonpath((root, dest)) != root:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with archive.open(member) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return target


def prepare(prefix=None, out_dir=OUT_DIR, environ=os.environ):
    """Extract the libraries the interpreter at ``prefix`` keeps in its DLLs and set the variables
    tk-inter reads; ``{env var: directory}`` of what was set (empty when nothing needed doing)."""
    prefix = prefix or sys.base_prefix
    if all(env in environ for env, _folder, _marker in LIBRARIES) or _has_classic_folders(prefix):
        return {}
    done = {}
    for env, (dll, folder) in find_libraries(os.path.join(prefix, "DLLs")).items():
        if env in environ:
            continue
        environ[env] = done[env] = extract(dll, folder, out_dir)
    return done


class NuitkaPluginTclZipfs(NuitkaPluginBase):
    plugin_name = "tcl-zipfs"
    plugin_desc = "Extracts the Tcl/Tk libraries a Tcl 9 build keeps inside its DLLs, for tk-inter."

    def __init__(self):
        for env, path in prepare().items():
            self.info(f"{env} = {path} (extracted from the interpreter's Tcl DLLs)")
