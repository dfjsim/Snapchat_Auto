"""Building a Snapchat Auto selection from evidence identifiers.

The point of this module is that an integrator has **identifiers** — a conversation id out of
``arroyo.db``, a ``ZSNAPID`` out of ``scdb-27``, a ``CACHE_KEY`` out of ``cache_controller.db`` — and
should never have to know how Snapchat Auto spells its row anchors. Hand-building
``conv-<id>|msg-12.0`` from the documentation is the failure mode this module exists to prevent: a
message id is qualified with its conversation because a server message id is a per-conversation
ordinal, and an integrator who does not know that produces a selection naming a message in every chat.

So: call :func:`anchor_for`, or better, use :class:`SelectionBuilder` and pass the identifiers you
have. Each ``add_*`` records the alternate keys automatically, which is what lets a selection built
here resolve exactly like one saved from the reports — the whole reason the alternates are part of the
format rather than an internal detail.

What this module does **not** know, and cannot:

* whether the run that will consume the selection has the rows you named — that is answered when the
  partial report is built, and it refuses rather than silently dropping them;
* the relation vocabulary and the field keys the *installed executable* supports. Those come from
  ``Snapchat_Auto --describe-selection-api``, which is the handshake to run before writing a file:
  pinning a version of this package does not remove mismatch, it relocates it.

Stdlib only. Nothing here imports Snapchat Auto.
"""

from . import format as _format
from .format import (KINDS, SCHEMA, SCHEMA_MAX, SCHEMA_MIN, TOOL, SelectionFormatError,
                     selection_counts, selection_digest, selection_js_text, selection_json_text)

#: The version of *this Python surface*, separate from :data:`SCHEMA` (the file format). The two move
#: independently: a new method here does not change what a file looks like, and a new optional field in
#: the file does not change the calls. An integrator checks both.
API_VERSION = 1


#: What identifiers each kind is built from. The first of each tuple is the one the anchor is made of;
#: the rest travel with the row as alternates, and are what let it be found again if the primary moves.
IDENTIFIERS = {
    "conv": {"primary": ("conversation_id",), "alternates": ("server_id",),
             "note": "arroyo.db client_conversation_id — a device-assigned UUID."},
    "msg": {"primary": ("conversation_id", "server_message_id"), "alternates": ("ts", "sender"),
            "note": "A server message id is a per-conversation ordinal, so it is only meaningful "
                    "together with its conversation. ts is unix SECONDS — arroyo.db stores "
                    "creation_timestamp in milliseconds, so divide it. sender is the sender's "
                    "permanent user id (arroyo.db conversation_message.sender_id), matched "
                    "case-insensitively and the only accepted spelling — the displayed name is not a "
                    "key, being only what the device knew at extraction time. ts and sender matter "
                    "for one case: a message with no server id yet, which is anchored on its "
                    "position in the conversation and so has an id that can move."},
    "ct": {"primary": ("user_id",), "alternates": ("username", "conversation_id"),
           "note": "The permanent user id when known. A contact with none is anchored on its "
                   "username, then on a conversation id, so those travel too."},
    "mem": {"primary": ("snap_id",), "alternates": ("media_id", "entry_id", "cache_keys"),
            "note": "ZGALLERYSNAP.ZSNAPID — the only one of the three that is unique to one Memory "
                    "row. ZMEDIAID names the media object a group's members share by design and "
                    "ZENTRYID an album entry that can cover several snaps, so neither is a substitute "
                    "for the snap id; they travel because they are free once you have read scdb-27. "
                    "cache_keys is a list of cache_controller.db "
                    "CACHE_KEY values the media was recovered from (for CDN media, "
                    "sha256(<CDN URL token>)[:32]) and is the only identifier available to a tool "
                    "that never read scdb-27 — snap_id may be omitted when it is given, and the row "
                    "id is then a placeholder the run resolves through the key. A cache key names a "
                    "file and one file can belong to several Memories, so it is matched last and "
                    "reported rather than guessed at when it does not name exactly one."},
    "cc": {"primary": ("cache_key",), "alternates": ("sha256",),
           "note": "A CACHE_KEY in cache_controller.db. sha256 is of the cached bytes as stored."},
    "cm": {"primary": ("sha256",), "alternates": ("raw_sha256", "rel"),
           "note": "SHA-256 of the *recovered* content, which is what the report identifies the row "
                   "by — so a build that decodes differently moves it. Every copy's raw SHA-256 and "
                   "the path under Library/Caches travel with it."},
}


def anchor_for(kind, **identifiers):
    """The canonical row id Snapchat Auto uses for one item. Raises ``ValueError`` on a bad call.

    ``anchor_for("mem", snap_id="…")`` -> ``"mem-…"``;
    ``anchor_for("msg", conversation_id="…", server_message_id="12.0")`` -> ``"conv-…|msg-12.0"``.

    Asserted against the generators in this repository's tests, so the two cannot drift.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind '{kind}'. Known: {', '.join(sorted(KINDS))}")
    get = lambda name: str(identifiers.get(name) or "").strip()          # noqa: E731

    if kind == "conv":
        value = get("conversation_id")
        _need(value, kind, "conversation_id")
        return f"conv-{value}"
    if kind == "msg":
        conv, smid = get("conversation_id"), get("server_message_id")
        _need(conv, kind, "conversation_id")
        _need(smid, kind, "server_message_id")
        # qualified, always: the anchor on the page is page-local, the *store* id is not
        return f"conv-{conv}|msg-{smid}"
    if kind == "mem":
        value = get("snap_id")
        if value:
            return f"mem-{value}"
        # No ZSNAPID: a caller that never read scdb-27 has only the cache file the media came from.
        # The placeholder says so in the id itself rather than presenting a key as a snap id — the run
        # resolves it through the `cachekeys` alternate and records the real id it landed on.
        keys = [str(k).strip() for k in (identifiers.get("cache_keys") or ()) if str(k).strip()]
        if keys:
            return f"mem-by-cachekey-{_safe(keys[0])}"
        _need(value, kind, "snap_id (or cache_keys)")
    if kind == "cc":
        value = get("cache_key")
        _need(value, kind, "cache_key")
        return f"ck-{value}"
    if kind == "cm":
        value = get("sha256") or get("rel")
        _need(value, kind, "sha256 or rel")
        return f"cm-{value}"
    # ct: the fallback chain the Contacts report uses, and the same character substitution
    for name in ("user_id", "username", "conversation_id"):
        value = get(name)
        if value:
            return "ct-" + _safe(value)
    return "ct-unknown"


def _need(value, kind, name):
    if not value:
        raise ValueError(f"a '{kind}' selection needs {name}")


def _safe(value):
    """The character substitution ``contacts_report.contact_anchor`` applies to a contact anchor."""
    return "".join(c if (c.isalnum() and c.isascii()) or c in "_.:-" else "_" for c in str(value))


class SelectionBuilder:
    """Collect selections, then write the file.

    ``sources`` and ``run_id`` are optional and normally absent from an externally produced selection:
    an external tool has no access to Snapchat Auto's source fingerprints. A partial run built from such
    a file reports *"source verification not possible"* rather than refusing — which is the difference
    between an externally built selection and one saved from the reports, and it belongs in the
    provenance rather than in a failure.
    """

    def __init__(self, *, run_id="", tool_version="", sources=None, note="", exported=""):
        self.run_id = run_id or ""
        self.tool_version = tool_version or ""
        self.sources = sources
        self.note = note or ""
        self.exported = exported or ""
        self.relations = ""
        self._rows = {kind: {} for kind in KINDS}

    # ---------------------------------------------------------------- adding

    def add_conversation(self, conversation_id, *, server_id=None):
        """Every message of this conversation follows only if the ``conv_messages`` relation is on."""
        anchor = anchor_for("conv", conversation_id=conversation_id)
        return self._add("conv", anchor, {"conv": conversation_id, "server": server_id})

    def add_message(self, conversation_id, server_message_id, *, ts=None, sender=None):
        """One message. ``ts`` and ``sender`` let it be found again if the report's own anchor for it
        moves, which happens to messages that carry no server id.

        ``ts`` is unix **seconds**, and is recorded as whole seconds however it is given: the report's
        own value is a float, so a selection carrying ``1700000000`` and one carrying
        ``1700000000.0`` have to mean the same row. Note that ``arroyo.db`` stores
        ``creation_timestamp`` in *milliseconds* — :func:`validate` names a ``ts`` that still looks
        like one, because a timestamp that does not match is indistinguishable from one never sent.

        ``sender`` is the sender's **permanent user id** (``conversation_message.sender_id``), matched
        case-insensitively and the only accepted spelling. The report displays the sender's *name*
        instead, so this is the one identifier that cannot be read off a page; the name is **not** a
        key, being only what the device knew at extraction time.

        Both only matter for a message with no server message id yet — one the app had not sent when
        the extraction was taken. Those are anchored on their position in the conversation, so their id
        moves if a later run recovers one more message; anything with a server id is matched on that.
        """
        anchor = anchor_for("msg", conversation_id=conversation_id,
                            server_message_id=server_message_id)
        return self._add("msg", anchor, {"conv": conversation_id, "smid": server_message_id,
                                         "ts": _seconds(ts), "sender": sender})

    def add_contact(self, user_id=None, *, username=None, conversation_id=None):
        anchor = anchor_for("ct", user_id=user_id, username=username,
                            conversation_id=conversation_id)
        return self._add("ct", anchor, {"uid": user_id, "user": username, "conv": conversation_id})

    def add_memory(self, snap_id=None, *, media_id=None, entry_id=None, cache_keys=()):
        """One Memory, by its ``ZSNAPID`` — or, when you do not have one, by the cache file(s) its
        media was recovered from.

        ``cache_keys`` is a list of ``cache_controller.db`` CACHE_KEY values (equivalently: for
        CDN-downloaded media, ``sha256(<the token in the CDN URL>)[:32]``). It exists for a tool that
        never read ``scdb-27`` and so has no snap id at all: pass what you have and the run matches the
        Memory on it. **A cache key names a file, and one file can belong to several Memories** — a
        grouped media object is exactly that — and it never picks one of several: when the rows it names
        are all one group the run includes the whole group and says so, and when they are not it reports
        the doubt rather than choosing. **Pass every key you can derive**, not one: most Memories carry
        at least one key that names only them, so sending all of them names the exact row where a single
        shared key gets its whole group.

        ``media_id`` / ``entry_id`` are not a substitute for the snap id and having neither costs little
        — ``ZMEDIAID`` is shared by a group's members by design and ``ZENTRYID`` can cover several
        snaps, so both hit the same non-discriminating case a cache key does.

        With no ``snap_id`` the row id is a placeholder built from the first cache key, which no row of
        a report will carry. That is deliberate: the run then resolves the row through the key and
        records the real id it landed on in the report's provenance, instead of the id looking like a
        snap id that was simply not found.
        """
        anchor = anchor_for("mem", snap_id=snap_id, cache_keys=cache_keys)
        return self._add("mem", anchor, {"snap": snap_id, "mediaid": media_id, "entry": entry_id,
                                         "cachekeys": [str(k) for k in cache_keys if k] or None})

    def add_cache_entry(self, cache_key, *, sha256=None):
        anchor = anchor_for("cc", cache_key=cache_key)
        return self._add("cc", anchor, {"key": cache_key, "sha": sha256})

    def add_cached_file(self, sha256=None, *, raw_sha256=(), rel=None):
        """A Library/Caches row. Pass every copy's ``raw_sha256`` you have.

        This row's id is the hash of its *recovered* content, and the report merges rows by that
        content — so a build that decodes or decrypts differently gives the same file a different id.
        The raw hashes are what find it again, and are worth supplying even when the decoded hash is
        known.
        """
        anchor = anchor_for("cm", sha256=sha256, rel=rel)
        raw = [r for r in ([raw_sha256] if isinstance(raw_sha256, str) else list(raw_sha256 or ())) if r]
        return self._add("cm", anchor, {"sha": sha256, "raw": raw or None, "rel": rel})

    def _add(self, kind, anchor, keys):
        record = {name: value for name, value in keys.items() if value not in (None, "", [], ())}
        # a row already present keeps whichever identifiers either call knew
        self._rows[kind].setdefault(anchor, {}).update(record)
        return anchor

    # ---------------------------------------------------------------- policy and output

    def set_relations(self, spec):
        """The ``--relations`` spec to record alongside the selection. Same vocabulary as the CLI.

        Recorded for the reader, not validated here: the relation names belong to the executable that
        will consume the file, and :func:`describe` on that executable is what confirms them.
        """
        self.relations = str(spec or "")
        return self

    def count(self):
        return {kind: len(rows) for kind, rows in self._rows.items() if rows}

    def to_payload(self):
        payload = {"tool": TOOL, "schema": SCHEMA, "api_version": API_VERSION,
                   "tool_version": self.tool_version, "run_id": self.run_id,
                   "sources": self.sources, "exported": self.exported,
                   "selections": {kind: dict(rows) for kind, rows in self._rows.items() if rows}}
        if self.note:
            payload["note"] = self.note
        if self.relations:
            payload["relations"] = self.relations
        return payload

    def write_json(self, path):
        """Write ``selection.json`` — the form to hand to Snapchat Auto. Returns the path."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(selection_json_text(self.to_payload()))
        return path

    def write_js(self, path):
        """Write the drop-in ``selection.js`` a report auto-loads. Returns the path.

        Prefer :meth:`write_json` unless you are placing the file next to the reports yourself: a
        browser will not save a ``.js``, and a ``.json`` renamed to ``.js`` fails silently.
        """
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(selection_js_text(self.to_payload()))
        return path


def validate(payload):
    """Human-readable problems with a payload. ``[]`` means it is valid.

    What an integrator runs before writing a file. Absent provenance (``sources``, ``run_id``,
    ``tool_version``) is **not** a problem — an externally produced selection legitimately has none.
    """
    problems = []
    if not isinstance(payload, dict):
        return ["the payload is not an object"]
    if payload.get("tool") not in (None, TOOL):
        problems.append(f"'tool' should be {TOOL!r}, not {payload.get('tool')!r}")
    schema = payload.get("schema")
    if schema is None:
        problems.append("no 'schema'")
    elif not isinstance(schema, int):
        problems.append(f"'schema' should be a number, not {schema!r}")
    elif not SCHEMA_MIN <= schema <= SCHEMA_MAX:
        problems.append(f"schema {schema} is outside what this build reads "
                        f"({SCHEMA_MIN}-{SCHEMA_MAX})")

    selections = payload.get("selections")
    if not isinstance(selections, dict):
        problems.append("'selections' is missing or is not an object")
        return problems
    if not any(selections.values()):
        problems.append("nothing is selected")

    for kind, rows in selections.items():
        if kind not in KINDS:
            problems.append(f"unknown kind '{kind}'. Known: {', '.join(sorted(KINDS))}")
            continue
        if not isinstance(rows, dict):
            problems.append(f"selections['{kind}'] should be an object of id -> keys")
            continue
        for row_id, keys in rows.items():
            if not isinstance(row_id, str) or not row_id:
                problems.append(f"selections['{kind}'] has an id that is not a string")
                continue
            if keys != 1 and not isinstance(keys, dict):
                problems.append(f"'{row_id}' should map to an object of identifiers, or to 1 when "
                                f"none were recorded — not {keys!r}")
            if kind == "msg" and isinstance(keys, dict) and keys.get("ts") is not None:
                problems += _ts_problems(row_id, keys["ts"])
            if kind == "msg" and "|msg-" not in row_id:
                problems.append(f"'{row_id}' is not qualified with its conversation. A server message "
                                f"id is a per-conversation ordinal, so a bare 'msg-<n>' names a "
                                f"different message in every chat — use anchor_for('msg', …)")
            if kind != "msg" and not row_id.startswith(_PREFIX[kind]):
                problems.append(f"'{row_id}' does not look like a '{kind}' id "
                                f"(expected it to start with {_PREFIX[kind]!r})")
    return problems


_PREFIX = {"conv": "conv-", "msg": "conv-", "ct": "ct-", "mem": "mem-", "cc": "ck-", "cm": "cm-"}

#: Roughly 1973 in seconds, and below any plausible date in milliseconds -- so a `ts` at or above it
#: is a millisecond value, which is what `arroyo.db` itself stores.
_MILLIS_FLOOR = 100_000_000_000


def _seconds(value):
    """A ``ts`` as whole unix seconds. Anything unusable is left alone for :func:`validate` to name,
    rather than being silently dropped here."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return value


def _ts_problems(row_id, ts):
    try:
        seconds = float(ts)
    except (TypeError, ValueError):
        return [f"'{row_id}' has a 'ts' that is not a number ({ts!r}) — it is unix seconds"]
    if seconds >= _MILLIS_FLOOR:
        return [f"'{row_id}' has a 'ts' of {ts}, which looks like milliseconds. arroyo.db stores "
                f"creation_timestamp in milliseconds; divide by 1000. A ts that does not match is "
                f"indistinguishable from one that was never sent, so this fails silently"]
    return []


def describe():
    """What this build of the *format* supports, as plain data.

    The executable's ``--describe-selection-api`` returns this plus what only it knows: its own
    version, the relation vocabulary and the field keys. Check ``schema_min``/``schema_max`` against
    your own :data:`SCHEMA` **before** writing a file — pinning a version of this package does not
    prevent a mismatch with the build an examiner has installed, it only moves it.
    """
    return {
        "tool": TOOL,
        "api_version": API_VERSION,
        "schema_write": SCHEMA,
        "schema_min": SCHEMA_MIN,
        "schema_max": SCHEMA_MAX,
        "kinds": {kind: {"report": report,
                         "id_prefix": _PREFIX[kind],
                         "primary": list(IDENTIFIERS[kind]["primary"]),
                         "alternates": list(IDENTIFIERS[kind]["alternates"]),
                         "note": IDENTIFIERS[kind]["note"]}
                  for kind, report in sorted(KINDS.items())},
    }


def read_selection(path):
    """Read a selection file of either form. Re-exported for symmetry with the writers."""
    return _format.read_selection(path)


def parse_selection_text(text):
    """Parse a selection file's text, wrapped or bare. Re-exported for symmetry."""
    return _format.parse_selection_text(text)
