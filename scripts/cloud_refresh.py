"""Retrieve from Snapchat's servers for a run folder that already exists, and refresh its reports.

The reports are static pages, so a retrieval the examiner asks for after reading them has to be
done by the tool and the reports rebuilt. Nothing is unzipped again: ``ExtractedData/`` is reused,
and so are ``Reports/run_id.txt`` and ``Reports/selection.js`` — the examiner's saved selections
survive (ticks not yet saved from an open browser tab do not; the Cloud window says so).

Two ways to refresh:

* **targeted** — the three reports the retrieved media changes, in their dependency order: Memories
  (which does the retrieval), then the Library/Caches report, then the cache_controller report,
  which read the manifests the Memories report writes. The others are left as they are.
* **full** — the whole pipeline again (the caller re-runs :func:`Snapchat_Auto.run` with the
  request). Used when this build is not the one that wrote the run's reports, since a targeted
  refresh would leave the run's reports from two different builds.

The run's own settings (timezone, padding, tile server, keychain, ZIP) come from
``Reports/run_settings.json``, which every run of this version writes.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

SETTINGS = "run_settings.json"


def settings_path(run_folder, reports_subdir="Reports"):
    return os.path.join(run_folder, reports_subdir, SETTINGS)


def write_settings(run_folder, **settings):
    """Record how a run was made, so a later retrieval can refresh it the same way."""
    from scripts.app_version import get_version
    path = settings_path(run_folder)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(dict(settings, tool_version=get_version()), fh, indent=1)


def load_settings(run_folder):
    path = settings_path(run_folder)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def refresh_mode(settings, wanted="targeted"):
    """``(mode, why)``: the refresh a run folder can take with this build."""
    from scripts.app_version import get_version
    if wanted == "full":
        return "full", "asked for"
    if not settings:
        return "full", "the run folder records no settings (it was made by an older version)"
    if settings.get("tool_version") != get_version():
        return "full", (f"its reports were written by {settings.get('tool_version')}, this is "
                        f"{get_version()} — a targeted refresh would mix two builds' reports")
    return "targeted", "same build"


def targeted(run_folder, request, settings, keychain=None):
    """Retrieve (through the Memories report) and re-render the three reports it changes."""
    from scripts import cache_controller_report, cache_media_report, memories_media_report
    root = os.path.join(run_folder, "ExtractedData")
    app = memories_media_report.find_app_container(root)
    reports = os.path.join(run_folder, "Reports")
    started = os.getcwd()
    os.chdir(run_folder)
    try:
        memories_media_report.main(app, keychain=keychain or settings.get("keychain") or "",
                                   outdir=os.path.join(reports, "Memories"),
                                   padding=settings.get("padding") or "both",
                                   tz=settings.get("tz") or "local", src_root=root,
                                   tile_server=settings.get("tile_server") or "",
                                   cloud=request, run_folder=run_folder)
        for label, module, sub in (("Cached media", cache_media_report, "CacheMedia"),
                                   ("cache_controller", cache_controller_report, "CacheController")):
            try:
                module.main(app, outdir=os.path.join(reports, sub), tz=settings.get("tz") or "local",
                            src_root=root, report_dir=reports)
            except Exception as error:                     # noqa: BLE001 - as in a full run
                logger.error(f"{label} report failed: {error}")
    finally:
        os.chdir(started)
