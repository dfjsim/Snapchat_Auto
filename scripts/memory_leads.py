"""Possible Memory — a lead, never a link: which Memories a cached file *might* be the media of.

The reports link a cached file to a Memory only on an identifier or on the bytes. Some files have
neither: a snap editor's working copy of a snap that was later saved to Memories is byte-identical to
that Memory's media once decrypted, yet nothing recorded on the device connects the two. What the
device does record is **time** — when the app claimed the file, when the filesystem says it was
written and read, when the Memory was created — and a cached video whose claim was made in the same
second a video Memory was saved is worth looking at.

So this lists such coincidences as leads, ranked, with every difference shown, and says how many
Memories fell inside the window — a lead among forty is not a lead among one. It is never a link: it
is never drawn as one, never counted as one, never followed by a partial report. The way to prove or
rule out a lead is the retrieval from Snapchat's servers, whose decrypted copy either is byte-identical
to the file or is not (``cloud_memories.find_identical``). When the Memory's media is on the device, that
comparison has already been made with the device's own copy — and a match is a link, not a lead.

``ZDURATION`` is not used: it has been seen to differ from the media's real length.

A lead is shown on **both** ends: on the cached file's row in the cache_controller report, and on the
Memory's own page and index row in the Memories report. The leads are worked out once, by the
cache_controller report — only it knows which files nothing else accounts for — and it renders after
the Memories report, so the Memory side cannot be baked into those pages. The cache_controller report
writes :data:`SCRIPT_NAME` next to its rows instead, and the Memories pages load it the way every page
loads its data, with ``<script src>``: :data:`MEMORY_JS` turns it into the index badge, the index
filter and the panel on the Memory's page. A page whose run wrote no such file shows no lead.
"""
import bisect
import json
import os

#: How far apart a file time and a Memory time may be (seconds) and still make a lead.
LEAD_WINDOW_S = 600
#: How many leads a file lists.
MAX_LEADS = 5

LEAD_BASIS = (
    "NOT a link — a lead. No identifier on the device and no comparison of bytes connects this file "
    "to these Memories. They are listed because a time recorded for the file (when the app claimed "
    "it — CACHE_FILE_CLAIM.CREATION_TIMESTAMP_MILLIS — or when the device's filesystem says it was "
    "created, modified or last read) falls within {window} of a time recorded for the Memory (its "
    "ZGALLERYSNAP creation or capture time, or its album entry's creation), and the file is the same "
    "kind of media (video or image) as the Memory. Every difference is shown, with how many Memories "
    "fell inside the window: a coincidence among many Memories means little. ZDURATION is not used — "
    "it can differ from the media's real length. Where this run recovered a Memory's media from the "
    "device, it has already been compared with this file, and it is not identical. To prove or rule "
    "out a lead, retrieve that Memory from Snapchat's servers (copy its snap id below): when the "
    "decrypted copy is byte-identical to this file, the file becomes linked to it, proven by content.")


def _window_text(seconds):
    return f"{seconds // 60} minutes" if seconds % 60 == 0 else f"{seconds} seconds"


def basis(window_s=LEAD_WINDOW_S):
    return LEAD_BASIS.format(window=_window_text(window_s))


def kind_of_ext(ext):
    """``video`` / ``image`` / None for a file extension from sniff."""
    ext = (ext or "").lower()
    if ext in ("mp4", "mov", "m4v", "webm", "3gp"):
        return "video"
    if ext in ("jpg", "jpeg", "png", "webp", "heic", "gif"):
        return "image"
    return None


def find_leads(files, memories, window_s=LEAD_WINDOW_S, max_leads=MAX_LEADS):
    """``{file key: {"leads": [...], "in_window": n}}`` for the files that have any.

    ``files``    ``{key: {"kind": video|image|None, "points": [(label, unix seconds)], "ctx19": bool}}``
    ``memories`` ``{snap id: {"kind": video|image|None, "points": [(label, unix seconds)]}}``

    A Memory is a candidate for a file when both kinds are known and equal, and at least one file
    time and one Memory time are within ``window_s`` of each other. Each lead lists every such pair;
    leads are ranked by their closest pair.
    """
    timeline = sorted((t, sid, label) for sid, m in memories.items()
                      for label, t in m.get("points") or () if t)
    times = [t for t, _sid, _label in timeline]
    out = {}
    for key, f in files.items():
        if not f.get("kind"):
            continue
        pairs = {}
        for flabel, ft in f.get("points") or ():
            if not ft:
                continue
            lo = bisect.bisect_left(times, ft - window_s)
            hi = bisect.bisect_right(times, ft + window_s)
            for mt, sid, mlabel in timeline[lo:hi]:
                if memories[sid].get("kind") != f["kind"]:
                    continue
                pairs.setdefault(sid, []).append(
                    {"file": flabel, "memory": mlabel, "delta_s": round(mt - ft, 3)})
        if not pairs:
            continue
        leads = []
        for sid, plist in pairs.items():
            plist.sort(key=lambda p: abs(p["delta_s"]))
            leads.append({"snap_id": sid, "best_delta_s": plist[0]["delta_s"], "pairs": plist,
                          "kind": f["kind"], "ctx19": bool(f.get("ctx19"))})
        leads.sort(key=lambda lead: (abs(lead["best_delta_s"]), lead["snap_id"]))
        out[key] = {"leads": leads[:max_leads], "in_window": len(leads), "window_s": window_s}
    return out


# ------------------------------------------------------------------------- the Memory's side

#: Written by the cache_controller report into its ``data/`` folder; read by the Memories pages.
SCRIPT_NAME = "memory_leads.js"

MEMORY_BASIS = (
    "NOT a link — a lead: the same one the cache_controller report lists on the cached file's row. "
    "No identifier on the device and no comparison of bytes connects these cached files to this "
    "Memory. They are listed because a time recorded for the file (when the app claimed it, or when "
    "the device's filesystem says it was created, modified or last read) falls within {window} of a "
    "time recorded for this Memory, the file is the same kind of media, and nothing else — no Memory "
    "and no chat — accounts for the file. Each file says how many Memories fell inside its window and "
    "where this one ranks among them: a coincidence among many Memories means little. ZDURATION is "
    "not used — it can differ from the media's real length. If this run recovered this Memory's media "
    "from the device, it has already been compared with these files and is not identical to them. To "
    "prove or rule out a lead, retrieve this Memory from Snapchat's servers: when the decrypted copy "
    "is byte-identical to the file, the file becomes linked to this Memory, proven by content.")


def memory_basis(window_s=LEAD_WINDOW_S):
    return MEMORY_BASIS.format(window=_window_text(window_s))


def by_memory(found_by_file, keep=None):
    """Turn :func:`find_leads`' result round: ``{snap id (upper case): [file lead]}``.

    Each file lead is what the file's own panel says about that Memory, compacted: ``ck`` the file's
    CACHE_KEY, ``d`` the closest difference, ``p`` the pairs (``[file time, Memory time, Memory minus
    file]``, at most four, as the file's panel shows them), ``rank``/``of`` where the Memory stands
    among the leads the file lists, ``n`` how many Memories fell in the file's window. A Memory beyond
    the file's :data:`MAX_LEADS` closest is not one of its leads on either side. ``keep(snap id)``
    limits it to the Memories a partial extract holds: no page of it can ask about another.
    """
    out = {}
    for key, found in (found_by_file or {}).items():
        if not found:
            continue
        leads = found.get("leads") or []
        for rank, lead in enumerate(leads, 1):
            if keep is not None and not keep(lead["snap_id"]):
                continue
            out.setdefault(str(lead["snap_id"]).upper(), []).append({
                "ck": key, "kind": lead.get("kind"), "d": lead["best_delta_s"],
                "p": [[p["file"], p["memory"], p["delta_s"]] for p in lead["pairs"][:4]],
                "rank": rank, "of": len(leads), "n": found.get("in_window", len(leads)),
                "c19": bool(lead.get("ctx19"))})
    for files in out.values():
        files.sort(key=lambda x: (abs(x["d"]), x["ck"]))
    return out


def script_text(found_by_file, run_id="", window_s=LEAD_WINDOW_S, keep=None):
    """The text of :data:`SCRIPT_NAME`: one call, so a page that loads it learns everything at once."""
    payload = {"run": run_id or "", "window_s": window_s, "basis": memory_basis(window_s),
               "by_snap": by_memory(found_by_file, keep)}
    return ("SCMemoryLeads(" + json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")) + ");\n")


def write_script(data_dir, found_by_file, run_id="", keep=None):
    """Write :data:`SCRIPT_NAME` — always, even empty, so a re-render never leaves an older run's."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, SCRIPT_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(script_text(found_by_file, run_id, keep=keep))
    return path


#: Defined before the data file loads, so the file's one call has somewhere to put what it carries.
LOADER_JS = "function SCMemoryLeads(d){window.SC_MLEADS=d;}"

#: What the Memories pages do with it. ``scLeadsIndex`` marks the index rows (a badge in one cell,
#: words in the search text, ``pcf`` in the filter metadata) and reveals the filter; ``scLeadsPage``
#: fills the Memory page's panel. Neither ever adds a link to the Memory's own records: the badge and
#: the panel point at the cached files, the way the file's panel points at its Memories.
MEMORY_JS = r"""
function scLeadData(){
 var d=window.SC_MLEADS;
 /* a data file left by another run in the same folder says nothing about this one */
 if(!d||!d.by_snap||(d.run&&window.SCAUTO_RUN&&d.run!==window.SCAUTO_RUN))return null;
 return d;}
function scLeadEsc(s){return String(s===undefined||s===null?'':s).replace(/[&<>"']/g,function(c){
 return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function scLeadSigned(s){var g=s>=0?'+':'\u2212';s=Math.abs(s);
 return s<10?g+s.toFixed(1)+' s':s<120?g+s.toFixed(0)+' s':g+(s/60).toFixed(1)+' min';}
function scLeadHint(text){return '<span class="hint"><span class="qm" onclick="hint(event,this)">?</span>'+
 '<span class="tip">'+scLeadEsc(text)+'</span></span>';}
/* The badge: a link to the cached files in the cache_controller report, all of them at once. */
function scLeadBadge(prefix,keys,text,title){
 return '<a class="pcf" target="scauto_cache" href="'+prefix+'CacheController/CacheController_report.html'+
  '#find='+keys.map(encodeURIComponent).join('|')+'" title="'+scLeadEsc(title)+'">'+text+'</a>';}
function scLeadsIndex(prefix,cell){
 var d=scLeadData();
 if(!d||typeof SCV==='undefined')return 0;
 var mins=Math.round(d.window_s/60),group={};
 var n=SCV.annotate(function(r){
  var files=d.by_snap[String(r[0]).slice(4).toUpperCase()];
  if(!files||!files.length)return false;
  var keys=files.map(function(f){return f.ck;}),many=files.length>1;
  r[1][cell]+=scLeadBadge(prefix,keys,'≈ '+(many?files.length+' possible files':'possible file'),
   'NOT proven: '+files.length+' cached file'+(many?'s':'')+' that nothing else accounts for '+
   (many?'have':'has')+' a time within '+mins+' minutes of this Memory\'s. Opens '+
   (many?'them':'it')+' in the cache_controller report; the Memory\'s page lists every difference.');
  r[2]+=' possible cached file lead not proven';
  r[5].pcf='y';
  /* a Memory folded behind another is out of sight while the fold is on: its group's row says so */
  var lead=r[5].lead;
  if(lead&&lead!==r[0])group[lead]=(group[lead]||[]).concat(keys);
  return true;});
 SCV.annotate(function(r){
  var keys=group[r[0]];
  if(!keys||r[5].pcf)return false;
  r[1][cell]+=scLeadBadge(prefix,keys,'≈ possible file (grouped)',
   'NOT proven: a Memory grouped in this row has a cached file that nothing else accounts for '+
   'within '+mins+' minutes of its times. Open the row to see which Memory.');
  return true;});
 if(n){
  var o=document.getElementById('pcfy'),l=document.getElementById('pcfl');
  if(o)o.textContent='with a possible cached file ('+n+')';
  if(l)l.style.display='';}
 return n;}
function scLeadsPage(prefix){
 var host=document.getElementById('memleads'),d=scLeadData();
 if(!host||!d)return 0;
 var sids=(host.getAttribute('data-snaps')||'').split(' ').filter(Boolean),multi=sids.length>1,
     rows=[],ids=[];
 sids.forEach(function(s){
  var files=d.by_snap[s.toUpperCase()]||[];
  if(files.length)ids.push(s);
  files.forEach(function(f){rows.push([s,f]);});});
 if(!rows.length)return 0;
 var mins=Math.round(d.window_s/60),body='';
 rows.forEach(function(x){
  var s=x[0],f=x[1],pairs=f.p.map(function(p){
   return scLeadEsc(p[0])+' vs '+scLeadEsc(p[1])+': '+scLeadSigned(p[2]);}).join('<br>');
  body+='<tr>'+(multi?'<td class="mono">'+scLeadEsc(s)+'</td>':'')+
   '<td><a class="cclink" target="scauto_cache" href="'+prefix+
   'CacheController/CacheController_report.html#ck-'+scLeadEsc(f.ck)+'">🗄 '+
   scLeadEsc(f.ck)+'</a>'+(f.c19?'<div class="muted">claimed as Memories media (context 19)</div>':'')+
   '</td><td>'+scLeadEsc(f.kind||'')+'</td><td>'+scLeadSigned(f.d)+'</td>'+
   '<td class="mono">'+pairs+'</td><td>'+(f.rank===1?'closest':'#'+f.rank)+' of the '+f.of+
   ' it lists'+(f.n>f.of?' ('+f.n+' in its window)':'')+'</td></tr>';});
 var offer=host.getAttribute('data-offer')==='1'?
  ' <button class="copyids" onclick=\'scCopySnapIds('+scLeadEsc(JSON.stringify(ids))+
  ',"this Memory")\'>📋 Copy snap ID'+(ids.length>1?'s':'')+'</button> <span class="muted">'+
  'to retrieve '+(ids.length>1?'them':'it')+' from Snapchat&#39;s servers and compare</span>':'';
 host.innerHTML='<div class="sect">Possible cached file — NOT proven'+scLeadHint(d.basis)+
  '</div><div class="leadnote">'+rows.length+' cached file'+(rows.length>1?'s':'')+
  ' that nothing else accounts for '+(rows.length>1?'have':'has')+' a time within '+mins+
  ' minutes of '+(multi?'a Memory of this page':'this Memory')+'&#39;s and '+
  (rows.length>1?'are':'is')+' the same kind of media.'+offer+'</div>'+
  '<table class="files"><tr>'+(multi?'<th>Snap</th>':'')+'<th>Cached file</th><th>Kind</th>'+
  '<th>Closest</th><th>File time vs Memory time (Memory minus file)</th>'+
  '<th>This Memory among the file&#39;s leads</th></tr>'+body+'</table>';
 return rows.length;}
"""

#: The badge on the Memories index and the panel on a Memory's page, in the cache report's lead colours.
MEMORY_CSS = """
 .vcells>.vc .pcf{background:#fffbe6;color:#6b5a00;border:1px dashed #c9a400;border-radius:3px;
   font-size:9px;font-weight:700;letter-spacing:.04em;padding:0 4px;margin-top:3px;display:inline-block;
   text-decoration:none;text-transform:uppercase}
 .vcells>.vc .pcf:hover{background:#fff3c0}
 .leadnote{font-size:12px;color:#6b5a00;margin:4px 0}
 .leadnote button.copyids{font-size:11.5px;padding:2px 8px;border:1px solid #bcbcd0;border-radius:5px;
   background:#fff;cursor:pointer;font-weight:600;color:#2d2d71;margin-left:6px}
"""
