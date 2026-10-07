"""The Library/Caches report's attribution reads two names right.

A file byte-identical to a *piece* of a cache entry — a bundle's child, a byte-range part — links to
the entry: the cache_controller report has a row for the entry and none for the piece, so a link to
the piece's file name led nowhere. And the word before ``~<UUID>`` in a claim key is an owner
username only in ``<USERNAME>~<snapId>``: ``thumbnail~<UUID>`` names a type, not a person.
Every input is synthetic.
"""
from scripts import cache_media_report as cm

KEY = "aaaabbbbccccddddeeeeffff00001111"
CHILD = "z0123456789abcdef0123456789abcdef"
SNAP = "ABCDEF01-2345-4678-9ABC-DEF012345678"


def test_a_piece_of_a_cache_entry_is_named_by_the_entry():
    claimed = {KEY: {CHILD}}                    # a claimed bundle whose CHILDREN name the child
    assert cm.sccontent_key(KEY, claimed) == (KEY, "")
    assert cm.sccontent_key(f"{KEY}_{CHILD}", claimed) == (KEY, f"the bundle child {CHILD}")
    assert cm.sccontent_key(f"{KEY}_4096-8192") == (KEY, "a byte-range part")
    assert cm.sccontent_key(f"{KEY}_PREFETCH") == (KEY, "a byte-range part")
    # a child no claimed bundle lists is a row of its own in the cache_controller report
    assert cm.sccontent_key(f"{KEY}_{CHILD}") == (f"{KEY}_{CHILD}", "")
    assert cm.sccontent_key(f"{KEY}_zother", claimed) == (f"{KEY}_zother", "")


def test_only_a_name_is_taken_for_the_owner():
    assert cm.claim_owner(f"SYNTH.OWNER~{SNAP}") == "SYNTH.OWNER"
    assert cm.claim_owner(f"content~SYNTH.OWNER~{SNAP}") == "SYNTH.OWNER"
    for not_a_name in (f"thumbnail~{SNAP}", f"profilethumbnail~{SNAP}", SNAP, f"{SNAP}~{SNAP}",
                       f"SnapVideoFilterState-SYNTH.OWNER~{SNAP}", f"1:{SNAP}:12:0:0"):
        assert cm.claim_owner(not_a_name) == "", not_a_name
