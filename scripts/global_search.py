"""Search every report of a folder at once — ``search.html``, beside ``selection.js``.

Each report searches only itself, so a value an examiner holds — a CACHE_KEY, a hash, a file name, a
snap id, a few words of a message — had to be tried report by report, and a file that only one report
lists was found only by whoever tried that one. This page runs the search of every report at once.

It is the **same** search, on the same data: every report already keeps its rows in ``data/index.js``
with a pre-built, lower-case search text per row (``report_ui.write_rows``), and its own search box
matches that text — case-insensitive, ``a|b`` for either. The page loads those files the way the reports
do, with ``<script src>`` (a ``file://`` page may not ``fetch`` its neighbours), on the first search
only, and applies the same rule to them. So a hit here is a row the report's own box finds, and the
count beside each report is the count its box gives. Every hit opens its row in its report's named tab;
*Open all* opens the report filtered to the same search (``#find=``), every match expanded.

Messages are searched too: each conversation's page keeps its message rows in
``Conversations/pages/data/<key>/index.js``, listed here when the page is written.

Not searched: the legacy single-page reports, which keep no row data; and anything a row shows only
when expanded, unless its report put it in the row's search text — exactly as in the reports.

The page is rewritten with the folder's index (``Snapchat_Auto.write_index``), so it always lists the
reports that folder holds; in a partial extract it searches the extract and says so.
"""
import html
import json
import os

from scripts import app_version, partial_report, report_ui

PAGE = report_ui.SEARCH_PAGE

#: ``(key, title, page, rows, named tab, the cells a hit is labelled with)`` — the reports whose rows
#: are searched, in the order the page lists them. The cells are picked per report so a hit reads as
#: what it is (a name and a user id; a CACHE_KEY and its category; a Memory's kind, ids and date).
SOURCES = (
    ("contacts", "Contacts", "Contacts/Contacts_report.html", "Contacts/data/index.js",
     "scauto_contacts", (1, 2, 4)),
    ("conversations", "Conversations", "Conversations/Conversations_report.html",
     "Conversations/data/index.js", "scauto_convs", (2, 4, 3)),
    ("memories", "Memories", "Memories/Memories_report.html", "Memories/data/index.js",
     "scauto_memories", (2, 4, 7)),
    ("cache", "Cache controller (cache_controller.db)", "CacheController/CacheController_report.html",
     "CacheController/data/index.js", "scauto_cache", (2, 1, 5, 6)),
    ("cachemedia", "Cached media (Library/Caches)", "CacheMedia/CacheMedia_report.html",
     "CacheMedia/data/index.js", "scauto_cachemedia", (2, 1, 5, 6)),
)
#: The Android Memories report has columns of its own: snap id, type, created.
ANDROID_MEMORY_CELLS = (8, 4, 2)
#: A message: created, sender, content, type.
MESSAGE_CELLS = (1, 3, 5, 4)
#: Reports this page cannot search, named when the folder holds them.
NOT_SEARCHED = (
    ("Communications_legacy/Communications_legacy_report.html", "Communications (legacy)"),
    ("LocalMemories_legacy/LocalMemories_legacy_report.html", "Local Memories (legacy)"),
)


def conversation_pages(report_dir):
    """``[[key, page, rows]]`` for every conversation page that has its message rows."""
    pages_dir = os.path.join(report_dir, "Conversations", "pages")
    data_dir = os.path.join(pages_dir, "data")
    if not os.path.isdir(data_dir):
        return []
    out = []
    for key in sorted(os.listdir(data_dir)):
        if (os.path.isfile(os.path.join(data_dir, key, "index.js"))
                and os.path.isfile(os.path.join(pages_dir, f"{key}.html"))):
            out.append([key, f"Conversations/pages/{key}.html",
                        f"Conversations/pages/data/{key}/index.js"])
    return out


def sources(report_dir, platform="ios"):
    """The sources this folder holds, as the page's script takes them."""
    out = []
    for key, title, page, data, tab, cells in SOURCES:
        if not (os.path.isfile(os.path.join(report_dir, page))
                and os.path.isfile(os.path.join(report_dir, data))):
            continue
        if key == "memories" and platform == "android":
            cells = ANDROID_MEMORY_CELLS
        out.append({"key": key, "title": title, "page": page, "data": data, "tab": tab,
                    "cells": list(cells)})
        if key == "conversations":
            pages = conversation_pages(report_dir)
            if pages:
                out.append({"key": "messages", "title": "Messages", "tab": "scauto_conv_page",
                            "cells": list(MESSAGE_CELLS), "pages": pages})
    return out


#: The search itself, apart from the page so it can be run on its own (tests/test_global_search.py).
#: ``terms`` and ``hits`` are the reports' own rule (report_ui.VTABLE_JS ``terms``/``hit``).
CORE_JS = r"""
var SCQ=(function(){
"use strict";
function terms(q){
 var out=[],p=String(q||'').toLowerCase().split('|');
 for(var i=0;i<p.length;i++){var t=p[i].trim();if(t)out.push(t);}
 return out;}
function hits(rows,ts){
 var out=[];
 if(!ts.length||!rows)return out;
 for(var i=0;i<rows.length;i++){
  var s=rows[i][2]||'';
  for(var k=0;k<ts.length;k++)if(s.indexOf(ts[k])>=0){out.push(i);break;}}
 return out;}
var ENT={amp:'&',lt:'<',gt:'>',quot:'"',apos:"'",nbsp:' ',middot:'·',mdash:'—',
 ndash:'–',hellip:'…'};
/* A cell's text: its markup dropped, its character references read. Never parsed as HTML — the page
   only ever writes this text back escaped. */
function text(cell){
 return String(cell===undefined||cell===null?'':cell).replace(/<[^>]*>/g,' ')
  .replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);/gi,function(m,e){
   if(e.charAt(0)==='#'){
    var n=(e.charAt(1)==='x'||e.charAt(1)==='X')?parseInt(e.slice(2),16):parseInt(e.slice(1),10);
    try{return String.fromCodePoint(n);}catch(x){return m;}}
   var v=ENT[e.toLowerCase()];
   return v===undefined?m:v;})
  .replace(/\s+/g,' ').trim();}
function label(row,cells){
 var parts=[];
 for(var i=0;i<cells.length;i++){
  var t=text((row[1]||[])[cells[i]]);
  if(!t||t==='—'||t==='▸')continue;
  parts.push(t.length>90?t.slice(0,90)+'…':t);}
 return parts.join(' · ')||String(row[0]);}
/* Where in the row's search text the first term matched, with some context either side. */
function snippet(s,ts,width){
 s=String(s||'');width=width||48;
 var at=-1,len=0;
 for(var k=0;k<ts.length;k++){var i=s.indexOf(ts[k]);if(i>=0&&(at<0||i<at)){at=i;len=ts[k].length;}}
 if(at<0)return null;
 var a=Math.max(0,at-width),b=Math.min(s.length,at+len+width);
 return {pre:(a>0?'…':'')+s.slice(a,at),hit:s.slice(at,at+len),
         post:s.slice(at+len,b)+(b<s.length?'…':'')};}
/* The fragment that opens a report filtered to the same search (report_ui.find_fragment). */
function findHash(ts){return '#find='+ts.map(encodeURIComponent).join('|');}
return {terms:terms,hits:hits,text:text,label:label,snippet:snippet,findHash:findHash};
})();
"""

#: The page: loading the reports' rows, the search box, the results.
PAGE_JS = r"""
var SCS_cur=null;
/* What every data file calls. Only rows are needed here; detail chunks are never loaded. */
var SCV={setRows:function(r){if(SCS_cur)SCS_cur.rows=r;},detail:function(){}};
var SCS_items=[];
SC_SRC.forEach(function(src){
 if(src.pages)src.pages.forEach(function(p){
  SCS_items.push({src:src,key:p[0],page:p[1],data:p[2],rows:null});});
 else SCS_items.push({src:src,page:src.page,data:src.data,rows:null});});
var SCS_state='idle',SCS_wait=[],SCS_res=null,SCS_more={},SCS_t=0;
function SCS_esc(s){return String(s===undefined||s===null?'':s).replace(/[&<>"']/g,function(c){
 return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function SCS_status(t){document.getElementById('status').textContent=t;}
function SCS_load(done){
 if(SCS_state==='done'){done();return;}
 SCS_wait.push(done);
 if(SCS_state==='loading')return;
 SCS_state='loading';
 var i=0,t0=Date.now();
 (function next(){
  if(i>=SCS_items.length){
   SCS_state='done';
   var n=0;SCS_items.forEach(function(it){if(it.rows)n+=it.rows.length;});
   SCS_status(n.toLocaleString()+' rows read from the reports\u2019 data files in '+
    ((Date.now()-t0)/1000).toFixed(1)+' s');
   var w=SCS_wait;SCS_wait=[];w.forEach(function(f){f();});return;}
  var it=SCS_items[i++];
  SCS_status('Loading '+it.src.title+(it.key?' ('+i+' of '+SCS_items.length+')':'')+'…');
  var sc=document.createElement('script');
  sc.src=it.data;
  SCS_cur=it;
  sc.onload=function(){SCS_cur=null;next();};
  sc.onerror=function(){SCS_cur=null;it.missing=true;next();};
  document.head.appendChild(sc);})();}
/* The conversation a message page belongs to, as the Conversations index names it. */
function SCS_convTitle(key,page){
 var conv=SCS_items.filter(function(it){return it.src.key==='conversations';})[0];
 if(conv&&conv.rows){
  var needle='pages/'+key+'.html';
  for(var i=0;i<conv.rows.length;i++)
   if((conv.rows[i][1]||[]).join('').indexOf(needle)>=0)return SCQ.label(conv.rows[i],[2,4]);}
 return 'Conversation '+key;}
function SCS_run(q){
 var ts=SCQ.terms(q);
 if(!ts.length){SCS_res=null;document.getElementById('results').innerHTML='';
  document.getElementById('summary').innerHTML='';return;}
 SCS_load(function(){
  var res={ts:ts,q:q,groups:[],total:0};
  SC_SRC.forEach(function(src){
   var g={src:src,parts:[],n:0,missing:false};
   SCS_items.forEach(function(it){
    if(it.src!==src)return;
    if(it.missing)g.missing=true;
    var h=SCQ.hits(it.rows,ts);
    if(h.length){g.parts.push({it:it,hits:h});g.n+=h.length;}});
   res.total+=g.n;res.groups.push(g);});
  SCS_res=res;SCS_more={};SCS_draw();});}
function SCS_hitHtml(it,i,ts){
 var r=it.rows[i],sn=SCQ.snippet(r[2],ts);
 return '<li><a target="'+SCS_esc(it.src.tab)+'" href="'+SCS_esc(it.page)+'#'+
  encodeURIComponent(r[0])+'">'+SCS_esc(SCQ.label(r,it.src.cells))+'</a>'+
  (sn?'<div class="snip">'+SCS_esc(sn.pre)+'<mark>'+SCS_esc(sn.hit)+'</mark>'+SCS_esc(sn.post)+
   '</div>':'')+'</li>';}
function SCS_list(id,part,ts,first){
 var shown=SCS_more[id]||first,h=part.hits,out='<ul class="hits">';
 for(var k=0;k<h.length&&k<shown;k++)out+=SCS_hitHtml(part.it,h[k],ts);
 out+='</ul>';
 if(h.length>shown)out+='<button class="more" onclick="SCS_showMore(\''+id+'\','+shown+
  ')">Show '+Math.min(200,h.length-shown)+' more of '+(h.length-shown)+'</button>';
 return out;}
function SCS_showMore(id,shown){SCS_more[id]=shown+200;SCS_draw();}
function SCS_draw(){
 var res=SCS_res;
 if(!res)return;
 var sum='<b>'+res.total.toLocaleString()+'</b> row'+(res.total===1?'':'s')+' match'+
  (res.total===1?'es':'')+' <span class="q">'+SCS_esc(res.ts.join(' | '))+'</span>',body='',none=[];
 res.groups.forEach(function(g,gi){
  var src=g.src,id='g'+gi;
  sum+=g.n?' <a class="chip" href="#'+id+'" onclick="return SCS_jump(\''+id+'\')">'+
   SCS_esc(src.title)+' <b>'+g.n.toLocaleString()+'</b></a>':
   ' <span class="chip none">'+SCS_esc(src.title)+' <b>0</b></span>';
  var head='<h2 id="'+id+'">'+SCS_esc(src.title)+' <span class="n">'+g.n.toLocaleString()+
   (src.pages?' message'+(g.n===1?'':'s')+' in '+g.parts.length+' conversation'+
    (g.parts.length===1?'':'s'):' row'+(g.n===1?'':'s'))+'</span>';
  if(g.n&&!src.pages)head+=' <a class="openall" target="'+SCS_esc(src.tab)+'" href="'+
   SCS_esc(src.page)+SCQ.findHash(res.ts)+'">Open '+(g.n===1?'it':'all '+g.n.toLocaleString())+
   ' in the report ▸</a>';
  head+='</h2>';
  var miss='<div class="miss">Some of this report&#39;s data files could not be loaded — '+
   'keep each report&#39;s data folder next to it.</div>';
  if(g.missing)head+=miss;
  /* a report with nothing to show is one line at the end, unless its data did not load: then
     "no match" would be a claim the page cannot make */
  if(!g.n){if(g.missing)body+='<section class="none">'+head+'</section>';else none.push(src.title);
   return;}
  var inner='';
  if(src.pages)g.parts.forEach(function(part,pi){
   inner+='<h3>'+SCS_esc(SCS_convTitle(part.it.key,part.it.page))+' <span class="n">'+
    part.hits.length+' message'+(part.hits.length===1?'':'s')+'</span> <a class="openall" target="'+
    SCS_esc(src.tab)+'" href="'+SCS_esc(part.it.page)+SCQ.findHash(res.ts)+'">Open '+
    (part.hits.length===1?'it':'all '+part.hits.length)+' in the conversation ▸</a></h3>'+
    SCS_list(id+'p'+pi,part,res.ts,20);});
  else inner=SCS_list(id,g.parts[0],res.ts,50);
  body+='<section>'+head+inner+'</section>';});
 if(none.length)body+='<div class="nomatch">No match in '+SCS_esc(none.join(', '))+'.</div>';
 document.getElementById('summary').innerHTML=sum;
 document.getElementById('results').innerHTML=body;}
function SCS_jump(id){var e=document.getElementById(id);if(e)e.scrollIntoView();return false;}
function SCS_go(){clearTimeout(SCS_t);SCS_run(document.getElementById('gq').value);return false;}
function SCS_typed(){
 clearTimeout(SCS_t);
 var v=document.getElementById('gq').value.trim();
 if(v.length===1)return;                            /* one character matches nearly every row */
 SCS_t=setTimeout(function(){SCS_run(v);},350);}
/* "#q=<search>" from a report's All reports link, "?q=" from the index page without script. The
   fragment is consumed, as report_ui.NAV_JS does, so the same link clicked again still arrives. */
function SCS_fromUrl(){
 var h=location.hash,q=null;
 if(h&&h.slice(0,3)==='#q='){q=decodeURIComponent(h.slice(3));try{location.hash='_';}catch(e){}}
 else if(!SCS_res){var m=/[?&]q=([^&]*)/.exec(location.search);
  if(m)q=decodeURIComponent(m[1].replace(/\+/g,' '));}
 if(q===null)return;
 document.getElementById('gq').value=q;
 SCS_run(q);}
window.addEventListener('hashchange',SCS_fromUrl);
SCS_fromUrl();
"""

_CSS = """
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif,"Apple Color Emoji","Snapchat Auto Emoji";
   background:#f4f4f8;color:#1b1b1f;margin:0}
 header{background:#2d2d71;color:#fff;padding:18px 26px} header h1{margin:0;font-size:20px}
 header .sub{opacity:.85;font-size:13px;margin-top:4px}
 main{max-width:1100px;padding:16px 26px 40px}
 form.gs{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 form.gs input{font-size:15px;padding:8px 11px;border:1px solid #bcbcd0;border-radius:6px;
   flex:1;min-width:280px}
 form.gs button{font-size:14px;padding:8px 14px;border:1px solid #2d2d71;border-radius:6px;
   background:#2d2d71;color:#fff;font-weight:600;cursor:pointer}
 .how{font-size:12.5px;color:#555;line-height:1.5;margin:8px 0 2px}
 .how code{font-family:ui-monospace,Consolas,monospace;background:#e9e9f2;padding:0 4px;border-radius:3px}
 #status{font-size:12px;color:#777;margin:6px 0 10px;min-height:16px}
 #summary{font-size:13.5px;margin:8px 0 14px;line-height:2}
 #summary .q{font-family:ui-monospace,Consolas,monospace;background:#fff6cc;padding:1px 5px;border-radius:3px}
 .chip{display:inline-block;margin:0 3px;padding:1px 9px;border:1px solid #c9cdf0;border-radius:11px;
   background:#fff;color:#2d2d71;text-decoration:none;font-size:12.5px;line-height:1.7}
 .chip.none{color:#999;border-color:#e2e2ea}
 section{background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 16px;margin-bottom:12px}
 section.none{opacity:.7}
 h2{font-size:15px;margin:2px 0 6px;color:#2d2d71} h3{font-size:13px;margin:12px 0 4px;color:#333}
 .n{color:#777;font-weight:400;font-size:12.5px}
 a.openall{font-size:12px;font-weight:600;color:#25348a;background:#e7ecff;border:1px solid #b9c3f0;
   border-radius:10px;padding:1px 9px;text-decoration:none;margin-left:6px;white-space:nowrap}
 ul.hits{list-style:none;margin:0;padding:0}
 ul.hits li{padding:5px 0;border-top:1px solid #f0f0f5;font-size:13px}
 ul.hits li a{color:#2d2d71;text-decoration:none;font-weight:600;overflow-wrap:anywhere}
 ul.hits li a:hover{text-decoration:underline}
 .snip{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#777;overflow-wrap:anywhere;
   margin-top:2px}
 .snip mark{background:#fff0a8;color:#1b1b1f}
 button.more{margin:6px 0 2px;font-size:12px;padding:3px 10px;border:1px solid #bcbcd0;border-radius:5px;
   background:#fff;color:#2d2d71;cursor:pointer;font-weight:600}
 .miss{font-size:12px;color:#7a1f1f;margin:0 0 6px}
 .nomatch{font-size:12.5px;color:#777;margin:4px 2px 12px}
 .notsearched{font-size:12px;color:#777;margin-top:18px}
"""


def page_html(srcs, *, closure=None, prov=None, not_searched=()):
    """The page for these sources."""
    partial_css, banner, _figures = partial_report.page_chrome(closure, None, prov)
    scope = ("this partial extract" if closure is not None else "this run")
    titles = ", ".join(s["title"] for s in srcs) or "no report"
    missing = ("<div class='notsearched'>Not searched: " + ", ".join(html.escape(t) for t in not_searched)
               + " — single-page reports that keep no row data to search.</div>") if not_searched else ""
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<title>Search all reports</title>{report_ui.emoji_font_link("")}'
        f'<style>{_CSS}{partial_css}</style></head><body>'
        f'<header><h1>Snapchat Auto v{html.escape(app_version.get_version())} &mdash; search all reports'
        f'</h1><div class="sub">Every report of {scope}: {html.escape(titles)}</div></header>'
        f'{banner}<main>'
        '<form class="gs" onsubmit="return SCS_go()">'
        '<input type="search" id="gq" autofocus oninput="SCS_typed()" '
        'placeholder="CACHE_KEY, snap id, hash, file name, URL, user id, words of a message…">'
        '<button type="submit">&#128270; Search</button></form>'
        '<div class="how">The search each report&#39;s own box runs, over every report at once: it '
        'matches the identifiers, hashes, file names, URLs, message text and timestamps each row is '
        'indexed by, not case-sensitive, and <code>a|b</code> finds rows holding either. Each hit opens '
        'its row in its report; <i>Open all</i> opens the report filtered to the same search. The rows '
        'are read from the reports&#39; data folders on the first search, which takes a moment on a '
        'large report.</div>'
        '<div id="status"></div><div id="summary"></div><div id="results"></div>'
        f'{missing}</main>'
        f'<script>var SC_SRC={json.dumps(srcs, ensure_ascii=False).replace("</", "<" + chr(92) + "/")};'
        f'{CORE_JS}{PAGE_JS}</script>'
        '</body></html>')


def write_page(report_dir, *, closure=None, prov=None, platform="ios"):
    """Write ``search.html`` into ``report_dir``; its path, or None when there is no report to search."""
    srcs = sources(report_dir, platform)
    if not srcs:
        return None
    not_searched = [title for rel, title in NOT_SEARCHED
                    if os.path.isfile(os.path.join(report_dir, rel))]
    path = os.path.join(report_dir, PAGE)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page_html(srcs, closure=closure, prov=prov, not_searched=not_searched))
    return path
