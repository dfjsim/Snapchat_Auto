"""The Library/Caches files linked to a Memory, listed on the Memory's own page.

The Library/Caches report links a cached file to a Memory four ways (``cache_media_report.attribute``):
a caching-media pack the Memories report decrypted with the Memory's key, a file keyed by the CDN URL
of one of the Memory's media, and a file byte-identical to the Memory's media — as this run recovered
it from the device, or as it was retrieved from Snapchat's servers. Only the first is something the
Memories report sees for itself, and even those vanished from its page when the same bytes had also
come from an SCContent file. So a Library/Caches row could say "Memory …" while the Memory's page
named no such file.

The Library/Caches report renders after the Memories report, so — as with the cache_controller
report's leads (``memory_leads``) — it writes what it linked as :data:`SCRIPT_NAME`, keyed by snap id,
and the Memory's page loads it with ``<script src>`` and lists every row with how it was linked. The
list is exactly the Library/Caches report's own links: one decision, shown at both ends.
"""
import json
import os

#: Written by the Library/Caches report into its ``data/`` folder; read by the Memory pages.
SCRIPT_NAME = "memory_links.js"

#: How each kind of link reads on the Memory's page.
HOW = {
    "pack": "caching-media pack decrypted with this Memory's key — its media is listed under Media "
            "files",
    "url": "keyed by the CDN URL of this Memory's media",
    "device": "≡ byte-identical to this Memory's media, as recovered from the device",
    "cloud": "☁ byte-identical to the copy retrieved from Snapchat's servers",
}

BASIS = (
    "Every Library/Caches row the Library/Caches report links to this Memory, with the reason it "
    "gives. It is that report's decision, listed here so the link can be seen from both ends: a "
    "caching-media pack it saw this report decrypt with the Memory's key; a file whose name is the "
    "CDN URL of the Memory's media; or a file byte-identical to the Memory's media. Copies are the "
    "files with those same bytes under Library/Caches; open the row for their paths.")


def how_of(link):
    """Which of :data:`HOW` a Library/Caches memory link is."""
    if link.get("how"):
        return link["how"]
    if link.get("by_content"):
        return link["by_content"]
    return "pack" if link.get("media_path") is not None else "url"


def by_memory(entries, keep=None):
    """``{snap id (upper case): [row]}`` for the Library/Caches entries' Memory links."""
    out = {}
    for entry in entries:
        anchor = f"cm-{entry.get('sha256') or entry.get('rel')}"
        for link in entry.get("links") or ():
            if link.get("kind") != "memory":
                continue
            sid = str(link["snap_id"])
            if keep is not None and not keep(sid):
                continue
            rows = out.setdefault(sid.upper(), [])
            if any(r["a"] == anchor for r in rows):
                continue
            rows.append({"a": anchor, "rel": entry.get("rel", ""), "n": len(entry.get("copies") or ()),
                         "ext": entry.get("ext") or "", "bytes": entry.get("bytes") or 0,
                         "how": how_of(link), "basis": link.get("basis", "")})
    for rows in out.values():
        rows.sort(key=lambda r: (list(HOW).index(r["how"]) if r["how"] in HOW else 9, r["rel"]))
    return out


def script_text(entries, run_id="", keep=None):
    payload = {"run": run_id or "", "basis": BASIS, "how": HOW, "by_snap": by_memory(entries, keep)}
    return ("SCMemoryCacheMedia(" + json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")) + ");\n")


def write_script(data_dir, entries, run_id="", keep=None):
    """Write :data:`SCRIPT_NAME` — always, even empty, so a re-render never leaves an older run's."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, SCRIPT_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(script_text(entries, run_id, keep))
    return path


LOADER_JS = "function SCMemoryCacheMedia(d){window.SC_MCM=d;}"

#: Fills the Memory page's ``#memcm`` from the data file; uses ``scLeadEsc``/``scLeadHint`` from
#: memory_leads.MEMORY_JS, which every Memory page carries.
PAGE_JS = r"""
function scCacheMediaPage(prefix){
 var host=document.getElementById('memcm'),d=window.SC_MCM;
 if(!host||!d||!d.by_snap||(d.run&&window.SCAUTO_RUN&&d.run!==window.SCAUTO_RUN))return 0;
 var sids=(host.getAttribute('data-snaps')||'').split(' ').filter(Boolean),multi=sids.length>1,
     body='',n=0,seen={};
 sids.forEach(function(s){
  (d.by_snap[s.toUpperCase()]||[]).forEach(function(r){
   if(!multi&&seen[r.a])return;
   seen[r.a]=1;n++;
   body+='<tr>'+(multi?'<td class="mono">'+scLeadEsc(s)+'</td>':'')+
    '<td><a class="cclink" target="scauto_cachemedia" href="'+prefix+
    'CacheMedia/CacheMedia_report.html#'+encodeURIComponent(r.a)+'">🗂 '+scLeadEsc(r.rel)+
    '</a></td><td>'+r.n+'</td><td>'+scLeadEsc(String(r.ext||'').toUpperCase())+'</td><td>'+
    Math.floor((r.bytes||0)/1024)+' KB</td><td>'+scLeadEsc((d.how||{})[r.how]||r.how)+
    scLeadHint(r.basis)+'</td></tr>';});});
 if(!n)return 0;
 host.innerHTML='<div class="sect">Library/Caches — files linked to this Memory'+scLeadHint(d.basis)+
  '</div><table class="files"><tr>'+(multi?'<th>Snap</th>':'')+'<th>File (Library/Caches row)</th>'+
  '<th>Copies</th><th>Type</th><th>Size</th><th>Linked by</th></tr>'+body+'</table>';
 return n;}
"""
