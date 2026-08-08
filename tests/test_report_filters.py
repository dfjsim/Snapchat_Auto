"""The index-table filter controls, and what each one is allowed to claim.

A filter in a forensic report is not only a convenience — it is a statement about the data behind
it. Two of the ones pinned here were making claims nothing established:

* the Memories "Media" filter offered "complete only", and its "complete" silently included both
  media whose completeness this tool never verified and Memories with no recovered media at all;
* a conversation with no message in ``arroyo.db`` was dropped from the report entirely, so "not
  listed" and "no messages" looked identical to the reader.

And two controls that could not lie were simply missing: a way to clear the filters, and a way to
isolate the messages the write-ahead log deleted. A control that can only ever return an empty
table is not offered at all, which is the other half of the same rule.

Every input here is synthetic. No extraction data is required or used.
"""
from scripts import conversations_report as conv
from scripts import memories_media_report as memories_report
from scripts import report_ui


# ------------------------------------------------------------------ Memories: what "complete" means

def test_media_state_keeps_apart_what_complete_used_to_swallow():
    """Four states, because "not incomplete" was three different things wearing one label."""
    assert memories_report._media_state([], 0) == "none"
    assert memories_report._media_state([{"complete": True}], 0) == "whole"
    assert memories_report._media_state([{"complete": True}, {"complete": False}], 1) == "partial"
    # None is "we did not check" (a plaintext file has no padding to check) — not "it is whole"
    assert memories_report._media_state([{"complete": None}], 0) == "unverified"
    assert memories_report._media_state([{"complete": True}, {"complete": None}], 0) == "unverified"


def test_an_unverified_file_is_never_counted_as_complete():
    """The whole point: "verified complete" must mean verified, on every file of the Memory."""
    mixed = [{"complete": True}, {"complete": None}]
    assert memories_report._media_state(mixed, 0) != "whole"


# ------------------------------------------------------------------ conversations: activity dates

def _info(**kw):
    base = dict(conv._ARROYO_BLANK)
    base.update(kw)
    return base


def test_message_times_are_used_when_there_are_messages():
    act = conv._activity([1_700_000_000, 1_700_003_600], _info(created_ms=1_600_000_000_000),
                         lambda cocoa: f"T{cocoa}")
    assert act["source"] == "messages"
    assert act["first_sort"] == 1_700_000_000 and act["last_sort"] == 1_700_003_600


def test_a_conversation_with_no_message_still_gets_a_range():
    """What Cellebrite shows for an empty conversation, and where it comes from."""
    act = conv._activity([], _info(created_ms=1_600_000_000_000,
                                   feed_first_ms=1_650_000_000_000,
                                   feed_last_ms=1_660_000_000_000), lambda cocoa: f"T{cocoa}")
    assert act["source"] == "feed"
    assert act["first_sort"] == 1_650_000_000 and act["last_sort"] == 1_660_000_000


def test_the_feed_range_falls_back_to_the_creation_timestamp():
    act = conv._activity([], _info(created_ms=1_600_000_000_000), lambda cocoa: f"T{cocoa}")
    assert act["source"] == "feed"
    assert act["first_sort"] == act["last_sort"] == 1_600_000_000


def test_a_conversation_with_no_date_at_all_says_so_rather_than_inventing_one():
    act = conv._activity([], _info(), lambda cocoa: f"T{cocoa}")
    assert act["source"] == "" and act["first"] == "" and act["first_sort"] == 0


def test_a_feed_date_is_marked_in_the_cell_and_a_message_date_is_not():
    """The columns are labelled "activity" because the two are different statements."""
    from_msgs = conv._first_last({"activity": {"first": "2023-01-01 00:00:00 UTC",
                                               "source": "messages"}}, "first")
    from_feed = conv._first_last({"activity": {"first": "2023-01-01 00:00:00 UTC",
                                               "source": "feed"}}, "first")
    assert "feedtag" not in from_msgs
    assert "feedtag" in from_feed and "no message" in from_feed


def test_the_creation_timestamp_is_kept_even_when_messages_supply_the_range():
    """A restored device creates its conversation rows on restore day; the two disagreeing is
    itself a finding, so the value is carried whether or not it is the one displayed."""
    act = conv._activity([1_700_000_000], _info(created_ms=1_600_000_000_000),
                         lambda cocoa: f"T{cocoa}")
    assert act["source"] == "messages" and act["created_sort"] == 1_600_000_000


# ------------------------------------------------------------------ a control with nothing to match

def test_the_wal_filter_is_absent_when_the_wal_deleted_nothing():
    """No corpus device has one, so this is the only thing holding the rule in place."""
    assert conv._wal_filter_html(0, "message") == ""
    assert conv._wal_filter_html(0, "conversation") == ""


def test_the_wal_filter_appears_with_its_count_when_there_is_something_to_find():
    msgs = conv._wal_filter_html(3, "message")
    assert 'id="wal"' in msgs and "(3)" in msgs and "live" in msgs
    convs = conv._wal_filter_html(2, "conversation")
    assert "deleted message(s) (2)" in convs


def test_the_media_filter_disables_a_state_no_memory_is_in():
    """Same rule, stated in the option itself: "incomplete only — 0" is why nothing came back."""
    opts = memories_report._media_filter_options({"whole": 5, "unverified": 1})
    assert "partially cached (incomplete) &mdash; 0</option>" in opts
    assert 'value="partial" disabled' in opts, "a state with no rows must not look choosable"
    assert 'value="whole">' in opts and "verified complete &mdash; 5" in opts
    assert 'value="none" disabled' in opts


def test_counted_options_is_the_shared_rule_not_a_memories_quirk():
    """Thumbnail and My Eyes Only had the same problem — a control that could return nothing."""
    opts = report_ui.counted_options((("y", "only My Eyes Only"), ("n", "exclude")), {"n": 82})
    assert 'value="y" disabled' in opts and "only My Eyes Only &mdash; 0" in opts
    assert 'value="n">' in opts and "exclude &mdash; 82" in opts


# ------------------------------------------------------------------ clearing the filters

def test_every_index_report_offers_a_way_out_of_its_filters():
    """"The report says 0 rows" is regularly one control left set three filters ago."""
    button = report_ui.clear_filters_button("memory")
    assert "SCV.clearFilters()" in button and "clearflt" in button
    assert "clearFilters:clearFilters" in report_ui.VTABLE_JS, "not exported from SCV"


def test_an_optional_filter_can_be_read_without_existing():
    """`match` runs on every report; the -wal control exists on only some of them."""
    assert "function scFv(" in report_ui.VTABLE_JS
    assert "function scFvReset(" in report_ui.VTABLE_JS
