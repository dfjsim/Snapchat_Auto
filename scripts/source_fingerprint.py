"""What a run consumed, hashed — so a later run can prove it is looking at the same evidence.

A partial report is a subset of a report the examiner ticked rows in, and the two are only comparable
if they were built from the same extraction. So a normal run records the artifacts it read (path,
size, MD5, SHA-256) plus the tool version, the reports show that on their index page, the examiner's
saved selection carries a copy, and a partial run checks it before doing anything.

Two verdicts come out of that, and they answer different questions:

* :func:`verify` — *is this the same evidence?* A difference is worth stopping for, but the examiner
  may legitimately have a better extraction of the same device and choose to go on.
* :func:`check_version` — *was it read by the same build?* A newer Snapchat_Auto may extract more
  paths from the ZIP, decrypt media an older one could not, or classify bytes differently. Reusing an
  older run's output under a newer build would hide exactly those improvements, so
  :func:`reuse_allowed` requires **both** an identical version and identical artifacts.

Scope, stated rather than implied: these are the databases, plists and the keychain — the artifacts
that decide *what the reports contain*. Cached media files are **not** fingerprinted here; every one
of them already carries its MD5 and SHA-256 in the cache reports. The extraction ZIP is recorded by
path, size and mtime always, and hashed only on request: tens of GB is a long sequential read, and it
is the per-artifact hashes that actually bind the output.

Each database is fingerprinted **with its ``-wal`` and ``-shm`` sidecars**, because
``scripts/data/sqlite_open.py`` reads every database twice — with the log applied and without it — and
marks the rows only one reading contains. The log is therefore not incidental: it is where the deleted
and superseded rows come from (see docs/sqlite_wal_handling.md).

So a differing log is a **real difference in the evidence**, and is treated as one. It gets its own
verdict line — which says precisely *what* differs rather than blaming the database — but it is never
presented as harmless. Two logs can leave the current rows identical while recovering a different set
of deleted rows, and "the live data looks the same" is not a reason to trust it.
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timezone

from scripts import app_version

logger = logging.getLogger(__name__)

SOURCES_FILE = "sources.json"
SOURCES_SCHEMA = 1

#: Sidecars a SQLite database is fingerprinted with.
#:
#: The ``-wal`` and not the ``-shm``, deliberately. The log holds committed pages that have not been
#: checkpointed, which is where the recovered deleted and superseded rows come from — evidence. The
#: ``-shm`` is only the shared-memory *index* over that log: it holds no data of its own, is rebuilt
#: from the log, and ``sqlite_open`` goes out of its way never to create one beside the original.
#:
#: Measured, and the reason this is not merely tidiness: two runs of the same build on the same
#: extraction leave the ``-shm`` of the Memories databases with a **new mtime**, because something on
#: that path still opens the original in place. Fingerprinting it would therefore have the tool fail
#: its own verification over a file it modified itself — the precise false alarm that would teach an
#: examiner to wave a sidecar difference through, when a differing ``-wal`` is the one thing here that
#: must never be waved through.
SIDECARS = ("-wal",)

#: The roles a caller may report, in the order they are shown. The caller passes the paths it already
#: resolved rather than this module re-globbing for them — one place decides where an artifact lives,
#: and it is the parser that has to find it anyway.
ROLES = (
    ("arroyo", "arroyo.db", "chat messages and conversations", True),
    ("scdb", "scdb-27.sqlite3", "Memories index", True),
    ("gallery_encrypteddb", "gallery.encrypteddb", "Memories keys / geolocation (old schema)", True),
    ("cache_controller", "cache_controller.db", "the cached-file index", True),
    ("contentmanager", "contentManagerDb.db", "cached content metadata", True),
    ("primary_docobjects", "primary.docobjects", "contacts / friends", False),
    ("user_plist", "user.plist", "the account's own identifiers", False),
    ("client_encryption", "ClientEncryptionService.plist", "the story-cache AES key", False),
    ("group_plist", "group.snapchat.picaboo.plist", "friends / groups", False),
    ("keychain", "keychain", "the keychain / keystore supplied for this run", False),
)

_ROLE_INFO = {name: (label, why, db) for name, label, why, db in ROLES}


# --------------------------------------------------------------------------- hashing

def _hashes(path):
    """``(md5, sha256, bytes)`` of a file in one pass."""
    md5, sha = hashlib.md5(), hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            md5.update(block)
            sha.update(block)
            total += len(block)
    return md5.hexdigest(), sha.hexdigest(), total


def fingerprint(path, *, hash_bytes=True):
    """One artifact's record, or a *present: False* marker when it is not there.

    A missing artifact is recorded rather than omitted: "this run had no gallery.encrypteddb" is a
    finding, and a later run that suddenly has one is a difference worth showing.
    """
    if not path:
        return {"present": False, "path": "", "why": "not located"}
    try:
        stat = os.stat(path)
    except OSError as error:
        return {"present": False, "path": str(path), "why": f"{error.strerror or error}"}
    record = {
        "present": True,
        "path": str(path).replace("\\", "/"),
        "name": os.path.basename(path),
        "bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
    }
    if hash_bytes:
        try:
            record["md5"], record["sha256"], _n = _hashes(path)
        except OSError as error:
            record["why"] = f"could not be read: {error.strerror or error}"
            record["present"] = False
    return record


def _with_sidecars(path):
    """An artifact plus its ``-wal``/``-shm``, each fingerprinted in its own right."""
    record = fingerprint(path)
    sidecars = {}
    for suffix in SIDECARS:
        side = f"{path}{suffix}" if path else ""
        if side and os.path.exists(side):
            sidecars[suffix] = fingerprint(side)
    if sidecars:
        record["sidecars"] = sidecars
    return record


# --------------------------------------------------------------------------- collecting

def collect(artifacts, *, zip_path="", keychain_path="", hash_zip=False, extra=None):
    """Fingerprint every artifact a run used. ``artifacts`` is ``{role: path}``.

    Roles not in :data:`ROLES` are accepted and recorded — better an unexpected role in the manifest
    than a source the run read and did not declare.
    """
    out = {
        "schema": SOURCES_SCHEMA,
        "tool": "Snapchat_Auto",
        "tool_version": app_version.get_version(),
        "collected": datetime.now().astimezone().isoformat(timespec="seconds"),
        "artifacts": {},
        "note": ("The databases, plists and keychain this run read. Cached media files are not "
                 "listed here: each one's MD5 and SHA-256 is in the cache reports."),
    }
    for role, path in (artifacts or {}).items():
        is_db = _ROLE_INFO.get(role, ("", "", False))[2]
        record = _with_sidecars(path) if is_db else fingerprint(path)
        label, why = _ROLE_INFO.get(role, (role, ""))[:2]
        record["label"], record["why_it_matters"] = label, why
        out["artifacts"][role] = record

    if keychain_path is not None and "keychain" not in out["artifacts"]:
        record = fingerprint(keychain_path)
        record["label"], record["why_it_matters"] = _ROLE_INFO["keychain"][:2]
        out["artifacts"]["keychain"] = record

    if zip_path:
        # Hashed only on request: a 50 GB sequential read to restate what the per-artifact hashes
        # already bind. Its identity (name, size, mtime) is always worth recording.
        out["zip"] = fingerprint(zip_path, hash_bytes=bool(hash_zip))
        out["zip"]["hashed"] = bool(hash_zip)
    if extra:
        out.update(extra)
    out["digest"] = digest(out)
    return out


def digest(sources):
    """A single value standing for "these artifacts".

    Over the sorted ``role:sha256`` pairs, so it does not move when the manifest gains a field, when
    an artifact is recorded in a different order, or when the run was made. Sidecars are included:
    they are part of what the databases read.
    """
    parts = []
    for role, record in sorted((sources.get("artifacts") or {}).items()):
        parts.append(f"{role}:{record.get('sha256', '-') if record.get('present') else 'absent'}")
        for suffix, side in sorted((record.get("sidecars") or {}).items()):
            parts.append(f"{role}{suffix}:{side.get('sha256', '-')}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def compact(sources):
    """The part of a manifest worth carrying inside every report page.

    Two reasons this is a projection rather than the whole thing:

    * **Reproducibility.** The full manifest carries ``collected`` — when the run happened — so
      embedding it made every page of every report differ between two runs of the same build on the
      same evidence. A report is a work product; two runs must produce the same one.
    * **Size.** The full manifest is several KB, and it would be repeated in every conversation page
      and every Memory sub-page — megabytes of identical JSON on a device with a few hundred Memories.

    What survives is identity: per artifact whether it was present, where it was, and its hashes;
    plus the tool version and the digest. That is exactly what :func:`verify` compares and what the
    tool needs in order to offer the same paths back.
    """
    if not sources:
        return None
    out = {"tool_version": sources.get("tool_version"), "digest": sources.get("digest"),
           "artifacts": {}}
    for role, record in (sources.get("artifacts") or {}).items():
        small = {"present": bool(record.get("present")), "path": record.get("path", "")}
        for field in ("md5", "sha256"):
            if record.get(field):
                small[field] = record[field]
        sidecars = {suffix: {"sha256": side.get("sha256", "")}
                    for suffix, side in (record.get("sidecars") or {}).items()}
        if sidecars:
            small["sidecars"] = sidecars
        out["artifacts"][role] = small
    z = sources.get("zip") or {}
    if z.get("path"):
        out["zip"] = {"path": z.get("path"), "name": z.get("name", ""),
                      "bytes": z.get("bytes", 0), "hashed": bool(z.get("hashed"))}
        if z.get("sha256"):
            out["zip"]["sha256"] = z["sha256"]
    return out


def write_sources(report_dir, sources):
    """Write ``<report_dir>/sources.json``; return its path (or "" when it could not be written)."""
    path = os.path.join(report_dir or ".", SOURCES_FILE)
    try:
        os.makedirs(report_dir or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sources, fh, indent=1)
            fh.write("\n")
        return path
    except OSError as error:
        logger.warning(f"Could not write {path}: {error}")
        return ""


def read_sources(report_dir):
    """Read ``<report_dir>/sources.json``, or ``None`` when there is none."""
    try:
        with open(os.path.join(report_dir or ".", SOURCES_FILE), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- verifying

MATCH = "match"
DIFFERS = "differs"
MISSING_NOW = "missing-now"
NEW_NOW = "new-now"
UNKNOWN = "unknown"

_BAD = (DIFFERS, MISSING_NOW, NEW_NOW)


class Verdict:
    """The outcome of comparing two source manifests, or two tool versions.

    ``ok`` is the only thing callers should branch on; ``lines`` is what they show. A verdict with
    nothing to compare is **not** ok-by-default — it reports :data:`UNKNOWN`, because "we could not
    check" and "it matched" must never read the same.
    """

    def __init__(self, kind, ok, lines, summary, comparable=True):
        self.kind = kind
        self.ok = ok
        self.lines = lines
        self.summary = summary
        self.comparable = comparable

    @property
    def problems(self):
        return [line for line in self.lines if line["status"] in _BAD]

    def as_dict(self):
        return {"kind": self.kind, "ok": self.ok, "comparable": self.comparable,
                "summary": self.summary, "lines": self.lines}

    def __bool__(self):
        return self.ok


def _line(role, what, status, expected, found, note=""):
    return {"role": role, "what": what, "status": status,
            "expected": expected, "found": found, "note": note}


def _sha(record):
    if not record:
        return ""
    return record.get("sha256", "") if record.get("present") else "absent"


def verify(expected, found):
    """Compare the source manifest a selection carries against this run's.

    Sidecars get their own line so the examiner is told *what* differs rather than being handed one
    verdict for a whole database. That is a diagnosis, not a discount: a differing ``-wal`` fails the
    verdict exactly as a differing database does. The log is where the recovered deleted and
    superseded rows come from, so two logs can agree on every current row and still disagree about
    what was deleted.
    """
    if not expected or not (expected.get("artifacts")):
        return Verdict("sources", False, [], comparable=False,
                       summary=("No source fingerprints to check against — this selection was saved "
                                "before the tool recorded them, so it cannot be verified that it "
                                "came from this extraction."))

    exp_art = expected.get("artifacts") or {}
    got_art = (found or {}).get("artifacts") or {}
    lines = []
    for role in sorted(set(exp_art) | set(got_art)):
        exp, got = exp_art.get(role), got_art.get(role)
        label = (exp or got or {}).get("label") or role
        if exp is None:
            lines.append(_line(role, label, NEW_NOW, "not recorded", _sha(got),
                               "this run read an artifact the earlier one did not"))
            continue
        if got is None:
            lines.append(_line(role, label, MISSING_NOW, _sha(exp), "not recorded",
                               "the earlier run read an artifact this one did not"))
            continue
        if not exp.get("present") and not got.get("present"):
            lines.append(_line(role, label, MATCH, "absent", "absent",
                               "absent from both runs"))
        elif not got.get("present"):
            lines.append(_line(role, label, MISSING_NOW, _sha(exp), "absent",
                               got.get("why") or "not found in this extraction"))
        elif not exp.get("present"):
            lines.append(_line(role, label, NEW_NOW, "absent", _sha(got),
                               "present now, absent then"))
        elif exp.get("sha256") == got.get("sha256"):
            lines.append(_line(role, label, MATCH, exp.get("sha256"), got.get("sha256")))
        else:
            lines.append(_line(role, label, DIFFERS, exp.get("sha256"), got.get("sha256"),
                               "different bytes"))

        for suffix in SIDECARS:
            e = (exp.get("sidecars") or {}).get(suffix)
            g = (got.get("sidecars") or {}).get(suffix)
            if e is None and g is None:
                continue
            what = f"{label} {suffix}"
            # ASCII: this note is shown on a page *and* printed to a console, and the console
            # code page turns an em dash into a replacement character.
            note = ("The log is where the deleted and superseded rows come from, so a different "
                    "log is different evidence even when the current rows are identical. Do not "
                    "treat this as harmless because the database file matches.")
            if e is None:
                lines.append(_line(role, what, NEW_NOW, "not recorded", _sha(g), note))
            elif g is None:
                lines.append(_line(role, what, MISSING_NOW, _sha(e), "absent", note))
            elif e.get("sha256") == g.get("sha256"):
                lines.append(_line(role, what, MATCH, e.get("sha256"), g.get("sha256")))
            else:
                lines.append(_line(role, what, DIFFERS, e.get("sha256"), g.get("sha256"), note))

    bad = [line for line in lines if line["status"] in _BAD]
    n_match = sum(1 for line in lines if line["status"] == MATCH)
    if not bad:
        summary = f"source artifacts verified: {n_match} of {len(lines)} identical"
    else:
        kinds = sorted({line["status"] for line in bad})
        summary = (f"{len(bad)} of {len(lines)} source artifact(s) do not match "
                   f"({', '.join(kinds)})")
    return Verdict("sources", not bad, lines, summary)


def check_version(expected, running=None):
    """Compare the build that produced a selection against the one running now.

    Exact, ``+build.<N>`` included. The point is that a build which extracts more paths or decrypts
    more successfully must not silently inherit an older build's output, and a build tag is precisely
    what distinguishes two otherwise identical versions.
    """
    running = running or app_version.get_version()
    if not expected:
        return Verdict("version", False, [], comparable=False,
                       summary=("The selection does not record which build produced it, so nothing "
                                f"can be reused from an earlier run. Running {running}."))
    same = expected == running
    line = _line("tool_version", "Snapchat_Auto version", MATCH if same else DIFFERS,
                 expected, running,
                 "" if same else ("A different build may extract more paths from the ZIP or decrypt "
                                  "media this one cannot, so nothing an earlier run produced can be "
                                  "reused."))
    summary = (f"same build: {running}" if same else
               f"the selection was made with {expected}; this is {running}")
    return Verdict("version", same, [line], summary)


def reuse_allowed(version_verdict, sources_verdict, *, no_reuse=False):
    """Whether a run may reuse anything an earlier one extracted or decrypted.

    Both must hold: the same build, and byte-identical artifacts. Given both, re-deriving would be
    deterministic, which is what makes reuse equivalent to re-derivation rather than a shortcut. Given
    either in doubt, everything is re-derived from the evidence.
    """
    if no_reuse:
        return False, "reuse was switched off for this run"
    if not (version_verdict and version_verdict.ok):
        return False, "a different build produced the earlier run"
    if not (sources_verdict and sources_verdict.ok):
        if sources_verdict is not None and not sources_verdict.comparable:
            return False, "the earlier run's source artifacts were never recorded"
        return False, "the source artifacts are not identical"
    return True, "same build and identical source artifacts"


# --------------------------------------------------------------------------- presenting
#
# Only the plain-text form lives here. Each consumer presents the manifest in its own
# idiom -- the run index in its key/value rows, a partial report's provenance alongside the
# verdict -- and a shared HTML renderer would have to be all of them at once.

_STATUS_TEXT = {MATCH: "same", DIFFERS: "DIFFERS", MISSING_NOW: "MISSING NOW",
                NEW_NOW: "NEW NOW", UNKNOWN: "not checked"}


def verdict_text(verdict):
    """The verdict as plain lines, for the log and the run's own record."""
    out = [verdict.summary]
    for line in verdict.lines:
        if line["status"] == MATCH:
            continue                                   # the log names what is wrong, not what is fine
            # ASCII: this goes to a Windows console, whose code page turns an em dash into a
        # replacement character right where the examiner is reading what went wrong.
        out.append(f"  {line['what']}: {_STATUS_TEXT.get(line['status'], line['status'])}"
                   f" -- recorded {_short(line['expected'])}, found {_short(line['found'])}"
                   + (f" ({line['note']})" if line["note"] else ""))
    return "\n".join(out)


def _short(value):
    value = str(value or "")
    return value if len(value) <= 16 else value[:16] + "..."
