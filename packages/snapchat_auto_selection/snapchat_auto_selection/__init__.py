"""Snapchat Auto selections — read, write and build the file that names which rows to disclose.

A small, **stdlib-only** distribution so another tool can produce a selection Snapchat Auto will accept
without depending on Snapchat Auto itself. Two layers:

* :mod:`snapchat_auto_selection.format` — the file itself: both forms (``.json`` and the drop-in
  ``.js``), reading, migrating, digesting, and installing one next to a report folder.
* :mod:`snapchat_auto_selection.api` — :class:`~snapchat_auto_selection.api.SelectionBuilder`,
  :func:`~snapchat_auto_selection.api.anchor_for`, :func:`~snapchat_auto_selection.api.validate` and
  :func:`~snapchat_auto_selection.api.describe`: build a selection from the identifiers you already
  have, rather than from Snapchat Auto's anchor spelling.

``SCHEMA`` (the file format) and ``API_VERSION`` (this Python surface) move independently. Within a
schema, changes are additive only — new optional fields, new kinds, new alternate keys — and
``validate()`` is the check to run. Anything that would break an existing file bumps ``SCHEMA``, while
``SCHEMA_MIN`` stays as low as this build still reads.

Before writing a file for a particular installation, ask that installation what it supports:

    Snapchat_Auto --describe-selection-api

That is the handshake. Pinning a version of this package does not remove version mismatch — the
examiner's installed build may read an older schema than this one writes — it only relocates it.
"""

from .api import (API_VERSION, IDENTIFIERS, SelectionBuilder, anchor_for, describe, validate)
from .format import (KINDS, SCHEMA, SCHEMA_MAX, SCHEMA_MIN, TOOL, SelectionFormatError,
                     install_selection, migrate_selection, parse_selection_text, read_selection,
                     selection_counts, selection_digest, selection_js_text, selection_json_text)

__all__ = [
    "API_VERSION", "IDENTIFIERS", "KINDS", "SCHEMA", "SCHEMA_MAX", "SCHEMA_MIN", "TOOL",
    "SelectionBuilder", "SelectionFormatError",
    "anchor_for", "describe", "install_selection", "migrate_selection", "parse_selection_text",
    "read_selection", "selection_counts", "selection_digest", "selection_js_text",
    "selection_json_text", "validate",
]

__version__ = "1.0.0"
