"""Reading, writing and installing the examiner's selection file.

A report opened from ``file://`` cannot write to disk, and browsers give each ``file://`` page its own
private, tab-scoped storage — so what makes a selection last is **a file the examiner saves**
(``report_ui.SELECT_JS`` documents the measurements behind that). This module is the other side of
that exchange: everything Python needs to read such a file back, verify it, and put it where the
reports will load it.

Two forms of one payload:

* ``selection.json`` — plain JSON, and the **default** the reports offer, because browsers flag a
  ``.js`` download as dangerous and may refuse it outright. This is the copy to keep with the case
  and the file to hand back to the tool.
* ``selection.js`` — the same payload wrapped in ``SCSel.preload(…)``, which is what a report
  auto-loads with ``<script src>``.

A bare ``.json`` renamed to ``selection.js`` does **not** work, and fails silently: JSON at
statement position is a syntax error the browser discards without a word, leaving the reports open
with nothing selected and no indication why. :func:`install_selection` exists so nobody has to try —
the tool converts and places the file itself.

Stdlib only, and it imports nothing outside the standard library and nothing from Snapchat Auto. That
is deliberate and it is enforced by a test: this is the surface an external tool needs in order to
produce a selection of its own, so it must be installable on its own — without pandas, OpenCV, a GUI
toolkit or the rest of the application. See :mod:`snapchat_auto_selection.api` for the typed
selection-building surface layered on top of this one, and ``docs/selection_format.md`` for the
normative description of the file itself.

The application imports this module too (``scripts/selection_file.py`` re-exports it), so there is one
implementation of the format and nothing to keep in sync.
"""

import os
import re
import json
import copy
import hashlib
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Bumped from 1 to 2 for: message ids qualified with their conversation (a bare "msg-12.0" named a
# different message in every chat), per-row key records instead of a bare 1, and the `sources` /
# `tool_version` provenance a partial run verifies against. Readers accept 1; writers emit CURRENT.
SCHEMA = 2
SCHEMA_MIN = 1
SCHEMA_MAX = 2

TOOL = "Snapchat_Auto"

#: The selection kinds, and the report each belongs to.
KINDS = {
    "conv": "Conversations",
    "msg": "Conversations",
    "ct": "Contacts",
    "mem": "Memories",
    "cc": "CacheController",
    "cm": "CacheMedia",
}


class SelectionFormatError(ValueError):
    """The file is not a Snapchat Auto selection file, or is one we cannot read."""


# --------------------------------------------------------------------------- reading

def parse_selection_text(text):
    """Parse either form of the selection file and return its payload dict.

    Accepts the wrapped ``SCSel.preload({…});`` form, bare JSON, a UTF-8 BOM, CRLF line endings and
    a leading comment block — i.e. everything the browser or an examiner's editor may produce. The
    payload is located the same way the in-browser loader locates it, by taking the outermost
    ``{ … }``, so the two cannot disagree about what a file means.
    """
    if text is None:
        raise SelectionFormatError("The selection file is empty.")
    body = text.lstrip("﻿")
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        # ASCII: this reaches a Windows console, whose code page mangles an em dash into a
        # replacement character right where the examiner is trying to read what went wrong.
        raise SelectionFormatError(
            "That file contains no selection data. Expected JSON, or a selection.js holding "
            "SCSel.preload({...}).")
    try:
        payload = json.loads(body[start:end + 1])
    except ValueError as error:
        raise SelectionFormatError(f"The selection data is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise SelectionFormatError("The selection data is not an object.")
    # A schema-1 file written by hand may be just the selections map, with no envelope.
    if "selections" not in payload:
        if not all(isinstance(v, dict) for v in payload.values()):
            raise SelectionFormatError(
                'The selection data has no "selections" and is not a bare selections map.')
        payload = {"selections": payload}
    if not isinstance(payload.get("selections"), dict):
        raise SelectionFormatError('"selections" is not an object.')
    schema = payload.get("schema") or 1
    if not isinstance(schema, int) or schema < SCHEMA_MIN:
        raise SelectionFormatError(f"Unrecognised selection schema {schema!r}.")
    if schema > SCHEMA_MAX:
        raise SelectionFormatError(
            f"That selection file is schema {schema}; this build of {TOOL} reads up to "
            f"{SCHEMA_MAX}. Use a newer build, or re-save the selection from this one's reports.")
    payload["schema"] = schema
    return payload


def read_selection(path):
    """:func:`parse_selection_text` on a file, with the path named in any error."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as error:
        raise SelectionFormatError(f"Could not read {path}: {error}") from error
    try:
        return parse_selection_text(text)
    except SelectionFormatError as error:
        raise SelectionFormatError(f"{path}: {error}") from error


def selection_counts(payload):
    """``{kind: number of rows}`` for the selections in *payload*, kinds with none omitted."""
    out = {}
    for kind, rows in (payload.get("selections") or {}).items():
        if rows:
            out[kind] = len(rows)
    return out


# --------------------------------------------------------------------------- writing

def _preamble(payload):
    run = payload.get("run_id") or "default"
    return (f"/* {TOOL} — examiner selections for run {run}.\n"
            "   Keep this file as <report folder>/selection.js so every report of this run loads it.\n"
            "   It is also a plain record you can file with the case.\n"
            f"   Written by {TOOL} (selection schema {payload.get('schema', SCHEMA)}). */\n")


def selection_js_text(payload):
    """The drop-in ``selection.js`` text for *payload* — what a report loads with ``<script src>``."""
    return _preamble(payload) + "SCSel.preload(" + json.dumps(payload, indent=1) + ");\n"


def selection_json_text(payload):
    """The plain-JSON text for *payload* — the form to keep with the case."""
    return json.dumps(payload, indent=1) + "\n"


def selection_digest(payload):
    """A stable digest of *what is selected*, independent of which form the file took.

    Hashing the file itself would give two different answers for the same selection saved as
    ``.json`` and as ``.js`` (and a third if the timestamp moved), which is no use for saying "this
    partial report was built from that selection". This hashes the selections alone, canonically
    ordered, so the provenance can quote one value and have it mean something.
    """
    canon = json.dumps(payload.get("selections") or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- migration

_QUALIFIED_MSG = re.compile(r".+\|msg-")


def migrate_selection(payload):
    """Return ``(schema-2 payload, unattributed message ids)``.

    Before schema 2 a message selection was a bare page anchor (``msg-12.0``). Message numbers
    restart in every conversation, so that id matched a different message in every chat — ticking one
    message marked several. Such an id names no conversation and **cannot be attributed to one
    here**: it is split out rather than guessed at, and the caller decides (see
    ``partial_report``'s ``--legacy-msg-ids``, which resolves it against the parsed chat only when
    exactly one conversation contains it, and otherwise refuses).

    Everything else is carried across unchanged, including a bare ``1`` where no key record was
    recorded.
    """
    out = copy.deepcopy(payload)
    out["schema"] = SCHEMA
    out.setdefault("tool", TOOL)
    selections = out.get("selections") or {}
    unattributed = []
    msgs = selections.get("msg") or {}
    if msgs:
        kept = {}
        for mid, value in msgs.items():
            if _QUALIFIED_MSG.match(mid):
                kept[mid] = value
            else:
                unattributed.append(mid)
        selections["msg"] = kept
    out["selections"] = selections
    if unattributed:
        logger.warning(
            f"{len(unattributed)} message selection(s) predate per-conversation message ids and "
            f"name a message number with no conversation: {', '.join(sorted(unattributed)[:5])}"
            + (" ..." if len(unattributed) > 5 else ""))
    return out, unattributed


# --------------------------------------------------------------------------- installing

def install_selection(report_dir, source_path, *, force=False, backup=True, run_id=None):
    """Write *source_path*'s selections into ``<report_dir>/selection.js``.

    This is the way a saved ``.json`` becomes the file the reports auto-load. A browser cannot do it
    (a ``file://`` page cannot write to disk) and renaming will not do it either — the reports load
    the file as a script, and bare JSON is a syntax error the browser swallows in silence.

    Returns ``(path, info)``. Refuses a selection saved for a *different* run unless ``force``,
    because installing another case's selection into a report folder is the mistake worth blocking
    rather than reporting afterwards.
    """
    payload = read_selection(source_path)
    if payload.get("tool") and payload["tool"] != TOOL:
        raise SelectionFormatError(
            f"{source_path}: that file says it was written by {payload['tool']!r}, not {TOOL}.")

    if run_id is None:
        run_id = _read_run_id(report_dir)
    theirs = payload.get("run_id") or ""
    mismatch = bool(run_id and theirs and theirs != run_id)
    if mismatch and not force:
        raise SelectionFormatError(
            f"That selection was saved for run {theirs}, and {report_dir} is run {run_id}. Its ids "
            f"may name nothing in these reports. Re-run with force to install it anyway.")

    payload, unattributed = migrate_selection(payload)
    payload.setdefault("installed", datetime.now().astimezone().isoformat(timespec="seconds"))
    payload["installed_from"] = os.path.basename(source_path)

    target = os.path.join(report_dir or ".", "selection.js")
    backed_up = None
    if backup and os.path.isfile(target):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backed_up = f"{target}.bak-{stamp}"
        try:
            os.replace(target, backed_up)
        except OSError as error:            # a failed backup must not cost the examiner the old file
            raise SelectionFormatError(
                f"Could not set the existing {target} aside ({error}); nothing was changed.") from error

    try:
        os.makedirs(report_dir or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(selection_js_text(payload))
    except OSError as error:
        raise SelectionFormatError(f"Could not write {target}: {error}") from error

    info = {"counts": selection_counts(payload), "digest": selection_digest(payload),
            "schema_in": payload.get("schema"), "run_id": theirs, "report_run_id": run_id,
            "run_id_mismatch": mismatch, "unattributed_msg_ids": unattributed,
            "backup": backed_up, "source_sha256": file_sha256(source_path)}
    return target, info


def file_sha256(path):
    """SHA-256 of a file, or "" when it cannot be read — the selection file as supplied."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return ""


def _read_run_id(report_dir):
    """The run id in ``<report_dir>/run_id.txt``, or "" when there is none.

    Read directly rather than through ``report_ui.run_id``, which *creates* the file when it is
    missing — installing a selection must not invent a run id for a folder that has none.
    """
    try:
        with open(os.path.join(report_dir or ".", "run_id.txt"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""
