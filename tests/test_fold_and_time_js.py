"""The folded-row and time-window logic, executed rather than pattern-matched.

Two pieces of behaviour live only in the browser, and both decide what an examiner is shown:

* **folding** — the Memories index shows a group of Memories as its earliest member's row and keeps
  the rest inside that row. Every member still has a row in the data, so a group must be found by
  *any* member's values, "Select all shown" must tick the members the filters match and no others,
  and unfolding must give every Memory its row back;
* **the time window** — *between* / *within ±N of*, compared against the wall clocks the report
  displays, with the rule that a row carrying no timestamp cannot be said to fall inside a window.

`test_report_js_valid.py` only parses this code, and a Python test can only assert on the source
text. So this runs it: node, a minimal DOM stub, and assertions on what SCV actually put in the view.
The stub is small because the table asks little of the DOM — element lookups, a rect and a couple of
listeners — and everything the tests check is decided before any of it is drawn.

Every input is synthetic. No extraction data is required or used.
"""
import json
import shutil
import subprocess

import pytest

from scripts import report_ui

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


# The DOM the virtual table touches, and nothing else. Element values are what the filter controls
# would hold, so a test sets a filter by writing to VALUES before calling SCV.refilter().
_STUB = r"""
var VALUES={};
function el(id){
 return {id:id,
  get value(){return VALUES[id]===undefined?'':VALUES[id];},
  set value(v){VALUES[id]=v;},
  get checked(){return !!VALUES[id];},
  set checked(v){VALUES[id]=v;},
  style:{},classList:{add:function(){},remove:function(){},toggle:function(){}},
  innerHTML:'',textContent:'',
  getBoundingClientRect:function(){return {top:0,height:0};},
  addEventListener:function(){},
  querySelector:function(){return null;},
  querySelectorAll:function(){return [];},
  appendChild:function(){},setAttribute:function(){},getAttribute:function(){return null;},
  children:[],closest:function(){return null;}};}
var NODES={};
var document={
 getElementById:function(id){return NODES[id]||(NODES[id]=el(id));},
 querySelectorAll:function(){return [];},
 createElement:function(){return el('created');},
 head:{appendChild:function(){}}};
var window={addEventListener:function(){},requestAnimationFrame:function(){},
            scrollTo:function(){},innerHeight:800,pageYOffset:0};
window.document=document;
/* The selection store, stubbed to a plain set so "select all shown" can be read back. */
var TICKED={};
var SCSel={get:function(kind,id){return !!TICKED[id];},
           setMany:function(kind,ids,on){ids.forEach(function(i){
             if(on)TICKED[i]=1;else delete TICKED[i];});},
           count:function(){return Object.keys(TICKED).length;},
           onChange:function(){}};
var flt_calls=0;
function flt(){flt_calls++;}
"""

_HARNESS = "function report(o){console.log(JSON.stringify(o));}\n"


def _run(rows, script, *, folded=True, extra="", set_rows=True, opts=""):
    """Load VTABLE_JS + TIME_JS in node over ``rows``, run ``script``, return its printed JSON.

    ``extra`` is a report's own script, for the parts of the filtering that live there (the
    Conversations scope control) — taken from the module rather than restated, so the test cannot
    pass against a copy that has drifted from what the report emits.
    """
    source = "\n".join([
        _STUB,
        report_ui.VTABLE_JS,
        "function scFv(id){return document.getElementById(id).value;}",
        "function scFvReset(id){document.getElementById(id).value='';}",
        report_ui.TIME_JS,
        extra,
        _HARNESS,
        f"var ROWS={json.dumps(rows)};",
        # Skipped by the test for a data file that failed to load: that is the whole difference
        # between "this report has no rows" and "this report's rows never arrived".
        "SCV.setRows(ROWS);" if set_rows else "",
        "SCV.init({mount:'vwrap',win:'vwin',pad:'vpad',header:'#vhdr',missing:'vmiss',"
        "empty:'vempty',rowHeight:100,cols:'1fr',selKind:'mem',detailBase:'data/detail-',"
        f"folded:function(){{return {'true' if folded else 'false'};}},{opts}"
        "query:function(){return document.getElementById('q').value;},"
        "match:function(m,r){return scTimeHit(scTimeWin('t'),m.ts)"
        "&&(!document.getElementById('meo').checked||m.meo==='y');}});",
        script,
    ])
    proc = subprocess.run([NODE, "-"], input=source, text=True, encoding="utf-8",
                          capture_output=True)
    if proc.returncode:
        pytest.fail(f"the harness failed:\n{proc.stderr}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


# What the table decided: how many rows it will draw, how many rows the filters matched (the two
# differ exactly when a fold is in effect), and which leads are standing in for a member.
_SHOWN = "report({rows:SCV.count(),matching:SCV.matching(),hits:SCV.foldHits()});"


def _row(rid, *, lead=None, ts=(), text="", meo="n"):
    meta = {"ts": list(ts), "meo": meo}
    if lead:
        meta["lead"] = lead
    return [rid, ["&#9656;", rid], text or rid.lower(), {}, 0, meta]


# --------------------------------------------------------------------------- folding

@needs_node
def test_a_group_is_one_row_and_its_members_are_still_there():
    rows = [_row("mem-A", lead="mem-A"), _row("mem-B", lead="mem-A"),
            _row("mem-C", lead="mem-A"), _row("mem-D")]
    out = _run(rows, _SHOWN)

    assert out[0] == {"rows": 2, "matching": 4, "hits": {}}, \
        "the group's three rows should be one row on screen, and all four should still match"


@needs_node
def test_unfolding_gives_every_memory_its_own_row_back():
    rows = [_row("mem-A", lead="mem-A"), _row("mem-B", lead="mem-A"), _row("mem-C")]
    out = _run(rows, _SHOWN, folded=False)

    assert out[0]["rows"] == 3 and out[0]["matching"] == 3


@needs_node
def test_a_group_is_found_by_a_folded_members_own_value():
    """The lead does not match; a member does. Hiding the group would lose that member entirely."""
    rows = [_row("mem-A", lead="mem-A", text="apple"),
            _row("mem-B", lead="mem-A", text="banana"),
            _row("mem-C", text="cherry")]
    out = _run(rows, "VALUES['q']='banana';SCV.refilter();" + _SHOWN)

    assert out[0]["rows"] == 1, "the lead's row stands in for the member that matched"
    assert out[0]["matching"] == 1, "one memory matched, not the whole group"
    assert out[0]["hits"] == {"mem-A": "mem-B"}, \
        "the lead has to say which member it is standing in for, so the row can be opened on it"


@needs_node
def test_a_lead_that_matches_on_its_own_is_not_reported_as_a_member_hit():
    rows = [_row("mem-A", lead="mem-A", text="apple"), _row("mem-B", lead="mem-A", text="apple")]
    out = _run(rows, "VALUES['q']='apple';SCV.refilter();" + _SHOWN)

    assert out[0]["hits"] == {}, "nothing to point at: the row the examiner sees matched"
    assert out[0]["matching"] == 2


@needs_node
def test_select_all_shown_ticks_the_members_that_match_and_no_others():
    """A folded member the filters do not match must not be ticked just because its lead is on
    screen — that would put rows the examiner did not ask for into a disclosure selection."""
    rows = [_row("mem-A", lead="mem-A", meo="y"), _row("mem-B", lead="mem-A", meo="n"),
            _row("mem-C", lead="mem-A", meo="y"), _row("mem-D", meo="n")]
    out = _run(rows, "VALUES['meo']=true;SCV.refilter();SCV.selectShown(true);"
                     "report(Object.keys(TICKED).sort());")

    assert out[0] == ["mem-A", "mem-C"]


@needs_node
def test_select_all_shown_reaches_folded_members_at_all():
    rows = [_row("mem-A", lead="mem-A"), _row("mem-B", lead="mem-A"), _row("mem-C")]
    out = _run(rows, "SCV.selectShown(true);report(Object.keys(TICKED).sort());")

    assert out[0] == ["mem-A", "mem-B", "mem-C"], \
        "the fold is a way of showing rows, not a reason to leave them out of a selection"


@needs_node
def test_a_folded_member_is_still_reachable_by_id():
    """What makes the fold cheap: a folded row is still a row. Every cross-report link into it, and
    `goTo`, resolve through the same id they always did."""
    rows = [_row("mem-A", lead="mem-A"), _row("mem-B", lead="mem-A")]
    out = _run(rows, "report({has:SCV.hasRow('mem-B'),opened:SCV.openRow('mem-B')});")

    assert out[0]["has"] is True and out[0]["opened"] is True


@needs_node
def test_the_leads_reached_through_a_member_are_opened_on_it():
    rows = [_row("mem-A", lead="mem-A", text="apple"),
            _row("mem-B", lead="mem-A", text="banana"),
            _row("mem-C", lead="mem-C", text="cherry"),
            _row("mem-D", lead="mem-C", text="banana")]
    out = _run(rows, "VALUES['q']='banana';SCV.refilter();report({opened:SCV.openFoldHits()});")

    assert out[0]["opened"] == 2, "both groups were reached through a member, so both open"


# --------------------------------------------------------------------------- the time window

@needs_node
@pytest.mark.parametrize("mode,values,expected", [
    ("", {}, ["mem-A", "mem-B", "mem-C"]),
    ("range", {"tfrom": "2026-01-02T00:00", "tto": "2026-01-02T23:59"}, ["mem-B"]),
    ("range", {"tfrom": "2026-01-02T00:00"}, ["mem-B", "mem-C"]),
    ("range", {"tto": "2026-01-02T00:00"}, ["mem-A"]),
    ("near", {"tat": "2026-01-03T12:00", "tn": "13", "tunit": "h"}, ["mem-C"]),
    ("near", {"tat": "2026-01-03T12:00", "tn": "2", "tunit": "d"}, ["mem-B", "mem-C"]),
])
def test_the_window_matches_the_wall_clocks_it_is_given(mode, values, expected):
    keys = {
        "mem-A": report_ui.ts_keys("2026-01-01 08:00:00 EST (UTC-05:00)"),
        "mem-B": report_ui.ts_keys("2026-01-02 09:30:00 EST (UTC-05:00)"),
        "mem-C": report_ui.ts_keys("2026-01-03 23:45:00 EST (UTC-05:00)"),
    }
    rows = [_row(rid, ts=ts) for rid, ts in keys.items()]
    setup = "".join(f"VALUES[{key!r}]={value!r};" for key, value in values.items())
    out = _run(rows, f"VALUES['tmode']={mode!r};{setup}SCV.refilter();"
                     "report({n:SCV.count()});")

    assert out[0]["n"] == len(expected)


@needs_node
def test_a_row_with_no_timestamp_is_hidden_by_a_window_not_included_by_it():
    """A carved Memory has no ZGALLERYSNAP row and so no time at all. It cannot be shown to fall
    inside the window asked for, and presenting it as if it did would be an answer we do not have."""
    rows = [_row("mem-A", ts=report_ui.ts_keys("2026-01-02 09:30:00 UTC")), _row("mem-N", ts=())]
    out = _run(rows, "VALUES['tmode']='range';VALUES['tfrom']='2026-01-01T00:00';"
                     "SCV.refilter();report({n:SCV.count()});")

    assert out[0]["n"] == 1

    both = _run(rows, "report({n:SCV.count()});")
    assert both[0]["n"] == 2, "with no window set it must be visible again"


@needs_node
def test_an_incomplete_or_empty_window_is_no_window():
    """Half-typed input must not empty the table: «between» with neither end, and a moment that is
    still being typed, both mean the filter is not set yet."""
    rows = [_row("mem-A", ts=report_ui.ts_keys("2026-01-02 09:30:00 UTC"))]
    out = _run(rows, "VALUES['tmode']='range';SCV.refilter();report({n:SCV.count()});"
                     "VALUES['tmode']='near';VALUES['tat']='2026-01';VALUES['tn']='1';"
                     "SCV.refilter();report({n:SCV.count()});")

    assert [o["n"] for o in out] == [1, 1]


@needs_node
def test_the_window_and_the_fold_work_together():
    """The pair the request asked for: a timestamp that only a folded member carries still finds the
    group, and the lead says which member it was so the row can be opened on it."""
    rows = [_row("mem-A", lead="mem-A", ts=report_ui.ts_keys("2026-01-01 08:00:00 UTC")),
            _row("mem-B", lead="mem-A", ts=report_ui.ts_keys("2026-06-15 12:00:00 UTC")),
            _row("mem-C", ts=report_ui.ts_keys("2026-12-31 23:00:00 UTC"))]
    out = _run(rows, "VALUES['tmode']='near';VALUES['tat']='2026-06-15T12:30';"
                     "VALUES['tn']='1';VALUES['tunit']='h';SCV.refilter();" + _SHOWN)

    assert out[0]["rows"] == 1 and out[0]["matching"] == 1
    assert out[0]["hits"] == {"mem-A": "mem-B"}


# --------------------------------------------------- the group's own selection box

@needs_node
def test_a_lead_row_carries_a_group_box_and_a_lone_row_does_not():
    rows = [_row("mem-A", lead="mem-A"), _row("mem-B", lead="mem-A"), _row("mem-C")]
    out = _run(rows, "report({html:document.getElementById('vwin').innerHTML});",
               opts="groupBox:true,")

    html = out[0]["html"]
    assert html.count('class="grpbox"') == 1, "one group box, on the lead of the only group"
    assert 'data-grp="mem-A"' in html


@needs_node
def test_unfolding_takes_the_group_box_away():
    """With the fold off every Memory has a row and a box of its own, so a control for "the whole
    group" would be a second way to do the same thing."""
    rows = [_row("mem-A", lead="mem-A"), _row("mem-B", lead="mem-A")]
    out = _run(rows, "report({html:document.getElementById('vwin').innerHTML});",
               folded=False, opts="groupBox:true,")

    assert "grpbox" not in out[0]["html"]


@needs_node
def test_the_group_box_reports_none_some_or_all():
    rows = [_row("mem-A", lead="mem-A"), _row("mem-B", lead="mem-A"), _row("mem-C", lead="mem-A")]
    out = _run(rows, "report({none:SCV.foldCount('mem-A')});"
                     "TICKED['mem-B']=1;report({some:SCV.foldCount('mem-A')});"
                     "TICKED['mem-A']=1;TICKED['mem-C']=1;"
                     "report({all:SCV.foldCount('mem-A')});",
               opts="groupBox:true,")

    assert out[0]["none"] == [0, 3]
    assert out[1]["some"] == [1, 3] and out[2]["all"] == [3, 3]


@needs_node
def test_the_group_box_acts_on_the_whole_group_whatever_the_filters_say():
    """Unlike "Select all shown", which follows the filters: this control is labelled "the whole
    group", so it has to mean the group as the app data has it."""
    rows = [_row("mem-A", lead="mem-A", meo="y"), _row("mem-B", lead="mem-A", meo="n"),
            _row("mem-C", lead="mem-A", meo="y"), _row("mem-D")]
    out = _run(rows, "VALUES['meo']=true;SCV.refilter();"
                     "report({n:SCV.selectGroup('mem-A',true),ticked:Object.keys(TICKED).sort()});"
                     "SCV.selectGroup('mem-A',false);report({after:Object.keys(TICKED)});",
               opts="groupBox:true,")

    assert out[0]["n"] == 3 and out[0]["ticked"] == ["mem-A", "mem-B", "mem-C"]
    assert out[1]["after"] == [], "and clears the same three"


def test_the_three_state_box_is_set_as_a_property_not_an_attribute():
    """`indeterminate` cannot be written into the HTML a row is built from, so it has to be applied
    after every render — like the fold-hit marking."""
    assert "box.indeterminate=n>0&&n<total;" in report_ui.VTABLE_JS
    assert "markGroupBoxes();" in report_ui.VTABLE_JS


# --------------------------------------------------- an empty report vs a missing data file

@needs_node
def test_a_report_with_no_rows_does_not_accuse_itself_of_a_missing_data_folder():
    """A partial extract can legitimately contain no row of a kind. Saying "keep the data folder next
    to the HTML" there sends the examiner after a fault that is not present."""
    out = _run([], "report({banner:document.getElementById('vmiss').style.display||'',"
                   "empty:document.getElementById('vempty').innerHTML});")

    assert out[0]["banner"] != "block"
    assert out[0]["empty"] == "This report contains no rows."


@needs_node
def test_a_data_file_that_never_loaded_still_raises_the_banner():
    out = _run([], "report({banner:document.getElementById('vmiss').style.display});",
               set_rows=False)

    assert out[0]["banner"] == "block"


@needs_node
def test_a_report_that_has_rows_keeps_its_own_filtered_to_nothing_wording():
    rows = [_row("mem-A", ts=report_ui.ts_keys("2026-01-02 09:30:00 UTC"))]
    out = _run(rows, "document.getElementById('vempty').innerHTML='No memory matches the filters.';"
                     "VALUES['tmode']='range';VALUES['tfrom']='2030-01-01T00:00';SCV.refilter();"
                     "report({empty:document.getElementById('vempty').innerHTML});")

    assert out[0]["empty"] == "No memory matches the filters."


# --------------------------------------------------- the checkboxes inside a folded row

def test_an_inline_key_record_wins_over_the_rows():
    """A folded member's checkbox sits inside its *lead's* virtual row. Reading the row's key record
    there would file one Memory's tick under another Memory's identifiers, and a later partial run
    would resolve that selection to the wrong Memory."""
    js = report_ui.SELECT_JS
    handler = js[js.index("document.addEventListener('change'"):]
    handler = handler[:handler.index("set(el.getAttribute('data-kind')")]

    assert handler.index("data-keys") < handler.index("SCV.selKeys"), \
        "the box's own data-keys must be tried before the row's"


def test_a_box_inside_an_expanded_row_is_synced_and_the_rows_own_box_is_not():
    """The row's own checkbox is rebuilt from the store by rowHtml; a box in the detail HTML is not,
    so it needs putting back after every redraw or a ticked Memory renders unticked."""
    assert "if(b.closest('.vcells'))return;" in report_ui.SELECT_JS
    assert "if(window.scSyncBoxes)scSyncBoxes();" in report_ui.VTABLE_JS


# ------------------------------------------------------- the Conversations scope, executed

@needs_node
def test_the_scope_control_keeps_the_two_kinds_of_conversation_time_apart():
    """The one thing this must never do is answer «message times» with a feed date.

    A conversation holding no message still has first/last activity, taken from the app's chat feed —
    which says it was active then, not that a message existed then. Its `mt` list is empty, so the
    «message times only» scope has to return nothing for it rather than fall back to the feed date.
    """
    from scripts import conversations_report

    rows = [_row("conv-A")]
    rows[0][5] = {"ct": report_ui.ts_keys("2026-03-01 10:00:00 UTC"), "mt": []}
    probe = ("VALUES['tmode']='range';VALUES['tfrom']='2026-03-01T00:00';"
             "VALUES['tto']='2026-03-01T23:59';"
             "['mt','ct','both'].forEach(function(s){VALUES['tscope']=s;"
             "report({scope:s,hit:scTimeHit(scTimeWin('t'),scConvTimes(ROWS[0][5]))});});")
    out = _run(rows, probe, extra=conversations_report._REPORT_JS)

    assert [(o["scope"], o["hit"]) for o in out] == \
        [("mt", False), ("ct", True), ("both", True)]


# --------------------------------------------------------------------------- the key, in Python

def test_a_displayed_timestamp_reads_back_as_its_own_wall_clock():
    """The number a row is filtered on comes from the string the examiner is looking at, so the two
    cannot disagree — whatever timezone the run was rendered in."""
    assert report_ui.ts_key("2026-01-02 09:30:00 EST (UTC-05:00)") == \
        report_ui.ts_key("2026-01-02 09:30:00 UTC") == \
        report_ui.ts_key("2026-01-02T09:30:00")


def test_a_missing_or_unparseable_timestamp_has_no_key():
    for text in ("", None, "no date recorded", "2026", "not a time at all"):
        assert report_ui.ts_key(text) is None


def test_seconds_are_optional_and_default_to_zero():
    assert report_ui.ts_key("2026-01-02 09:30") == report_ui.ts_key("2026-01-02 09:30:00")


def test_the_keys_of_a_row_are_sorted_deduplicated_and_drop_the_empties():
    keys = report_ui.ts_keys("2026-01-03 00:00:00 UTC", "", "2026-01-01 00:00:00 UTC",
                             "2026-01-03 00:00:00 UTC", "no date recorded")

    assert keys == sorted(keys) and len(keys) == 2
