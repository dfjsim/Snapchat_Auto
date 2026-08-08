"""The application's own version and distribution name.

This lived in ``Snapchat_Auto.py``, but the version is no longer needed only by the GUI and the
update check: a report stamps it into the page, and — because it decides whether a later run may
reuse anything a previous one extracted or decrypted — into the examiner's saved selection file.
Every report module therefore needs it, and none of them can import ``Snapchat_Auto``: doing so
re-runs that module's top-level logging/environment setup, which under a Nuitka onefile build is
worse than merely wasteful.

Stdlib only, deliberately. ``pyproject.toml`` is the single source of truth (the build copies it
next to the binary — see ``[tool.wix-build] copy_files``), which is what keeps the ``+build.<N>``
tag the update check compares against in step with the version a report claims.
"""

import os
import sys
import logging

logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    _app_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.argv[0] or ".")))
else:
    # scripts/ -> the project root
    _app_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pyproject_candidates():
    """Locations pyproject.toml may live in across run modes: source tree, a Nuitka onefile bundle
    (dirname(__file__)), a PyInstaller bundle (sys._MEIPASS), and beside the built binary."""
    seen, out = set(), []
    for base in (_app_path,
                 os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 os.path.dirname(os.path.abspath(__file__)),
                 getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else None):
        if not base:
            continue
        cand = os.path.join(base, "pyproject.toml")
        if cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def _pyproject_field(key):
    """Read `[project].<key>` from whichever pyproject.toml this run mode can see, else None."""
    try:
        import tomllib
    except Exception:
        return None
    for path in _pyproject_candidates():
        try:
            with open(path, "rb") as f:
                value = tomllib.load(f).get("project", {}).get(key)
        except FileNotFoundError:
            continue
        except Exception:
            continue
        if value:
            return value
    return None


def get_version():
    """Return the project version — from a bundled/source pyproject.toml, else package metadata.

    The build bundles pyproject.toml (see build_nuitka.cmd) so the frozen GUI shows the real version;
    Nuitka sets neither sys.frozen nor sys._MEIPASS, so we probe several candidate locations.
    """
    v = _pyproject_field("version")
    if v:
        return v
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("Snapchat_Auto")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    return "unknown"


def get_project_name():
    """The distribution name. It is the first half of the installer filenames the update check
    matches (`Snapchat_Auto-<version>-win64.msi`), so it has to come from the same pyproject.toml
    as the version it is published with."""
    return _pyproject_field("name") or "Snapchat_Auto"
