"""Which Memories could use a copy from Snapchat's servers, retrieving them, and showing what came back.

:mod:`scripts.cloud_download` does the requesting; this module is the Memories side of it:

* :func:`candidate` says, for each Memory, whether a server copy would add anything — its media is
  **missing** from the extraction, or the copy on the device is **incomplete** (only part of it
  cached, a video with only a still, only the transcoded backup, an overlay the row records but the
  device does not hold) — or whether it has been **retrieved** already. The Memories index filters
  on it, which is how an examiner sees what a retrieval would be for before asking for one.
* :func:`plan_jobs` turns the examiner's request into the files to fetch: the scopes (missing,
  incomplete, a selection file, pasted snap ids) and the **date rules**, which apply to every scope.
* :func:`cloud_phase` runs the engine; :func:`attach` reads what ``CloudDownloads/`` holds back into
  the Memories as ``cloud_files`` — kept apart from ``media_files``, so nothing derived from device
  evidence (groups, hashes, states) can change because of them — and :func:`find_identical` compares
  their bytes with every cached file on the device, which is the one way to prove that an unlinked
  cache file is a given Memory's media.

The method credit is in :mod:`scripts.cloud_download`.
"""
import datetime
import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field

from scripts import cloud_download as cd

logger = logging.getLogger(__name__)

CLOUD_BASIS = (
    "Retrieved from Snapchat's servers — NOT device evidence. The device recorded where this "
    "Memory's media is stored (scdb-27 › ZGALLERYSNAP.ZMEDIADOWNLOADURL / ZMEDIAREDIRECTURI, and "
    "the ZOVERLAY… columns for its overlay). At the examiner's request, and under the legal "
    "authority they recorded, that address was requested; what came back was decrypted with this "
    "Memory's own AES key, the key the device holds for it. The bytes as received and as decrypted "
    "are kept in CloudDownloads/, and every request — when, under which authority, what the server "
    "answered — is in CloudDownloads/cloud_manifest.jsonl. What a server returns on a given day is "
    "not proof of what the device held: compare it with the device's own copies before relying on "
    "it. Method after DFIR-HBG's Snapchat_DownloadMemories_iOS, with overlay retrieval contributed "
    "there by John Hyla (snoop168); that repository carries no licence, so none of its code is used.")

CANDIDATE_BASIS = (
    "Whether a copy from Snapchat's servers could add anything to what the extraction holds. "
    "«media missing» — no full copy of the media was recovered from the device (at most a "
    "thumbnail, a low-resolution render or a poster frame). «local copy incomplete» — what the "
    "device holds is only part of it: a partially cached file, a video with only a still, only the "
    "app's transcoded backup, or an overlay the row records that the device does not hold. «no "
    "download address» — one of those, but the row records no https address to ask for. "
    "«retrieved» — a copy was retrieved from the servers in this run folder. Nothing is requested "
    "unless the examiner asks, under a recorded legal authority.")

STATES = (("missing", "media missing from the extraction"),
          ("incomplete", "local copy incomplete"),
          ("nourl", "missing or incomplete, no download address"),
          ("retrieved", "☁ retrieved from Snapchat's servers"))

#: The addresses for each kind of file, in the order they are tried.
ROLE_COLUMNS = {"media": ("ZMEDIADOWNLOADURL", "ZMEDIAREDIRECTURI"),
                "overlay": ("ZOVERLAYDOWNLOADURL", "ZOVERLAYREDIRECTURI")}
#: Roles of recovered device files that are the media itself (not a thumbnail or a render).
FULL_ROLES = {"full", "cached", "transcoded"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "webm"}

SCOPES = ("missing", "incomplete", "selection", "snaps")
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


# ----------------------------------------------------------------------------------- candidates

def usable_urls(m):
    """``{"media": [(column, url)], "overlay": [...]}`` — the https addresses the row records."""
    urls = m.get("urls") or {}
    out = {}
    for role, columns in ROLE_COLUMNS.items():
        found = [(c, urls[c]) for c in columns
                 if isinstance(urls.get(c), str) and urls[c].lower().startswith("https://")]
        if found:
            out[role] = found
    return out


def candidate(m):
    """``{"state", "reasons", "roles", "keyed"}`` for one Memory (see :data:`CANDIDATE_BASIS`)."""
    files = [f for f in m.get("media_files") or [] if not f.get("generated")]
    full = [f for f in files if f.get("role") in FULL_ROLES]
    reasons, roles = [], []
    if not full:
        reasons.append("no full copy of the media on the device")
        roles.append("media")
    else:
        if any(f.get("complete") is False for f in full):
            reasons.append("the device holds only part of the media")
        is_video = m.get("media_type") == 1
        if is_video and not any((f.get("ext") or "").lower() in VIDEO_EXTS for f in full):
            reasons.append("a video with only a still on the device")
        if all(f.get("role") == "transcoded" for f in full):
            reasons.append("only the app's transcoded backup on the device")
        if reasons:
            roles.append("media")
    if (m.get("urls") or {}).get("ZOVERLAYDOWNLOADURL") \
            and not any(f.get("role") == "overlay" for f in files):
        reasons.append("an overlay the row records is not on the device")
        roles.append("overlay")
    usable = usable_urls(m)
    roles = [r for r in roles if r in usable] if usable else roles
    if m.get("cloud_files"):
        state = "retrieved"
    elif not reasons:
        state = ""
    elif not usable or not roles:
        state = "nourl"
    else:
        state = "missing" if not full else "incomplete"
    return {"state": state, "reasons": reasons, "roles": roles,
            "keyed": bool(m.get("key") and m.get("iv"))}


# ---------------------------------------------------------------------------------- date rules

#: The Memory timestamps a date rule can name: "<table>.<column>", plus the MemData creation time.
MEMDATA_FIELD = "MEMDATA.creationTimeMs"


@dataclass
class DateRule:
    """One row of the date filter: a range (either end open) on one or more timestamps.

    ``fields`` is a tuple of timestamp names ("ZGALLERYSNAP.ZCAPTURETIMEUTC", …) or ``("*",)`` for
    every one; a set of fields shares the range, which is how one range is applied to several
    timestamps without entering it again. ``start`` / ``end`` are aware UTC datetimes or None.
    """
    fields: tuple
    start: datetime.datetime = None
    end: datetime.datetime = None
    mode: str = "include"                                  # include | exclude
    entered: str = ""                                      # as the examiner typed it

    def as_dict(self):
        return {"fields": list(self.fields), "mode": self.mode, "entered": self.entered,
                "start_utc": self.start.isoformat() if self.start else None,
                "end_utc": self.end.isoformat() if self.end else None}


def memory_instants(m):
    """``{field: aware UTC datetime}`` for every timestamp of a Memory a rule can name."""
    out = {}
    for name, value in (m.get("raw_times") or {}).items():
        if isinstance(value, (int, float)) and value:
            out[name] = (datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)
                         + datetime.timedelta(seconds=float(value)))
    created = [rec.get("created_ms") for rec in m.get("memdata") or [] if rec.get("created_ms")]
    if created:
        out[MEMDATA_FIELD] = datetime.datetime.fromtimestamp(min(created) / 1000,
                                                             datetime.timezone.utc)
    return out


def rule_matches(rule, instants):
    """True when any of the rule's fields has a value inside the range. An empty field never does."""
    names = list(instants) if "*" in rule.fields else rule.fields
    for name in names:
        when = instants.get(name)
        if when is None:
            continue
        if (rule.start is None or when >= rule.start) and (rule.end is None or when <= rule.end):
            return True
    return False


def passes(rules, instants):
    """At least one include rule matches (or there is none), and no exclude rule does."""
    includes = [r for r in rules if r.mode == "include"]
    if includes and not any(rule_matches(r, instants) for r in includes):
        return False
    return not any(rule_matches(r, instants) for r in rules if r.mode == "exclude")


def parse_when(text, target, *, end=False):
    """A date or date-time typed in the report's timezone, as an aware UTC datetime.

    ``target`` is the timezone (a tzinfo; None for the examiner machine's local time). A date alone
    means the start of that day, or its last second when ``end`` is set, so ``2024-03-01`` to
    ``2024-03-31`` covers the whole of March.
    """
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d"):
        try:
            naive = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d" and end:
            naive += datetime.timedelta(days=1, seconds=-1)
        aware = naive.astimezone() if target is None else naive.replace(tzinfo=target)
        return aware.astimezone(datetime.timezone.utc)
    raise ValueError(f"not a date: {text!r} (use YYYY-MM-DD or YYYY-MM-DD HH:MM)")


def parse_rules(spec, target, known_fields):
    """Rules from ``"<field[,field…]|*>|<from>|<to>|include;…"`` — the command line's form.

    A field may be named in full ("ZGALLERYSNAP.ZCAPTURETIMEUTC"), by column alone (the snap's
    table is assumed, then the entry's), or ``MEMDATA``. Raises ValueError with the problem.
    """
    rules = []
    for part in [p.strip() for p in (spec or "").split(";") if p.strip()]:
        bits = [b.strip() for b in part.split("|")]
        if len(bits) != 4:
            raise ValueError(f"a date rule is <fields>|<from>|<to>|include or exclude: {part!r}")
        names, start, stop, mode = bits
        mode = mode.lower()
        if mode not in ("include", "exclude"):
            raise ValueError(f"the last part of a date rule is include or exclude: {part!r}")
        fields = tuple(resolve_field(n, known_fields) for n in names.split(",") if n.strip())
        if not fields:
            raise ValueError(f"a date rule names no timestamp: {part!r}")
        rules.append(DateRule(fields, parse_when(start, target), parse_when(stop, target, end=True),
                              mode, part))
    return rules


def resolve_field(name, known_fields):
    name = name.strip()
    if name == "*":
        return "*"
    if name.upper() in ("MEMDATA", MEMDATA_FIELD.upper()):
        return MEMDATA_FIELD
    if "." in name:
        if name in known_fields:
            return name
        raise ValueError(f"unknown timestamp {name!r}")
    for table in ("ZGALLERYSNAP", "ZGALLERYENTRY"):
        if f"{table}.{name.upper()}" in known_fields:
            return f"{table}.{name.upper()}"
    raise ValueError(f"unknown timestamp {name!r}")


def known_fields(memories):
    """Every timestamp name a rule can use on these Memories, in a stable order."""
    names = set()
    for m in memories.values():
        names.update(memory_instants(m))
        names.update(k for k in (m.get("raw_times") or {}))
    return sorted(names) + ([MEMDATA_FIELD] if MEMDATA_FIELD not in names else [])


# ------------------------------------------------------------------------------------- planning

@dataclass
class CloudRequest:
    """What the examiner asked for. Built by the GUI or the command line, never remembered."""
    authority: cd.Authority
    scopes: set = field(default_factory=lambda: {"missing"})
    selection_ids: set = field(default_factory=set)        # snap ids from a selection file
    snap_ids: set = field(default_factory=set)              # snap ids pasted in
    selection_path: str = ""
    date_rules: list = field(default_factory=list)
    date_spec: str = ""                                     # the rules as entered, for the record
    tz: str = "local"                                       # the timezone dates are entered in
    overlays: bool = True
    redownload: bool = False
    pace: cd.Pace = field(default_factory=cd.Pace)
    allow_hosts: tuple = ()
    entry_point: str = "run"
    control: object = None
    on_event: object = None
    runner: object = None                                   # callable(run) -> Summary (a GUI)
    fetcher: object = None                                  # tests replace the network here

    def problems(self):
        out = list(self.authority.problems()) + self.pace.problems()
        if not self.scopes:
            out.append("nothing to retrieve was chosen")
        if "selection" in self.scopes and not self.selection_ids:
            out.append("the selection file names no Memory")
        if "snaps" in self.scopes and not self.snap_ids:
            out.append("no snap id was given")
        return out

    def scope_record(self):
        return {"scopes": sorted(self.scopes), "selection": self.selection_path,
                "selection_memories": len(self.selection_ids), "snap_ids": sorted(self.snap_ids),
                "overlays": self.overlays, "redownload": self.redownload}


def snap_ids_from_text(text):
    """``(ids, rejected)`` from pasted text: UUIDs separated by anything, upper-cased."""
    ids, rejected = [], []
    for token in re.split(r"[\s,;]+", text or ""):
        token = token.strip().strip("'\"")
        if not token:
            continue
        if token.lower().startswith("mem-"):
            token = token[4:]
        if _UUID.fullmatch(token):
            ids.append(token.upper())
        else:
            rejected.append(token)
    return list(dict.fromkeys(ids)), rejected


def snap_ids_from_selection(path):
    """The snap ids of the Memories a saved selection file names."""
    from scripts import selection_file
    payload = selection_file.read_selection(path)
    mem = (payload.get("selections") or {}).get("mem") or {}
    out = set()
    for row_id, rec in mem.items():
        sid = (rec.get("snap") if isinstance(rec, dict) else None) or (
            row_id[4:] if row_id.startswith("mem-") else "")
        if sid:
            out.add(str(sid).upper())
    return out


def plan_jobs(memories, request):
    """``(jobs, plan)``: the files to fetch and the counts the examiner is shown."""
    by_upper = {str(sid).upper(): sid for sid in memories}
    wanted, why = [], {}
    for sid, m in memories.items():
        cand = m.get("cloud") or candidate(m)
        reasons = []
        if "missing" in request.scopes and cand["state"] == "missing":
            reasons.append("missing")
        if "incomplete" in request.scopes and cand["state"] == "incomplete":
            reasons.append("incomplete: " + "; ".join(cand["reasons"]))
        if "selection" in request.scopes and str(sid).upper() in request.selection_ids:
            reasons.append("in the selection file")
        if "snaps" in request.scopes and str(sid).upper() in request.snap_ids:
            reasons.append("requested by snap id")
        if reasons:
            wanted.append(sid)
            why[sid] = reasons
    unknown = sorted((request.snap_ids | request.selection_ids) - set(by_upper))
    plan = {"in_scope": len(wanted), "unknown_ids": unknown, "dropped_by_dates": 0,
            "no_key": 0, "no_url": 0, "rules": []}
    instants = {sid: memory_instants(memories[sid]) for sid in wanted}
    for rule in request.date_rules:
        # how many Memories in scope have no value for any of the rule's timestamps: the rule cannot
        # match them, and the examiner should know how many that is
        empty = sum(1 for sid in wanted
                    if not (instants[sid] if "*" in rule.fields
                            else [f for f in rule.fields if f in instants[sid]]))
        plan["rules"].append({"rule": rule.entered or rule.as_dict(), "no_value": empty})
    jobs = []
    for sid in wanted:
        m = memories[sid]
        if request.date_rules and not passes(request.date_rules, instants[sid]):
            plan["dropped_by_dates"] += 1
            continue
        if not (m.get("key") and m.get("iv")):
            plan["no_key"] += 1
            continue
        usable = usable_urls(m)
        roles = ["media"]
        if request.overlays and "ZOVERLAYDOWNLOADURL" in (m.get("urls") or {}):
            roles.append("overlay")
        added = False
        for role in roles:
            if usable.get(role):
                jobs.append(cd.Job(str(sid), role, usable[role], "; ".join(why[sid])))
                added = True
        if not added:
            plan["no_url"] += 1
    plan["jobs"] = len(jobs)
    plan["memories"] = len({j.snap_id for j in jobs})
    return jobs, plan


def decryptor(memories, decrypt_sccontent):
    """The engine's decrypt callback, with the Memories' own keys."""
    def decrypt(job, raw):
        m = memories.get(job.snap_id) or {}
        if not raw:
            return {"result": "empty"}
        padded, stripped, ext, tail_ok = decrypt_sccontent(raw, m.get("key"), m.get("iv"))
        if padded is None:
            return {"result": "no-key" if not m.get("key") else "not-media"}
        return {"result": "plaintext" if tail_ok is None else "ok", "ext": ext, "tail_ok": tail_ok,
                "data": stripped}
    return decrypt


def all_fields():
    """Every timestamp a date rule can name, whatever the schema (see TIMESTAMP_FIELDS)."""
    from scripts.memories_media_report import TIMESTAMP_FIELDS
    return list(TIMESTAMP_FIELDS) + [MEMDATA_FIELD]


def rules_from_spec(spec, tz):
    """The command line's date rules, read in the run's timezone. Raises ValueError."""
    from scripts.memories_media_report import resolve_tz
    return parse_rules(spec, resolve_tz(tz)[0], all_fields())


def cloud_phase(memories, run_folder, request, decrypt_sccontent):
    """Plan and run one retrieval. Returns the engine's :class:`~cloud_download.Summary` or None."""
    problems = request.problems()
    if problems:
        raise ValueError("; ".join(problems))
    if request.date_spec and not request.date_rules:
        request.date_rules = rules_from_spec(request.date_spec, request.tz)
    jobs, plan = plan_jobs(memories, request)
    logger.info(f"Snapchat's servers: {plan['in_scope']} Memory/Memories in scope, "
                f"{plan['dropped_by_dates']} left out by the date rules, {plan['no_key']} without a "
                f"usable key, {plan['no_url']} without an https address — {plan['jobs']} file(s) to "
                f"retrieve for {plan['memories']} Memory/Memories")
    for unknown in plan["unknown_ids"]:
        logger.warning(f"  snap id {unknown}: no such Memory in this run")
    if not jobs:
        return None
    policy = cd.HostPolicy(cd.DEFAULT_HOST_SUFFIXES + tuple(request.allow_hosts))
    fetcher = request.fetcher or cd.UrllibFetcher(policy, cd.default_user_agent())
    control = request.control or cd.Control(request.pace)
    engine = cd.Engine(cd.Store(run_folder), fetcher, control, request.authority,
                       on_event=request.on_event or _log_event)

    def run():
        return engine.run(jobs, decrypt=decryptor(memories, decrypt_sccontent),
                          redownload=request.redownload,
                          scope=dict(request.scope_record(), plan=plan),
                          entry_point=request.entry_point,
                          date_rules=[r.as_dict() for r in request.date_rules])
    summary = request.runner(run) if request.runner else run()
    if summary is None:
        return None
    logger.info(f"Snapchat's servers: {summary.done} retrieved, {summary.failed} failed, "
                f"{summary.skipped} already retrieved ({summary.stopped})")
    return summary


def _log_event(event):
    if event.kind in ("item_done", "item_failed", "item_skipped"):
        job = event.job
        logger.info(f"  [{event.done + event.failed + event.skipped}/{event.total}] "
                    f"{job.snap_id} {job.role}: {event.text}")
    elif event.kind == "item_start" and event.eta_s:
        if (event.done + event.failed) % 10 == 0:
            logger.info(f"  … about {event.eta_s / 60:.0f} min to go at this pace")
    elif event.kind == "log":
        logger.info(f"  {event.text}")


# --------------------------------------------------------------------------- what came back

def attach(memories, run_folder, media_dir):
    """Fill ``m["cloud_files"]`` from ``CloudDownloads/`` and publish them under ``media/cloud/``.

    Only retrievals that succeeded and decrypted are attached; the latest per Memory and role. The
    published file is a hard link to the decrypted copy where the volume allows, else a copy.
    """
    store = cd.Store(run_folder)
    if not os.path.exists(store.manifest):
        return 0
    sessions = {}
    latest = {}
    for rec in store.records():
        if rec.get("type") == "session":
            sessions[rec["session"]] = rec
        elif rec.get("type") == "request" and rec.get("http_status") == 200 \
                and (rec.get("decrypt") or {}).get("stored"):
            latest[(rec["snap_id"], rec["role"])] = rec
    problems = store.verify()
    if problems:
        logger.warning(f"Snapchat's servers: {store.manifest} does not verify — "
                       f"{problems[0]} ({len(problems)} problem(s)); its records are shown as read")
    cloud_dir = os.path.join(media_dir, "cloud")
    attached = 0
    by_upper = {str(sid).upper(): sid for sid in memories}
    for (snap_id, role), rec in sorted(latest.items()):
        sid = by_upper.get(str(snap_id).upper())
        if sid is None:
            continue
        dec = rec["decrypt"]
        src = os.path.join(store.base, dec["stored"])
        if not os.path.exists(src):
            continue
        os.makedirs(cloud_dir, exist_ok=True)
        name = f"{sid}_{role}_{rec.get('seq', 1)}.{dec.get('ext') or 'bin'}"
        dest = os.path.join(cloud_dir, name)
        if not os.path.exists(dest):
            try:
                os.link(src, dest)
            except OSError:
                shutil.copyfile(src, dest)
        session = sessions.get(rec.get("session")) or {}
        memories[sid].setdefault("cloud_files", []).append({
            "role": role, "out": f"cloud/{name}", "path": f"media/cloud/{name}",
            "ext": dec.get("ext") or "", "bytes": dec.get("bytes"),
            "hashes": [("", dec.get("md5", ""), dec.get("sha256", ""))],
            "encrypted": {"bytes": rec.get("bytes"), "sha256": rec.get("sha256_encrypted"),
                          "md5": rec.get("md5_encrypted"), "stored": rec.get("stored_encrypted")},
            "retrieved_utc": rec.get("utc_end") or rec.get("utc_start"),
            "url_column": rec.get("url_column"), "url": rec.get("url"),
            "final_url": rec.get("final_url"), "http_status": rec.get("http_status"),
            "headers": rec.get("headers") or {}, "session": rec.get("session"),
            "authority": rec.get("authority") or session.get("authority") or {},
            "decrypt": dec.get("result"), "tail_ok": dec.get("tail_ok"),
            "reason": rec.get("reason", ""), "same_as_device": [], "identical_cached": []})
        attached += 1
    if attached:
        logger.info(f"Snapchat's servers: {attached} retrieved file(s) attached to their Memories")
    return attached


#: The order a file's proofs are listed and chosen in: the device's own copy of a Memory's media is
#: device evidence, a copy from Snapchat's servers is not.
PROOF_ORDER = {"device": 0, "decrypted": 1, "encrypted": 2}


def find_identical(memories, scfull, scparts, resolve, extra_files=(), size_of=None):
    """Compare the cached files on the device with every Memory media whose bytes are known; record
    each cache file that is byte-identical to one.

    Two references: the media **this run recovered from the device** for each Memory (``media_files``
    — decrypted with the Memory's own key, or stored plain), and what was **retrieved from Snapchat's
    servers** (``cloud_files``: decrypted, and as received). Either identifies a cache file that no
    identifier connects to the Memory — the snap editor's working copy of a snap later saved is
    byte-identical to its Memory's media — and the device's own copy needs no retrieval at all. The
    device's copies are never compared with a cache file some Memory's media was recovered from: that
    file is linked by its identifiers already, and identical media puts Memories in one group anyway.

    ``resolve(cache_key)`` returns ``(bytes, …)`` as ``_resolve_sccontent`` does; ``size_of(cache_key)``,
    when given, says how many bytes it would return, so a file rebuilt from byte-range parts is only
    read when its size could match. Sizes first throughout: only a file of a size some reference has is
    hashed. ``extra_files`` adds other files to compare against (``[(label, path)]``). Returns
    ``{cache_key or label: [record]}``, each list in :data:`PROOF_ORDER`.
    """
    targets = {}
    own = {str(f.get("cache_key") or "").lower() for m in memories.values()
           for f in m.get("media_files") or [] if f.get("cache_key")}
    for m in memories.values():
        for f in m.get("media_files") or []:
            if f.get("generated") or not f.get("bytes") or not f.get("hashes"):
                continue
            if f["hashes"][0][2]:
                targets.setdefault(("device", f["bytes"]), []).append(
                    (m, f, f["hashes"][0][2], own))
        for f in m.get("cloud_files") or []:
            if f.get("bytes"):
                targets.setdefault(("decrypted", f["bytes"]), []).append(
                    (m, f, f["hashes"][0][2], set()))
            enc = f.get("encrypted") or {}
            if enc.get("bytes"):
                targets.setdefault(("encrypted", enc["bytes"]), []).append(
                    (m, f, enc.get("sha256"), set()))
    if not targets:
        return {}
    sizes = {size for _what, size in targets}
    found = {}

    def check(key, data_or_path, size):
        hits = [(what, m, f, sha, own) for (what, sz), lst in targets.items() if sz == size
                for m, f, sha, own in lst if str(key).lower() not in own]
        if not hits:
            return
        if isinstance(data_or_path, str):
            digest = _sha256_file(data_or_path)
        else:
            digest = hashlib.sha256(data_or_path).hexdigest()
        for what, m, f, sha, _own in hits:
            if not sha or digest != sha:
                continue
            recs = found.setdefault(key, [])
            if any(r["snap_id"] == m["snap_id"] and r["what"] == what for r in recs):
                continue                         # the same bytes recovered twice for one Memory
            if what == "device":
                rec = {"snap_id": m["snap_id"], "role": f.get("role"), "what": what, "sha256": sha,
                       "bytes": size, "source": f.get("source", ""),
                       "from": f.get("cache_key") or "/".join(
                           x for x in (f.get("folder"), f.get("item")) if x)}
            else:
                rec = {"snap_id": m["snap_id"], "role": f["role"], "what": what, "sha256": sha,
                       "bytes": size, "retrieved_utc": f.get("retrieved_utc"),
                       "session": f.get("session"),
                       "authority_note": (f.get("authority") or {}).get("note", "")}
            recs.append(rec)
            f.setdefault("identical_cached", []).append({"cache_key": key, "what": what})

    full_keys = {str(k).lower() for k in scfull}
    for key, paths in scfull.items():
        for path in paths[:1]:
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size in sizes:
                check(key, path, size)
    for key in {k for k in scparts if str(k).lower() not in full_keys}:
        if size_of is not None:
            try:
                if size_of(key) not in sizes:
                    continue
            except Exception:                               # noqa: BLE001
                continue
        try:
            data = resolve(key)[0]
        except Exception:                                   # noqa: BLE001
            data = None
        if data and len(data) in sizes:
            check(key, data, len(data))
    for label, path in extra_files:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size in sizes:
            check(label, path, size)
    for recs in found.values():
        recs.sort(key=lambda r: PROOF_ORDER.get(r["what"], 9))
    return found


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_manifests(memories, outdir, identical=None):
    """``cloud_media.json`` (what the reports and index.html say about retrieved media) and
    ``media_by_content.json`` (the cache files proven identical, for the cache reports).

    ``media_by_content.json`` carries ``by_cache_key`` (:func:`find_identical`'s result) and
    ``by_sha256``: the SHA-256 of every Memory media whose bytes are known — recovered from the device
    and retrieved from the servers — for a report that hashes its own files (Library/Caches)."""
    retrieved = {sid: [{k: f.get(k) for k in ("role", "out", "retrieved_utc", "url_column",
                                               "http_status", "session", "authority", "decrypt")}
                       for f in m["cloud_files"]]
                 for sid, m in memories.items() if m.get("cloud_files")}
    path = os.path.join(outdir, "cloud_media.json")
    if retrieved:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "memories": retrieved, "provenance": provenance(memories)},
                      fh, indent=1)
    elif os.path.exists(path):
        os.remove(path)
    # by_sha256 lets a report that hashes its own files (Library/Caches) find them without a cache key
    by_sha256 = {}
    for sid, m in memories.items():
        for f in m.get("media_files") or []:
            sha = (f.get("hashes") or [("", "", "")])[0][2]
            if sha and not f.get("generated") and f.get("bytes"):
                rec = {"snap_id": sid, "role": f.get("role"), "what": "device",
                       "source": f.get("source", ""),
                       "from": f.get("cache_key") or "/".join(
                           x for x in (f.get("folder"), f.get("item")) if x)}
                if not any(r["snap_id"] == sid for r in by_sha256.get(sha, ())):
                    by_sha256.setdefault(sha, []).append(rec)
        for f in m.get("cloud_files") or []:
            common = {"snap_id": sid, "role": f.get("role"), "retrieved_utc": f.get("retrieved_utc"),
                      "session": f.get("session"),
                      "authority_note": (f.get("authority") or {}).get("note", "")}
            if f.get("hashes") and f["hashes"][0][2]:
                by_sha256.setdefault(f["hashes"][0][2], []).append(dict(common, what="decrypted"))
            enc = (f.get("encrypted") or {}).get("sha256")
            if enc:
                by_sha256.setdefault(enc, []).append(dict(common, what="encrypted"))
    path = os.path.join(outdir, "media_by_content.json")
    if identical or by_sha256:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "by_cache_key": identical or {}, "by_sha256": by_sha256}, fh,
                      indent=1)
    elif os.path.exists(path):
        os.remove(path)


def provenance(memories):
    """What a report must say about server-retrieved media it contains, or None when there is none."""
    files = [f for m in memories.values() for f in m.get("cloud_files") or []]
    if not files:
        return None
    sessions = {}
    for f in files:
        auth = f.get("authority") or {}
        sessions.setdefault(f.get("session"), {"note": auth.get("note", ""),
                                               "attested_utc": auth.get("attested_utc", ""),
                                               "session": f.get("session")})
    times = sorted(f.get("retrieved_utc") or "" for f in files if f.get("retrieved_utc"))
    return {"memories": sum(1 for m in memories.values() if m.get("cloud_files")),
            "files": len(files), "sessions": list(sessions.values()),
            "first_utc": times[0] if times else "", "last_utc": times[-1] if times else ""}
