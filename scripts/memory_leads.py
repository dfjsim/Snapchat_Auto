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
to the file or is not (``cloud_memories.find_identical``).

``ZDURATION`` is not used: it has been seen to differ from the media's real length.
"""
import bisect

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
    "it can differ from the media's real length. To prove or rule out a lead, retrieve that Memory "
    "from Snapchat's servers (copy its snap id below): when the decrypted copy is byte-identical to "
    "this file, the file becomes linked to it, proven by content.")


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
