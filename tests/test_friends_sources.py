"""The friends/contacts source chain in ParseSnapchat_iOS.

`getFriendsPlist` is the first of four sources main() tries in order. A missing 'share_user' key
means the app version keeps the friends list elsewhere — a fact about the device, not a failure —
so it must fall through quietly and must not leave a scratch file behind in the run folder.
"""
import io
import logging
import os
import plistlib

import pytest

from scripts import ParseSnapchat_iOS as parse_snapchat_ios
from scripts.data import ccl_bplist


def _nskeyedarchiver(payload):
    """A minimal NSKeyedArchiver blob of the kind Snapchat embeds in these plists."""
    return plistlib.dumps({
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": ["$null", payload],
    }, fmt=plistlib.FMT_BINARY)


def _write_plist(tmp_path, mapping):
    path = tmp_path / "group.snapchat.picaboo.plist"
    with open(path, "wb") as handle:
        plistlib.dump(mapping, handle)
    return str(path)


def test_missing_share_user_falls_through_without_an_error_log(tmp_path, caplog, monkeypatch):
    """iOS 13.49 has no 'share_user'; that used to be logged as an ERROR on every run."""
    monkeypatch.chdir(tmp_path)
    plist = _write_plist(tmp_path, {"some_other_key": b"x"})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(KeyError):
            parse_snapchat_ios.getFriendsPlist(plist)

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("does not keep the friends list there" in r.message for r in caplog.records)


def test_older_user_format_is_a_warning_not_a_silent_pass(tmp_path, caplog, monkeypatch):
    """'user' is a format the script cannot read — that one is worth a warning."""
    monkeypatch.chdir(tmp_path)
    plist = _write_plist(tmp_path, {"user": _nskeyedarchiver({"NAME": "x"})})

    with caplog.at_level(logging.DEBUG):
        try:
            parse_snapchat_ios.getFriendsPlist(plist)
        except Exception:                                  # downstream parsing is out of scope here
            pass

    assert any(r.levelno == logging.WARNING and "does not support yet" in r.message
               for r in caplog.records)


def test_no_scratch_plist_is_left_in_the_working_directory(tmp_path, monkeypatch):
    """The parsers used to round-trip the blob through ./test.plist, i.e. into the run folder."""
    monkeypatch.chdir(tmp_path)
    plist = _write_plist(tmp_path, {"some_other_key": b"x"})

    with pytest.raises(KeyError):
        parse_snapchat_ios.getFriendsPlist(plist)

    assert not os.path.exists(tmp_path / "test.plist")


def test_in_memory_parse_matches_the_old_file_round_trip(tmp_path):
    """Replacing the scratch file with BytesIO must not change what the deserialiser returns."""
    blob = _nskeyedarchiver({"NAME": "alice", "N": 3, "SUB": plistlib.UID(2)})

    scratch = tmp_path / "t.plist"
    scratch.write_bytes(blob)
    with open(scratch, "rb") as handle:
        via_file = ccl_bplist.deserialise_NsKeyedArchiver(ccl_bplist.load(handle),
                                                          parse_whole_structure=True)
    via_memory = ccl_bplist.deserialise_NsKeyedArchiver(ccl_bplist.load(io.BytesIO(blob)),
                                                        parse_whole_structure=True)

    # BplistUID has no __eq__, so compare the rendered structure rather than object identity.
    assert repr(via_file) == repr(via_memory)
