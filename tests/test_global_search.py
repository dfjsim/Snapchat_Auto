"""Searching every report at once (``search.html``, scripts/global_search.py).

The page must run the reports' own search — the same text, the same rule — over the rows the reports
wrote, so its counts are the counts each report's box gives. So beside the Python side (which reports
a folder holds, which conversation pages it lists, the index page's search box), the page itself is
run in node over a folder of synthetic reports: its data files loaded as a browser would, a query
given in the fragment, and the results it draws read back.

Every input is synthetic.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

import Snapchat_Auto as app
from scripts import global_search, report_ui

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

CK1, CK2 = "a" * 32, "b" * 32


def _report(root, page, data_dir, rows):
    os.makedirs(os.path.join(root, os.path.dirname(page)), exist_ok=True)
    with open(os.path.join(root, page), "w", encoding="utf-8") as fh:
        fh.write("<!doctype html>")
    if data_dir is not None:
        os.makedirs(os.path.join(root, data_dir), exist_ok=True)
        report_ui.write_rows(os.path.join(root, data_dir), rows)


def _row(anchor, cells, text):
    return [anchor, cells, text.lower(), {}, None, {}]


@pytest.fixture
def folder(tmp_path):
    root = str(tmp_path / "Reports")
    _report(root, "Contacts/Contacts_report.html", "Contacts/data", [
        _row("ct-u1", ["", "Alice Test", "alice-test", "", "u-0001"], "alice test alice-test u-0001")])
    _report(root, "Conversations/Conversations_report.html", "Conversations/data", [
        _row("conv-c1", ["", "Private", "Chat with Alice", "c1",
                         "Alice Test", "", "", "", "", "<a href='pages/k1.html#conv-c1'>open</a>"],
             "chat with alice c1")])
    _report(root, "Conversations/pages/k1.html", "Conversations/pages/data/k1", [
        _row("msg-1.0", ["", "2024-03-10 08:30", "Received", "alice-test", "Text", "see " + CK1],
             "see " + CK1),
        _row("msg-2.0", ["", "2024-03-10 08:31", "Sent", "owner", "Text", "nothing"], "nothing")])
    _report(root, "CacheController/CacheController_report.html", "CacheController/data", [
        _row(f"ck-{CK1}", ["", "Snap editor", CK1, "", "", "mp4", "1.0 MB"], f"{CK1} snap editor mp4"),
        _row(f"ck-{CK2}", ["", "Other", CK2, "", "", "jpg", "2 KB"], f"{CK2} other jpg")])
    _report(root, "Memories/Memories_report.html", None, [])            # a page with no data folder
    return root


# ----------------------------------------------------------------------------------- the folder

def test_only_the_reports_the_folder_holds_are_searched(folder):
    srcs = global_search.sources(folder)
    assert [s["key"] for s in srcs] == ["contacts", "conversations", "messages", "cache"]
    msgs = srcs[2]
    assert msgs["pages"] == [["k1", "Conversations/pages/k1.html",
                              "Conversations/pages/data/k1/index.js"]]
    assert msgs["tab"] == "scauto_conv_page"
    assert global_search.sources(folder, "android")[0]["cells"] == list(global_search.SOURCES[0][5])


def test_android_memories_are_labelled_from_their_own_columns(folder):
    report_ui.write_rows(os.path.join(folder, "Memories", "data"), [])
    srcs = {s["key"]: s for s in global_search.sources(folder, "android")}
    assert srcs["memories"]["cells"] == list(global_search.ANDROID_MEMORY_CELLS)


def test_no_report_no_page(tmp_path):
    assert global_search.write_page(str(tmp_path)) is None
    assert not (tmp_path / global_search.PAGE).exists()


def test_the_page_names_what_it_cannot_search(folder):
    _report(folder, "Communications_legacy/Communications_legacy_report.html", None, [])
    path = global_search.write_page(folder)
    doc = open(path, encoding="utf-8").read()
    assert "Not searched: Communications (legacy)" in doc
    assert "<\\/" not in doc or "</script>" in doc                 # the source list stays in its script


def test_the_index_page_has_a_search_box_that_opens_the_page(folder):
    run = os.path.dirname(folder)
    app.write_index(run, "Reports")
    doc = open(os.path.join(run, "index.html"), encoding="utf-8").read()
    assert 'action="Reports/search.html"' in doc and 'target="scauto_search"' in doc
    assert os.path.isfile(os.path.join(folder, "search.html"))


def test_every_report_links_to_it_from_its_search_box():
    link = report_ui.search_all_link("../")
    assert 'href="../search.html"' in link and 'target="scauto_search"' in link
    assert "scSearchAll(this)" in link and "function scSearchAll" in report_ui.NAV_JS


# ------------------------------------------------------------------------------------ the search

def _node(source):
    proc = subprocess.run([NODE, "-"], input=source, text=True, encoding="utf-8", capture_output=True)
    if proc.returncode:
        pytest.fail(f"node failed:\n{proc.stderr}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


@needs_node
def test_the_core_is_the_reports_own_search():
    rows = [["a", ["<b>Alice</b> &amp; co", "x"], "alice u-1", {}, None, {}],
            ["b", ["Bob", "y"], "bob u-2", {}, None, {}],
            ["c", ["—", "Carol"], "carol", {}, None, {}]]
    out = _node(global_search.CORE_JS + f"var R={json.dumps(rows)};" + r"""
console.log(JSON.stringify({
 terms:SCQ.terms(' Alice | | BOB '),
 hits:SCQ.hits(R,SCQ.terms('alice|bob')),
 none:SCQ.hits(R,SCQ.terms('')),
 label:SCQ.label(R[0],[0,1]),
 skip:SCQ.label(R[2],[0,1]),
 snip:SCQ.snippet('xxxxxxxx alice yyyy',['alice'],4),
 find:SCQ.findHash(['a b','c'])}));""")[0]
    assert out["terms"] == ["alice", "bob"]
    assert out["hits"] == [0, 1] and out["none"] == []
    assert out["label"] == "Alice & co · x"                        # markup dropped, references read
    assert out["skip"] == "Carol"                                  # an empty-dash cell is no label
    assert out["snip"] == {"pre": "…xxx ", "hit": "alice", "post": " yyy…"}
    assert out["find"] == "#find=a%20b|c"


_DOM = r"""
const fs=require('fs'),path=require('path'),vm=require('vm');
const ROOT=process.env.SC_ROOT;
const EL={};
function el(id){return EL[id]||(EL[id]={id:id,value:'',innerHTML:'',textContent:''});}
globalThis.document={getElementById:el,
 createElement:function(){return {};},
 head:{appendChild:function(sc){
  const file=path.join(ROOT,sc.src);
  if(!fs.existsSync(file)){sc.onerror();return;}
  vm.runInThisContext(fs.readFileSync(file,'utf8'));sc.onload();}}};
globalThis.window={addEventListener:function(){}};
globalThis.location={hash:process.env.SC_HASH,search:''};
"""


def _search(folder, query, after_writing=None):
    doc = open(global_search.write_page(folder), encoding="utf-8").read()
    if after_writing:
        after_writing()
    script = re.findall(r"<script>(.*?)</script>", doc, re.S)[-1]
    env = dict(os.environ, SC_ROOT=folder, SC_HASH="#q=" + query)
    proc = subprocess.run(
        [NODE, "-"], input=_DOM + f"vm.runInThisContext({json.dumps(script)});"
        "console.log(JSON.stringify({summary:el('summary').innerHTML,results:el('results').innerHTML,"
        "status:el('status').textContent,q:el('gq').value,hash:location.hash}));",
        text=True, encoding="utf-8", capture_output=True, env=env)
    if proc.returncode:
        pytest.fail(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


@needs_node
def test_one_query_finds_a_value_in_every_report_that_holds_it(folder):
    out = _search(folder, CK1.upper())
    assert out["q"] == CK1.upper() and out["hash"] == "_"           # the fragment is consumed
    res = out["results"]
    # the cache entry, linked to its row in the cache report's named tab…
    assert f'href="CacheController/CacheController_report.html#ck-{CK1}"' in res
    assert 'target="scauto_cache"' in res
    # …and the message that mentions it, on its conversation's page, under the conversation's name
    assert 'href="Conversations/pages/k1.html#msg-1.0"' in res and "Chat with Alice" in res
    assert "<b>2</b> rows match" in out["summary"]
    # "Open all" is the report filtered to the same search, as a #find= link
    assert f"CacheController_report.html#find={CK1}" in res
    assert "No match in Contacts, Conversations." in res
    assert CK2 not in res and "msg-2.0" not in res


@needs_node
def test_either_term_and_a_report_whose_data_did_not_load(folder):
    gone = os.path.join(folder, "CacheController", "data", "index.js")
    out = _search(folder, "alice-test|chat with|" + CK2, after_writing=lambda: os.remove(gone))
    res = out["results"]
    assert "ct-u1" in res and "conv-c1" in res                     # either term, in two reports
    assert "<b>2</b> rows match" in out["summary"]
    # the cache report's rows never arrived: it is not "no match", it is said to be missing
    assert "could not be loaded" in res and "No match in Messages." in res
    assert "Cache controller" not in res.split("No match in")[1]
