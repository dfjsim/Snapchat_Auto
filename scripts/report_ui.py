"""
Shared HTML report UI: virtualized index tables + cross-report navigation.

Both big index tables (Memories and cache_controller) used to be plain HTML tables with every row
— and, for cache_controller, every row's expanded detail — in the document. That is what made large
extractions unusable: a 50 000-row index produced a multi-hundred-MB file that the browser had to
parse and lay out in one go.

This module provides the pieces both reports now share:

* :data:`VTABLE_JS` / :data:`VTABLE_CSS` — a small dependency-free **virtual table**. Only the rows
  in (and just outside) the viewport exist in the DOM; everything else is a pair of spacers. The
  row data lives in sibling ``data/*.js`` files, so the HTML document itself stays small.
* :func:`write_rows` / :func:`write_details` — write those data files. Row data is compact
  (one array per row); the heavy per-row detail HTML is split into numbered chunks that are only
  fetched when a row in that chunk is actually expanded.
* :data:`NAV_JS` — the cross-report anchor behaviour shared by *every* report, including the plain
  ones (Communications, Memories detail sub-pages): scroll an ``#anchor`` into view **below** the
  sticky toolbar, highlight it, and — crucially — keep working when the link is clicked again into
  an already-open tab (see "Re-entrant anchors" below).
* :data:`HINT_JS` / :data:`HINT_CSS` / :func:`info_icon` — the "?" popover used by all reports.
* :data:`PAGE_CSS` — the page chrome (header, toolbar, sections, key/value grids, media buttons)
  the Conversations and Contacts reports share.

Why ``data/*.js`` and not ``fetch()``/JSON: the reports are opened from ``file://``, where
``fetch``/``XMLHttpRequest`` are blocked by the browser's origin rules. A ``<script src=…>`` is a
plain subresource load and is allowed, so the data files are JS files that call back into ``SCV``.

Re-entrant anchors
------------------
Reports open each other in *named* tabs (``scauto_cache``…), so a second click on the same link
lands in the tab that is already open. If that link's URL is identical to the tab's current URL the
browser fires **no** event at all, so the target row would never be expanded/scrolled to (the
"only works the first time" bug). ``NAV_JS`` fixes this by consuming the fragment — it clears
``location.hash`` right after acting on it — so the next click is always a real hash *change*.
``history.replaceState`` is deliberately not used: it throws on ``file://`` documents.
"""

import os
import json
import html
import uuid
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- run identity

def run_id(report_dir):
    """A stable id for one set of reports, stored in ``<report_dir>/run_id.txt``.

    The examiner's row selections are saved in the browser under this id, so every report of the
    same run (Memories index, Memory detail sub-pages, cache_controller) shares one selection while
    a different case/run keeps its own. Regenerating the reports into the same folder keeps the id —
    and therefore the selections.
    """
    path = os.path.join(report_dir or ".", "run_id.txt")
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read().strip()
            if existing:
                return existing
        value = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        os.makedirs(report_dir or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
        return value
    except OSError as error:
        logger.debug(f"Could not read/write the run id in {report_dir}: {error}")
        return "default"


# --------------------------------------------------------------------------- "?" popovers

# The popover is placed with `position:fixed` while it is open, and only then. An absolutely
# positioned tip is clipped by the first ancestor that hides its overflow — which is exactly what
# happens to the "?" in a column header (`.vhdr .vc` clips so long titles can ellipsize) and inside
# a virtual row: the popover appeared cut off, or not at all. A fixed element is not clipped by an
# overflow ancestor, so the tip is measured, positioned next to its icon in viewport coordinates
# and nudged back inside the window when it would fall off the right or bottom edge.
HINT_JS = """
function scHintPlace(tip,icon){
 var r=icon.getBoundingClientRect(),pad=8;
 tip.style.position='fixed';tip.style.left='0px';tip.style.top='0px';   // measure at a known origin
 var w=tip.offsetWidth,h=tip.offsetHeight;
 var left=r.right+6,top=r.top-4;
 if(left+w>window.innerWidth-pad)left=Math.max(pad,r.left-w-6);         // flip to the icon's left
 if(top+h>window.innerHeight-pad)top=Math.max(pad,window.innerHeight-h-pad);
 tip.style.left=left+'px';tip.style.top=top+'px';}
function scHintClose(){
 document.querySelectorAll('.hint.open').forEach(function(x){
  x.classList.remove('open');
  var t=x.querySelector('.tip');
  if(t){t.style.position='';t.style.left='';t.style.top='';}});}
function hint(ev,el){ev.stopPropagation();
 var h=el.parentNode,was=h.classList.contains('open');
 scHintClose();
 if(was)return;
 h.classList.add('open');
 var tip=h.querySelector('.tip');
 if(tip)scHintPlace(tip,el);}
document.addEventListener('click',scHintClose);
// a fixed tip would stay behind while the page (or a virtual table) scrolls under it
window.addEventListener('scroll',scHintClose,true);
window.addEventListener('resize',scHintClose);
"""

# The Memories and cache_controller reports carry their own copy of this inside their big CSS
# f-strings; newer reports use this one.
HINT_CSS = """
 .hint{position:relative;display:inline-block}
 .qm{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;
   border-radius:50%;background:#c9cdf0;color:#25348a;font-size:10px;font-weight:700;cursor:pointer;
   margin:0 4px;user-select:none;vertical-align:middle}
 .qm:hover{background:#2d2d71;color:#fff}
 .tip{display:none;position:absolute;left:20px;top:-4px;z-index:9999;background:#1f1f52;color:#fff;
   padding:8px 11px;border-radius:6px;font-size:11.5px;font-weight:400;width:340px;line-height:1.45;
   box-shadow:0 3px 10px rgba(0,0,0,.35);text-transform:none;letter-spacing:normal;text-align:left;
   white-space:normal}
 .hint.open .tip{display:block}
"""


def info_icon(text):
    """A small round "?" the examiner can click for an explanation of how something was derived.

    Every association a report makes (which identifier matched, which artifact a value came from,
    whether a value is interpreted or raw) should be explainable in place — see
    ``docs/forensics_tool_guidelines.md``.
    """
    if not text:
        return ""
    return ('<span class="hint"><span class="qm" onclick="hint(event,this)">?</span>'
            f'<span class="tip">{html.escape(str(text))}</span></span>')

# --------------------------------------------------------------------------- anchor navigation

# Shared by every report. Pages with a virtual table hand the work to SCV.goTo(); plain pages fall
# back to a normal element lookup. Both scroll the target clear of the sticky toolbar/header.
NAV_JS = """
function scStick(){
 var w=document.querySelector('.stickytop');
 if(w)return w.getBoundingClientRect().height+6;
 var t=0;
 document.querySelectorAll('.toolbar,.vhdr').forEach(function(e){
  var s=getComputedStyle(e);
  if(s.position==='sticky'||s.position==='fixed')t+=e.getBoundingClientRect().height;});
 return t+6;}
function scFlash(el){
 document.querySelectorAll('.schl').forEach(function(x){x.classList.remove('schl');});
 if(el)el.classList.add('schl');}
function scGo(hash){
 if(!hash||hash.length<2)return;
 var id=decodeURIComponent(hash.slice(1));
 if(window.SCV&&SCV.hasRow(id)){SCV.goTo(id,true);return;}
 var el=document.getElementById(id);
 if(!el)return;
 var y=el.getBoundingClientRect().top+window.pageYOffset-scStick();
 window.scrollTo(0,Math.max(0,y));
 scFlash(el);}
function scConsumeHash(){
 var h=location.hash;
 if(!h||h.length<2||h==='#_')return;
 // Consume the fragment so clicking the same link again into this already-open tab still fires a
 // hashchange (browsers do nothing when the URL, fragment included, is unchanged). The sentinel
 // '_' matches no element, so — unlike an empty fragment — the browser does not scroll to the top.
 try{location.hash='_';}catch(e){}
 scGo(h);
 // once more after layout settles (fonts/images can still be arriving on the first pass)
 setTimeout(function(){scGo(h);},80);}
window.addEventListener('hashchange',scConsumeHash);
window.addEventListener('pageshow',scConsumeHash);
"""

NAV_CSS = """
 .schl{background:#fff6cc !important;box-shadow:inset 3px 0 0 #e0a800}
 [id]{scroll-margin-top:120px}
"""

# --------------------------------------------------------------------------- row selection

# Which memories / cache files matter for the case is the examiner's own working state, so it has
# to survive closing the browser and be filable with the case — but a report opened from ``file://``
# has almost nothing to store state in. Measured in Chrome (and designed for the strictest
# behaviour): ``localStorage`` on a ``file://`` page is **partitioned per browsing context** — a
# second tab on the *same* file starts empty, a sub-page in the same folder starts empty, and an
# iframe bridge between two file:// documents is partitioned too. It only survives a reload of the
# same tab.
#
# So the durable store here is a **file the examiner saves**: ``Reports/selection.js``. Every page
# of the run loads it at startup (that is how the index and a detail sub-page agree on what is
# selected), changes are held in memory, and "Save selections" downloads the file back so it can be
# dropped next to the reports and filed with the case. ``localStorage`` is kept purely as a
# same-tab safety net so an accidental reload does not lose work.
SELECT_JS = """
var SCSel=(function(){
 var KEY='scauto-sel:'+(window.SCAUTO_RUN||'default');
 var data={},subs=[],dirty=false,loadedStamp='';
 function bag(kind){if(!data[kind])data[kind]={};return data[kind];}
 function notify(){subs.forEach(function(f){try{f();}catch(e){}});}
 function stash(){                                  // same-tab safety net only (see above)
  try{window.localStorage.setItem(KEY,JSON.stringify(
   {saved:new Date().toISOString(),dirty:dirty,selections:data}));}catch(e){}}
 function touched(){dirty=true;stash();notify();}
 function apply(o){
  var sel=(o&&o.selections)||{};
  data={};
  for(var k in sel){data[k]={};for(var id in sel[k])data[k][id]=1;}
  loadedStamp=(o&&o.exported)||'';
  dirty=false;}
 // Reports/selection.js calls this before the page initialises; it is the durable state.
 function preload(o){apply(o);restash();notify();}
 function restash(){
  // an in-tab copy newer than the saved file wins: that is the accidental-reload case
  try{
   var raw=window.localStorage.getItem(KEY);
   if(!raw)return;
   var l=JSON.parse(raw);
   if(l&&l.saved&&(!loadedStamp||l.saved>loadedStamp)&&l.selections){
    data=l.selections;dirty=!!l.dirty;}
  }catch(e){}}
 function get(kind,id){return !!bag(kind)[id];}
 function set(kind,id,on){
  if(on)bag(kind)[id]=1;else delete bag(kind)[id];
  touched();}
 function setMany(kind,ids,on){
  var b=bag(kind);
  ids.forEach(function(id){if(on)b[id]=1;else delete b[id];});
  touched();}
 function ids(kind){return Object.keys(bag(kind));}
 function count(kind){return ids(kind).length;}
 function total(){var n=0;for(var k in data)n+=Object.keys(data[k]).length;return n;}
 function clear(kind){data[kind]={};touched();}
 function isDirty(){return dirty;}
 function payload(){
  return {tool:'Snapchat_Auto',run_id:(window.SCAUTO_RUN||'default'),
          exported:new Date().toISOString(),selections:data};}
 function saveFile(){
  var body="/* Snapchat Auto \\u2014 examiner selections for run "+(window.SCAUTO_RUN||'default')+
   ".\\n   Keep this file as <report folder>/selection.js so every report of this run loads it.\\n"+
   "   It is also a plain record you can file with the case. */\\n"+
   "SCSel.preload("+JSON.stringify(payload(),null,1)+");\\n";
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([body],{type:'application/javascript'}));
  a.download='selection.js';
  document.body.appendChild(a);a.click();
  setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},0);
  dirty=false;stash();notify();}
 function loadFile(file,done){
  var r=new FileReader();
  r.onload=function(){
   var text=String(r.result||''),start=text.indexOf('{'),end=text.lastIndexOf('}');
   try{
    var o=JSON.parse(start>=0?text.slice(start,end+1):text);
    // an explicit load replaces what is here — unlike startup, the same-tab backup must not win
    apply(o.selections?o:{selections:o});
    stash();notify();
   }catch(e){done(null,'That file is not a Snapchat Auto selection file.');return;}
   done(total(),null);};
  r.readAsText(file);}
 restash();
 window.addEventListener('beforeunload',function(ev){
  if(!dirty)return;
  ev.preventDefault();ev.returnValue='';});
 // one delegated handler for every checkbox in the document, virtual rows included
 document.addEventListener('change',function(ev){
  var el=ev.target;
  if(!el||!el.classList||!el.classList.contains('selbox'))return;
  set(el.getAttribute('data-kind'),el.getAttribute('data-id'),el.checked);});
 return {get:get,set:set,setMany:setMany,ids:ids,count:count,total:total,clear:clear,
         preload:preload,onChange:function(f){subs.push(f);},saveFile:saveFile,loadFile:loadFile,
         dirty:isDirty};
})();
// reflect the stored state onto plain (non-virtual) checkboxes, e.g. on a detail sub-page
function scSyncBoxes(){
 document.querySelectorAll('input.selbox[data-id]').forEach(function(b){
  if(b.closest('.vr'))return;                       // virtual rows are re-rendered from the store
  b.checked=SCSel.get(b.getAttribute('data-kind'),b.getAttribute('data-id'));});}
"""

SELECT_CSS = """
 input.selbox{width:15px;height:15px;cursor:pointer;accent-color:#2d2d71;margin:0}
 .selbar{display:inline-flex;align-items:center;gap:7px;flex-wrap:wrap}
 .selbar #selcount{color:#2d2d71;font-weight:700}
 .selbar button,.selbar .filebtnlike{font-size:12.5px;padding:5px 9px;border:1px solid #bcbcd0;
   border-radius:5px;background:#fff;cursor:pointer;font-weight:600;color:#2d2d71}
 .selbar button:hover,.selbar .filebtnlike:hover{background:#e7e7f4}
 .sellabel{display:inline-flex;align-items:center;gap:7px;background:#eef0ff;border:1px solid #c9cdf0;
   border-radius:6px;padding:5px 10px;font-size:13px;font-weight:600;color:#2d2d71;cursor:pointer}
 .sellabel:has(input:checked){background:#2d2d71;color:#fff;border-color:#2d2d71}
 .selnote{color:#8a5a00;font-size:11.5px}
 .selrow{display:inline-flex;align-items:center;gap:8px;background:#eef0ff;border:1px solid #c9cdf0;
   border-radius:6px;padding:4px 10px;font-size:12.5px;font-weight:600;color:#2d2d71;cursor:pointer}
 .selrow:has(input:checked){background:#2d2d71;color:#fff;border-color:#2d2d71}
"""

# Toolbar glue shared by both index reports. `flt()` (defined by each report) is called when the
# "selected only" filter is toggled.
SELECT_TOOLBAR_JS = """
function scSelClear(){
 var n=SCSel.count(window.SCAUTO_SELKIND);
 if(!n){alert('Nothing is selected here.');return;}
 if(confirm('Clear all '+n+' selection(s) in this report?'))SCSel.clear(window.SCAUTO_SELKIND);}
function scSelSave(){
 SCSel.saveFile();
 alert('selection.js was downloaded.\\n\\nPut it next to the reports (replace '+
  '<report folder>\\\\selection.js) and every report of this run will load your selections the '+
  'next time it is opened. Keep a copy with the case file.');}
function scSelLoad(input){
 var f=input.files&&input.files[0];
 if(!f)return;
 if(SCSel.dirty()&&!confirm('Loading a selection file replaces what is selected here, '+
   'and you have unsaved changes. Continue?')){input.value='';return;}
 SCSel.loadFile(f,function(total,err){
  input.value='';
  alert(err?err:(total+' selection(s) loaded.'));});}
function scSelNote(){
 var e=document.getElementById('selnote');
 if(!e)return;
 e.textContent=SCSel.dirty()?'unsaved \\u2014 use "Save selections"':'';}
"""


def selection_toolbar(noun):
    """The selection controls both index reports put in their toolbar."""
    return (
        '<span class="selbar">'
        '<label class="sellabel" title="Show only the rows you have selected">'
        '<input type="checkbox" id="selonly" onchange="flt()">Selected only</label>'
        '<span id="selcount">0 selected</span>'
        f'<button onclick="SCV.selectShown(true)" title="Select every {noun} matching the current '
        'filters (not only the ones on this page)">Select all shown</button>'
        f'<button onclick="SCV.selectShown(false)" title="Unselect every {noun} matching the '
        'current filters">Unselect shown</button>'
        '<button onclick="scSelClear()" title="Clear the whole selection">Clear</button>'
        '<button onclick="scSelSave()" title="Download selection.js — put it next to the reports '
        'to have every report of this run load your selections, and keep a copy with the case">'
        '💾 Save selections</button>'
        '<label class="filebtnlike" title="Load a selection.js saved earlier">'
        'Load…<input type="file" id="selfile" accept=".js,.json,application/json" hidden '
        'onchange="scSelLoad(this)"></label>'
        '<span class="selnote" id="selnote"></span></span>')


SELECTION_STUB = """/* Snapchat Auto — examiner selections for this run.

   Every report of this run loads this file at startup, which is how the Memories index, the Memory
   detail sub-pages and the cache_controller report agree on what you have selected. It starts
   empty: tick rows in a report, press "Save selections", and replace this file with the
   selection.js your browser downloads. It is a plain text record you can file with the case.

   (A report opened from file:// cannot write to disk, and browsers give each file:// page its own
   private, tab-scoped storage — so saving this file is what makes a selection last.) */
SCSel.preload({"tool": "Snapchat_Auto", "run_id": %s, "exported": "", "selections": {}});
"""


def write_selection_stub(report_dir, run_id_value):
    """Create ``<report_dir>/selection.js`` if it is not there yet.

    Never overwritten: once the examiner saves their selections over it, regenerating the reports
    must not wipe their work.
    """
    path = os.path.join(report_dir or ".", "selection.js")
    if os.path.isfile(path):
        return path
    try:
        os.makedirs(report_dir or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(SELECTION_STUB % json.dumps(run_id_value))
    except OSError as error:
        logger.debug(f"Could not write the selection stub in {report_dir}: {error}")
    return path

# --------------------------------------------------------------------------- page chrome

# The page furniture every report repeats: header band, toolbar, section headings, key/value grids,
# sub-tables, media buttons and chips. The Memories and cache_controller reports predate this and
# keep their own (identical-looking) copies inside their own CSS; the Conversations and Contacts
# reports use this one so the two new reports cannot drift apart.
PAGE_CSS = """
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f4f8;color:#1b1b1f}
 header{background:#2d2d71;color:#fff;padding:16px 24px} header h1{margin:0;font-size:20px}
 header a{color:#cfd3ff} .sum{opacity:.85;font-size:13px;margin-top:4px} .sum b{color:#fff}
 .note{background:#fff8e0;border:1px solid #e6d48a;color:#6a5300;padding:8px 24px;font-size:12.5px}
 .warn{background:#ffe8e8;border:1px solid #e0a0a0;color:#7a1f1f;padding:10px 24px;font-size:13px}
 .toolbar{background:#ececf4;border-bottom:1px solid #d7d7e2;padding:10px 24px;
   display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:13px}
 .toolbar input,.toolbar select{font-size:13px;padding:5px 8px;border:1px solid #bcbcd0;
   border-radius:5px}
 .toolbar input[type=search]{min-width:280px} .toolbar label{color:#555;font-weight:600}
 .toolbar button{font-size:13px;padding:5px 10px;border:1px solid #bcbcd0;border-radius:5px;
   background:#fff;cursor:pointer;font-weight:600;color:#2d2d71}
 .toolbar button:hover{background:#e7e7f4}
 a.back{display:inline-block;margin:14px 24px 0;color:#2d2d71;font-weight:600;text-decoration:none;
   font-size:13px} a.back:hover{text-decoration:underline}
 .mono{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}
 .muted{color:#999} .more{background:#d7d7ee;color:#33367a;border-radius:8px;padding:0 6px;
   font-size:10px}
 .sect{margin-top:12px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#2d2d71;
   font-weight:700;border-bottom:1px solid #e2e2ee;padding-bottom:2px}
 .grid{display:grid;grid-template-columns:auto 1fr;gap:2px 14px;font-size:12.5px;margin-top:4px;
   max-width:1000px}
 .grid .k{color:#666} .grid .v{overflow-wrap:anywhere}
 .grid .v.hex{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#7a1f5a}
 .grid .v.mono{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:#33367a}
 table.sub{border-collapse:collapse;margin-top:5px;font-size:11.5px}
 table.sub th{background:#e7e7f2;color:#2d2d71;text-align:left;padding:3px 8px}
 table.sub td{border:1px solid #e0e0e8;padding:3px 8px;overflow-wrap:anywhere;vertical-align:middle}
 .filebtn{display:inline-flex;align-items:center;gap:5px;text-decoration:none;font-weight:700;
   font-size:11px;color:#25348a;background:#e7ecff;border:1px solid #b9c3f0;border-radius:6px;
   padding:2px 7px;max-width:100%}
 .filebtn:hover{background:#d5deff}
 .filebtn img{max-width:96px;max-height:52px;object-fit:cover;border-radius:4px;display:block}
 .filebtn.img{padding:2px;gap:4px} .filebtn.img .lbl{padding-right:5px;text-transform:uppercase}
 .filebtn.play{padding:5px 9px;font-size:12px}
 .filenone{color:#999;font-size:11px}
 .chips{margin-top:4px}
 .chip{display:inline-block;margin:2px 6px 2px 0;padding:2px 8px;border-radius:10px;font-size:11px;
   text-decoration:none;font-weight:600}
 .chip.cache{background:#e7ecff;color:#25348a;border:1px solid #b9c3f0}
 .chip.ok{background:#eef7ee;color:#2f7d32} .chip.miss{background:#f6efef;color:#9a5a5a}
 .chip.warn{background:#fff3d6;color:#8a5a00;border:1px solid #e6c983}
 a.detail{color:#2d2d71;font-weight:600;text-decoration:none;white-space:nowrap}
 a.detail:hover{text-decoration:underline}
"""

# --------------------------------------------------------------------------- virtual table

VTABLE_CSS = """
 .stickytop{position:sticky;top:0;z-index:6}
 .vwrap{position:relative}
 .vhdr{display:grid;background:#1f1f52;color:#fff;font-size:12px;font-weight:600}
 .vhdr .vc{padding:7px 10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
   cursor:pointer;user-select:none}
 .vhdr .vc.nosort{cursor:default} .vhdr .ar{opacity:.45;font-size:10px;margin-left:3px}
 .vhdr .vc.sorted .ar{opacity:1}
 .vpad{width:100%} .vwin{position:absolute;left:0;right:0;top:0}
 .vr{border-bottom:1px solid #e2e2ea;background:#fff;box-sizing:border-box;overflow:hidden}
 .vr:hover{background:#eef0ff} .vr.open{background:#fafaff;overflow:visible}
 .vcells{display:grid;align-items:start;box-sizing:border-box}
 .vcells>.vc{padding:6px 10px;overflow:hidden;box-sizing:border-box;min-width:0}
 .vr.clickable{cursor:pointer}
 .vdet{padding:2px 16px 16px 34px;background:#fafaff;border-top:1px dashed #dcdce8;cursor:default}
 .vcells>.vc.sel{display:flex;align-items:center;justify-content:center;padding:0}
 .vr:has(input.selbox:checked){background:#eff2ff;box-shadow:inset 3px 0 0 #2d2d71}
 .vhdr .vc.sel{display:flex;align-items:center;justify-content:center;padding:0;cursor:default}
 .pager{background:#f4f4fa;border-bottom:1px solid #d7d7e2;padding:6px 24px;font-size:12.5px;
   color:#555;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 .pager select{font-size:12.5px;padding:3px 6px;border:1px solid #bcbcd0;border-radius:5px}
 .pager label{font-weight:600;color:#555}
 .pager .pgnav{display:inline-flex;gap:4px;align-items:center}
 .pager button{font-size:13px;line-height:1;min-width:26px;padding:4px 7px;border:1px solid #bcbcd0;
   border-radius:5px;background:#fff;color:#2d2d71;font-weight:700;cursor:pointer}
 .pager button:hover:not(:disabled){background:#e7e7f4}
 .pager button:disabled{color:#bbb;cursor:default}
 .pager .pgrange{color:#777}
 .vempty{padding:26px 24px;color:#777;font-size:13px}
 .vwait{padding:26px 24px;color:#777;font-size:13px}
 .vmiss{background:#ffe8e8;border:1px solid #e0a0a0;color:#7a1f1f;padding:10px 24px;font-size:13px}
 td.tog,.vc.tog{color:#2d2d71;font-weight:700;text-align:center;padding-left:4px;padding-right:4px}
 .vr.open .vc.tog{color:#8a1f5a}
"""

# The engine. Row payload (from data/index.js):
#   [id, [cell html, …], search text (lower-case), {sortable col -> key}, detail chunk, {meta}]
VTABLE_JS = r"""
var SCV=(function(){
"use strict";
var C=null,rows=[],byId={},view=[],vpos={},slice=[],pos={},cum=null,exp={},expH={},det={},
    chunkState={},mount,win,pad,pager,hlId=null,lastA=-1,lastB=-1,dirty=true,
    sortCol=-1,sortDir=1,scheduled=false,measuring=0,pageSize=0,page=0,pagerSig='';

function setRows(r){rows=r;byId={};for(var i=0;i<rows.length;i++)byId[rows[i][0]]=i;
 if(C)refilter();}

function init(o){
 C=o;mount=document.getElementById(o.mount);win=document.getElementById(o.win);
 pad=document.getElementById(o.pad);
 pager=o.pager?document.getElementById(o.pager):null;
 pageSize=o.pageSize||0;
 mount.addEventListener('click',onClick);
 window.addEventListener('scroll',schedule,{passive:true});
 window.addEventListener('resize',function(){dirty=true;schedule();});
 if(!rows.length){var m=document.getElementById(o.missing);if(m)m.style.display='block';}
 if(o.sort!==undefined&&o.sort>=0){sortCol=o.sort;sortDir=o.sortDir||1;}
 if(o.selKind&&window.SCSel)SCSel.onChange(function(){
  if(C.selectedOnly&&C.selectedOnly())refilter();else{dirty=true;render();}
  if(C.selCount)C.selCount(SCSel.count(o.selKind));});
 refilter();
 sync();
 if(C.selCount&&window.SCSel)C.selCount(SCSel.count(o.selKind));}

/* ---------- filtering / sorting ---------- */
function refilter(){
 if(!C)return;
 var q=(C.query?C.query():'').toLowerCase(),m=C.match||null;
 view=[];
 for(var i=0;i<rows.length;i++){
  var r=rows[i];
  if(q&&r[2].indexOf(q)<0)continue;
  if(m&&!m(r[5]||{},r))continue;
  view.push(i);}
 if(sortCol>=0)sortView();
 rebuild();}

function sortView(){
 var c=String(sortCol),d=sortDir;
 view.sort(function(a,b){
  var va=rows[a][3][c],vb=rows[b][3][c];
  if(va===undefined||va===null)va='';
  if(vb===undefined||vb===null)vb='';
  if(typeof va==='number'&&typeof vb==='number')return (va-vb)*d;
  return String(va).localeCompare(String(vb))*d;});}

function setSort(col){
 sortDir=(sortCol===col)?-sortDir:1;sortCol=col;
 if(sortCol>=0)sortView();
 rebuild();sync();}

function sync(){
 var hs=document.querySelectorAll(C.header+' .vc'),off=C.selKind?1:0;   // the checkbox column
 for(var i=0;i<hs.length;i++){
  hs[i].classList.toggle('sorted',i-off===sortCol);
  var ar=hs[i].querySelector('.ar');
  if(ar)ar.textContent=(i-off===sortCol)?(sortDir>0?'▲':'▼'):'↕';}}

/* ---------- paging ---------- */
function pageCount(){return pageSize>0?Math.max(1,Math.ceil(view.length/pageSize)):1;}

function setPageSize(n){pageSize=+n||0;page=0;rebuild();}

function setPage(n){
 var last=pageCount()-1;
 page=Math.max(0,Math.min(last,+n||0));
 rebuild();
 window.scrollTo(0,Math.max(0,mount.getBoundingClientRect().top+window.pageYOffset-
   (window.scStick?scStick():60)));}

function renderPager(){
 if(!pager)return;
 var n=view.length,pages=pageCount(),from=n?(pageSize>0?page*pageSize:0)+1:0,
     to=pageSize>0?Math.min(n,(page+1)*pageSize):n;
 var sig=[n,pages,page,pageSize].join('|');
 if(sig===pagerSig)return;                          // avoid rebuilding it on every expand/measure
 pagerSig=sig;
 var opts='';
 for(var i=0;i<pages;i++)opts+='<option value="'+i+'"'+(i===page?' selected':'')+'>'+(i+1)+'</option>';
 var dis=pages<2?' disabled':'';
 var sizes=[100,250,500,1000,5000];
 if(pageSize>0&&sizes.indexOf(pageSize)<0)sizes.push(pageSize);   // e.g. set from the console
 sizes.sort(function(a,b){return a-b;});
 pager.innerHTML=
  '<label>Rows per page <select onchange="SCV.setPageSize(this.value)">'+
  sizes.map(function(s){
    return '<option value="'+s+'"'+(pageSize===s?' selected':'')+'>'+s+'</option>';}).join('')+
  '<option value="0"'+(pageSize?'':' selected')+'>all</option></select></label>'+
  '<span class="pgnav"><button onclick="SCV.setPage(0)"'+dis+' title="first page">&laquo;</button>'+
  '<button onclick="SCV.setPage('+(page-1)+')"'+(page?'':' disabled')+' title="previous page">&lsaquo;</button>'+
  '<label>Page <select onchange="SCV.setPage(this.value)"'+dis+'>'+opts+'</select> of '+pages+'</label>'+
  '<button onclick="SCV.setPage('+(page+1)+')"'+(page<pages-1?'':' disabled')+' title="next page">&rsaquo;</button>'+
  '<button onclick="SCV.setPage('+(pages-1)+')"'+dis+' title="last page">&raquo;</button></span>'+
  '<span class="pgrange">'+(n?('showing '+from+'&ndash;'+to+' of '+n):'nothing to show')+'</span>';}

/* ---------- geometry ---------- */
function rowH(i){var id=rows[i][0];return C.rowHeight+(exp[id]?(expH[id]||C.estDetail||260):0);}

function rebuild(){
 vpos={};
 for(var v=0;v<view.length;v++)vpos[view[v]]=v;
 var pages=pageCount();
 if(page>=pages)page=pages-1;
 if(page<0)page=0;
 slice=pageSize>0?view.slice(page*pageSize,(page+1)*pageSize):view;
 pos={};
 var n=slice.length;
 cum=new Float64Array(n+1);
 for(var k=0;k<n;k++){pos[slice[k]]=k;cum[k+1]=cum[k]+rowH(slice[k]);}
 pad.style.height=cum[n]+'px';
 renderPager();
 if(C.count)C.count(view.length,rows.length);
 var e=document.getElementById(C.empty);
 if(e)e.style.display=view.length?'none':'block';
 dirty=true;render();}

function find(y){var lo=0,hi=cum.length-1;
 while(lo<hi){var mid=(lo+hi+1)>>1;if(cum[mid]<=y)lo=mid;else hi=mid-1;}
 return lo;}

function schedule(){if(scheduled)return;scheduled=true;
 window.requestAnimationFrame(function(){scheduled=false;render();});}

function render(){
 if(!C||!cum)return;
 var n=slice.length;
 if(!n){win.innerHTML='';win.style.top='0px';lastA=lastB=-1;return;}
 var top=mount.getBoundingClientRect().top+window.pageYOffset;
 var y=window.pageYOffset-top,vh=window.innerHeight;
 var a=find(Math.max(0,y-600)),b=find(y+vh+600)+1;
 if(b>n)b=n;
 if(!dirty&&a===lastA&&b===lastB)return;
 var h=[];
 for(var k=a;k<b;k++)h.push(rowHtml(slice[k]));
 win.style.top=cum[a]+'px';
 win.innerHTML=h.join('');
 lastA=a;lastB=b;dirty=false;
 measure(a,b);}

/* An expanded row's real height is only known once it is in the DOM: measure it, remember it and
   rebuild the offsets when it differs from what we assumed. */
function measure(a,b){
 if(measuring>3)return;
 var changed=false,kids=win.children;
 for(var j=0;j<kids.length;j++){
  var el=kids[j],i=+el.getAttribute('data-i'),id=rows[i][0];
  if(!exp[id])continue;
  var h=el.offsetHeight-C.rowHeight;
  if(h>0&&Math.abs((expH[id]||0)-h)>1){expH[id]=h;changed=true;}}
 if(changed){measuring++;rebuild();measuring--;}}

/* ---------- rows ---------- */
function rowHtml(i){
 var r=rows[i],id=r[0],op=!!exp[id],cells=r[1],s='';
 /* optional per-row class from the row's own filter metadata, e.g. marking outgoing messages */
 var extra=C.rowClass?(' '+C.rowClass(r[5]||{},r)):'';
 s='<div class="vr'+(op?' open':'')+(C.detailBase?' clickable':'')+extra+
   (hlId===id?' schl':'')+'" id="'+id+'" data-i="'+i+'" style="height:'+
   (op?'auto':C.rowHeight+'px')+'"><div class="vcells" style="height:'+C.rowHeight+
   'px;grid-template-columns:'+(C.selKind?'30px ':'')+C.cols+'">';
 if(C.selKind)s+='<div class="vc sel"><input type="checkbox" class="selbox" data-kind="'+
   C.selKind+'" data-id="'+id+'"'+(SCSel.get(C.selKind,id)?' checked':'')+
   ' title="mark this row as relevant (saved in this browser; use Export to keep it)"></div>';
 for(var c=0;c<cells.length;c++)s+='<div class="vc c'+c+'">'+cells[c]+'</div>';
 s+='</div>';
 if(op)s+='<div class="vdet">'+(det[id]!==undefined?det[id]:
   '<div class="vwait">loading detail…</div>')+'</div>';
 return s+'</div>';}

function onClick(ev){
 if(!C.detailBase)return;
 var t=ev.target;
 if(t.closest('a')||t.closest('.qm')||t.closest('.vdet')||t.closest('input,select,button'))return;
 var el=t.closest('.vr');
 if(!el)return;
 toggle(+el.getAttribute('data-i'));}

function toggle(i){var id=rows[i][0];if(exp[id])close(i);else open(i);}

function open(i){
 var id=rows[i][0];
 if(exp[id])return;
 exp[id]=1;dirty=true;
 loadDetail(i,function(){dirty=true;rebuild();});
 rebuild();}

function close(i){var id=rows[i][0];delete exp[id];dirty=true;rebuild();}

/* ---------- lazy detail chunks ---------- */
function loadDetail(i,cb){
 var r=rows[i],id=r[0],n=r[4];
 if(!C.detailBase||det[id]!==undefined||n===null||n===undefined){cb();return;}
 var st=chunkState[n];
 if(st==='done'){cb();return;}
 if(st&&st.push){st.push(cb);return;}
 chunkState[n]=[cb];
 var sc=document.createElement('script');
 sc.src=C.detailBase+n+'.js';
 sc.onerror=function(){var q=chunkState[n];chunkState[n]='done';
  det[id]='<div class="vmiss">Detail data file missing: '+sc.src+
   ' — keep the report\'s data folder next to the HTML file.</div>';
  if(q&&q.push)q.forEach(function(f){f();});};
 document.head.appendChild(sc);}

function detail(n,obj){
 for(var k in obj)det[k]=obj[k];
 var q=chunkState[n];chunkState[n]='done';
 if(q&&q.push)q.forEach(function(f){f();});
 dirty=true;render();}

/* ---------- expand / collapse all (the current page) ---------- */
function expandAll(on,limit){
 if(on&&slice.length>(limit||500))return false;
 if(!on){exp={};expH={};dirty=true;rebuild();return true;}
 var need={};
 slice.forEach(function(i){var r=rows[i];exp[r[0]]=1;
  if(C.detailBase&&det[r[0]]===undefined&&r[4]!==null)need[r[4]]=1;});
 dirty=true;rebuild();
 Object.keys(need).forEach(function(n){
  var any=slice.find(function(i){return rows[i][4]==n;});
  if(any!==undefined)loadDetail(any,function(){dirty=true;rebuild();});});
 return true;}

/* ---------- selection over the whole filtered set ---------- */
function selectShown(on){
 if(!C.selKind)return 0;
 SCSel.setMany(C.selKind,view.map(function(i){return rows[i][0];}),on);
 return view.length;}

/* ---------- anchor navigation ---------- */
function hasRow(id){return byId[id]!==undefined;}

function goTo(id,expand){
 var i=byId[id];
 if(i===undefined)return false;
 if(vpos[i]===undefined){if(C.reset)C.reset();refilter();}   // clear filters hiding the target
 if(vpos[i]===undefined)return false;
 if(pageSize>0){                                             // and turn to the page holding it
  var p=Math.floor(vpos[i]/pageSize);
  if(p!==page){page=p;rebuild();}}
 if(pos[i]===undefined)return false;
 hlId=id;
 if(expand&&C.detailBase&&!exp[rows[i][0]])open(i);
 else{dirty=true;render();}
 scrollTo(i);
 return true;}

function scrollTo(i){
 var top=mount.getBoundingClientRect().top+window.pageYOffset;
 var y=top+cum[pos[i]]-(window.scStick?scStick():60);
 window.scrollTo(0,Math.max(0,y));
 dirty=true;render();}

return {init:init,setRows:setRows,detail:detail,refilter:refilter,setSort:setSort,
        expandAll:expandAll,goTo:goTo,hasRow:hasRow,selectShown:selectShown,
        setPage:setPage,setPageSize:setPageSize,page:function(){return page;},
        pages:pageCount,count:function(){return view.length;}};
})();
"""


# --------------------------------------------------------------------------- data files

def _write_js(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def write_rows(data_dir, rows):
    """Write ``<data_dir>/index.js`` — the compact row payload the virtual table renders from.

    ``rows`` is a list of ``[anchor id, [cell html…], search text, {col: sort key}, chunk, {meta}]``.
    """
    os.makedirs(data_dir, exist_ok=True)
    payload = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    _write_js(os.path.join(data_dir, "index.js"), "SCV.setRows(" + payload + ");\n")


def write_details(data_dir, details, chunk_size=250):
    """Write the per-row detail HTML as ``detail-<n>.js`` chunks; return ``{row id: chunk}``.

    ``details`` is an ordered list of ``(anchor id, html)``. Only the chunk holding a row the
    examiner actually expands is ever loaded by the browser, which is what keeps a 100 000-entry
    report openable.
    """
    os.makedirs(data_dir, exist_ok=True)
    chunk_of = {}
    for start in range(0, len(details), chunk_size):
        n = start // chunk_size
        block = details[start:start + chunk_size]
        obj = {}
        for rid, html_text in block:
            obj[rid] = html_text
            chunk_of[rid] = n
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        _write_js(os.path.join(data_dir, f"detail-{n}.js"),
                  "SCV.detail(" + str(n) + "," + payload + ");\n")
    return chunk_of


def missing_data_banner(kind):
    """The banner shown when ``data/index.js`` could not be loaded (report moved without its data)."""
    return (f'<div class="vmiss" id="vmiss" style="display:none">This report\'s row data '
            f'(<code>data/index.js</code>) could not be loaded. Keep the <code>data</code> folder '
            f'next to {kind} when copying the report.</div>')
