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
* :func:`xref` / :func:`narrow` / :data:`PARTIAL_CSS` — every cross-report link goes through
  ``xref``, which is what lets a **partial** report mark a link whose target it does not contain
  instead of leaving one that goes nowhere. With no closure it returns the caller's markup unchanged,
  so a full report is unaffected.
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
import re
import sys
import json
import html
import uuid
import shutil
import logging
import calendar
from datetime import datetime
from urllib.parse import quote

from scripts import app_version
from scripts import selection_file
from scripts import source_fingerprint

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- bundled assets

def copy_css(dest_dir):
    """Copy the bundled Bootstrap ``css`` folder next to a legacy report. True when it is there.

    The two legacy reports each used to do this inline with a bare ``except:`` that logged
    "Could not copy the CSS folder, result might look a bit worse". Because run folders are meant
    to be reused (``--run-name``), ``copytree`` hit ``FileExistsError`` on **every** re-run and the
    reports were reported as degraded while the CSS was in fact already in place. ``dirs_exist_ok``
    makes the copy idempotent, and only a real ``OSError`` is now reported — with the reason.
    """
    if getattr(sys, "frozen", False):
        source = os.path.join(sys._MEIPASS, "css")
    else:
        source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "css")
    target = os.path.join(dest_dir, "css")
    try:
        shutil.copytree(source, target, dirs_exist_ok=True)
        return True
    except OSError as error:
        logger.warning(f"Could not copy the CSS folder from {source} to {target} ({error}) — the "
                       "report is complete but will be unstyled")
        return False


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


def find_fragment(tokens):
    """The ``#find=…`` fragment for a link whose target is a **set** of rows, not one row.

    The receiving report filters itself to these tokens (matched with OR) and expands every row that
    matches — see ``findAll`` in :data:`VTABLE_JS`. Use it instead of an ``#anchor`` when one entry
    corresponds to several rows in the other report: an anchor can only reach the first of them, and
    one chip per row makes the cell unreadable.

    Each token must be something the target rows carry in their search text (a CACHE_KEY, a snap id,
    a hash). Returns "" when nothing usable was passed, so the caller falls back to a plain link.
    """
    seen, parts = set(), []
    for token in tokens or ():
        text = str(token or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            parts.append(quote(text, safe=""))
    return "#find=" + "|".join(parts) if parts else ""


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


# --------------------------------------------------------------------------- links out of a partial

#: What a link to an excluded row says. The plan wrote this marker as U+20E0 (COMBINING ENCLOSING
#: CIRCLE BACKSLASH), which is a combining mark and renders unpredictably with no base character, so
#: the reports use U+2298 (CIRCLED DIVISION SLASH) instead — same reading, one standalone glyph.
XOUT_MARK = "&#8856;"
XOUT_LABEL = "not in this partial report"

_ANCHOR_INNER = re.compile(r"^<a\b[^>]*>(.*)</a>$", re.S)
_ANY_TAG = re.compile(r"<[^>]+>")


def xref(link_html, targets, *, closure=None, label=None, brief=False, hint=""):
    """One cross-report link, either live or marked absent — the single place that decision is made.

    A link between two reports is only meaningful when both ends are in the folder. In a partial
    report they often are not, and the two dishonest options are a link that goes nowhere and a link
    that was silently deleted: the first misleads, the second hides that an association exists at all.
    So an excluded target keeps **its label and its identifier as visible text** and gains the
    :data:`XOUT_LABEL` marker. The examiner can still see *what* is not here, and
    ``partial_manifest.json`` lists every one.

    ``link_html``  the anchor the report would emit anyway, built by the caller. This helper *wraps*,
                   it does not build: every call site has its own classes, target window, emoji and
                   attribute order, and rebuilding all of them here would rewrite the markup of every
                   full report for no gain. With ``closure=None`` the argument is returned untouched,
                   so a full run is byte-identical by construction rather than by inspection.
    ``targets``    the ``(kind, row id)`` pairs this link reaches. Live when **any** of them is
                   included; a link to a set of rows should be narrowed with :func:`narrow` first, so
                   its label states how many it really reaches.
    ``label``      overrides the visible text; by default the anchor's own inner HTML is kept.
    ``brief``      for a fixed-height index cell: the marker alone, with the sentence in the tooltip.
    """
    if closure is None:
        return link_html
    targets = [(kind, row_id) for kind, row_id in targets if kind and row_id]
    if not targets or any(closure.has(kind, row_id) for kind, row_id in targets):
        return link_html
    if label is None:
        match = _ANCHOR_INNER.match(link_html.strip())
        # not an anchor we recognise: strip the markup rather than emit it, so a live <a> can never
        # survive inside the marker
        label = match.group(1) if match else _ANY_TAG.sub("", link_html)
    for kind, row_id in targets:
        closure.note_excluded(kind, row_id)
    reaches = ", ".join(dict.fromkeys(row_id for _kind, row_id in targets))
    title = hint or f"{XOUT_LABEL}. In the full report this link reaches {reaches}."
    text = XOUT_MARK if brief else f"{XOUT_MARK} {XOUT_LABEL}"
    return (f'<span class="xout" title="{html.escape(title)}">{label}'
            f'<span class="xno">{text}</span></span>')


def narrow(closure, kind, values, anchor):
    """The subset of *values* whose row is in this partial report, and how many were dropped.

    For a link whose target is a **set** of rows (a ``#find=`` fragment, see :func:`find_fragment`):
    the fragment is narrowed to the rows that are actually there, so the receiving report does not
    open filtered to nothing, and the caller can state the true count in its own label.
    """
    if closure is None:
        return list(values), 0
    kept = [value for value in values if closure.has(kind, anchor(value))]
    return kept, len(values) - len(kept)


# The partial report's own furniture: the banner, the "N of M" figures, the excluded-link marker, the
# grouped-sibling note and the provenance block. Rendered by `partial_report`; styled here so all five
# reports look the same, and harmless in a full report, which emits none of it.
PARTIAL_CSS = """
 .pbanner{background:#fff3cd;border-top:3px solid #b8860b;border-bottom:1px solid #e0c060;
   color:#5c4400;padding:10px 24px;font-size:13px;line-height:1.5}
 .pbanner b{color:#3d2d00} .pbanner .pttl{font-size:14px;letter-spacing:.3px}
 .pbanner .pmis{color:#7a1f1f} .pbanner ul{margin:6px 0 0 18px;padding:0}
 .pfig{opacity:.85;font-size:12px}
 .xout{color:#7a6000;background:#fff8e0;border:1px dashed #d0b060;border-radius:4px;
   padding:1px 5px;font-size:11.5px;white-space:nowrap;display:inline-block;max-width:100%;
   overflow:hidden;text-overflow:ellipsis;vertical-align:middle}
 .xout .xno{margin-left:5px;opacity:.8;font-weight:600}
 .psib{background:#eef0ff;border:1px solid #c4c8ee;color:#2d2d71;border-radius:4px;
   padding:1px 5px;font-size:11.5px;margin-left:6px}
 .prov{margin:0;background:#fff;border-bottom:1px solid #dcdce8;font-size:12.5px}
 .prov>summary{cursor:pointer;padding:8px 24px;font-weight:600;color:#2d2d71}
 .prov .provbody{padding:2px 24px 14px}
 .prov table{border-collapse:collapse;margin:6px 0;font-size:12px}
 .prov th,.prov td{border:1px solid #dcdce8;padding:3px 7px;text-align:left;vertical-align:top}
 .prov th{background:#f4f4f8} .prov .mono{font-family:ui-monospace,Consolas,monospace}
 .prov .no{color:#7a1f1f} .prov .yes{color:#1f6a3a}
 /* A long sub-section of the provenance, folded so the links to the reports stay on the first
    screen. Its summary carries the answer, so nothing is hidden that the reader has to expand for. */
 .prov .provsec{margin:5px 0;border:1px solid #e4e4ee;border-radius:5px;background:#fafafd}
 .prov .provsec>summary{cursor:pointer;padding:4px 9px;color:#2d2d71}
 .prov .provsec>summary>span{color:#555;font-weight:400}
 .prov .provsec[open]>summary{border-bottom:1px solid #e4e4ee}
 .prov .provsec>:not(summary){padding:0 9px}
 .prov .provsec table{margin:6px 0}
"""

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
 // "#find=<token>[|<token>…]" — a link whose target is a SET of rows, not one row
 if(id.slice(0,5)==='find='&&window.SCV&&SCV.findAll){SCV.findAll(id.slice(5));return;}
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
# One source of truth for the schema number: the module that reads and writes the file. What the JS
# below emits and what Python accepts must never be able to drift apart.
SELECTION_SCHEMA = selection_file.SCHEMA

SELECT_JS = """
var SCSel=(function(){
 var SCHEMA=__SCHEMA__;
 var KEY='scauto-sel:'+(window.SCAUTO_RUN||'default');
 var data={},legacy={},subs=[],dirty=false,loadedStamp='',loadedSchema=SCHEMA;
 function bag(kind){if(!data[kind])data[kind]={};return data[kind];}
 function notify(){subs.forEach(function(f){try{f();}catch(e){}});}
 function stash(){                                  // same-tab safety net only (see above)
  try{window.localStorage.setItem(KEY,JSON.stringify(
   {saved:new Date().toISOString(),dirty:dirty,schema:loadedSchema,
    selections:data,legacy:legacy}));}catch(e){}}
 function touched(){dirty=true;stash();notify();}
 /* A row's stored value is its *key record* — the evidence-derived identifiers that let a partial
    run find this row again even if our own id for it moved (a better decoder changes a
    Library/Caches row's content hash, a better carver shifts a positional message anchor). A bare
    1 is still accepted and means "no alternates recorded". */
 /* Split incoming selections into what we can attribute and what we cannot.
    Before schema 2 a message id was a bare per-page anchor ("msg-12.0"), and message numbers restart
    in every conversation — so the same id matched a different message in every chat. Such an id
    names no conversation and cannot be attributed to one after the fact: it is quarantined rather
    than guessed at, because promoting it on whichever page happens to be open would invent a fact,
    and would put a message the examiner never ticked into a partial report.
    Every route into the store goes through here — including the localStorage restore, which would
    otherwise let a stash written by an older build smuggle bare ids back in. */
 function split(sel){
  var keep={},quarantine={};
  for(var k in sel){
   keep[k]={};
   for(var id in sel[k]){
    if(k==='msg'&&id.indexOf('|')<0){
     if(!quarantine[k])quarantine[k]={};
     quarantine[k][id]=sel[k][id]||1;
     continue;}
    keep[k][id]=sel[k][id]||1;}}
  return {data:keep,legacy:quarantine};}
 function apply(o){
  var parts=split((o&&o.selections)||{});
  data=parts.data;legacy=parts.legacy;
  loadedSchema=(o&&o.schema)||1;
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
    var parts=split(l.selections);
    data=parts.data;dirty=!!l.dirty;
    // a stash from an older build carries no `legacy` of its own; take whatever the split found
    legacy=l.legacy||{};
    for(var k in parts.legacy){
     if(!legacy[k])legacy[k]={};
     for(var id in parts.legacy[k])legacy[k][id]=parts.legacy[k][id];}
    if(l.schema)loadedSchema=l.schema;}
  }catch(e){}}
 function get(kind,id){return !!bag(kind)[id];}
 function set(kind,id,on,keys){
  if(on)bag(kind)[id]=keys||1;else delete bag(kind)[id];
  touched();}
 /* `keysFor` is optional and is called per id, so "select all shown" records the same key records
    a row-by-row tick would. */
 function setMany(kind,ids,on,keysFor){
  var b=bag(kind);
  ids.forEach(function(id){
   if(on)b[id]=(keysFor&&keysFor(id))||1;else delete b[id];});
  touched();}
 function ids(kind,prefix){
  var all=Object.keys(bag(kind));
  return prefix?all.filter(function(id){return id.indexOf(prefix)===0;}):all;}
 function keys(kind,id){var v=bag(kind)[id];return (v&&v!==1)?v:null;}
 /* `prefix` scopes the count to one page's rows, so a conversation page's "N selected" counts the
    messages selected *in this conversation*, not every conversation's. */
 function count(kind,prefix){return ids(kind,prefix).length;}
 function total(){var n=0;for(var k in data)n+=Object.keys(data[k]).length;return n;}
 function legacyIds(kind){return Object.keys(legacy[kind]||{});}
 function schema(){return loadedSchema;}
 /* `prefix` limits the clear to one page's rows — message ids are qualified with their
    conversation, so a per-conversation Clear must not wipe every conversation's ticks. */
 function clear(kind,prefix){
  if(!prefix){data[kind]={};}
  else{var b=bag(kind);
   Object.keys(b).forEach(function(id){if(id.indexOf(prefix)===0)delete b[id];});}
  touched();}
 function isDirty(){return dirty;}
 function payload(){
  return {tool:'Snapchat_Auto',schema:SCHEMA,
          tool_version:(window.SCAUTO_VERSION||''),
          run_id:(window.SCAUTO_RUN||'default'),
          sources:(window.SCAUTO_SOURCES||null),
          exported:new Date().toISOString(),selections:data};}
 /* Two forms of the same payload. ".json" is the default because browsers flag a ".js" download as
    dangerous and may refuse it outright; ".js" is the drop-in the reports auto-load. A bare .json
    renamed to selection.js is a *silent* failure — JSON at statement position is a syntax error the
    browser swallows — so nothing here ever suggests renaming it. */
 function saveFile(format){
  var js=(format==='js'),body;
  if(js)body="/* Snapchat Auto \\u2014 examiner selections for run "+
    (window.SCAUTO_RUN||'default')+
    ".\\n   Keep this file as <report folder>/selection.js so every report of this run loads it.\\n"+
    "   It is also a plain record you can file with the case. */\\n"+
    "SCSel.preload("+JSON.stringify(payload(),null,1)+");\\n";
  else body=JSON.stringify(payload(),null,1)+"\\n";
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([body],
   {type:js?'application/javascript':'application/json'}));
  a.download=js?'selection.js':'selection.json';
  document.body.appendChild(a);a.click();
  setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},0);
  dirty=false;stash();notify();}
 function loadFile(file,done){
  var r=new FileReader();
  r.onload=function(){
   var text=String(r.result||''),start=text.indexOf('{'),end=text.lastIndexOf('}'),o;
   try{
    o=JSON.parse(start>=0?text.slice(start,end+1):text);
   }catch(e){done(null,'That file is not a Snapchat Auto selection file.');return;}
   // a selection from another run loads, but not silently: its ids may name nothing here
   if(o.run_id&&window.SCAUTO_RUN&&o.run_id!==window.SCAUTO_RUN&&
      !confirm('That selection was saved for run '+o.run_id+', and these reports are run '+
               window.SCAUTO_RUN+'.\\n\\nLoad it anyway?')){done(null,null);return;}
   // an explicit load replaces what is here — unlike startup, the same-tab backup must not win
   apply(o.selections?o:{selections:o});
   stash();notify();
   done(total(),null);};
  r.readAsText(file);}
 restash();
 /* No beforeunload guard. There was one, and it was wrong twice over.
    It claimed "changes you made may not be saved" when every tick is written to this tab's
    localStorage the moment it is made, so closing the tab loses nothing it warned about. And `dirty`
    is restored from that same storage, so once the examiner had ticked anything *every* report tab they
    opened afterwards prompted on close -- including tabs they had not touched, and tabs opened after
    they had already saved the file elsewhere (each file:// tab has its own storage, so a save in one
    cannot clear the flag in another). A browser will not show custom text in that dialog either, so it
    could not even explain itself.
    What the examiner does still need is a reminder to export the selection to a file, and that is the
    persistent "unsaved -- use Save selections" note in the toolbar, which does not hijack tab close. */
 /* One delegated handler for every checkbox in the document, virtual rows included. The key record
    is read from the row itself (`data-i` on the .vr) rather than emitted into every row's markup —
    at 100 000 rows that attribute would cost more than the selection is worth. Hand-written
    checkboxes carry `data-keys` inline; there are only a handful of those.

    An inline `data-keys` wins over the row's, and the order matters: a hand-written box can sit
    INSIDE a virtual row (the Memories index renders a folded group's members in their lead's
    expanded area, each with its own box). Reading the row's keys there would file one Memory's tick
    under another Memory's identifiers, and a later run would then resolve the selection to the wrong
    row — the exact failure this key record exists to prevent. `data-keys` is always a statement
    about the box that carries it. */
 document.addEventListener('change',function(ev){
  var el=ev.target;
  if(!el||!el.classList||!el.classList.contains('selbox'))return;
  var keys=null,raw=el.getAttribute('data-keys'),vr;
  if(raw){try{keys=JSON.parse(raw);}catch(e){}}
  else if((vr=el.closest?el.closest('.vr[data-i]'):null)&&window.SCV&&SCV.selKeys)
   keys=SCV.selKeys(+vr.getAttribute('data-i'));
  set(el.getAttribute('data-kind'),el.getAttribute('data-id'),el.checked,keys);});
 return {get:get,set:set,setMany:setMany,ids:ids,keys:keys,count:count,total:total,clear:clear,
         preload:preload,onChange:function(f){subs.push(f);},saveFile:saveFile,loadFile:loadFile,
         dirty:isDirty,legacy:legacyIds,schema:schema};
})();
// Reflect the stored state onto every checkbox the virtual table does not draw itself — the ones on
// a detail sub-page, and the hand-written ones inside an expanded row (the Memories index puts a
// folded group's members there, each with its own box). Only a row's OWN checkbox is skipped: that
// one is rebuilt from the store by rowHtml on every render, and re-setting it here would be a second
// answer to the same question. A box in a `.vdet` is not — the detail HTML is one static string, so
// without this a Memory ticked anywhere else renders unticked the moment its row is redrawn.
// NOTE the invariant this rests on: `data-id` is the *store* id and must be unique across the whole
// run. A page-local anchor (a message's, whose number restarts per conversation) must be prefixed —
// see C.selPrefix in VTABLE_JS.
function scSyncBoxes(){
 document.querySelectorAll('input.selbox[data-id]').forEach(function(b){
  if(b.closest('.vcells'))return;                   // the row's own box: drawn from the store
  b.checked=SCSel.get(b.getAttribute('data-kind'),b.getAttribute('data-id'));});}
""".replace("__SCHEMA__", str(SELECTION_SCHEMA))

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
 .sellegacy{background:#fff5e0;border:1px solid #e0bf80;color:#6b4a00;padding:8px 12px;
   border-radius:6px;font-size:12px;line-height:1.5;margin:6px 0}
 .selrow{display:inline-flex;align-items:center;gap:8px;background:#eef0ff;border:1px solid #c9cdf0;
   border-radius:6px;padding:4px 10px;font-size:12.5px;font-weight:600;color:#2d2d71;cursor:pointer}
 .selrow:has(input:checked){background:#2d2d71;color:#fff;border-color:#2d2d71}
"""

# Toolbar glue shared by both index reports. `flt()` (defined by each report) is called when the
# "selected only" filter is toggled. `window.SCAUTO_SELPREFIX` scopes Clear and the count to one
# page's rows where the anchors are page-local (the Conversations message table).
SELECT_TOOLBAR_JS = """
function scSelClear(){
 var p=window.SCAUTO_SELPREFIX||'',n=SCSel.count(window.SCAUTO_SELKIND,p);
 if(!n){alert('Nothing is selected here.');return;}
 if(confirm('Clear all '+n+' selection(s) '+(p?'on this page':'in this report')+'?'))
  SCSel.clear(window.SCAUTO_SELKIND,p);}
function scSelLegacyCheck(){
 var n=SCSel.legacy('msg').length;
 if(!n)return true;
 return confirm(n+' message selection(s) in the file you loaded predate per-conversation message '+
  'ids: they name a message number with no conversation, and the same number exists in every '+
  'chat.\\n\\nSaving now drops them. Re-tick those messages afterwards.\\n\\nSave anyway?');}
/* ".json" is the default: browsers flag a ".js" download as dangerous and may refuse it outright.
   The ".js" form is the drop-in the reports auto-load. Never suggest renaming one to the other —
   bare JSON loaded as a script is a syntax error the browser swallows, which would leave the
   examiner with an empty selection and no message at all. */
function scSelSaveJson(){
 if(!scSelLegacyCheck())return;
 SCSel.saveFile('json');
 alert('selection.json was downloaded.\\n\\nThis is the copy to keep with the case, and the file '+
  'to hand to Snapchat_Auto when building a partial report.\\n\\nTo have the reports load it '+
  'again, use "Load\\u2026" \\u2014 or let the tool install it '+
  '(Snapchat_Auto --install-selection selection.json).');}
function scSelSaveJs(){
 if(!scSelLegacyCheck())return;
 SCSel.saveFile('js');
 alert('selection.js was downloaded.\\n\\nPut it next to the reports (replace '+
  '<report folder>\\\\selection.js) and every report of this run will load your selections the '+
  'next time it is opened.\\n\\nIf your browser blocked the download, save the .json instead and '+
  'let the tool install it \\u2014 do NOT rename a .json to .js, the reports cannot read that.');}
function scSelSave(){scSelSaveJson();}     /* the older single-button entry point */
function scSelLoad(input){
 var f=input.files&&input.files[0];
 if(!f)return;
 if(SCSel.dirty()&&!confirm('Loading a selection file replaces what is selected here, '+
   'and you have unsaved changes. Continue?')){input.value='';return;}
 SCSel.loadFile(f,function(total,err){
  input.value='';
  if(err){alert(err);return;}
  if(total===null)return;                  // the examiner cancelled a cross-run load
  scSelNote();
  alert(total+' selection(s) loaded.');});}
function scSelNote(){
 var e=document.getElementById('selnote');
 if(e)e.textContent=SCSel.dirty()?'unsaved \\u2014 use "Save selections"':'';
 var g=document.getElementById('sellegacy');
 if(!g)return;
 var n=SCSel.legacy('msg').length;
 g.style.display=n?'block':'none';
 if(n)g.innerHTML='\\u26a0 This selection file predates per-conversation message ids (schema 1). '+
  '<b>'+n+'</b> message selection(s) in it name a message number with no conversation \\u2014 the '+
  'same number exists in several chats, so they cannot be attributed to one and are not counted '+
  'here. Re-tick those messages and save; saving now drops them.';}
"""


FEED_DATE_TITLE = ("Not a message time — this conversation holds no message in arroyo.db. Taken "
                   "from the conversation's own row in the app's chat feed; open the conversation "
                   "for which field.")


def activity_cell(text, source):
    """A first/last activity value, marked when it did not come from a message.

    Shared by the Conversations index and the Contacts report so the same date cannot be presented
    two different ways — and so the marker travels as a *source string* between them rather than as
    markup: the Contacts report escapes what it is handed, so a ready-made cell arrived there as
    visible tag soup.
    """
    if not text:
        return '<span class="muted" title="Neither a message nor the conversation\'s own row in ' \
               'arroyo.db carries a date for this conversation.">no date recorded</span>'
    if source == "messages":
        return html.escape(text)
    return (f'<span class="fromfeed" title="{html.escape(FEED_DATE_TITLE)}">'
            f'{html.escape(text)} <span class="feedtag">feed</span></span>')


def counted_options(states, counts):
    """``<option>``s that say how many rows each one will return, and grey out when that is none.

    A dropdown whose only possible outcome is an empty table is indistinguishable from a broken
    report, and that is how it was read: "incomplete only" on a device with no incomplete media
    looked like a filter that did not work. Stating the count turns the same control into an answer
    — the choice is still visible (that this device has none of something is worth seeing), it just
    cannot be mistaken for a way to find rows that are not there.

    ``states`` is ``[(value, label)]`` in display order; ``counts`` maps value -> number of rows.
    """
    return "".join(
        f'<option value="{value}"{"" if counts.get(value) else " disabled"}>'
        f'{label} &mdash; {counts.get(value, 0)}</option>' for value, label in states)


_TS_PREFIX = re.compile(r"\s*(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::(\d\d))?")


def ts_key(text):
    """A displayed timestamp -> the seconds since 1970 of the **wall clock it shows**, or None.

    Every report renders its times through one formatter whose output starts
    ``YYYY-MM-DD HH:MM:SS`` in the run's chosen timezone, and this reads that back. So the number a
    row is filtered on is derived from the string the examiner is looking at, and the two cannot
    disagree — which is the whole point of not keeping a second, independently converted copy of
    each time.

    It is deliberately **not** a UTC epoch. The examiner types a date they read off the report, so
    the comparison has to be wall clock against wall clock; converting either side into real UTC
    would need the run's zone (and its DST history for that date) in the browser, and would shift
    every entered time by the offset if it got it wrong. Both sides here are naive, so a run in any
    timezone filters correctly with no zone arithmetic at all.
    """
    match = _TS_PREFIX.match(text or "")
    if not match:
        return None
    year, month, day, hour, minute, second = (int(g or 0) for g in match.groups())
    return int(calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)))


def ts_keys(*texts):
    """The distinct `ts_key` values of several displayed timestamps, sorted. Empties are dropped.

    This is what a row carries for the time filter. A row that yields an empty list has no time this
    report can read, and `scTimeHit` will not match it while a window is set — see `time_filter`.
    """
    keys = {key for key in (ts_key(text) for text in texts) if key is not None}
    return sorted(keys)


_TIME_UNITS = (("m", "minutes"), ("h", "hours"), ("d", "days"))


def time_filter(prefix, *, label="Time", scopes=(), hint="", noun="row"):
    """The shared date/time window control: *any time*, *between* two points, or *within ± N of* one.

    ``prefix`` namespaces the element ids so a page can carry more than one (the Conversations index
    has one; its conversation pages have their own). ``scopes`` is ``[(value, label)]`` for reports
    where a row has more than one kind of timestamp — the Conversations index applies the window to
    the conversation's own activity, to its messages' times, or to both — and is omitted where the
    question does not arise.

    Both inputs are ``datetime-local``, which needs no library and works on ``file://``. What is
    entered is read as a wall clock and compared against `ts_key` values, so it means the time as
    the report displays it, in the run's timezone — see `ts_key`.
    """
    scope_html = ""
    if scopes:
        options = "".join(f'<option value="{value}">{html.escape(text)}</option>'
                          for value, text in scopes)
        scope_html = (f'<label class="tfscope" title="Which of this row\'s timestamps the window is '
                      f'applied to.">of <select id="{prefix}scope" oninput="flt()">{options}'
                      f'</select></label>')
    units = "".join(f'<option value="{value}"{" selected" if value == "h" else ""}>{text}</option>'
                    for value, text in _TIME_UNITS)
    full_hint = (hint + " " if hint else "") + (
        f"The window is compared against the times as this report displays them, in the run's "
        f"timezone — so enter what you read in the table. A {noun} matches when ANY of its "
        f"timestamps falls inside the window, including the ones only its detail shows. A {noun} "
        f"with no readable timestamp at all cannot be shown to fall inside a window, so it is "
        f"hidden while one is set rather than being included on the strength of nothing — clear "
        f"the filter to see it again. Leaving one end of «between» empty makes the window "
        f"open-ended in that direction.")
    return (
        f'<label class="tfl" title="{html.escape(full_hint)}">{html.escape(label)}'
        f'{info_icon(full_hint)} '
        f'<select id="{prefix}mode" oninput="scTimeMode(\'{prefix}\')">'
        f'<option value="">any time</option>'
        f'<option value="range">between</option>'
        f'<option value="near">within</option></select></label>'
        f'<span class="tfg" id="{prefix}gr" style="display:none">'
        f'<input type="datetime-local" id="{prefix}from" step="1" oninput="flt()" '
        f'title="From (inclusive). Leave empty for «anything up to the other end».">'
        f'<span class="tfsep">and</span>'
        f'<input type="datetime-local" id="{prefix}to" step="1" oninput="flt()" '
        f'title="To (inclusive). Leave empty for «anything from the other end onwards»."></span>'
        f'<span class="tfg" id="{prefix}gn" style="display:none">'
        f'<input type="number" id="{prefix}n" value="1" min="0" step="any" oninput="flt()" '
        f'title="How far either side of the moment below.">'
        f'<select id="{prefix}unit" oninput="flt()">{units}</select>'
        f'<span class="tfsep">of</span>'
        f'<input type="datetime-local" id="{prefix}at" step="1" oninput="flt()" '
        f'title="The moment to search around."></span>'
        + scope_html)


TIME_CSS = """
 .tfl{white-space:nowrap} .tfg{display:inline-flex;align-items:center;gap:5px}
 .tfg input[type=datetime-local]{font-size:12px} .tfg input[type=number]{width:64px;font-size:12px}
 .tfsep{color:#777;font-size:12px} .tfscope{white-space:nowrap}
 /* the member of a folded group whose timestamp matched the window */
 .mhit{background:#fff6d9;box-shadow:0 0 0 2px #e6c983 inset;border-radius:5px}
"""


TIME_JS = r"""
/* The shared date/time window (report_ui.time_filter). One window, three states: off, a range, or
   ±N around a moment. Every value here is a naive wall clock — see report_ui.ts_key for why the
   comparison is deliberately not done in UTC. */
function scTimeVal(id){
 var v=scFv(id);
 if(!v)return null;
 var m=/^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)(?::(\d\d))?/.exec(v);
 if(!m)return null;                                    /* still being typed */
 return Date.UTC(+m[1],+m[2]-1,+m[3],+m[4],+m[5],+(m[6]||0))/1000;}

function scTimeWin(p){
 var mode=scFv(p+'mode');
 if(!mode)return null;
 if(mode==='range'){
  var a=scTimeVal(p+'from'),b=scTimeVal(p+'to');
  if(a===null&&b===null)return null;                   /* "between" with neither end set is off */
  return {a:a===null?-Infinity:a,b:b===null?Infinity:b};}
 var at=scTimeVal(p+'at');
 if(at===null)return null;
 var n=parseFloat(scFv(p+'n'));
 if(!isFinite(n)||n<0)return null;
 var mult={m:60,h:3600,d:86400}[scFv(p+'unit')||'h']||3600;
 return {a:at-n*mult,b:at+n*mult};}

/* A row matches when any of its timestamps is inside the window. No timestamps means no match: a
   row we cannot place in time may not be presented as one that falls in the window asked for. */
function scTimeHit(win,list){
 if(!win)return true;
 if(!list||!list.length)return false;
 for(var i=0;i<list.length;i++)if(list[i]>=win.a&&list[i]<=win.b)return true;
 return false;}

/* `quiet` is set by scTimeReset, which runs inside the report's own reset() — the refilter that
   follows it is already on its way, and asking for a second one would filter the table twice. */
function scTimeMode(p,quiet){
 var mode=scFv(p+'mode');
 var gr=document.getElementById(p+'gr'),gn=document.getElementById(p+'gn');
 if(gr)gr.style.display=(mode==='range')?'':'none';
 if(gn)gn.style.display=(mode==='near')?'':'none';
 if(!quiet&&window.flt)flt();}

function scTimeReset(p){
 scFvReset(p+'mode');scFvReset(p+'from');scFvReset(p+'to');scFvReset(p+'at');
 var n=document.getElementById(p+'n');if(n)n.value='1';
 var u=document.getElementById(p+'unit');if(u)u.value='h';
 var s=document.getElementById(p+'scope');if(s)s.selectedIndex=0;
 scTimeMode(p,true);}
"""


def clear_filters_button(noun="row"):
    """The "clear every filter" control, last in each report's filter bar.

    Every report already had the machinery — ``C.reset()``, which `findAll`/`goTo` call so a filter
    cannot hide the row a cross-report link was aimed at. What was missing was a way for the
    examiner to ask for it. Without one, "the report says 0 rows" is regularly one control left set
    three filters ago, on a bar that does not fit on one line at every window width. It is coloured
    as a destructive action because it is the one button here that throws work away — the row
    selection survives, but the query, the dropdowns and *Selected only* do not.
    """
    return (f'<button type="button" class="clearflt" onclick="SCV.clearFilters()" '
            f'title="Put the search box and every filter above back to «any», and show every '
            f'{noun} again. Your ticked selections are kept.">&#10005; Clear all filters</button>')


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
        '<button onclick="scSelSaveJson()" title="Download selection.json — the copy to keep with '
        'the case, and the file to hand to Snapchat_Auto when building a partial report">'
        '💾 Save selections (.json)</button>'
        '<button onclick="scSelSaveJs()" title="Download the drop-in selection.js — put it next to '
        'the reports to have every report of this run load your selections. Some browsers block a '
        '.js download; use the .json in that case.">Save as selection.js</button>'
        '<label class="filebtnlike" title="Load a selection file saved earlier (.json or .js)">'
        'Load…<input type="file" id="selfile" accept=".js,.json,application/json" hidden '
        'onchange="scSelLoad(this)"></label>'
        '<span class="selnote" id="selnote"></span></span>'
        '<div class="sellegacy" id="sellegacy" style="display:none"></div>')


SELECTION_STUB = """/* Snapchat Auto — examiner selections for this run.

   Every report of this run loads this file at startup, which is how the Memories index, the Memory
   detail sub-pages and the cache_controller report agree on what you have selected. It starts
   empty: tick rows in a report and press "Save selections (.json)". Keep that .json with the case —
   it is also the file you hand back to Snapchat_Auto to build a partial report.

   To have the reports load your selections automatically again, either use "Load…" each session or
   let the tool put them here for you:

       Snapchat_Auto --install-selection selection.json

   Do NOT simply rename a .json to selection.js. This file is loaded as a script, and bare JSON is a
   syntax error the browser discards without a word — the reports would open with nothing selected
   and no indication why. "Save as selection.js" produces the drop-in form when you want one.

   (A report opened from file:// cannot write to disk, and browsers give each file:// page its own
   private, tab-scoped storage — so a saved file is what makes a selection last.) */
SCSel.preload({"tool": "Snapchat_Auto", "schema": %d, "run_id": %s, "tool_version": %s,
               "sources": null, "exported": "", "selections": {}});
"""


_SOURCES_CACHE = {}


def sources_script(report_dir):
    """``window.SCAUTO_SOURCES=…;`` for a report's ``<head>``, or "" when there is nothing recorded.

    This is what puts the source fingerprints into the examiner's saved selection file, so a partial
    run can check them against the extraction it is handed — even when the original report folder is
    no longer at hand. Per artifact rather than a digest alone: a digest can say *that* something
    differs but not *which* artifact, and the paths are what let the tool offer the same ZIP and
    keychain back.

    Only the identity part travels — see :func:`source_fingerprint.compact`. Embedding the whole
    manifest put the run's own timestamp in every page, which made two runs of the same build on the
    same evidence produce different reports.

    Read from ``sources.json`` rather than passed in, because every report already receives its
    report directory and threading one more argument through five generators would only be a second
    way for the two to disagree. Cached per directory: five reports and every conversation page ask.
    """
    key = os.path.abspath(report_dir or ".")
    if key not in _SOURCES_CACHE:
        sources = source_fingerprint.compact(source_fingerprint.read_sources(report_dir))
        _SOURCES_CACHE[key] = (f"window.SCAUTO_SOURCES={json.dumps(sources, separators=(',', ':'))};"
                               if sources else "")
    return _SOURCES_CACHE[key]


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
            fh.write(SELECTION_STUB % (SELECTION_SCHEMA, json.dumps(run_id_value),
                                       json.dumps(app_version.get_version())))
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
 /* The one control on the bar that discards work, so it is the one that looks like it does. */
 .toolbar button.clearflt{background:#fdeceb;border-color:#e3b3ae;color:#9a2b20}
 .toolbar button.clearflt:hover{background:#f8d9d6;border-color:#d29089}
 a.back{display:inline-block;margin:14px 24px 0;color:#2d2d71;font-weight:600;text-decoration:none;
   font-size:13px} a.back:hover{text-decoration:underline}
 .mono{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}
 .muted{color:#999} .more{background:#d7d7ee;color:#33367a;border-radius:8px;padding:0 6px;
   font-size:10px}
 /* An activity date that is NOT a message time (report_ui.activity_cell). Muted and tagged so the
    column cannot be read as "a message was sent then". Used by Conversations and Contacts. */
 .fromfeed{color:#6a6a80}
 .feedtag{background:#e7e7f2;color:#5a5a86;border:1px solid #d2d2e4;border-radius:7px;
   padding:0 5px;font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.03em}
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
 /* "open" is an action, not a word in a sentence: it reads as a button, like the cache_controller
    report's chips, so it is obvious the row has a page behind it */
 a.openbtn{display:inline-flex;align-items:center;gap:4px;text-decoration:none;font-weight:700;
   font-size:11px;color:#25348a;background:#e7ecff;border:1px solid #b9c3f0;border-radius:10px;
   padding:3px 9px;white-space:nowrap}
 a.openbtn:hover{background:#d5deff;border-color:#8f9fe0}
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
 .vcells>.vc.sel{display:flex;align-items:center;justify-content:center;padding:0;gap:6px}
 /* The group box (SCV.selectGroup): a lead row's control for the whole fold, next to — and visibly
    not the same thing as — the row's own selection box. Squared off, because a three-state control
    that looked identical to the per-row one would be read as one. */
 input.grpbox{width:14px;height:14px;cursor:pointer;accent-color:#5a5a96;margin:0;border-radius:0}
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
    sortCol=-1,sortDir=1,scheduled=false,measuring=0,pageSize=0,page=0,pagerSig='',
    kids={},foldHit={},hits={},nhit=0,loaded=false;

function setRows(r){rows=r;byId={};loaded=true;
 for(var i=0;i<rows.length;i++)byId[rows[i][0]]=i;
 buildFolds();
 if(C)refilter();}

/* ---------- folded rows ----------
   A row may be *folded* into another: it keeps its place in `rows` — so its anchor, its selection
   id and every cross-report link into it go on working — but the table shows its lead's row instead
   and renders it inside that row's detail. The Memories index folds a group of Memories behind its
   earliest member, because a group is the same media under several snap rows and three rows read as
   three findings.

   The lead is shown when the lead OR any folded member matches the current filters, and when only a
   member matched, `foldHit` remembers which — that is what lets the report open the lead and point
   at the member instead of showing a row that appears not to match at all. */
function buildFolds(){
 kids={};
 for(var i=0;i<rows.length;i++){
  var lead=(rows[i][5]||{}).lead;
  if(lead&&lead!==rows[i][0])(kids[lead]=kids[lead]||[]).push(i);}}

function folded(){return !!(C&&C.folded&&C.folded());}

function foldHits(){return foldHit;}

/* Open every lead the filters reached only through a folded member. Called by the report after
   refilter(): a lead whose own cells do not match is otherwise a row with no visible reason to be
   there, so there is no count at which leaving them shut is the better answer — and that rules out
   the obvious `open()` per row, whose rebuild() would make this quadratic. Rows are marked open, the
   chunks they need are fetched once each, and the offsets are rebuilt once, as expandAll does.
   Targeted at the member hits only; expandAll() would open the whole page. */
function openFoldHits(){
 var ids=Object.keys(foldHit),need={},opened=0;
 for(var k=0;k<ids.length;k++){
  var id=ids[k],i=byId[id];
  if(i===undefined||exp[id])continue;
  exp[id]=1;opened++;
  if(C.detailBase&&det[id]===undefined&&rows[i][4]!==null&&rows[i][4]!==undefined)
   need[rows[i][4]]=i;}
 if(!opened)return 0;
 dirty=true;rebuild();
 Object.keys(need).forEach(function(n){
  loadDetail(need[n],function(){dirty=true;rebuild();});});
 return opened;}

function openRow(id){var i=byId[id];if(i===undefined||exp[id])return false;open(i);return true;}

function init(o){
 C=o;mount=document.getElementById(o.mount);win=document.getElementById(o.win);
 pad=document.getElementById(o.pad);
 pager=o.pager?document.getElementById(o.pager):null;
 pageSize=o.pageSize||0;
 mount.addEventListener('click',onClick);
 /* The group box carries no data-id, so SELECT_JS's own handler ignores it: what it means is "every
    Memory of this fold", which only this module knows the membership of. */
 mount.addEventListener('change',function(ev){
  var box=ev.target;
  if(!box||!box.classList||!box.classList.contains('grpbox'))return;
  selectGroup(box.getAttribute('data-grp'),box.checked);});
 window.addEventListener('scroll',schedule,{passive:true});
 window.addEventListener('resize',function(){dirty=true;schedule();});
 /* "The data file did not load" and "this report has no rows" are different statements, and only the
    first is a fault. They were told apart by rows.length, so a report that legitimately contains
    nothing — a partial extract holding no Library/Caches file, say — accused itself of a missing
    data folder. `loaded` is set by setRows, which the data file calls even with an empty array, so
    the banner now fires only when that script really did not run. */
 if(!loaded){var m=document.getElementById(o.missing);if(m)m.style.display='block';}
 if(o.sort!==undefined&&o.sort>=0){sortCol=o.sort;sortDir=o.sortDir||1;}
 if(o.selKind&&window.SCSel)SCSel.onChange(function(){
  if(C.selectedOnly&&C.selectedOnly())refilter();else{dirty=true;render();}
  if(C.selCount)C.selCount(SCSel.count(o.selKind,o.selPrefix));});
 refilter();
 sync();
 if(C.selCount&&window.SCSel)C.selCount(SCSel.count(o.selKind,o.selPrefix));}

/* ---------- filtering / sorting ---------- */
/* A query is split on '|', and a row matches when it contains ANY of the parts. One token behaves
   exactly as it always did; the OR form is what lets a single cross-report link land on every row
   an entry corresponds to (see findAll) instead of silently picking the first of them. */
function terms(q){
 var out=[],p=q.split('|');
 for(var i=0;i<p.length;i++){var t=p[i].trim();if(t)out.push(t);}
 return out;}

function hit(text,ts){
 for(var i=0;i<ts.length;i++)if(text.indexOf(ts[i])>=0)return true;
 return false;}

function refilter(){
 if(!C)return;
 var ts=terms((C.query?C.query():'').toLowerCase()),m=C.match||null;
 function matches(i){
  var r=rows[i];
  if(ts.length&&!hit(r[2],ts))return false;
  return !(m&&!m(r[5]||{},r));}
 var fold=folded();
 view=[];foldHit={};hits={};nhit=0;
 for(var i=0;i<rows.length;i++){
  var id=rows[i][0],lead=fold?(rows[i][5]||{}).lead:null;
  if(lead&&lead!==id)continue;                       /* shown inside its lead's row instead */
  var own=matches(i),mine=own?[i]:[];
  /* Which rows of this fold matched, kept per shown lead. "Select all shown" needs it: a folded
     member the filters do NOT match must not be ticked just because its lead is on screen, and a
     member that DOES match must be, because that is the row the examiner asked for. */
  if(fold&&kids[id])
   for(var k=0;k<kids[id].length;k++)
    if(matches(kids[id][k]))mine.push(kids[id][k]);
  if(!mine.length)continue;
  /* the lead itself does not match — but a member it is hiding does, and dropping the group because
     its earliest member is not the one searched for would lose that member entirely */
  if(!own)foldHit[id]=rows[mine[0]][0];
  hits[i]=mine;nhit+=mine.length;
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
 /* `nhit` is every matching row; `view.length` is how many rows that is on screen. The two differ
    only when a fold is in effect, and a report that showed one as the other would report a group of
    three as one memory. */
 if(C.count)C.count(view.length,rows.length,nhit);
 /* Each report words its empty message as "nothing matches the current filters", which is the wrong
    statement when the report has no rows at all — a partial extract holding no Library/Caches file
    would read as a filter left set. The report's own wording is kept for the case it describes. */
 var e=document.getElementById(C.empty);
 if(e){
  if(!view.length&&!rows.length){
   if(e.getAttribute('data-filtered')===null)e.setAttribute('data-filtered',e.innerHTML);
   e.innerHTML=C.emptyAll||'This report contains no rows.';}
  else if(e.getAttribute('data-filtered')!==null)
   e.innerHTML=e.getAttribute('data-filtered');
  e.style.display=view.length?'none':'block';}
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
 markFoldHits();
 markGroupBoxes();
 /* Hand-written checkboxes inside an expanded row come from static detail HTML, so their state has
    to be put back after every redraw — see scSyncBoxes. Guarded because the selection code is not
    loaded on every page that uses this table. */
 if(window.scSyncBoxes)scSyncBoxes();
 measure();}

/* Point at the folded member whose timestamp (or search term) is why its lead is on screen. Done
   after every render rather than baked into the detail HTML: the detail is one static string shared
   by every filter state, and innerHTML is rewritten each time the window scrolls. */
function markFoldHits(){
 for(var id in foldHit){
  var host=document.getElementById(id);
  if(!host)continue;
  var el=host.querySelector('[data-mem="'+foldHit[id]+'"]');
  if(el)el.classList.add('mhit');}}

/* Re-measure the open rows after something inside one changed size — an image or a video that
   finished loading, say. Without this the row keeps the height it had while the media was still
   blank, and every offset below it is wrong, which is what makes scrolling jump.
   It must only *measure*: re-rendering unconditionally would rewrite the window's innerHTML, which
   recreates those media elements, which fire their load event again — an endless loop. measure()
   rebuilds only when a height really changed, so this settles after one round. */
function remeasure(){measuring=0;measure();}

/* An expanded row's real height is only known once it is in the DOM: measure it, remember it and
   rebuild the offsets when it differs from what we assumed. */
function measure(){
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
   'px;grid-template-columns:'+(C.selKind?(C.selWidth||'30px')+' ':'')+C.cols+'">';
 if(C.selKind){
  s+='<div class="vc sel"><input type="checkbox" class="selbox" data-kind="'+
   C.selKind+'" data-id="'+selId(id)+'"'+(SCSel.get(C.selKind,selId(id))?' checked':'')+
   ' title="mark this row as relevant (saved in this browser; use Export to keep it)">';
  /* The group box: a second, three-state control on a lead row, standing for the whole fold. It is
     separate from the row's own box on purpose — that one's data-id IS this row's selection id, and
     making one box write several ids would stop it reporting its own row's state, which "Selected
     only", "Select all shown" and the selection file all read. */
  if(C.groupBox&&folded()&&kids[id])
   s+='<input type="checkbox" class="grpbox" data-grp="'+id+'">';
  s+='</div>';}
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

/* ---------- selection ---------- */

/* A row's *anchor* is only unique within its own document — a message's number restarts in every
   conversation, so `msg-12.0` names a different message on every conversation page. The *store* id
   has to be unique across the whole run, so a table whose anchors are page-local sets `selPrefix`
   (the Conversations message table uses "conv-<id>|"). The anchor itself is left alone, which is
   why every cross-report link, `cache_links.json` record and SCV.goTo target still resolves. */
function selId(id){return (C&&C.selPrefix?C.selPrefix:'')+id;}
/* Every "Selected only" predicate goes through this too, even in the tables that set no prefix: a
   filter that tested the bare anchor would silently stop matching the day its table acquired one. */

/* The evidence-derived identifiers for a row, recorded alongside the tick so a later run can find
   this row again even if our own id for it moved. Looked up on demand rather than emitted into
   every row's markup — see the change handler in SELECT_JS. */
function selKeys(i){
 if(!C||!C.selKeys||!rows[i])return null;
 try{return C.selKeys(rows[i])||null;}catch(e){return null;}}

/* ---------- a folded group's own selection ---------- */

/* The rows one fold covers: the lead and every member, whether or not the filters match them. This
   is the group as the app data has it, which is what a control labelled "the whole group" must act
   on — selectShown is the one that answers to the filters instead. */
function foldRows(id){
 var i=byId[id];
 return i===undefined?[]:[i].concat(kids[id]||[]);}

/* ``[selected, total]`` for one fold, so the group box can be none / some / all. */
function foldCount(id){
 var mine=foldRows(id),n=0;
 for(var k=0;k<mine.length;k++)if(SCSel.get(C.selKind,selId(rows[mine[k]][0])))n++;
 return [n,mine.length];}

function selectGroup(id,on){
 var mine=foldRows(id),byStoreId={},list=[];
 for(var k=0;k<mine.length;k++){
  var s=selId(rows[mine[k]][0]);byStoreId[s]=mine[k];list.push(s);}
 if(!list.length)return 0;
 SCSel.setMany(C.selKind,list,on,function(s){return selKeys(byStoreId[s]);});
 return list.length;}

/* `indeterminate` is a property, not an attribute, so a three-state box cannot be drawn by the HTML
   the row is built from — it has to be set after every render, like the fold-hit marking. */
function markGroupBoxes(){
 if(!C.groupBox)return;
 var boxes=win.querySelectorAll('input.grpbox[data-grp]');
 for(var j=0;j<boxes.length;j++){
  var box=boxes[j],c=foldCount(box.getAttribute('data-grp')),n=c[0],total=c[1];
  box.checked=n===total&&total>0;
  box.indeterminate=n>0&&n<total;
  box.title=(n===0?'None':n===total?'All':n+' of '+total)+
   ' of the '+total+' Memories grouped in this row are selected. Click to select them all'+
   (n===total?' (they are; click to clear them)':'')+
   '. The box to the left is this row\'s own Memory.';}}

/* Every row the current filters match, including the members of a folded group — but only the ones
   that match. See the note in refilter(). */
function selectShown(on){
 if(!C.selKind)return 0;
 var byStoreId={},list=[];
 for(var v=0;v<view.length;v++){
  var mine=hits[view[v]]||[view[v]];
  for(var k=0;k<mine.length;k++){
   var s=selId(rows[mine[k]][0]);byStoreId[s]=mine[k];list.push(s);}}
 SCSel.setMany(C.selKind,list,on,function(s){return selKeys(byStoreId[s]);});
 return list.length;}

/* ---------- anchor navigation ---------- */
function hasRow(id){return byId[id]!==undefined;}

/* The target of a "#find=<token>[|<token>…]" link: filter this table to those tokens and open
   every row that matches.

   One entry in one report is regularly several rows in another — the same cached bytes can sit
   under more than one path, and one Memory owns many cached files. Linking to the first of them
   would hide the rest, and emitting one link per row makes a cell unreadable. So a link that has
   several targets carries them all and lands on a filtered, expanded page: what the examiner
   arrives at is the complete set, with the query that produced it visible in the search box. */
function findAll(token){
 if(!C||!token)return false;
 if(C.reset)C.reset();                                 // drop filters that would hide a match
 if(C.setQuery)C.setQuery(token);
 else{var q=document.getElementById('q');if(q)q.value=token;}
 page=0;
 refilter();
 expandAll(true,500);
 window.scrollTo(0,Math.max(0,mount.getBoundingClientRect().top+window.pageYOffset-
   (window.scStick?scStick():60)));
 return true;}

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

/* Put every filter back to "any" and show the whole table again. The same C.reset() that
   findAll()/goTo() already use to stop a filter hiding the row they were sent to — the only new
   part is that the examiner can now ask for it, instead of hunting the control that is still set.
   Selected-only is a filter too and is cleared with the rest; the ticks themselves are untouched. */
function clearFilters(){
 if(C&&C.reset)C.reset();
 page=0;
 refilter();}

return {init:init,setRows:setRows,detail:detail,refilter:refilter,setSort:setSort,
        expandAll:expandAll,goTo:goTo,hasRow:hasRow,findAll:findAll,selectShown:selectShown,
        remeasure:remeasure,setPage:setPage,setPageSize:setPageSize,clearFilters:clearFilters,
        selId:selId,selKeys:selKeys,
        foldHits:foldHits,openFoldHits:openFoldHits,openRow:openRow,
        selectGroup:selectGroup,foldCount:foldCount,
        page:function(){return page;},
        pages:pageCount,count:function(){return view.length;},
        matching:function(){return nhit;}};
})();

/* Read / clear a filter control that a report only emits when the data has something for it to
   match — a "-wal" filter on a database with no such rows would be a control that can do nothing
   but return an empty table. `match` and `reset` go through these so the same JS works either way. */
function scFv(id){var e=document.getElementById(id);return e?e.value:'';}
function scFvReset(id){var e=document.getElementById(id);if(e)e.value='';}
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
