"""Reading a UFED keychain export (``backup_keychain_v2.plist``) — docs/ufed_keychain_format.md.

The export is built here by an encoder written from that description: DER dictionaries, sealed with
AES-256-GCM in ``_SFAuthenticatedCiphertext`` archives, a metadata key wrapped by a class key. What
the reader writes must be what the Memories reports search for Snapchat's items in.

Every input is synthetic; the keys are arbitrary bytes.
"""
import datetime
import plistlib

import pytest
from Crypto.Cipher import AES

from scripts import DecryptLocalMemories_iOS as memkeys
from scripts.data import ufed_keychain as uk

CLASS_KEY = bytes(range(32))
GROUP = memkeys.SNAPCHAT_ACCESS_GROUP


# ------------------------------------------------------------------------------- the encoder

def _len(n):
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def _der(value):
    if isinstance(value, bool):
        return _tlv(0x01, b"\xff" if value else b"\x00")
    if isinstance(value, int):
        return _tlv(0x02, value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big", signed=True))
    if isinstance(value, str):
        return _tlv(0x0C, value.encode())
    if isinstance(value, bytes):
        return _tlv(0x04, value)
    if isinstance(value, datetime.datetime):
        return _tlv(0x18, value.strftime("%Y%m%d%H%M%S.%fZ").encode())
    if isinstance(value, list):
        return _tlv(0x30, b"".join(_der(v) for v in value))
    if isinstance(value, dict):
        return _tlv(0x31, b"".join(_tlv(0x30, _der(k) + _der(v)) for k, v in value.items()))
    raise TypeError(value)


def _seal(key, plaintext, nonce):
    ciphertext, tag = AES.new(key, AES.MODE_GCM, nonce=nonce).encrypt_and_digest(plaintext)
    objects = ["$null", {"$class": plistlib.UID(5), "SFCiphertext": plistlib.UID(2),
                         "SFAuthenticationCode": plistlib.UID(3),
                         "SFInitializationVector": plistlib.UID(4)},
               ciphertext, tag, nonce,
               {"$classname": "_SFAuthenticatedCiphertext",
                "$classes": ["_SFAuthenticatedCiphertext", "SFCiphertext", "NSObject"]}]
    return plistlib.dumps({"$version": 100000, "$archiver": "NSKeyedArchiver",
                           "$top": {"root": plistlib.UID(1)}, "$objects": objects},
                          fmt=plistlib.FMT_BINARY)


def _entry(row, attributes, value, class_idx=7, with_secret=True, corrupt=False):
    metadata_key, item_key = bytes([row]) * 32, bytes([row + 100]) * 32
    tamper = f"0000000{row}-0000-4000-8000-000000000000"
    secret = _der({"TamperCheck": tamper, "v_Data": value})
    secret += bytes([16 - len(secret) % 16]) * (16 - len(secret) % 16)       # padding after the DER
    sealed_secret = _seal(item_key, secret, bytes([row]) * 32)
    if corrupt:
        sealed_secret = _seal(b"\x99" * 32, secret, bytes([row]) * 32)       # tag will not verify
    entry = {"table": "genp", "rowID": row, "version": 8, "classKeyIdx": class_idx,
             "metadata": {"wrappedKey": _seal(CLASS_KEY, metadata_key, b"\x01" * 32),
                          "ciphertext": _seal(metadata_key, _der(dict(attributes, TamperCheck=tamper)),
                                              b"\x02" * 32),
                          "tamperCheck": tamper},
             "data": {"ciphertext": sealed_secret, "tamperCheck": tamper}}
    if with_secret:
        entry["data"]["unwrappedKey"] = item_key
    return entry


def _export(tmp_path, entries):
    path = tmp_path / "backup_keychain_v2.plist"
    path.write_bytes(plistlib.dumps({"classKeyIdxToUnwrappedMetadataClassKey": {"7": CLASS_KEY},
                                     "keychainEntries": entries}, fmt=plistlib.FMT_BINARY))
    return str(path)


# --------------------------------------------------------------------------------- the tests

def test_items_are_decrypted_attributes_and_secret(tmp_path):
    when = datetime.datetime(2024, 3, 10, 11, 22, 33, 500000)
    path = _export(tmp_path, [_entry(1, {"agrp": GROUP, "acct": memkeys.EGOCIPHER_ACCOUNT,
                                         "cdat": when, "sync": 0, "pdmn": "ck"}, b"\x11" * 32)])
    items, problems = uk.decrypt_export(path)
    assert problems == 0 and len(items) == 1
    item = items[0]
    assert (item["agrp"], item["acct"], item["v_Data"]) == (GROUP, memkeys.EGOCIPHER_ACCOUNT,
                                                           b"\x11" * 32)
    assert item["cdat"] == when and item["sync"] == 0 and item["table"] == "genp"


def test_what_is_written_is_what_the_reports_search(tmp_path):
    path = _export(tmp_path, [
        _entry(1, {"agrp": GROUP, "acct": memkeys.EGOCIPHER_ACCOUNT}, b"\x11" * 32),
        _entry(2, {"agrp": GROUP, "gena": memkeys.PERSISTEDKEY_ACCOUNT.encode()}, b"\x22" * 40),
        _entry(3, {"agrp": "OTHER.group", "acct": "x"}, b"\x33")])
    out = tmp_path / "decrypted_keychain.plist"
    uk.main(path, str(out))
    found = list(memkeys._kc_items(plistlib.loads(out.read_bytes())))
    assert len(found) == 3
    snap = [i for i in found if memkeys._kc_field(i, memkeys._AGRP_FIELDS) == GROUP]
    assert {memkeys._kc_field(i, memkeys._ACCOUNT_FIELDS) for i in snap} == {
        memkeys.EGOCIPHER_ACCOUNT, memkeys.PERSISTEDKEY_ACCOUNT}
    assert {memkeys._kc_value_hex(i) for i in snap} == {"11" * 32, "22" * 40}


def test_an_unreadable_item_is_skipped_and_counted(tmp_path):
    path = _export(tmp_path, [
        _entry(1, {"agrp": GROUP, "acct": "a"}, b"\x01"),
        _entry(2, {"agrp": GROUP, "acct": "b"}, b"\x02", class_idx=99),        # no such class key
        _entry(3, {"agrp": GROUP, "acct": "c"}, b"\x03", with_secret=False),   # attributes only
        _entry(4, {"agrp": GROUP, "acct": "d"}, b"\x04", corrupt=True)])       # secret tag fails
    items, problems = uk.decrypt_export(path)
    assert problems == 1
    by_acct = {i["acct"]: i for i in items}
    assert set(by_acct) == {"a", "c", "d"}
    assert by_acct["a"]["v_Data"] == b"\x01"
    assert "v_Data" not in by_acct["c"] and "v_Data" not in by_acct["d"]


def test_not_an_export_is_refused(tmp_path):
    other = tmp_path / "k.plist"
    other.write_bytes(plistlib.dumps({"something": 1}))
    with pytest.raises(uk.FormatError):
        uk.decrypt_export(str(other))


@pytest.mark.parametrize("value", [
    "text", b"\x00\x01", 0, -129, 2 ** 40, True, False, ["a", 1, b"x"], {"nested": "dict"},
    "x" * 300,                                                     # a long-form length
])
def test_der_values_round_trip(value):
    assert uk.read_dictionary(_der({"k": value})) == {"k": value}


def test_der_is_strict():
    good = _der({"k": "v"})
    for bad in (good[:-1], b"\x30" + good[1:], good + b"\x00\x01", b"\x31\x03\x30\x01\x0c"):
        with pytest.raises(uk.FormatError):
            uk.read_dictionary(bad)
