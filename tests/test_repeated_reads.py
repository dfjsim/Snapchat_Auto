"""Shared inputs a run reads once, not once per report.

Two reports want the keychain and two want `primary.docobjects`, and each used to read its own
copy. The cost was small; the problem was the log. A repeated "persistedkey not present" WARNING or
a repeated "N identifier record(s)" line reads like a second, independent finding about the device,
which is exactly the wrong thing for a log an examiner has to stand behind.

Every input here is synthetic. No extraction data is required or used.
"""
import logging
import plistlib
import time

import pandas as pd

from scripts import DecryptLocalMemories_iOS as memkeys
from scripts import contacts_report


EGOCIPHER = "egocipher.key.avoidkeyderivation"
SNAP_GROUP = "3MY7A92V5W.com.toyopagroup.picaboo"


def _keychain(path, secret=b"\x11" * 32):
    """A GrayKey-shaped keychain plist holding one Snapchat egocipher item."""
    with open(path, "wb") as handle:
        plistlib.dump([{"acct": EGOCIPHER, "agrp": SNAP_GROUP, "v_Data": secret}], handle)
    return str(path)


def _reads(caplog):
    """How many times the keychain was actually parsed, by its one-per-read banner."""
    return len([r for r in caplog.records if "item(s) scanned" in r.message])


def test_second_read_of_the_same_keychain_is_served_from_the_cache(tmp_path, caplog):
    memkeys.clear_keychain_cache()
    path = _keychain(tmp_path / "keychain.plist")

    with caplog.at_level(logging.INFO):
        first = memkeys.read_keychain_status(path)
        second = memkeys.read_keychain_status(path)

    assert first == second
    assert _reads(caplog) == 1
    # the second call still says the keychain was used, but as a back-reference to the first
    assert sum(1 for r in caplog.records if "reusing the read of" in r.message) == 1
    # and the WARNING an examiner reads as a finding is stated exactly once
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_a_caller_editing_the_result_cannot_change_the_next_read(tmp_path):
    memkeys.clear_keychain_cache()
    path = _keychain(tmp_path / "keychain.plist")

    first = memkeys.read_keychain_status(path)
    first["egocipher"] = "TAMPERED"
    first["persistedkeys"]["injected"] = "0000"

    second = memkeys.read_keychain_status(path)
    assert second["egocipher"] != "TAMPERED"
    assert "injected" not in second["persistedkeys"]


def test_a_changed_keychain_is_read_again(tmp_path, caplog):
    """Caching on the path alone would serve stale keys after a re-export."""
    memkeys.clear_keychain_cache()
    path = _keychain(tmp_path / "keychain.plist")
    first = memkeys.read_keychain_status(path)

    time.sleep(0.01)                      # a distinct mtime, whatever the filesystem's resolution
    _keychain(tmp_path / "keychain.plist", secret=b"\x22" * 32)

    with caplog.at_level(logging.INFO):
        second = memkeys.read_keychain_status(path)

    assert second["egocipher"] != first["egocipher"]
    assert _reads(caplog) == 1


def test_clear_keychain_cache_forces_a_fresh_read(tmp_path, caplog):
    """`Snapchat_Auto.run` calls this so one case's keys never leak into the next run."""
    memkeys.clear_keychain_cache()
    path = _keychain(tmp_path / "keychain.plist")
    memkeys.read_keychain_status(path)
    memkeys.clear_keychain_cache()

    with caplog.at_level(logging.INFO):
        memkeys.read_keychain_status(path)

    assert _reads(caplog) == 1


def test_contacts_report_uses_the_identifiers_it_is_given(monkeypatch):
    """Passed identifiers must be applied without re-reading primary.docobjects."""
    calls = []
    monkeypatch.setattr(contacts_report, "load_identifiers",
                        lambda primary: calls.append(primary) or {})

    contacts = contacts_report.apply_identifiers(
        [{"user_id": "abc", "username": "", "display": "", "legacy_username": ""}],
        {"abc": {"username": "from-the-caller", "legacy_username": "", "superseded": []}})

    assert not calls
    assert contacts[0]["username"] == "from-the-caller"


def test_contacts_report_still_reads_the_file_when_given_nothing(tmp_path, monkeypatch):
    """The reports stay usable on their own — identifiers=None means load them here."""
    calls = []
    monkeypatch.setattr(contacts_report, "load_identifiers",
                        lambda primary: calls.append(primary) or {})
    monkeypatch.setattr(contacts_report, "generate_report",
                        lambda *a, **k: str(tmp_path / "Contacts_report.html"))

    contacts_report.main(pd.DataFrame(), str(tmp_path), primary="primary.docobjects")

    assert calls == ["primary.docobjects"]
