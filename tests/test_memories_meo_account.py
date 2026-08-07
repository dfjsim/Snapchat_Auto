"""Which account a locked My Eyes Only memory is reported against.

A MEO memory whose key is still wrapped is reported with the account it belongs to. That account
used to be named by its userHash alone — an identifier that appears nowhere else in the report,
so the notice read as if it were talking about some *other* account than the one named right below
it on the same page. These tests pin that the notice names the account the way every other panel
does (userId, hash beside it), and that it distinguishes a keychain with no persistedkey at all
from one that only carries another account's.

Every input here is synthetic. No extraction data is required or used.
"""
from scripts import memories_media_report as memories_report

HASH_A = "aa" * 32
HASH_B = "bb" * 32
UIDS = {HASH_A: "user-a-id", HASH_B: "user-b-id"}


def _locked(user_hash=HASH_A):
    """A member dict as _enc_html sees it: My Eyes Only, key still wrapped."""
    return {"snap_id": "SNAP-1", "user_hash": user_hash, "key": None, "iv": None,
            "is_meo": True, "key_wrapped": True}


def test_locked_meo_names_the_account_by_userid_and_hash():
    out = memories_report._enc_html([_locked()], UIDS, meo_owners=[])
    assert "user-a-id" in out
    assert HASH_A[:12] in out                      # the hash is kept, but beside the userId
    assert "user-b-id" not in out


def test_locked_meo_falls_back_to_the_userhash_when_no_userid_is_known():
    out = memories_report._enc_html([_locked()], {}, meo_owners=[])
    assert f"userHash {HASH_A[:12]}" in out


def test_empty_keychain_says_no_persistedkey_at_all():
    out = memories_report._enc_html([_locked()], UIDS, meo_owners=[])
    assert "at all" in out


def test_other_accounts_key_is_named_rather_than_reported_missing():
    """The keychain has a persistedkey — just not this account's. Saying only 'none for account X'
    invited the reading that X was the wrong account."""
    out = memories_report._enc_html([_locked()], UIDS, meo_owners=["user-b-id"])
    assert "user-b-id" in out and "at all" not in out


def test_this_accounts_key_present_but_failing_is_not_reported_as_missing():
    out = memories_report._enc_html([_locked()], UIDS, meo_owners=["user-a-id"])
    assert "did not unwrap" in out
    assert "no <code>com.snapchat.keyservice.persistedkey</code> item" not in out


def test_unknown_owners_keeps_the_neutral_wording():
    """meo_owners=None means the keychain's persistedkey named no userId — 'we cannot tell which
    account' must not be reported as 'there is none'."""
    out = memories_report._enc_html([_locked()], UIDS, meo_owners=None)
    assert "at all" not in out and "did not unwrap" not in out
    assert "not in the supplied keychain" in out or "is in the supplied keychain" in out


def test_a_usable_key_still_renders_the_key_and_iv():
    member = {"snap_id": "SNAP-1", "user_hash": HASH_A, "key": bytes(range(32)),
              "iv": bytes(range(16)), "is_meo": False, "key_wrapped": False, "key_source": None}
    out = memories_report._enc_html([member], UIDS, meo_owners=[])
    assert member["key"].hex() in out and member["iv"].hex() in out
    assert "My Eyes Only" not in out
    assert "Key source" not in out              # an ordinary key needs no provenance note


# ------------------------------------------- keys borrowed from the referenced media object
#
# Moving a Memory into My Eyes Only writes a new snap row whose own key is wrapped, but does not
# re-encrypt the media already cached: the row still points at the original media object, whose
# snap_key_iv row (encrypted=0) survives even after its ZGALLERYSNAP row is gone. Those rows used
# to be dropped for having no matching snap, so media that decrypts with no keychain at all was
# reported as "no key" and never recovered.

PLAIN_KEY, PLAIN_IV = bytes(range(32)), bytes(range(16))
WRAPPED_KEY, WRAPPED_IV = bytes(48), bytes(32)


def _memories(refs=("MEDIA-OBJ",), **over):
    m = {"snap_id": "MEO-SNAP", "user_hash": HASH_A, "key": None, "iv": None, "is_meo": True,
         "key_wrapped": True, "key_source": None, "media_refs": list(refs)}
    m.update(over)
    return {"MEO-SNAP": m}


def test_key_of_the_referenced_media_object_is_adopted():
    mems = _memories()
    n = memories_report.adopt_media_object_keys(
        mems, {"MEDIA-OBJ": (PLAIN_KEY, PLAIN_IV, 0, "main")}, persisted="")
    m = mems["MEO-SNAP"]
    assert n == 1 and (m["key"], m["iv"]) == (PLAIN_KEY, PLAIN_IV)
    assert m["key_source"] == "MEDIA-OBJ"       # provenance is recorded, never silently merged


def test_a_memory_with_its_own_key_is_left_alone():
    mems = _memories(key=PLAIN_KEY, iv=PLAIN_IV, is_meo=False, key_wrapped=False)
    other = bytes(range(1, 33))
    n = memories_report.adopt_media_object_keys(
        mems, {"MEDIA-OBJ": (other, PLAIN_IV, 0, "main")}, persisted="")
    assert n == 0 and mems["MEO-SNAP"]["key"] == PLAIN_KEY
    assert mems["MEO-SNAP"]["key_source"] is None


def test_a_wrapped_media_object_key_is_not_adopted_without_the_persistedkey():
    """48/32 is a wrapped pair, not a key — adopting it would hand AES garbage to every matcher."""
    mems = _memories()
    n = memories_report.adopt_media_object_keys(
        mems, {"MEDIA-OBJ": (WRAPPED_KEY, WRAPPED_IV, 1, "main")}, persisted="")
    assert n == 0 and mems["MEO-SNAP"]["key"] is None


def test_an_unreferenced_orphan_key_is_not_adopted():
    """The link is ZMEDIAID / ZDUPLICATEDFROMSNAPID. A key that merely sits in the same database
    proves nothing about this Memory."""
    mems = _memories(refs=())
    n = memories_report.adopt_media_object_keys(
        mems, {"SOME-OTHER-SNAP": (PLAIN_KEY, PLAIN_IV, 0, "main")}, persisted="")
    assert n == 0 and mems["MEO-SNAP"]["key"] is None


def test_a_borrowed_key_is_labelled_as_the_media_objects():
    member = {"snap_id": "MEO-SNAP", "user_hash": HASH_A, "key": PLAIN_KEY, "iv": PLAIN_IV,
              "is_meo": True, "key_wrapped": False, "key_source": "MEDIA-OBJ"}
    out = memories_report._enc_html([member], UIDS, meo_owners=[])
    assert "Key source" in out and "MEDIA-OBJ" in out
    assert "ZDUPLICATEDFROMSNAPID" in out
