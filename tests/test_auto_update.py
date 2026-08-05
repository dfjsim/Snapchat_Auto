"""What this repository still owns of the optional update check.

The check itself — which filenames count, which is newest, what happens when the folder is unset
or unreachable — lives in `dfjsim_shared_tools.auto_update` and is tested there. What is ours is
the release convention: the name and version this project publishes under have to be ones that
helper can find and compare, and the GUI has to survive the helper not being installed at all.

Every input is synthetic. No extraction data is required or used.
"""
import pytest

import Snapchat_Auto

auto_update = pytest.importorskip("dfjsim_shared_tools.auto_update",
                                  reason="the update check is optional, and so is its helper")


def test_this_projects_release_name_is_one_the_update_check_accepts(tmp_path):
    """End to end on the convention: the installer the builder names from pyproject.toml must be a
    file the update check can find and compare. Dropping the `+build.<N>` tag from the version, or
    renaming the artifact by hand, silently turns update checks off — nothing raises, no build is
    ever newer.
    """
    name, running = Snapchat_Auto.get_project_name(), Snapchat_Auto.get_version()
    (tmp_path / f"{name}-{running}{auto_update.ARCH_STR}.msi").write_bytes(b"")

    found, found_version = auto_update.newest_installer(name, tmp_path)

    assert found is not None, f"{name}-{running} is not a name the update check recognizes"
    assert str(found_version) == running


def test_a_missing_update_helper_leaves_the_gui_working(monkeypatch):
    """requirements.txt (the pip route the README documents) does not carry the helper, so the
    Check button has to answer for itself rather than raise on the import."""
    monkeypatch.setattr(Snapchat_Auto, "_updater", lambda: None)

    ok, message = Snapchat_Auto.check_installer_dir("//somewhere/builds")

    assert not ok
    assert "dfjsim_shared_tools" in message
