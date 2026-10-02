"""Possible Memory: leads by time and kind — listed with every difference, never a link.

Every input is synthetic.
"""
from scripts import cache_controller_report as cc
from scripts import memory_leads as ml

T = 1_710_000_000.0                 # a file time; Memory times are offsets from it


def _files(kind="video", **points):
    return {"K": {"kind": kind, "points": [(label, T + d) for label, d in points.items()],
                  "ctx19": False}}


def test_the_window_and_the_kind():
    mems = {"A": {"kind": "video", "points": [("created", T + 600)]},       # on the edge
            "B": {"kind": "video", "points": [("created", T + 601)]},       # just outside
            "C": {"kind": "image", "points": [("created", T + 1)]}}         # wrong kind
    found = ml.find_leads(_files(claim=0), mems)["K"]
    assert [lead["snap_id"] for lead in found["leads"]] == ["A"] and found["in_window"] == 1
    assert ml.find_leads(_files(kind=None, claim=0), mems) == {}


def test_ranking_and_every_pair_listed():
    mems = {"FAR": {"kind": "video", "points": [("created", T + 300)]},
            "NEAR": {"kind": "video", "points": [("created", T - 2), ("captured", T + 500)]}}
    found = ml.find_leads(_files(claim=0, birth=-20), mems)["K"]
    assert [lead["snap_id"] for lead in found["leads"]] == ["NEAR", "FAR"]
    near = found["leads"][0]
    assert near["best_delta_s"] == -2 and len(near["pairs"]) == 4     # 2 file × 2 Memory times


def test_the_panel_says_what_it_is_and_never_links():
    entry = {"memory": None, "leads": {"leads": [{"snap_id": "S-1", "best_delta_s": 0.0,
                                                  "pairs": [{"file": "claim, context 19",
                                                             "memory": "ZGALLERYSNAP.ZCREATETIMEUTC",
                                                             "delta_s": 0.0}],
                                                  "kind": "video", "ctx19": True}],
                                       "in_window": 3, "window_s": 600}}
    html = cc._leads_html(entry, "../")
    assert "NOT proven" in html and "possible: S-1" in html and "scCopySnapIds" in html
    assert "context 19" in html and "3 Memory/Memories" in html
    assert entry["memory"] is None                                  # nothing turned into a link
    assert cc._leads_html({"leads": None}, "../") == ""


def test_the_basis_names_the_window():
    assert "10 minutes" in ml.basis(600) and "ZDURATION is not used" in ml.basis()


# ------------------------------------------------------------------------- the Memory's side

import json                                                         # noqa: E402
import shutil                                                       # noqa: E402
import subprocess                                                   # noqa: E402

import pytest                                                       # noqa: E402

from scripts import memories_media_report as mr                     # noqa: E402
from scripts import report_ui                                       # noqa: E402

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

K1, K2 = "c" * 32, "d" * 32


def _two_files():
    mems = {"m-a": {"kind": "video", "points": [("created", T)]},
            "m-b": {"kind": "video", "points": [("created", T + 30)]}}
    files = {K1: {"kind": "video", "points": [("claim", T + 1)], "ctx19": True},
             K2: {"kind": "video", "points": [("claim", T + 25)], "ctx19": False}}
    return ml.find_leads(files, mems)


def test_the_leads_seen_from_each_memory():
    by = ml.by_memory(_two_files())
    assert set(by) == {"M-A", "M-B"}                                  # snap ids looked up in upper case
    a = by["M-A"]
    assert [f["ck"] for f in a] == [K1, K2]                           # closest file first
    assert (a[0]["rank"], a[0]["of"], a[0]["n"], a[0]["c19"]) == (1, 2, 2, True)
    assert a[1]["rank"] == 2 and a[1]["d"] == -25.0
    assert a[0]["p"] == [["claim", "created", -1.0]]


def test_the_data_file_is_one_call_naming_its_run(tmp_path):
    path = ml.write_script(str(tmp_path), _two_files(), "run-1")
    text = open(path, encoding="utf-8").read()
    assert text.startswith("SCMemoryLeads(") and text.rstrip().endswith(");")
    data = json.loads(text[len("SCMemoryLeads("):text.rindex(")")])
    assert data["run"] == "run-1" and data["window_s"] == ml.LEAD_WINDOW_S
    assert "NOT a link" in data["basis"] and "10 minutes" in data["basis"]
    # written even when there is nothing, so a re-render never leaves an earlier run's leads behind
    ml.write_script(str(tmp_path), {}, "run-2")
    assert '"by_snap":{}' in open(path, encoding="utf-8").read()


def test_the_memory_page_carries_only_the_ids_to_look_up():
    members = [{"snap_id": "S-1"}, {"snap_id": "S-2"}]
    assert mr._leads_placeholder(members, None) == (
        '<div id="memleads" data-snaps="S-1 S-2" data-offer="1"></div>')
    assert "data-offer" not in mr._leads_placeholder(members, object())   # never in a partial


def test_tenths_where_seconds_would_read_zero():
    assert cc._signed(-0.3) == "−0.3 s" and cc._signed(42) == "+42 s" and cc._signed(600) == "+10.0 min"


_DOM = r"""
var NODES={};
function el(id){return NODES[id]||(NODES[id]={id:id,value:'',innerHTML:'',textContent:'',style:{},
 attrs:{},getAttribute:function(k){return this.attrs[k]===undefined?null:this.attrs[k];},
 classList:{add:function(){},remove:function(){},toggle:function(){}},
 getBoundingClientRect:function(){return {top:0,height:0};},addEventListener:function(){},
 querySelector:function(){return null;},querySelectorAll:function(){return [];},children:[]});}
var document={getElementById:el,querySelectorAll:function(){return [];},
 createElement:function(){return el('x');},head:{appendChild:function(){}}};
var window={addEventListener:function(){},requestAnimationFrame:function(){},scrollTo:function(){},
 innerHeight:800,pageYOffset:0,SCAUTO_RUN:'run-1'};
function scFv(id){return el(id).value;}
"""


@needs_node
def test_the_index_marks_and_filters_the_memories_with_a_lead():
    rows = [["mem-M-A", ["", "", "video"], "m-a", {}, None, {}],
            ["mem-M-C", ["", "", "video"], "m-c", {}, None, {}]]
    source = "\n".join([
        _DOM, report_ui.VTABLE_JS, ml.LOADER_JS, ml.script_text(_two_files(), "run-1"), ml.MEMORY_JS,
        f"SCV.setRows({json.dumps(rows)});",
        "SCV.init({mount:'w',win:'v',pad:'p',header:'#h',rowHeight:10,cols:'1fr',"
        "query:function(){return '';},match:function(m){var pc=scFv('pcf');return !pc||m.pcf===pc;},"
        "count:function(n){window.N=n;}});",
        "var marked=scLeadsIndex('../',2);el('pcf').value='y';SCV.refilter();",
        "console.log(JSON.stringify({marked:marked,shown:window.N,shown_label:el('pcfl').style.display,"
        "option:el('pcfy').textContent}));",
        # another run's data file in the same folder says nothing about this one
        "window.SCAUTO_RUN='run-2';console.log(JSON.stringify({other:scLeadsIndex('../',2)}));"])
    proc = subprocess.run([NODE, "-"], input=source, text=True, encoding="utf-8", capture_output=True)
    assert proc.returncode == 0, proc.stderr
    first, second = [json.loads(line) for line in proc.stdout.splitlines()]
    assert first == {"marked": 1, "shown": 1, "shown_label": "",
                     "option": "with a possible cached file (1)"}
    assert second == {"other": 0}


@needs_node
def test_the_memory_page_lists_each_file_with_where_this_memory_ranks():
    source = "\n".join([
        _DOM, ml.LOADER_JS, ml.script_text(_two_files(), "run-1"), ml.MEMORY_JS,
        "var h=el('memleads');h.attrs={'data-snaps':'m-b','data-offer':'1'};",
        "var n=scLeadsPage('../../');console.log(JSON.stringify({n:n,html:h.innerHTML}));"])
    proc = subprocess.run([NODE, "-"], input=source, text=True, encoding="utf-8", capture_output=True)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["n"] == 2
    page = out["html"]
    assert "NOT proven" in page and "Copy snap ID" in page
    assert f'href="../../CacheController/CacheController_report.html#ck-{K2}"' in page
    assert page.index(K2) < page.index(K1)                     # m-b is closest to the second file
    assert "#2 of the 2 it lists" in page and "claimed as Memories media (context 19)" in page


@needs_node
def test_a_folded_group_row_says_one_of_its_memories_has_a_lead():
    # M-B has a lead and is folded behind M-Z, which has none of its own
    rows = [["mem-M-Z", ["", "", "video"], "m-z", {}, None, {"lead": "mem-M-Z"}],
            ["mem-M-B", ["", "", "video"], "m-b", {}, None, {"lead": "mem-M-Z"}]]
    source = "\n".join([
        _DOM, report_ui.VTABLE_JS, ml.LOADER_JS, ml.script_text(_two_files(), "run-1"), ml.MEMORY_JS,
        f"SCV.setRows({json.dumps(rows)});",
        "var R=null;SCV.annotate(function(r){if(!R)R=[];R.push(r);return false;});",
        "var n=scLeadsIndex('../',2);",
        "console.log(JSON.stringify({n:n,lead:R[0][1][2],lead_meta:R[0][5],member:R[1][1][2]}));"])
    proc = subprocess.run([NODE, "-"], input=source, text=True, encoding="utf-8", capture_output=True)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["n"] == 1                                         # one Memory has a lead, not two
    assert "possible file (grouped)" in out["lead"] and "pcf" not in out["lead_meta"]
    assert "2 possible files" in out["member"] and f"#find={K2}|{K1}" in out["member"]  # closest first


def test_a_partial_extract_names_only_the_memories_it_holds():
    by = ml.by_memory(_two_files(), keep=lambda sid: sid == "m-b")
    assert set(by) == {"M-B"}
