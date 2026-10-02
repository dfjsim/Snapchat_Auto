"""Every piece of third-party code the application carries is accounted for, and none is GPL.

``scripts/`` is compiled into an MIT-licensed EXE and MSI, so three things must hold: no source in it
carries a GPL/AGPL licence; every file in the repository that is not this project's own is named in
THIRD_PARTY_NOTICES.md; and every Python package the lock file says the app needs is in
THIRD_PARTY_LICENSES.txt at the version it is locked at. The build ships all three files.
"""
import os
import re
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build_tools"))

import collect_licenses  # noqa: E402


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def test_no_copyleft_source_in_the_application():
    copyleft = re.compile(r"GNU (Affero )?General Public License", re.I)
    offenders = []
    for base in ("scripts", "packages"):
        for dirpath, _dirs, names in os.walk(os.path.join(ROOT, base)):
            for name in names:
                if name.endswith(".py"):
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        head = fh.read(4000)
                    if copyleft.search(head) and "Lesser" not in head:
                        offenders.append(os.path.relpath(path, ROOT))
    assert offenders == []


def test_every_bundled_file_that_is_not_ours_has_a_notice():
    notices = _read("THIRD_PARTY_NOTICES.md")
    data = os.path.join(ROOT, "scripts", "data")
    for dirpath, _dirs, names in os.walk(data):
        if "__pycache__" in dirpath:
            continue
        for name in names:
            if name.endswith((".py", ".md", ".txt")):
                continue                                   # our code and documentation
            stem = "bootstrap" if name.startswith("bootstrap") else name
            assert stem in notices, f"{name} is bundled but not in THIRD_PARTY_NOTICES.md"
    assert "ccl_bplist.py" in notices


def test_every_locked_package_has_its_licence_listed():
    listed = _read("THIRD_PARTY_LICENSES.txt")
    with open(os.path.join(ROOT, "uv.lock"), "rb") as fh:
        versions = {collect_licenses._norm(p["name"]): p.get("version", "")
                    for p in tomllib.load(fh)["package"]}
    missing = [name for name in collect_licenses.runtime_closure()
               if not re.search(rf"^{re.escape(name)} {re.escape(versions.get(name, ''))}$", listed, re.M)]
    assert missing == [], ("THIRD_PARTY_LICENSES.txt is out of date — run "
                           "python build_tools/collect_licenses.py: " + ", ".join(missing))


def test_the_build_ships_the_licences():
    build = _read("build_nuitka.cmd")
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        copied = tomllib.load(fh)["tool"]["wix-build"]["copy_files"]
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.txt"):
        assert f"--include-data-files={name}={name}" in build
        assert name in copied
