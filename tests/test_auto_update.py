"""What this repository still owns of the optional update check.

The check itself — which filenames count, which is newest, what happens when the folder is unset
or unreachable — lives in `dfjsim_shared_tools.auto_update` and is tested there. What is ours is
the release convention: the name and version this project publishes under have to be ones that
helper can find and compare, and the GUI has to survive the helper not being installed at all.

Every input is synthetic. No extraction data is required or used.
"""
import pytest
from packaging import version

import Snapchat_Auto

auto_update = pytest.importorskip("dfjsim_shared_tools.auto_update",
                                  reason="the update check is optional, and so is its helper")


def test_this_projects_release_name_is_one_the_update_check_accepts(tmp_path):
    """End to end on the convention: the installer the builder names from pyproject.toml must be a
    file the update check can find and compare. Dropping the `+build.<N>` tag from the version, or
    renaming the artifact by hand, silently turns update checks off — nothing raises, no build is
    ever newer.

    Compared as **versions**, not as strings, which is what the check itself does: a pre-release
    version is not stored the way it is written (``1.6.0-beta.1`` is the PEP 440 version
    ``1.6.0b1``), so a string comparison would fail on a beta release while the check it stands for
    works perfectly.
    """
    name, running = Snapchat_Auto.get_project_name(), Snapchat_Auto.get_version()
    (tmp_path / f"{name}-{running}{auto_update.ARCH_STR}.msi").write_bytes(b"")

    found, found_version = auto_update.newest_installer(name, tmp_path)

    assert found is not None, f"{name}-{running} is not a name the update check recognizes"
    assert found_version == version.parse(running)


def test_a_pre_release_is_older_than_the_version_it_leads_to():
    """The reason `[project].version` carries the marker while the MSI ProductVersion cannot: it is
    what puts a beta *below* its own release and above the last one, so an examiner running the beta
    is still offered 1.6.0 when it lands."""
    beta = version.parse("1.6.0-beta.1+build.20260811")

    assert version.parse("1.5.2+build.20260808") < beta < version.parse("1.6.0+build.20260901")
    assert beta.is_prerelease


def test_a_missing_update_helper_leaves_the_gui_working(monkeypatch):
    """requirements.txt (the pip route the README documents) does not carry the helper, so the
    Check button has to answer for itself rather than raise on the import."""
    monkeypatch.setattr(Snapchat_Auto, "_updater", lambda: None)

    ok, message = Snapchat_Auto.check_installer_dir("//somewhere/builds")

    assert not ok
    assert "dfjsim_shared_tools" in message
