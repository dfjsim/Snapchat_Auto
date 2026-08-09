"""The selection file — re-exported from the stdlib-only distribution that owns it.

The implementation lives in ``packages/snapchat_auto_selection`` so an external tool can produce a
selection Snapchat Auto accepts without depending on Snapchat Auto: that distribution has no
dependencies at all, while this application pulls pandas, OpenCV, a GUI toolkit and a build backend.

This module stays as the name the rest of the application imports (``report_ui``, ``partial_report``,
the generators and the CLI all use ``scripts.selection_file``), and it is a re-export rather than a copy
— one implementation of the format, nothing to keep in sync. See ``docs/selection_format.md`` for the
format itself and ``snapchat_auto_selection.api`` for the building surface.
"""

from snapchat_auto_selection.format import (          # noqa: F401  (re-exported on purpose)
    KINDS,
    SCHEMA,
    SCHEMA_MAX,
    SCHEMA_MIN,
    TOOL,
    SelectionFormatError,
    file_sha256,
    install_selection,
    migrate_selection,
    parse_selection_text,
    read_selection,
    selection_counts,
    selection_digest,
    selection_js_text,
    selection_json_text,
)

__all__ = [
    "KINDS", "SCHEMA", "SCHEMA_MAX", "SCHEMA_MIN", "TOOL", "SelectionFormatError",
    "file_sha256", "install_selection", "migrate_selection", "parse_selection_text",
    "read_selection", "selection_counts", "selection_digest", "selection_js_text",
    "selection_json_text",
]
