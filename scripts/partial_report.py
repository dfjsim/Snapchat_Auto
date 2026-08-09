"""Turning a saved selection into the set of rows a run should render.

Two steps, in this order, both before anything is decrypted or published:

1. :func:`resolve` — map every ticked id onto a row that exists in **this** run. Most ids are read
   straight out of the evidence and match exactly; a couple are ours and can move between builds, so
   each ticked row also carries the identifiers it can be found by again (see
   docs/report_partial.md). Anything that cannot be found is **named**, and the caller decides whether
   to drop it or stop.
2. :func:`expand` — add the rows the examiner did not tick but asked for: the messages of a selected
   conversation, the cache entry behind a message's media, the Memory a cache entry belongs to. Every
   added row records **why** it is there.

Why both run against one set of indexes, up front
-------------------------------------------------
The relations run in *both* directions across a report order that is fixed by manifest dependencies
(Conversations -> Contacts -> Memories -> CacheMedia -> CacheController). A ticked cache entry has to
be able to pull in a Memory — but Memories renders before CacheController. A ticked Library/Caches file
has to do the same, and that walk happens after Memories. So resolving each relation inside the stage
that owns it cannot work in one direction, and duplicating the linking logic to work around that is
exactly what cross_report_linking.md warns against ("one list, read by **both** reports").

Instead every generator hands over its index — the rows it would render, and the edges it already
computed while building them — and the whole closure is decided once, here, before the expensive half
of each stage runs. That also means the filter is applied *before* media is published, so a partial
report never carries a file no included row references.

This module owns no linking logic of its own. It consumes the edges the generators already derive.
"""

import os
import html
import json
import logging
from collections import defaultdict
from datetime import datetime

from scripts import report_ui
from scripts import selection_file

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- kinds and relations

#: Selection kinds, and the report each belongs to.
KINDS = ("conv", "msg", "ct", "mem", "cc", "cm")


# An *edge* is a fact two reports share; a *relation* is the examiner's choice to follow one, in one
# direction. They are named separately, and that distinction is load-bearing: a generator records an
# edge in whichever direction it derives it — the Memories index knows "this Memory's media came from
# that cache entry", cache_controller knows "this entry belongs to that Memory" — and both relations
# over that one edge (`mem_cache`, `cache_memory`) have to find it. Keying the store by relation name
# instead meant the relation asked for in the direction its owner did not record silently found
# nothing.
EDGE_CONV_MESSAGE = "conv_message"                 # conv -> msg
EDGE_CONV_PARTICIPANT = "conv_participant"         # conv -> ct
EDGE_MEMORY_GROUP = "memory_group"                 # mem -> mem
EDGE_MEMORY_CACHE = "memory_cache"                 # mem <-> cc
EDGE_MESSAGE_CACHE = "message_cache"               # cc  <-> msg
EDGE_CACHE_CACHEMEDIA = "cache_cachemedia"         # cc  <-> cm
EDGE_MEMORY_CACHEMEDIA = "memory_cachemedia"       # mem <-> cm
EDGE_CACHEMEDIA_MESSAGE = "cachemedia_message"     # cm  <-> msg

EDGES = (EDGE_CONV_MESSAGE, EDGE_CONV_PARTICIPANT, EDGE_MEMORY_GROUP, EDGE_MEMORY_CACHE,
         EDGE_MESSAGE_CACHE, EDGE_CACHE_CACHEMEDIA, EDGE_MEMORY_CACHEMEDIA,
         EDGE_CACHEMEDIA_MESSAGE)


class Relation:
    """One optional hop the examiner can switch on: an edge, followed from ``src`` to ``dst``.

    ``edge`` of ``None`` means "any edge this row takes part in", which is what "show me what this
    file links to" amounts to. ``dst`` of ``None`` means "whatever it reaches".

    ``basis`` is shown next to the checkbox and recorded in the provenance: an examiner deciding
    whether to include something needs to know what the association rests on, not only its name.
    """

    __slots__ = ("key", "label", "edge", "src", "dst", "default", "basis")

    def __init__(self, key, label, edge, src, dst, default, basis):
        self.key, self.label = key, label
        self.edge = edge
        self.src, self.dst = src, dst
        self.default, self.basis = default, basis


RELATIONS = (
    Relation("conv_messages", "Every message of a selected conversation",
             EDGE_CONV_MESSAGE, "conv", "msg", True,
             "The conversation's own messages, as arroyo.db records them."),
    Relation("msg_cache", "The cache_controller entry behind an included message's media",
             EDGE_MESSAGE_CACHE, "msg", "cc", True,
             "A chat claim's EXTERNAL_KEY carries <type>:<conversation>:<message>:<part>, so the "
             "entry names the message directly."),
    Relation("participants", "The contact record of every participant of an included conversation",
             EDGE_CONV_PARTICIPANT, "conv", "ct", True,
             "The participant user ids on the conversation, matched to the friends artifact."),
    Relation("contact_conversations", "Every conversation an included contact takes part in",
             EDGE_CONV_PARTICIPANT, "ct", "conv", False,
             "The conversations that list this contact as a participant."),
    Relation("mem_group", "The other Memories grouped with a selected one",
             EDGE_MEMORY_GROUP, "mem", "mem", True,
             "Grouped because they share a ZMEDIAID and/or identical media bytes, so they are the "
             "same media under another snap row - and they share one detail page."),
    Relation("mem_cache", "The cache_controller entries a selected Memory's media came from",
             EDGE_MEMORY_CACHE, "mem", "cc", True,
             "A CACHE_FILE_CLAIM whose EXTERNAL_KEY carries the Memory's ZSNAPID or ZMEDIAID, or "
             "whose CACHE_KEY is SHA-256 of a CDN URL token - confirmed by the file decrypting."),
    Relation("mem_cachemedia", "The Library/Caches pack chunks of a selected Memory",
             EDGE_MEMORY_CACHEMEDIA, "mem", "cm", False,
             "caching-media .pack chunks are not indexed by cache_controller.db; they are matched "
             "by the pack's item hash."),
    Relation("cache_memory", "The Memory a selected cache entry belongs to",
             EDGE_MEMORY_CACHE, "cc", "mem", True,
             "The same claim shapes as mem_cache, read in the other direction."),
    Relation("cache_message", "The chat message a selected cache entry belongs to",
             EDGE_MESSAGE_CACHE, "cc", "msg", True,
             "The conversation and message id in the claim's EXTERNAL_KEY, matched against the "
             "chat report's manifest."),
    Relation("cache_cachemedia", "Library/Caches copies of a selected cache entry",
             EDGE_CACHE_CACHEMEDIA, "cc", "cm", False,
             "The same CACHE_KEY appearing under Library/Caches as well as in the SCContent "
             "folders."),
    Relation("cachemedia_origin", "What a selected Library/Caches file links to",
             None, "cm", None, True,
             "Whatever that file's row already links to: a cache entry, a Memory, or a message."),
)

RELATION_KEYS = tuple(r.key for r in RELATIONS)
_BY_KEY = {r.key: r for r in RELATIONS}

#: Structural inclusions that are never optional, because the row cannot be rendered without them.
CONTAINMENT = (
    "the conversation an included message belongs to",
    "the detail page of an included conversation",
)

PRESETS = {
    # containment only: exactly what was ticked, and what it cannot be shown without
    "minimal": {},
    "recommended": {r.key: r.default for r in RELATIONS},
    "all": {r.key: True for r in RELATIONS},
}


def default_options():
    """The default policy: the ``recommended`` relations, one hop, legacy reports left out."""
    return {"relations": dict(PRESETS["recommended"]),
            "transitive": False,
            "legacy_reports": False,
            "max_rows": 5000,
            "unresolved": "refuse"}


def parse_relations(spec):
    """``"minimal"`` / ``"recommended"`` / ``"all"`` / ``"a,b,-c"`` -> ``{key: bool}``.

    A bare list turns the named relations on and everything else off; a leading ``-`` on any token
    switches to "the recommended set, minus these", which is what an examiner reaches for far more
    often than naming all eleven.
    """
    spec = (spec or "recommended").strip()
    if spec in PRESETS:
        return dict(PRESETS[spec])
    tokens = [t.strip() for t in spec.replace(";", ",").split(",") if t.strip()]
    unknown = [t.lstrip("-") for t in tokens if t.lstrip("-") not in _BY_KEY]
    if unknown:
        raise ValueError(f"unknown relation(s): {', '.join(unknown)}. "
                         f"Known: {', '.join(RELATION_KEYS)}")
    subtractive = any(t.startswith("-") for t in tokens)
    out = dict(PRESETS["recommended"]) if subtractive else {r.key: False for r in RELATIONS}
    for token in tokens:
        if token.startswith("-"):
            out[token[1:]] = False
        else:
            out[token] = True
    return out


# --------------------------------------------------------------------------- what a generator hands over

class Index:
    """One report's rows, and the edges it already worked out while building them.

    A generator fills this in during its cheap half — reading databases, joining claims — and hands it
    over before it decrypts, publishes or renders anything.

    ``rows`` maps a selection id to whatever the generator wants back later (its own record); the
    closure only ever treats it as an opaque handle. ``keys`` maps an *alternate* identifier to the
    same id, which is what lets a selection made by an older build still find its row. ``edges`` are
    ``(edge name, src id, dst kind, dst id)`` — an **edge**, one of :data:`EDGES`, not a relation:
    record it in whichever direction you derived it and both relations over it will find it.
    """

    __slots__ = ("kind", "rows", "order", "keys", "edges", "groups", "parents")

    def __init__(self, kind):
        self.kind = kind
        self.rows = {}                        # id -> record, for membership tests
        self.order = []                       # (id, record) in the generator's own order
        self.keys = defaultdict(set)          # (key name, value) -> {id, …}
        self.edges = []
        self.groups = {}                      # id -> group id (Memories sharing a detail page)
        self.parents = {}                     # id -> the id that contains it (a message's conversation)

    def add(self, row_id, record=None, **alternates):
        """Record a row and the identifiers it can also be found by.

        ``order`` is the list, ``rows`` only the membership map — because **a row id is not
        guaranteed unique**. ``contact_anchor`` falls back username -> conversation id ->
        "ct-unknown", so two contacts can share one id. Rebuilding a report's rows from the map alone
        silently dropped every duplicate, which the corpus byte-diff caught as a Contacts report
        missing rows on all four devices. Two rows sharing an id do share one selection, which is a
        real limitation of that fallback chain — but neither may vanish from the report.
        """
        self.rows[row_id] = record
        self.order.append((row_id, record))
        for name, value in alternates.items():
            if value:
                self.keys[(name, str(value))].add(row_id)
        return row_id

    def has(self, row_id):
        return row_id in self.rows

    def keep(self, closure):
        """The rows a closure includes, in this index's own order, duplicates and all.

        ``closure`` of ``None`` means a full run: everything, unchanged. That is the path every
        existing caller takes, and it must stay indistinguishable from not having asked — which is
        what the corpus byte-diff and ``tests/test_index_render_split.py`` both check.
        """
        if closure is None:
            return list(self.order)
        wanted = closure.included.get(self.kind, ())
        return [(row_id, record) for row_id, record in self.order if row_id in wanted]

    def link(self, edge, src_id, dst_kind, dst_id):
        """Record one edge, from :data:`EDGES`. Direction does not matter — see :func:`_edge_map`.

        Cheap enough to call per row; nothing is resolved until :func:`expand`.
        """
        if src_id and dst_id:
            self.edges.append((edge, src_id, dst_kind, dst_id))

    def contains(self, child_id, parent_id):
        self.parents[child_id] = parent_id

    def __len__(self):
        # the row count, not the id count: two rows can share an id (see `add`), and the report's
        # "N of M" has to state how many rows there are
        return len(self.order)


class Stage:
    """What a generator's ``index()`` hands back, and what its ``render()`` takes.

    One shape for all five generators, so ``ParseSnapchat_iOS.main`` can collect every index, decide
    the closure once, and then render — without knowing what any particular report's model looks like.

    * ``model`` is the generator's own structure (its conversations list, memories dict, entry list).
      Nothing here interprets it.
    * ``sel`` is the :class:`Index` — the closure's view: which rows exist, what else they can be found
      by, and the edges this generator derived on the way.
    * ``meta`` is whatever else its ``render()`` needs and its ``index()`` already worked out (the run
      id, a timezone label, statistics), so the expensive half is not repeated.
    """

    __slots__ = ("kind", "model", "sel", "meta")

    def __init__(self, kind, model, sel=None, **meta):
        self.kind = kind
        self.model = model
        self.sel = sel if sel is not None else Index(kind)
        self.meta = meta

    def __getitem__(self, name):
        return self.meta[name]

    def get(self, name, default=None):
        return self.meta.get(name, default)

    def indexes(self):
        """Every index this stage contributes, keyed by kind.

        Usually one. The Conversations report contributes two — conversations and the messages inside
        them are separately selectable — so it puts both here and the orchestrator does not need to
        know which report is the exception.
        """
        return self.meta.get("indexes") or {self.kind: self.sel}


# --------------------------------------------------------------------------- resolution

#: How a ticked id is looked up, per kind, in order. The first entry is always the id itself.
#:
#: The order matters: an id we produced ourselves can move between builds, while an identifier read
#: out of the evidence cannot. So the evidence-derived alternates come next, and anything positional
#: or non-unique comes last.
RESOLVE_ORDER = {
    "mem": ("snap", "mediaid", "entry"),
    "cc": ("key", "sha"),
    "conv": ("conv", "server"),
    "msg": ("smid", "ts_sender"),
    "ct": ("uid", "user", "conv"),
    "cm": ("sha", "raw", "rel"),
}


class AmbiguousSelection(ValueError):
    """A ticked id matched more than one row, so including it would over-disclose."""


class Resolution:
    """What every ticked id turned into in this run."""

    def __init__(self):
        self.seeds = {kind: set() for kind in KINDS}
        self.how = {}                 # (kind, resolved id) -> how it matched
        self.moved = []               # ids that matched on an alternate rather than themselves
        self.unresolved = []          # ids nothing in this run matches
        self.ambiguous = []           # ids that matched several rows
        self.notes = []

    @property
    def ok(self):
        return not self.unresolved and not self.ambiguous

    def count(self):
        return sum(len(ids) for ids in self.seeds.values())

    def as_dict(self):
        return {"seeds": {k: sorted(v) for k, v in self.seeds.items() if v},
                "how": {f"{k}/{i}": how for (k, i), how in sorted(self.how.items())},
                "moved": self.moved, "unresolved": self.unresolved,
                "ambiguous": self.ambiguous, "notes": self.notes}


def _candidates(index, kind, ticked_id, keys):
    """Every (row id, how) this ticked id could mean, in priority order."""
    out = []
    if ticked_id in index.rows:
        out.append((ticked_id, "its own id"))
    for name in RESOLVE_ORDER.get(kind, ()):
        if name == "raw":
            # a Library/Caches row's *raw* bytes -- the one alternate that is a list, because one row
            # can be several copies of the same recovered content
            for value in (keys.get("raw") or []):
                for hit in sorted(index.keys.get(("raw", str(value)), ())):
                    out.append((hit, f"the raw SHA-256 of one of its copies ({str(value)[:12]}...)"))
            continue
        if name == "ts_sender":
            ts, sender = keys.get("ts"), keys.get("sender")
            if ts and sender:
                for hit in sorted(index.keys.get(("ts_sender", f"{ts}|{sender}"), ())):
                    out.append((hit, "its time and sender"))
            continue
        value = keys.get(name)
        if not value:
            continue
        for hit in sorted(index.keys.get((name, str(value)), ())):
            out.append((hit, f"its {name} ({str(value)[:24]})"))
    # de-duplicate, keeping the first (highest-priority) explanation for each row
    seen, unique = set(), []
    for row_id, how in out:
        if row_id not in seen:
            seen.add(row_id)
            unique.append((row_id, how))
    return unique


def resolve(indexes, selection, *, unresolved="refuse"):
    """Map every ticked id onto a row of this run. See the module docstring.

    ``unresolved`` is ``"refuse"`` (raise, naming what was not found) or ``"drop"`` (leave it out and
    list it). Refusing is the default: a selection that silently loses rows produces a partial report
    that is quietly missing evidence, which is worse than one that will not build.
    """
    result = Resolution()
    selections = (selection or {}).get("selections") or {}

    for kind in KINDS:
        index = indexes.get(kind)
        ticked = selections.get(kind) or {}
        if not ticked:
            continue
        if index is None:
            # the report itself was not produced -- its rows cannot be resolved, and saying so beats
            # reporting them as "not found in the extraction"
            for ticked_id in sorted(ticked):
                result.unresolved.append(
                    {"kind": kind, "id": ticked_id,
                     "why": f"the {kind} report was not produced by this run"})
            continue

        for ticked_id in sorted(ticked):
            keys = ticked[ticked_id]
            keys = keys if isinstance(keys, dict) else {}
            found = _candidates(index, kind, ticked_id, keys)
            if not found:
                result.unresolved.append(
                    {"kind": kind, "id": ticked_id,
                     "why": "no row of this run carries that id or any identifier recorded with it"})
                continue
            if len(found) > 1:
                result.ambiguous.append(
                    {"kind": kind, "id": ticked_id,
                     "matches": [row_id for row_id, _how in found],
                     "why": ("that id now matches several rows. A Library/Caches row is identified "
                             "by its recovered content, and rows are merged by that content, so a "
                             "build that decodes differently can split or merge them.")})
                continue
            row_id, how = found[0]
            result.seeds[kind].add(row_id)
            result.how[(kind, row_id)] = how
            if row_id != ticked_id:
                result.moved.append({"kind": kind, "was": ticked_id, "now": row_id, "how": how})

    if result.moved:
        logger.warning(f"{len(result.moved)} selected row(s) were found under a different id than "
                       f"the selection recorded; each is listed in the partial report's provenance")
        for move in result.moved:
            logger.info(f"  {move['kind']}: {move['was']} -> {move['now']} (matched {move['how']})")

    if result.ambiguous:
        detail = "; ".join(f"{a['kind']} {a['id']} matches {len(a['matches'])} rows"
                           for a in result.ambiguous[:5])
        raise AmbiguousSelection(
            f"{len(result.ambiguous)} selected row(s) are ambiguous in this run: {detail}. "
            f"Re-tick them in this run's reports and save the selection again.")

    if result.unresolved:
        listed = ", ".join(f"{u['kind']} {u['id']}" for u in result.unresolved[:5])
        more = f" (and {len(result.unresolved) - 5} more)" if len(result.unresolved) > 5 else ""
        message = (f"{len(result.unresolved)} selected row(s) do not exist in this run: {listed}"
                   f"{more}.")
        if unresolved == "refuse":
            raise LookupError(
                message + " Check that this is the extraction the selection was made from, or pass "
                          "the option to drop them.")
        logger.warning(message + " They are left out, and listed in the provenance.")
        result.notes.append(message)

    return result


# --------------------------------------------------------------------------- expansion

class Closure:
    """The rows a partial run will render, and why each one is there."""

    def __init__(self, resolution, totals, options):
        self.seeds = {k: set(v) for k, v in resolution.seeds.items()}
        self.included = {k: set(v) for k, v in resolution.seeds.items()}
        self.reasons = defaultdict(list)
        self.totals = dict(totals)
        self.options = options
        self.resolution = resolution
        self.warnings = []
        self.excluded_refs = defaultdict(int)      # (kind, id) -> links that point at it from inside
        self.ends = {}                             # the edge map, kept for the renderers (see reaches)

    def add(self, kind, row_id, reason):
        """Include a row, recording why. Returns True when it was not already in."""
        if not row_id or row_id in self.included.setdefault(kind, set()):
            if row_id and reason not in self.reasons[(kind, row_id)]:
                self.reasons[(kind, row_id)].append(reason)
            return False
        self.included[kind].add(row_id)
        self.reasons[(kind, row_id)].append(reason)
        return True

    def has(self, kind, row_id):
        return row_id in self.included.get(kind, ())

    def is_seed(self, kind, row_id):
        return row_id in self.seeds.get(kind, ())

    def counts(self):
        return {kind: {"selected": len(self.seeds.get(kind, ())),
                       "pulled_in": len(self.included.get(kind, ())) - len(self.seeds.get(kind, ())),
                       "total": self.totals.get(kind, 0)}
                for kind in KINDS if self.included.get(kind) or self.totals.get(kind)}

    def total(self):
        return sum(len(ids) for ids in self.included.values())

    def reaches(self, edge, kind, row_id, dst_kind=None):
        """The rows *edge* reaches from one row — sorted, and de-duplicated.

        For a link whose target is a **set** of rows addressed by a shared token rather than by a row
        id (a ``#find=`` fragment): the renderer has the token but not the ids, so it cannot tell
        whether anything it reaches is in the extract. The edges the generators derived can, and they
        are all known before the first page is written.
        """
        out = {(other_kind, other_id)
               for other_kind, other_id in self.ends.get((edge, kind, row_id), ())
               if not dst_kind or other_kind == dst_kind}
        return sorted(out)

    def note_excluded(self, kind, row_id):
        """Record that an included row links to *row_id*, which this extract does not contain.

        Called by :func:`report_ui.xref` as it marks the link. What it buys is the other half of the
        statement the marker makes on the page: the manifest can list every association that was cut
        and how many places pointed at it, so "what was left out" is answerable without reading
        every page.
        """
        if kind and row_id:
            self.excluded_refs[(kind, row_id)] += 1

    def excluded_ref_count(self):
        return sum(self.excluded_refs.values())

    def as_dict(self):
        return {"included": {k: sorted(v) for k, v in self.included.items() if v},
                "seeds": {k: sorted(v) for k, v in self.seeds.items() if v},
                "reasons": {f"{k}/{i}": why for (k, i), why in sorted(self.reasons.items())},
                "counts": self.counts(), "options": self.options,
                "excluded_refs": [{"kind": kind, "id": row_id, "links": n}
                                  for (kind, row_id), n in sorted(self.excluded_refs.items())],
                "resolution": self.resolution.as_dict(), "warnings": self.warnings}


def _edge_map(indexes):
    """Every edge, keyed for lookup from **either end**.

    ``(edge, kind, id) -> [(other kind, other id), …]``, with each edge entered under both of its
    ends. A generator records an edge in whichever direction it derives it — the Memories index knows
    "this Memory's media came from that cache entry", cache_controller knows "this entry belongs to
    that Memory" — and both are the same fact. A relation asked for in the direction its owner did not
    record must still find it, which is why the store is keyed by *edge* and not by relation name.
    """
    ends = defaultdict(list)
    for kind, index in indexes.items():
        if index is None:
            continue
        for edge, src_id, dst_kind, dst_id in index.edges:
            ends[(edge, kind, src_id)].append((dst_kind, dst_id))
            ends[(edge, dst_kind, dst_id)].append((kind, src_id))
    return ends


def _reachable(ends, relation, src_kind, src_id):
    """The rows *relation* reaches from one row, filtered to the kind it is defined to reach."""
    edges = (relation.edge,) if relation.edge else EDGES
    for edge in edges:
        for other_kind, other_id in ends.get((edge, src_kind, src_id), ()):
            if relation.dst and other_kind != relation.dst:
                continue
            if other_kind == src_kind and other_id == src_id:
                continue                      # an edge's own end is not something it reaches
            yield other_kind, other_id


def expand(indexes, resolution, options=None):
    """Grow the seeds into the full set of rows to render. Returns a :class:`Closure`."""
    options = options or default_options()
    relations = options.get("relations") or {}
    totals = {kind: len(index) for kind, index in indexes.items() if index is not None}
    closure = Closure(resolution, totals, options)

    for kind, ids in resolution.seeds.items():
        for row_id in ids:
            how = resolution.how.get((kind, row_id), "its own id")
            closure.reasons[(kind, row_id)].append(f"Selected by the examiner (matched {how})")

    ends = _edge_map(indexes)
    # kept on the closure: a renderer whose link addresses a *set* of rows needs to know which rows
    # those are, and every edge is already worked out here, before any page is written
    closure.ends = ends
    enabled = [r for r in RELATIONS if relations.get(r.key)]

    # One hop from each seed, repeated only when the examiner asked for a transitive closure. One hop
    # plus containment is predictable and explainable; following everything can drag in most of a case
    # (a message -> its cache entry -> its Memory -> that Memory's other entries -> their messages).
    frontier = {kind: set(ids) for kind, ids in resolution.seeds.items()}
    passes = 0
    while any(frontier.values()):
        passes += 1
        added = {kind: set() for kind in KINDS}
        for relation in enabled:
            for src_kind in ([relation.src] if relation.src else list(KINDS)):
                for src_id in sorted(frontier.get(src_kind, ())):
                    for dst_kind, dst_id in _reachable(ends, relation, src_kind, src_id):
                        index = indexes.get(dst_kind)
                        if index is None or dst_id not in index.rows:
                            continue
                        why = (f"Included because {src_kind} {src_id} was selected "
                               f"(relation: {relation.key})")
                        if closure.add(dst_kind, dst_id, why):
                            added[dst_kind].add(dst_id)
        _apply_containment(indexes, closure, added)
        if not options.get("transitive"):
            break
        frontier = added
        if passes > 25:                            # a cyclic graph must not spin forever
            closure.warnings.append("the transitive closure was stopped after 25 passes")
            break

    # containment for the seeds themselves, and for anything the hop added
    _apply_containment(indexes, closure, closure.included)

    limit = options.get("max_rows") or 0
    if limit and closure.total() > limit:
        closure.warnings.append(
            f"{closure.total()} rows are included, over the {limit}-row guard. Narrow the selection "
            f"or the relations, or raise the limit.")
    return closure


def _apply_containment(indexes, closure, scope):
    """Add what an included row cannot be rendered without.

    A message lives on its conversation's page, so the conversation comes too — not as a choice but
    because there is nowhere else to put the message. Applied repeatedly until nothing new appears,
    since a container may itself be contained.
    """
    changed = True
    while changed:
        changed = False
        for kind in list(KINDS):
            index = indexes.get(kind)
            if index is None or not index.parents:
                continue
            for row_id in sorted(closure.included.get(kind, ())):
                parent = index.parents.get(row_id)
                if not parent:
                    continue
                parent_kind = "conv" if kind == "msg" else kind
                if closure.add(parent_kind, parent,
                               f"Included because it holds {kind} {row_id}, which is in this extract"):
                    changed = True
    return closure


# --------------------------------------------------------------------------- what the pages say

#: The nouns the "N of M" figures use, per kind.
KIND_NOUN = {"conv": "conversation(s)", "msg": "message(s)", "ct": "contact(s)",
             "mem": "memory/memories", "cc": "cache_controller entry/entries",
             "cm": "Library/Caches file(s)"}

#: What the caller may put in the ``prov`` mapping every function below accepts. Every key is
#: optional: a partial run built from an externally produced selection knows some of these and not
#: others, and the report says which rather than implying it checked something it could not.
#:
#: ``selection``      the selection file as supplied: ``{"name", "sha256", "digest", "exported",
#:                    "schema", "tool_version", "counts"}``
#: ``sources``        the source verification verdict (:mod:`source_fingerprint`): ``{"ok", "text",
#:                    "rows"}``, or ``None`` when the selection carried no fingerprints to check
#: ``version``        the tool-version verdict, same shape
#: ``reuse``          ``{"allowed", "reused", "rederived", "why"}``
#: ``case_ref``       the examiner's case / exhibit reference, stamped on every page
#: ``built``          the build timestamp, already formatted, and ``tz_label`` beside it
#: ``withheld``       Phase 5: the field keys this extract withholds
#: ``tool_version``   the version that produced *this* extract
PROVENANCE_KEYS = ("selection", "sources", "version", "reuse", "case_ref", "built", "tz_label",
                   "withheld", "tool_version")


def _esc(value):
    return html.escape(str(value if value is not None else ""))


def figures_html(closure, kind):
    """``<b>3</b> of 412 conversation(s) in the extraction · 2 selected, 1 pulled in``.

    Appended to a report's own ``.sum`` line rather than replacing it: the line already counts the
    rows that were rendered, which in a partial run is the truth about this folder. What it cannot say
    on its own is how much of the extraction that is, and a subset presented without its denominator
    reads as the whole.
    """
    counts = closure.counts().get(kind)
    if not counts:
        return ""
    noun = KIND_NOUN.get(kind, "row(s)")
    shown = counts["selected"] + counts["pulled_in"]
    bits = [f'<b>{shown}</b> of {counts["total"]} {noun} in the extraction']
    if counts["pulled_in"]:
        bits.append(f'{counts["selected"]} selected, {counts["pulled_in"]} pulled in by a relation')
    else:
        bits.append(f'{counts["selected"]} selected')
    return f'<div class="sum pfig">{" &middot; ".join(bits)}</div>'


def sibling_badge(closure, kind, row_id):
    """The ``.psib`` note on a row that is here because of another row, not because it was ticked.

    A grouped Memory is the case this exists for — a sibling shares its detail page with a selected
    one, so without a badge it is indistinguishable from something the examiner chose.
    """
    if closure is None or closure.is_seed(kind, row_id) or not closure.has(kind, row_id):
        return ""
    reasons = closure.reasons.get((kind, row_id)) or ()
    if not reasons:
        return ""
    return f'<span class="psib" title="{_esc(reasons[0])}">not selected &mdash; included</span>'


def banner_html(closure, prov=None):
    """The PARTIAL banner every page of a partial report carries under its header.

    On every page, not only the index: a page handed on by itself has to say what it is. The
    mismatches go here too — if the examiner chose to build from evidence or a build that differs from
    the run the selection was made in, the report says so on its face and not only in a JSON file.

    The counts are not repeated here. They belong next to the report's own figures, in the header —
    see :func:`figures_html`, which every generator emits there.
    """
    prov = prov or {}
    head = '<div class="pttl"><b>&#9888; PARTIAL REPORT &mdash; SELECTED ITEMS ONLY</b></div>'
    parts = ['This is not the complete report. It contains only the rows the examiner selected, plus '
             'the related items the provenance below lists. Every cross-reference to an item that is '
             f'<b>not</b> here is marked {report_ui.XOUT_MARK} in place, and every one of them is '
             'listed in <span class="mono">partial_manifest.json</span>.']
    if prov.get("case_ref"):
        parts.append(f'Case / exhibit reference: <b>{_esc(prov["case_ref"])}</b>')

    problems = []
    for key, what in (("version", "the tool version that produced this extract differs from the one "
                                  "the selection was made with"),
                      ("sources", "this extract was built from source artifacts that differ from the "
                                  "run the selection was made in")):
        verdict = prov.get(key)
        if verdict is not None and not verdict.get("ok", True):
            problems.append(f'{what} &mdash; see the Sources table. {_esc(verdict.get("text") or "")}')
    if prov.get("sources") is None:
        problems.append("the selection carried no source fingerprints, so it was <b>not possible to "
                        "verify</b> that this is the evidence it was made from.")
    if prov.get("withheld"):
        problems.append(f'field-level exclusions are in force: {len(prov["withheld"])} field(s) are '
                        f'withheld from this extract and are marked as such where they would appear.')
    for warning in closure.warnings:
        problems.append(_esc(warning))
    if problems:
        parts.append('<ul class="pmis"><li>' + "</li><li>".join(problems) + "</li></ul>")
    return ('<div class="pbanner">' + head
            + "".join(f"<div>{p}</div>" for p in parts) + "</div>")


def page_chrome(closure, kind, prov=None):
    """``(extra css, banner, figures)`` for one report's shell — all three empty in a full run.

    Every generator drops these three strings into its document: the css after its own, the banner
    right after ``</header>``, the figures inside the ``.sum`` block. One call rather than three so a
    full run cannot half-emit the furniture, and so ``closure=None`` is provably three empty strings.
    """
    if closure is None:
        return "", "", ""
    return report_ui.PARTIAL_CSS, banner_html(closure, prov), figures_html(closure, kind)


def _verdict_rows(verdict):
    """The Phase 2 verdict as ``<tr>``s, or a single row saying it could not be checked."""
    if verdict is None:
        return ('<tr><td colspan="3">Not checked &mdash; the selection carried no fingerprints.'
                "</td></tr>")
    rows = []
    for row in verdict.get("rows") or ():
        mark = "yes" if row.get("ok") else "no"
        rows.append(f'<tr><td>{_esc(row.get("role"))}</td>'
                    f'<td class="mono">{_esc(row.get("detail") or "")}</td>'
                    f'<td class="{mark}">{_esc(row.get("verdict") or "")}</td></tr>')
    if not rows:
        mark = "yes" if verdict.get("ok") else "no"
        rows.append(f'<tr><td colspan="2">{_esc(verdict.get("text") or "")}</td>'
                    f'<td class="{mark}">{"match" if verdict.get("ok") else "differs"}</td></tr>')
    return "".join(rows)


def provenance_html(closure, prov=None, *, open_by_default=False):
    """The collapsible block that states how this extract was produced, and what it leaves out.

    Everything here is a statement someone reading the extract needs in order to know what they are
    holding: which selection, checked against which evidence, with which relations followed, and how
    much of each report is missing. The relations that were **not** followed are listed as well —
    an omission the reader cannot see is an omission they will not account for.
    """
    prov = prov or {}
    sel = prov.get("selection") or {}
    body = []

    rows = [("Tool", f'Snapchat Auto {_esc(prov.get("tool_version") or "")}'),
            ("Built", f'{_esc(prov.get("built") or "")} {_esc(prov.get("tz_label") or "")}')]
    if prov.get("case_ref"):
        rows.append(("Case / exhibit", _esc(prov["case_ref"])))
    rows += [("Selection file", f'<span class="mono">{_esc(sel.get("name") or "")}</span>'),
             ("Selection SHA-256", f'<span class="mono">{_esc(sel.get("sha256") or "")}</span>'),
             ("Selection digest", f'<span class="mono">{_esc(sel.get("digest") or "")}</span>'),
             ("Selection saved", f'{_esc(sel.get("exported") or "not recorded")} '
                                 f'(schema {_esc(sel.get("schema") or "?")}, '
                                 f'{_esc(sel.get("tool_version") or "version not recorded")})')]
    reuse = prov.get("reuse") or {}
    if reuse:
        rows.append(("Reused from the full run",
                     f'{reuse.get("reused", 0)} file(s); {reuse.get("rederived", 0)} re-derived from '
                     f'the evidence. {_esc(reuse.get("why") or "")}'))
    body.append("<table>" + "".join(f"<tr><th>{name}</th><td>{value}</td></tr>"
                                    for name, value in rows) + "</table>")

    body.append("<div><b>Source artifacts</b></div>")
    body.append('<table><tr><th>Role</th><th>Detail</th><th>Verdict</th></tr>'
                + _verdict_rows(prov.get("sources")) + "</table>")
    version = prov.get("version")
    if version is not None:
        mark = "yes" if version.get("ok") else "no"
        body.append(f'<div>Tool version: <span class="{mark}">{_esc(version.get("text") or "")}'
                    f"</span></div>")

    body.append("<div><b>What this extract contains</b></div>")
    counts = closure.counts()
    body.append('<table><tr><th>Report</th><th>Selected</th><th>Pulled in</th>'
                "<th>In the extraction</th></tr>"
                + "".join(f'<tr><td>{KIND_NOUN.get(kind, kind)}</td>'
                          f'<td>{c["selected"]}</td><td>{c["pulled_in"]}</td>'
                          f'<td>{c["total"]}</td></tr>' for kind, c in counts.items())
                + "</table>")

    relations = closure.options.get("relations") or {}
    body.append("<div><b>Related items</b> &mdash; "
                + ("following every included row's own links until nothing new is added "
                   "(transitive)" if closure.options.get("transitive")
                   else "one hop from each selected row, plus what a row cannot be shown without")
                + "</div>")
    body.append('<table><tr><th></th><th>Relation</th><th>Basis</th></tr>'
                + "".join(f'<tr><td class="{"yes" if relations.get(r.key) else "no"}">'
                          f'{"&#10004;" if relations.get(r.key) else "&#10008;"}</td>'
                          f'<td>{_esc(r.label)} <span class="mono">({_esc(r.key)})</span></td>'
                          f"<td>{_esc(r.basis)}</td></tr>" for r in RELATIONS)
                + "</table>")
    body.append("<div>Always included, never optional: " + "; ".join(CONTAINMENT) + ".</div>")
    body.append("<div>The legacy Communications / LocalMemories reports have no row selection, so "
                + ("they are included whole." if closure.options.get("legacy_reports")
                   else "they are <b>left out</b> of this extract entirely.") + "</div>")

    excluded = closure.excluded_ref_count()
    body.append(f"<div><b>{excluded}</b> cross-reference(s) point at "
                f"{len(closure.excluded_refs)} item(s) this extract does not contain; each is marked "
                f"in place and listed in <span class='mono'>partial_manifest.json</span>.</div>")
    if prov.get("withheld"):
        body.append(f'<div><b>{len(prov["withheld"])}</b> field(s) are withheld from this extract: '
                    f'<span class="mono">{_esc(", ".join(sorted(prov["withheld"])))}</span>. Each is '
                    f'shown with its label and marked withheld where it would appear.</div>')

    res = closure.resolution
    if res.moved or res.unresolved or res.notes:
        body.append("<div><b>How the selection resolved</b></div>")
        lines = [f'{m["kind"]} {m["was"]} &rarr; {m["now"]} (matched {m["how"]})' for m in res.moved]
        lines += [f'{u["kind"]} {u["id"]} was not found: {u["why"]}' for u in res.unresolved]
        lines += list(res.notes)
        body.append("<ul><li>" + "</li><li>".join(_esc(line) for line in lines) + "</li></ul>")

    return (f'<details class="prov"{" open" if open_by_default else ""}>'
            "<summary>How this partial report was produced, and what it leaves out</summary>"
            f'<div class="provbody">{"".join(body)}</div></details>')


def write_manifest(closure, report_dir, prov=None):
    """``partial_manifest.json`` — the machine-readable statement of what is here and what is not.

    One file for the whole extract, at the report root: the closure with a reason per row, every
    cross-reference that was cut, the relation policy, how each ticked row resolved, and the source
    and version verdicts. This is the file to read when the question is "what was left out", which
    no amount of on-page marking answers in aggregate.
    """
    payload = {"tool": selection_file.TOOL,
               "kind": "partial_manifest",
               "schema": selection_file.SCHEMA,
               "provenance": {key: (prov or {}).get(key) for key in PROVENANCE_KEYS},
               "relations": [{"key": r.key, "label": r.label, "basis": r.basis,
                              "followed": bool((closure.options.get("relations") or {}).get(r.key))}
                             for r in RELATIONS],
               "containment": list(CONTAINMENT),
               **closure.as_dict()}
    path = os.path.join(report_dir or ".", "partial_manifest.json")
    os.makedirs(report_dir or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    logger.info(f"Partial report manifest: {os.path.abspath(path)}")
    return path


def partial_dir(run_dir, stamp=None):
    """``<run>/Reports_partial_<stamp>`` — never the folder the full reports are in.

    A partial report is a different document made from the same evidence, and overwriting the report
    the examiner ticked rows in would destroy the thing the extract is a subset of.
    """
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(run_dir or ".", f"Reports_partial_{stamp}")


# --------------------------------------------------------------------------- reporting

def dry_run_text(closure):
    """What ``--dry-run`` prints: the closure, the reasons, and what was left out. ASCII only."""
    out = []
    res = closure.resolution
    out.append(f"Selection resolved: {res.count()} row(s) ticked")
    if res.moved:
        out.append(f"  {len(res.moved)} matched under a different id than the selection recorded:")
        for move in res.moved[:10]:
            out.append(f"    {move['kind']}: {move['was']} -> {move['now']} ({move['how']})")
    if res.unresolved:
        out.append(f"  {len(res.unresolved)} not found in this run:")
        for miss in res.unresolved[:10]:
            out.append(f"    {miss['kind']}: {miss['id']} - {miss['why']}")

    out.append("")
    out.append("This extract would contain:")
    out.append(f"  {'kind':<6} {'selected':>8} {'pulled in':>10} {'of total':>9}")
    for kind, counts in closure.counts().items():
        out.append(f"  {kind:<6} {counts['selected']:>8} {counts['pulled_in']:>10} "
                   f"{counts['total']:>9}")

    by_relation = defaultdict(int)
    for reasons in closure.reasons.values():
        for reason in reasons:
            if "relation: " in reason:
                by_relation[reason.rsplit("relation: ", 1)[1].rstrip(")")] += 1
    if by_relation:
        out.append("")
        out.append("Pulled in by:")
        for key, n in sorted(by_relation.items(), key=lambda kv: -kv[1]):
            out.append(f"  {key:<22} {n}")

    off = [r.key for r in RELATIONS if not (closure.options.get("relations") or {}).get(r.key)]
    if off:
        out.append("")
        out.append("Relations NOT followed: " + ", ".join(off))
    for warning in closure.warnings:
        out.append(f"WARNING: {warning}")
    return "\n".join(out)


def load_selection(path):
    """Read and migrate a selection file. Returns ``(payload, unattributed message ids)``."""
    payload = selection_file.read_selection(path)
    return selection_file.migrate_selection(payload)
