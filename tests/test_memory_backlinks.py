"""What the Library/Caches report links to a Memory, listed on the Memory's own page.

That report renders after the Memories report, so it writes its links as a data file the Memory's
page loads (scripts/memory_backlinks.py). The list must be exactly its links — every kind of them —
and a partial extract's must name only the Memories it holds.

Every input is synthetic.
"""
import json
import shutil
import subprocess

import pytest

from scripts import cache_media_report as cmr
from scripts import memory_backlinks as mb
from scripts import memory_leads as ml

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

SNAP = "77777777-aaaa-4bbb-8ccc-dddddddddddd"


def _entry(rel, sha, links, copies=1):
    return {"rel": rel, "sha256": sha, "ext": "mp4", "bytes": 4096, "copies": [{}] * copies,
            "links": links}


def _entries():
    pack = {"kind": "memory", "snap_id": SNAP, "how": "pack", "media_path": "media/x.jpg",
            "basis": "pack basis"}
    url = {"kind": "memory", "snap_id": SNAP, "how": "url", "basis": "url basis"}
    same = {"kind": "memory", "snap_id": SNAP, "how": "device", "by_content": "device",
            "basis": "device basis"}
    other = {"kind": "memory", "snap_id": "OTHER", "how": "url", "basis": "x"}
    return [_entry("caching-media/ab/" + "c" * 64 + "-0.pack", "1" * 64, [pack], copies=2),
            _entry("x.mp4", "2" * 64, [same, {"kind": "cache", "key": "k"}]),
            _entry("PINCache/https%3A%2F%2Fcf-st.sc-cdn.net%2Fd%2Fx", "3" * 64, [url, other])]


def test_every_kind_of_link_is_listed_for_its_memory():
    by = mb.by_memory(_entries())
    rows = by[SNAP.upper()]
    assert [r["how"] for r in rows] == ["pack", "url", "device"]          # in HOW's order
    assert rows[0] == {"a": "cm-" + "1" * 64, "rel": "caching-media/ab/" + "c" * 64 + "-0.pack",
                       "n": 2, "ext": "mp4", "bytes": 4096, "how": "pack", "basis": "pack basis"}
    assert set(by) == {SNAP.upper(), "OTHER"}


def test_a_partial_extract_names_only_its_memories(tmp_path):
    path = mb.write_script(str(tmp_path), _entries(), "run-1", keep=lambda sid: sid == SNAP)
    text = open(path, encoding="utf-8").read()
    data = json.loads(text[len("SCMemoryCacheMedia("):text.rindex(")")])
    assert set(data["by_snap"]) == {SNAP.upper()} and data["run"] == "run-1"


def test_how_is_read_from_older_links_too():
    assert mb.how_of({"media_path": ""}) == "pack"
    assert mb.how_of({"by_content": "cloud"}) == "cloud"
    assert mb.how_of({}) == "url"


def test_the_library_caches_report_says_how_it_linked():
    entry = {"name": "x.mp4", "rel": "x.mp4", "sha256": "cd" * 32, "bytes": 10}
    links = cmr.attribute(entry, {}, {}, {}, {"url_keys": {}}, {}, {}, {},
                          content={"cd" * 32: [{"snap_id": SNAP, "role": "full", "what": "device",
                                                "source": "SCContent", "from": "9" * 32}]})
    assert mb.how_of(links[0]) == "device"


_DOM = r"""
var NODES={};
function el(id){return NODES[id]||(NODES[id]={id:id,innerHTML:'',attrs:{},
 getAttribute:function(k){return this.attrs[k]===undefined?null:this.attrs[k];}});}
var document={getElementById:el};
var window={SCAUTO_RUN:'run-1'};
"""


@needs_node
def test_the_memory_page_lists_them():
    source = "\n".join([
        _DOM, mb.LOADER_JS, mb.script_text(_entries(), "run-1"), ml.MEMORY_JS, mb.PAGE_JS,
        f"var h=el('memcm');h.attrs={{'data-snaps':'{SNAP}'}};",
        "var n=scCacheMediaPage('../../');console.log(JSON.stringify({n:n,html:h.innerHTML}));",
        "window.SCAUTO_RUN='run-2';h.innerHTML='';",
        "console.log(JSON.stringify({n:scCacheMediaPage('../../'),html:h.innerHTML}));"])
    proc = subprocess.run([NODE, "-"], input=source, text=True, encoding="utf-8", capture_output=True)
    assert proc.returncode == 0, proc.stderr
    first, other_run = [json.loads(line) for line in proc.stdout.splitlines()]
    assert first["n"] == 3
    page = first["html"]
    assert 'href="../../CacheMedia/CacheMedia_report.html#cm-' + "1" * 64 + '"' in page
    assert "byte-identical to this Memory" in page and "keyed by the CDN URL" in page
    assert other_run == {"n": 0, "html": ""}              # another run's data file says nothing here
