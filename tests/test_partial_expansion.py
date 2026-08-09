"""Resolving a selection against a run, and growing it into the rows to render.

The two failures worth guarding hardest:

* **Over-inclusion.** A partial report is handed over. A row that arrives in it because an id was
  matched loosely, or because a relation followed further than asked, is evidence the examiner did not
  choose to disclose.
* **A relation that silently finds nothing** because the generator that owns it recorded the edge in
  the other direction. That is the reason the closure is decided once, up front, from every index at
  the same time — a ticked cache entry has to pull in a Memory even though Memories renders first.

Every input is synthetic: placeholder ids, hand-built indexes, no extraction data.
"""
import pytest

from scripts import partial_report as pr
from scripts.partial_report import Index


CONV_A = "aaaa0000-0000-4000-8000-00000000000a"
CONV_B = "aaaa0000-0000-4000-8000-00000000000b"
KEY_1 = "1" * 32
KEY_2 = "2" * 32
SHA_1 = "1" * 64
SHA_2 = "2" * 64


# --------------------------------------------------------------------------- a small synthetic run

def build_indexes():
    """Two conversations, two Memories in one group, two cache entries, one Library/Caches row.

    Every edge is recorded **the way the real generator records it** — in the direction the module
    that owns it derives it, not the direction a relation happens to be asked for. That is the point:
    the Memories index derives "this Memory's media came from that cache entry", so the `mem -> cc`
    edge is recorded there, and the `cache_memory` relation (cc -> mem) still has to find it.
    """
    conv = Index("conv")
    conv.add(f"conv-{CONV_A}", conv=CONV_A)
    conv.add(f"conv-{CONV_B}", conv=CONV_B, server="server-b")

    msg = Index("msg")
    for cid in (CONV_A, CONV_B):
        for n in (1, 2):
            mid = f"conv-{cid}|msg-{n}.0"
            # qualified with the conversation, as the real generator records it: message 3 exists in
            # every chat, so a bare ordinal is not a key to anything
            msg.add(mid, smid=f"{cid}|{n}.0")
            msg.contains(mid, f"conv-{cid}")
            conv.link(pr.EDGE_CONV_MESSAGE, f"conv-{cid}", "msg", mid)

    ct = Index("ct")
    ct.add("ct-u-alice", uid="u-alice", user="alice-test")
    ct.add("ct-u-bob", uid="u-bob", user="bob-test")
    conv.link(pr.EDGE_CONV_PARTICIPANT, f"conv-{CONV_A}", "ct", "ct-u-alice")
    conv.link(pr.EDGE_CONV_PARTICIPANT, f"conv-{CONV_A}", "ct", "ct-u-bob")
    conv.link(pr.EDGE_CONV_PARTICIPANT, f"conv-{CONV_B}", "ct", "ct-u-bob")

    mem = Index("mem")
    mem.add("mem-SNAP-0001", snap="SNAP-0001", mediaid="MEDIA-1")
    mem.add("mem-SNAP-0002", snap="SNAP-0002", mediaid="MEDIA-1")
    # grouped: same media under two snap rows, sharing one detail page
    mem.link(pr.EDGE_MEMORY_GROUP, "mem-SNAP-0001", "mem", "mem-SNAP-0002")

    cc = Index("cc")
    cc.add(f"ck-{KEY_1}", key=KEY_1)
    cc.add(f"ck-{KEY_2}", key=KEY_2)
    mem.link(pr.EDGE_MEMORY_CACHE, "mem-SNAP-0001", "cc", f"ck-{KEY_1}")
    # cache_controller derives "this entry belongs to that message" from the claim's EXTERNAL_KEY
    cc.link(pr.EDGE_MESSAGE_CACHE, f"ck-{KEY_2}", "msg", f"conv-{CONV_B}|msg-1.0")

    cm = Index("cm")
    cm.add(f"cm-{SHA_1}", sha=SHA_1, raw="raw-aaa", rel="Library/Caches/x/one")
    # the Library/Caches report derives what its own rows link to
    cm.link(pr.EDGE_CACHE_CACHEMEDIA, f"cm-{SHA_1}", "cc", f"ck-{KEY_1}")
    cm.link(pr.EDGE_MEMORY_CACHEMEDIA, f"cm-{SHA_1}", "mem", "mem-SNAP-0001")

    return {"conv": conv, "msg": msg, "ct": ct, "mem": mem, "cc": cc, "cm": cm}


def selection(**kinds):
    """A schema-2 payload from ``kind=[(id, keys), …]`` or ``kind=[id, …]``."""
    out = {}
    for kind, entries in kinds.items():
        out[kind] = {e if isinstance(e, str) else e[0]:
                     1 if isinstance(e, str) else e[1] for e in entries}
    return {"tool": "Snapchat_Auto", "schema": 2, "selections": out}


def closure_for(sel, **options):
    indexes = build_indexes()
    opts = pr.default_options()
    opts.update(options)
    return pr.expand(indexes, pr.resolve(indexes, sel), opts)


# --------------------------------------------------------------------------- resolution

def test_a_row_is_found_by_its_own_id():
    res = pr.resolve(build_indexes(), selection(mem=["mem-SNAP-0001"]))
    assert res.seeds["mem"] == {"mem-SNAP-0001"}
    assert res.how[("mem", "mem-SNAP-0001")] == "its own id"
    assert res.ok and not res.moved


def test_a_library_caches_row_is_found_by_a_copys_raw_hash_when_its_own_id_moved():
    """The id is the hash of the *recovered* content, so a build that decodes differently moves it.

    That is why every copy's raw hash travels with the selection.
    """
    sel = selection(cm=[(f"cm-{'9' * 64}", {"sha": "9" * 64, "raw": ["raw-aaa"],
                                            "rel": "Library/Caches/x/one"})])
    res = pr.resolve(build_indexes(), sel)
    assert res.seeds["cm"] == {f"cm-{SHA_1}"}
    assert "raw SHA-256" in res.how[("cm", f"cm-{SHA_1}")]
    assert res.moved and res.moved[0]["was"] == f"cm-{'9' * 64}"


def test_a_library_caches_row_falls_back_to_its_path_last():
    sel = selection(cm=[(f"cm-{'9' * 64}", {"rel": "Library/Caches/x/one"})])
    res = pr.resolve(build_indexes(), sel)
    assert res.seeds["cm"] == {f"cm-{SHA_1}"}
    assert "rel" in res.how[("cm", f"cm-{SHA_1}")]


def test_a_conversation_is_found_by_its_server_id():
    sel = selection(conv=[("conv-gone", {"server": "server-b"})])
    res = pr.resolve(build_indexes(), sel)
    assert res.seeds["conv"] == {f"conv-{CONV_B}"}


def test_a_contact_falls_back_from_user_id_to_username():
    sel = selection(ct=[("ct-unknown", {"user": "bob-test"})])
    res = pr.resolve(build_indexes(), sel)
    assert res.seeds["ct"] == {"ct-u-bob"}


def test_an_id_that_matches_nothing_refuses_by_default_and_names_it():
    with pytest.raises(LookupError, match="mem-SNAP-9999"):
        pr.resolve(build_indexes(), selection(mem=["mem-SNAP-9999"]))


def test_an_id_that_matches_nothing_can_be_dropped_and_is_then_listed():
    res = pr.resolve(build_indexes(), selection(mem=["mem-SNAP-0001", "mem-SNAP-9999"]),
                     unresolved="drop")
    assert res.seeds["mem"] == {"mem-SNAP-0001"}
    assert [u["id"] for u in res.unresolved] == ["mem-SNAP-9999"]
    assert not res.ok


def test_an_id_matching_several_rows_refuses_rather_than_picking_one():
    """Merged Library/Caches rows are the real case. Guessing would over-disclose."""
    indexes = build_indexes()
    indexes["cm"].add(f"cm-{SHA_2}", sha=SHA_2, raw="raw-aaa", rel="Library/Caches/x/two")
    sel = selection(cm=[(f"cm-{'9' * 64}", {"raw": ["raw-aaa"]})])
    with pytest.raises(pr.AmbiguousSelection, match="ambiguous"):
        pr.resolve(indexes, sel)


def test_a_kind_whose_report_was_not_produced_says_so():
    indexes = build_indexes()
    indexes["mem"] = None
    res = pr.resolve(indexes, selection(mem=["mem-SNAP-0001"]), unresolved="drop")
    assert "was not produced" in res.unresolved[0]["why"]


# --------------------------------------------------------------------------- expansion, one hop

def test_with_every_relation_off_a_message_pulls_in_only_its_conversation():
    """Containment is not optional -- a message has nowhere to be rendered but its conversation."""
    closure = closure_for(selection(msg=[f"conv-{CONV_A}|msg-1.0"]),
                          relations=dict(pr.PRESETS["minimal"]))
    assert closure.included["msg"] == {f"conv-{CONV_A}|msg-1.0"}
    assert closure.included["conv"] == {f"conv-{CONV_A}"}
    assert not closure.included["cc"] and not closure.included["ct"]
    assert "holds msg" in closure.reasons[("conv", f"conv-{CONV_A}")][0]


def test_conv_messages_brings_a_whole_conversation_and_only_that_one():
    closure = closure_for(selection(conv=[f"conv-{CONV_A}"]))
    assert closure.included["msg"] == {f"conv-{CONV_A}|msg-1.0", f"conv-{CONV_A}|msg-2.0"}
    assert closure.included["conv"] == {f"conv-{CONV_A}"}


def test_conv_messages_off_leaves_the_messages_out():
    closure = closure_for(selection(conv=[f"conv-{CONV_A}"]),
                          relations={**pr.PRESETS["recommended"], "conv_messages": False})
    assert not closure.included["msg"]


def test_participants_brings_the_contacts_of_an_included_conversation():
    closure = closure_for(selection(conv=[f"conv-{CONV_A}"]))
    assert closure.included["ct"] == {"ct-u-alice", "ct-u-bob"}


def test_mem_group_brings_the_sibling_and_records_why():
    closure = closure_for(selection(mem=["mem-SNAP-0001"]))
    assert closure.included["mem"] == {"mem-SNAP-0001", "mem-SNAP-0002"}
    assert "mem_group" in closure.reasons[("mem", "mem-SNAP-0002")][0]


def test_mem_group_off_leaves_the_sibling_out():
    closure = closure_for(selection(mem=["mem-SNAP-0001"]),
                          relations={**pr.PRESETS["recommended"], "mem_group": False})
    assert closure.included["mem"] == {"mem-SNAP-0001"}


# --------------------------------------------------------------------------- the backward directions
#
# The reason the closure is decided up front, from every index at once. Report order is fixed by
# manifest dependencies, so the stage that owns a relation is often not the stage that needs it.

def test_a_selected_cache_entry_pulls_in_its_memory_even_though_memories_renders_first():
    """The edge is recorded by the Memories index (mem -> cc); the relation is asked for cc -> mem."""
    closure = closure_for(selection(cc=[f"ck-{KEY_1}"]))
    assert "mem-SNAP-0001" in closure.included["mem"]
    assert "cache_memory" in closure.reasons[("mem", "mem-SNAP-0001")][0]


def test_a_selected_library_caches_file_pulls_in_what_it_links_to():
    closure = closure_for(selection(cm=[f"cm-{SHA_1}"]))
    assert f"ck-{KEY_1}" in closure.included["cc"]


def test_a_selected_memory_pulls_in_its_library_caches_chunks_when_asked():
    """The edge is recorded by the Library/Caches index (cm -> mem); asked for mem -> cm."""
    closure = closure_for(selection(mem=["mem-SNAP-0001"]),
                          relations={**pr.PRESETS["recommended"], "mem_cachemedia": True})
    assert f"cm-{SHA_1}" in closure.included["cm"]


def test_a_selected_cache_entry_pulls_in_its_message_and_that_messages_conversation():
    closure = closure_for(selection(cc=[f"ck-{KEY_2}"]))
    assert f"conv-{CONV_B}|msg-1.0" in closure.included["msg"]
    assert f"conv-{CONV_B}" in closure.included["conv"]        # containment, not a relation


# --------------------------------------------------------------------------- how far it goes

def test_one_hop_stops_before_the_second_relation():
    """A Memory pulls in its cache entry; that entry's own Library/Caches copy is a second hop."""
    closure = closure_for(selection(mem=["mem-SNAP-0001"]),
                          relations={**pr.PRESETS["recommended"], "cache_cachemedia": True})
    assert f"ck-{KEY_1}" in closure.included["cc"]
    assert not closure.included["cm"]


def test_transitive_follows_the_second_hop_when_asked():
    closure = closure_for(selection(mem=["mem-SNAP-0001"]),
                          relations={**pr.PRESETS["recommended"], "cache_cachemedia": True},
                          transitive=True)
    assert f"cm-{SHA_1}" in closure.included["cm"]


def test_a_transitive_closure_over_a_cycle_terminates():
    closure = closure_for(selection(mem=["mem-SNAP-0001"]),
                          relations=dict(PRESETS_ALL := pr.PRESETS["all"]), transitive=True)
    assert closure.total() > 0                                 # and it returned at all


def test_the_row_guard_warns_rather_than_silently_producing_a_huge_extract():
    closure = closure_for(selection(conv=[f"conv-{CONV_A}", f"conv-{CONV_B}"]), max_rows=2)
    assert any("row guard" in w for w in closure.warnings)


# --------------------------------------------------------------------------- accounting

def test_every_row_says_whether_it_was_ticked_or_pulled_in():
    closure = closure_for(selection(mem=["mem-SNAP-0001"]))
    assert closure.reasons[("mem", "mem-SNAP-0001")][0].startswith("Selected by the examiner")
    assert closure.reasons[("mem", "mem-SNAP-0002")][0].startswith("Included because")
    assert closure.is_seed("mem", "mem-SNAP-0001")
    assert not closure.is_seed("mem", "mem-SNAP-0002")


def test_the_counts_are_selected_pulled_in_and_the_extractions_own_total():
    closure = closure_for(selection(conv=[f"conv-{CONV_A}"]))
    counts = closure.counts()
    assert counts["conv"] == {"selected": 1, "pulled_in": 0, "total": 2}
    assert counts["msg"] == {"selected": 0, "pulled_in": 2, "total": 4}


def test_the_dry_run_report_names_the_relations_it_did_not_follow():
    closure = closure_for(selection(mem=["mem-SNAP-0001"]))
    text = pr.dry_run_text(closure)
    assert "This extract would contain" in text
    assert "Relations NOT followed" in text and "mem_cachemedia" in text
    assert "Pulled in by:" in text and "mem_group" in text
    assert text.isascii()                                      # it goes to a Windows console


# --------------------------------------------------------------------------- the policy vocabulary

def test_parse_relations_presets_and_subtraction():
    assert pr.parse_relations("minimal") == {}
    assert pr.parse_relations("all")["mem_cachemedia"] is True
    assert pr.parse_relations("recommended")["mem_cachemedia"] is False
    only = pr.parse_relations("mem_cache,cache_memory")
    assert only["mem_cache"] and not only["conv_messages"]
    # a leading "-" means "the recommended set, minus this" -- what an examiner actually reaches for
    minus = pr.parse_relations("-conv_messages")
    assert minus["participants"] is True and minus["conv_messages"] is False


def test_parse_relations_rejects_an_unknown_name_instead_of_ignoring_it():
    with pytest.raises(ValueError, match="unknown relation"):
        pr.parse_relations("mem_cache,not_a_relation")


def test_every_relation_declares_what_the_association_rests_on():
    """The basis is shown next to the checkbox and recorded in the provenance: an examiner deciding
    whether to include something needs to know what it is based on, not just its name."""
    for relation in pr.RELATIONS:
        assert relation.basis and len(relation.basis) > 30
        assert relation.label


def test_every_relation_names_a_real_edge():
    for relation in pr.RELATIONS:
        assert relation.edge is None or relation.edge in pr.EDGES, relation.key


def test_the_paired_relations_share_one_edge_in_opposite_directions():
    """The distinction the store rests on: an *edge* is the fact, a *relation* is the choice to follow
    it one way. Keying edges by relation name meant whichever relation its owner did not record found
    nothing — silently.
    """
    by_key = {r.key: r for r in pr.RELATIONS}
    for forward, backward in [("mem_cache", "cache_memory"),
                              ("cache_message", "msg_cache"),
                              ("participants", "contact_conversations")]:
        a, b = by_key[forward], by_key[backward]
        assert a.edge == b.edge, (forward, backward)
        assert (a.src, a.dst) == (b.dst, b.src), (forward, backward)


def test_an_edge_recorded_either_way_round_is_found_from_both_ends():
    """Directly, so the guarantee does not rest only on which way the fixture happens to record."""
    for recorded_by, other in (("mem", "cc"), ("cc", "mem")):
        indexes = {"mem": Index("mem"), "cc": Index("cc")}
        indexes["mem"].add("mem-X", snap="X")
        indexes["cc"].add("ck-Y", key="Y")
        src, dst = ("mem-X", "ck-Y") if recorded_by == "mem" else ("ck-Y", "mem-X")
        indexes[recorded_by].link(pr.EDGE_MEMORY_CACHE, src, other, dst)

        opts = pr.default_options()
        for seed_kind, seed_id, want_kind, want_id in (("mem", "mem-X", "cc", "ck-Y"),
                                                       ("cc", "ck-Y", "mem", "mem-X")):
            sel = {"schema": 2, "selections": {seed_kind: {seed_id: 1}}}
            closure = pr.expand(indexes, pr.resolve(indexes, sel), opts)
            assert want_id in closure.included[want_kind], (recorded_by, seed_kind)


def test_a_message_alternate_key_never_matches_another_conversations_message():
    """The Phase 1 collision, coming back through the alternate key rather than the id.

    ``server_message_id`` is a per-conversation ordinal, so message 3 exists in nearly every chat.
    Recording it as a bare ``smid`` alternate made one ticked message resolve to a message in *every*
    conversation that has that number -- which a real corpus run surfaced immediately as four ambiguous
    selections. Both alternates are qualified with the conversation, exactly as the row id is.
    """
    indexes = build_indexes()
    selection = {"schema": 2,
                 "selections": {"msg": {f"conv-{CONV_A}|msg-1.0": {"conv": CONV_A, "smid": "1.0"}}}}
    resolution = pr.resolve(indexes, selection)

    assert resolution.seeds["msg"] == {f"conv-{CONV_A}|msg-1.0"}
    assert not resolution.ambiguous and not resolution.moved

    # and the same message number in the other conversation is a different row, not a second match
    other = {"schema": 2,
             "selections": {"msg": {f"conv-{CONV_B}|msg-1.0": {"conv": CONV_B, "smid": "1.0"}}}}
    assert pr.resolve(indexes, other).seeds["msg"] == {f"conv-{CONV_B}|msg-1.0"}


def test_a_message_whose_anchor_moved_is_found_by_conversation_time_and_sender():
    """The fallback for a positional anchor, which recovering one more message would shift."""
    msg = Index("msg")
    msg.add(f"conv-{CONV_A}|msg-row7", smid="")
    msg.keys[("ts_sender", f"{CONV_A}|1700000000|u-alice")].add(f"conv-{CONV_A}|msg-row7")
    indexes = {"msg": msg}

    selection = {"schema": 2, "selections": {"msg": {f"conv-{CONV_A}|msg-row4": {
        "conv": CONV_A, "ts": 1700000000, "sender": "u-alice"}}}}
    resolution = pr.resolve(indexes, selection)
    assert resolution.seeds["msg"] == {f"conv-{CONV_A}|msg-row7"}
    assert resolution.moved and "conversation, time and sender" in resolution.moved[0]["how"]


def test_the_time_and_sender_fallback_is_scoped_to_one_conversation():
    """Two people can send at the same second in two chats; that is not one message."""
    msg = Index("msg")
    for cid in (CONV_A, CONV_B):
        row = f"conv-{cid}|msg-row1"
        msg.add(row, smid="")
        msg.keys[("ts_sender", f"{cid}|1700000000|u-alice")].add(row)
    indexes = {"msg": msg}

    selection = {"schema": 2, "selections": {"msg": {f"conv-{CONV_A}|msg-row9": {
        "conv": CONV_A, "ts": 1700000000, "sender": "u-alice"}}}}
    assert pr.resolve(indexes, selection).seeds["msg"] == {f"conv-{CONV_A}|msg-row1"}


def test_a_grouped_memory_resolves_on_its_own_id_and_is_not_called_ambiguous():
    """A real corpus run refused a selection that was never ambiguous.

    ``ZMEDIAID`` identifies a *media object*, and grouped Memories share one by design — that is the
    basis of Memory grouping. Collecting every candidate and refusing on more than one therefore made
    every grouped Memory unresolvable, because its ``mediaid`` alternate matched both members. An exact
    id match is the answer; the alternates exist only for an id that moved.
    """
    indexes = build_indexes()
    sel = selection(mem=[("mem-SNAP-0001", {"snap": "SNAP-0001", "mediaid": "MEDIA-1"})])
    res = pr.resolve(indexes, sel)

    assert res.seeds["mem"] == {"mem-SNAP-0001"}
    assert res.how[("mem", "mem-SNAP-0001")] == "its own id"
    assert res.ok and not res.ambiguous and not res.moved


def test_a_non_discriminating_alternate_is_skipped_for_one_that_names_a_single_row():
    """`mediaid` matches both members of a group, so it decides nothing; `entry` here decides."""
    indexes = build_indexes()
    indexes["mem"].keys[("entry", "ENTRY-2")].add("mem-SNAP-0002")
    sel = selection(mem=[("mem-SNAP-GONE", {"mediaid": "MEDIA-1", "entry": "ENTRY-2"})])
    res = pr.resolve(indexes, sel)

    assert res.seeds["mem"] == {"mem-SNAP-0002"}
    assert "entry" in res.how[("mem", "mem-SNAP-0002")]
    assert res.moved and res.moved[0]["was"] == "mem-SNAP-GONE"


def test_an_id_with_only_non_discriminating_alternates_is_named_ambiguous_with_what_was_tried():
    indexes = build_indexes()
    sel = selection(mem=[("mem-SNAP-GONE", {"mediaid": "MEDIA-1"})])
    with pytest.raises(pr.AmbiguousSelection, match="ambiguous"):
        pr.resolve(indexes, sel)

    res = pr.Resolution()
    row_id, _how, weak = pr._resolve_one(indexes["mem"], "mem", "mem-SNAP-GONE",
                                         {"mediaid": "MEDIA-1"})
    assert row_id is None
    assert weak and weak[0]["matches"] == ["mem-SNAP-0001", "mem-SNAP-0002"]
    assert "mediaid" in weak[0]["by"]
    assert res.ok                              # a fresh Resolution is unaffected by the probe
