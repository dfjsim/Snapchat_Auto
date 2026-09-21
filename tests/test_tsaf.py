"""Reading Snap's TSAF containers by key — ``Documents/user.plist`` and
``ClientEncryptionService.plist``.

The one rule that matters is "a value immediately follows its key". A signed-out account's
``user.plist`` keeps the ``username`` key with an empty value (the key, then a lone NUL, then the
next key), and a reader that takes "the next string" reports the *next key's name* as the username.
The synthetic files here reproduce both layouts seen on the test devices.

Every input is synthetic.
"""
import os

from scripts.data import tsaf
from scripts import ParseSnapchat_iOS as parser

UID = "0f0f0f0f-1111-4222-8333-444444444444"
LAGUNA = "abababab-cdcd-4efe-8f0f-101010101010"


def _tok(text):
    return b"\x08" + text.encode("utf-8") + b"\x00"


def _type(name):
    return b"," + _tok(name)


def _user_plist(username, user_id, laguna):
    """The signed-in layout: every key followed by its value."""
    return (b"TSAF\x03\x00\x04\x00\x02\x00\x00\x00\x00\x00\x00\x00\x10\x00\x00\x00"
            + _type("User") + _tok("current_version_number") + b"."
            + _tok("username") + _tok(username)
            + _tok("user_id") + _tok(user_id)
            + _tok("laguna_id") + _tok(laguna)
            + _tok("client_encryption") + _type("SCClientEncryption")
            + _tok("identifier") + _tok("5D9E2E85-4896-4EFF-80E9-4A00041A1242")
            + _tok("encryption_key") + _tok("a" * 44)
            + _tok("initialization_vector") + _tok("b" * 24) + b"\x00\x00")


def _signed_out_plist():
    """The signed-out layout: username / user_id / laguna_id each followed by a lone NUL."""
    return (b"TSAF\x03\x00\x04\x00\x02\x00\x00\x00\x00\x00\x00\x00\x0d\x00\x00\x00"
            + _type("User") + _tok("current_version_number") + b"."
            + _tok("username") + b"\x00" + _tok("user_id") + b"\x00" + _tok("laguna_id") + b"\x00"
            + _tok("client_encryption") + _type("SCClientEncryption")
            + _tok("identifier") + _tok("5D325B29-D4A9-4133-B6D9-58C3BC9861F5")
            + _tok("encryption_key") + _tok("c" * 44)
            + _tok("initialization_vector") + _tok("d" * 24) + b"\x00\x00")


def test_signed_in_account_reads_every_key():
    account = tsaf.account(_user_plist("alice", UID, LAGUNA))
    assert account == {"username": "alice", "user_id": UID, "laguna_id": LAGUNA,
                       "identifier": "5D9E2E85-4896-4EFF-80E9-4A00041A1242",
                       "encryption_key": "a" * 44, "initialization_vector": "b" * 24}


def test_signed_out_account_has_no_username_rather_than_the_next_key():
    account = tsaf.account(_signed_out_plist())
    assert "username" not in account and "user_id" not in account and "laguna_id" not in account
    assert account["identifier"] == "5D325B29-D4A9-4133-B6D9-58C3BC9861F5"


def test_a_type_name_is_nobodys_value_and_takes_none():
    fields = dict(tsaf.fields(_user_plist("alice", UID, LAGUNA)))
    assert fields["User"] == ""                                # a type, not a key
    assert fields["client_encryption"] == ""                   # its value is an object
    assert fields["SCClientEncryption"] == ""
    assert fields["identifier"] == "5D9E2E85-4896-4EFF-80E9-4A00041A1242"
    assert fields["current_version_number"] == ""              # an undecoded type follows it


def test_a_cache_entry_root_type_is_listed_as_a_type():
    """sccache.* entries open with a 0x1e-tagged root type name rather than a 0x08 string."""
    raw = (b"TSAF\x03\x00\x04\x00\x03\x00\x00\x00\x01\x00\x00\x00\x03\x00\x00\x00"
           b"\x1eSCCacheDataHandlerCacheEntry\x00\x00\x00\x02\x00\x00\x80"
           + _type("IMPListManagedBusinessProfilesResponse") + _tok("GPBData") + b"\x1f"
           + _tok("{}") + b"\x00\x00")
    assert tsaf.fields(raw) == [("SCCacheDataHandlerCacheEntry", ""),
                                ("IMPListManagedBusinessProfilesResponse", ""),
                                ("GPBData", ""), ("{}", "")]
    assert tsaf.value(tsaf.tokens(raw), "SCCacheDataHandlerCacheEntry") == ""


def test_ids_must_be_uuid_shaped():
    assert "user_id" not in tsaf.account(_user_plist("alice", "not-a-uuid", LAGUNA))


def test_not_tsaf_reads_as_nothing():
    assert tsaf.tokens(b"bplist00") == []
    assert tsaf.account(b"") == {}
    assert tsaf.fields(None) == []


def test_getuserid_reads_the_keyed_value_and_falls_back_to_the_first_uuid(tmp_path):
    keyed = os.path.join(tmp_path, "user.plist")
    with open(keyed, "wb") as fh:
        fh.write(_user_plist("alice", UID, LAGUNA))
    assert parser.getUserID(keyed) == UID

    # a file that is not TSAF: the regex fallback the function always had
    legacy = os.path.join(tmp_path, "legacy.plist")
    with open(legacy, "wb") as fh:
        fh.write(b"<plist>" + UID.encode() + b"</plist>")
    assert parser.getUserID(legacy) == UID

    signed_out = os.path.join(tmp_path, "out.plist")
    with open(signed_out, "wb") as fh:
        fh.write(_signed_out_plist())
    assert parser.getUserID(signed_out) == ""
    assert parser.getAccount(signed_out) == {"identifier": "5D325B29-D4A9-4133-B6D9-58C3BC9861F5",
                                             "encryption_key": "c" * 44,
                                             "initialization_vector": "d" * 24}
