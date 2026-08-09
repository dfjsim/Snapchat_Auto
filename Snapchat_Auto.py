import sys
import FreeSimpleGUI as sg
from scripts import ParseSnapchat_iOS
from scripts import getCacheAndroid
from scripts.data import extract_zip
from scripts import parseSnapvideos_PREFETCH
from scripts import offline_maps
from scripts import app_version
from scripts import selection_file
from scripts import source_fingerprint
from scripts import partial_report
import os
import json
import logging
import datetime
import textwrap
from html import escape as _esc

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "loglevel;0"

formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger()
logger.setLevel(logging.INFO)

consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(formatter)
logger.addHandler(consoleHandler)


if getattr(sys, 'frozen', False):
    app_path = sys._MEIPASS
else:
    app_path = os.path.dirname(os.path.abspath(__file__))

logger.info(app_path)


# The reports need the version too — a partial run refuses to reuse anything a *different* build
# extracted or decrypted — and a report module cannot import this one (that re-runs the logging and
# environment setup above). So both live in scripts/app_version.py and are re-exported here, which
# keeps every existing `get_version()` call site working.
get_version = app_version.get_version
get_project_name = app_version.get_project_name
_pyproject_field = app_version._pyproject_field


def _updater():
    """The shared update-check helper, or None when it is not installed.

    It is a project dependency, but requirements.txt — the pip route the README documents — does
    not carry it, and an update check is a convenience, never a precondition for a run.
    """
    try:
        from dfjsim_shared_tools import auto_update
    except Exception as error:
        logger.debug(f"Update checks unavailable: {error}")
        return None
    return auto_update


def check_installer_dir(directory):
    """`(ok, message)` describing the folder the GUI's "Check" button was pointed at."""
    updater = _updater()
    if updater is None:
        return False, ("The update helper (dfjsim_shared_tools) is not installed, so no update "
                       "check can run.")
    return updater.describe_installer_dir(get_project_name(), directory, get_version())


# Remembered GUI selections persist here between runs.
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".snapchat_auto_gui.json")

PADDING_OPTIONS = ['Both (with & without padding)', 'Without padding only', 'With padding only']
PADDING_MAP = {'Both (with & without padding)': 'both', 'Without padding only': 'strip', 'With padding only': 'keep'}
TZ_OPTIONS = ['Local time', 'UTC', 'America/Toronto', 'America/New_York', 'America/Chicago',
              'America/Los_Angeles', 'Europe/London', 'Europe/Paris', 'Australia/Sydney']


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as error:
        logger.warning(f"Could not save GUI settings: {error}")


DISCLAIMER_TEXT = (
    "Snapchat Auto is an independent, community fork provided AS IS, with NO WARRANTY of any "
    "kind.\n\n"
    "It has NOT been thoroughly tested across the many different versions of the Snapchat app, "
    "and the database schemas vary between versions. Some artifacts may therefore be parsed "
    "incompletely, or in rare cases incorrectly or potentially incorrectly in some cases.\n\n"
    "Use it as an aid to analysis — not as a sole authority. Always validate findings against the "
    "original artifacts and corroborate them with other tools before relying on them.")


def show_disclaimer(cfg):
    """Show the one-time AS-IS disclaimer, unless the user ticked 'Don't display again'.

    The choice is persisted in the GUI config (`hide_disclaimer`). Dismissing the dialog any way
    proceeds; it never blocks the run.
    """
    if cfg.get("hide_disclaimer"):
        return
    layout = [
        [sg.Text("Disclaimer — please read", font=("", 12, "bold"))],
        [sg.Text(DISCLAIMER_TEXT, size=(78, 10))],
        [sg.Checkbox("Don't display this again", key="hide")],
        [sg.Push(), sg.Button("I understand", key="ok"), sg.Push()],
    ]
    try:
        window = sg.Window("Snapchat Auto — Disclaimer", layout, modal=True, keep_on_top=True)
        _, values = window.read(close=True)
    except Exception as error:                              # never let the dialog block a run
        logger.debug(f"Could not show disclaimer dialog: {error}")
        return
    if values and values.get("hide"):
        cfg["hide_disclaimer"] = True
        save_config(cfg)


def add_log_file(directory):
    """Attach a file log handler that writes into `directory` (the report/working folder)."""
    log_path = os.path.join(directory, f"SnapchatAuto_{datetime.datetime.today().strftime('%Y%m%d_%H%M%S')}.log")
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.info(f"Log file: {os.path.abspath(log_path)}")


def write_index(root_dir, reports_subdir="Reports", zip_path=None, keychain_path=None,
                closure=None, prov=None):
    """Write <root_dir>/index.html linking to whichever sub-reports were produced under
    <root_dir>/<reports_subdir>/, with the source extraction / keychain paths at the top.

    With a ``closure`` this is a **partial** extract's own index: it carries the PARTIAL banner and the
    provenance block expanded, because this is the page a reader opens first and the one that has to
    say what the folder is before they read anything in it."""
    # Each report opens in its own *named* tab (target), shared with the cross-report links inside
    # the reports, so navigating between reports reuses one tab per report instead of piling up new
    # ones. Ctrl/Shift/middle-click still force a new tab/window (browser default).
    reports = [
        ("Contacts", f"{reports_subdir}/Contacts/Contacts_report.html",
         "Every contact recovered from the friends artifact, linked to their conversation(s).",
         "scauto_contacts"),
        ("Conversations", f"{reports_subdir}/Conversations/Conversations_report.html",
         "Every conversation, with a detail page per conversation: messages, senders, timestamps "
         "and cached chat media.", "scauto_convs"),
        ("Memories", f"{reports_subdir}/Memories/Memories_report.html",
         "Snapchat Memories with all associated media (SCContent + caching-media) and geolocation.",
         "scauto_memories"),
        ("Cache controller (cache_controller.db)", f"{reports_subdir}/CacheController/CacheController_report.html",
         "Every file indexed by cache_controller.db, i.e. the SCContent cache folders, linked to "
         "on-disk cache files, Memories and chats.",
         "scauto_cache"),
        ("Cached media (Library/Caches)", f"{reports_subdir}/CacheMedia/CacheMedia_report.html",
         "Everything under Library/Caches that cache_controller.db does not index: story renders, "
         "the URL-keyed caches, saved chat media, and the cached documents (DNS/HTTP caches, crash "
         "state).", "scauto_cachemedia"),
        ("Communications (legacy)",
         f"{reports_subdir}/Communications_legacy/Communications_legacy_report.html",
         "The original single-page chats + contacts + groups report, kept until the Conversations "
         "and Contacts reports have been validated.", "scauto_comms_legacy"),
        ("Local Memories (legacy)", f"{reports_subdir}/LocalMemories_legacy/LocalMemories_legacy_report.html",
         "Legacy Memories / My Eyes Only decryption report.", "scauto_localmem"),
    ]
    items = []
    for title, rel, desc, target in reports:
        if os.path.exists(os.path.join(root_dir, rel)):
            items.append(f'<li><a href="{rel}" target="{target}">{title}</a>'
                         f'<div class="d">{desc}</div></li>')
    if not items:
        return
    generated = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # source provenance block (extraction ZIP + keychain/keystore) shown at the top of the index
    def _src_row(label, path):
        val = _esc(path) if path else '<span class="none">(none provided)</span>'
        return f'<div class="srow"><span class="lbl">{label}</span><span class="val">{val}</span></div>'
    # The hashes of what the run actually read, on the face of the report rather than only in
    # sources.json — a partial report built later re-checks them and says whether they still match.
    fp = source_fingerprint.read_sources(os.path.join(root_dir, reports_subdir))
    artifact_rows = ""
    if fp and fp.get("artifacts"):
        rows = []
        for role, record in sorted(fp["artifacts"].items()):
            label = record.get("label") or role
            if not record.get("present"):
                why = record.get("why") or "not located"
                rows.append(f'<div class="srow"><span class="lbl">{_esc(label)}</span>'
                            f'<span class="val none">not in this extraction ({_esc(why)})</span></div>')
                continue
            rows.append(
                f'<div class="srow"><span class="lbl">{_esc(label)}</span><span class="val">'
                f'{_esc(record.get("path"))}<br>{record.get("bytes", 0):,} bytes'
                f'<br>MD5 {_esc(record.get("md5"))}<br>SHA-256 {_esc(record.get("sha256"))}'
                + "".join(
                    f'<br><b>{_esc(suffix)}</b> {side.get("bytes", 0):,} bytes '
                    f'&middot; SHA-256 {_esc(side.get("sha256"))}'
                    for suffix, side in sorted((record.get("sidecars") or {}).items()))
                + '</span></div>')
        z = fp.get("zip") or {}
        if z.get("present"):
            zip_hashes = (f'<br>MD5 {_esc(z.get("md5"))}<br>SHA-256 {_esc(z.get("sha256"))}'
                          if z.get("hashed") else
                          '<br><span class="none">not hashed &mdash; run with --hash-zip yes to '
                          'record it</span>')
            rows.append(f'<div class="srow"><span class="lbl">extraction ZIP</span>'
                        f'<span class="val">{z.get("bytes", 0):,} bytes{zip_hashes}</span></div>')
        artifact_rows = (
            '<div class="snote">The databases, plists and keychain this run read &mdash; what decides '
            'what every report here contains. Cached media files are not listed: each one carries its '
            'own MD5 and SHA-256 in the cache reports. A database is hashed together with its '
            '<b>-wal</b>/<b>-shm</b>, because each one is read twice, with the log applied and '
            'without.</div>'
            + "".join(rows)
            + f'<div class="srow"><span class="lbl">Tool version</span>'
              f'<span class="val">{_esc(fp.get("tool_version"))}</span></div>'
              f'<div class="srow"><span class="lbl">Source digest</span>'
              f'<span class="val">{_esc(fp.get("digest"))}</span></div>')
    sources = (f'<div class="sources"><div class="stitle">Sources</div>'
               f'{_src_row("Extraction", zip_path)}'
               f'{_src_row("Keychain / keystore", keychain_path)}'
               f'{artifact_rows}</div>')
    partial_css, banner, _figures = partial_report.page_chrome(closure, None, prov)
    provenance = (partial_report.provenance_html(closure, prov, open_by_default=True)
                  if closure is not None else "")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Snapchat Auto v{get_version()} report</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f4f8;color:#1b1b1f;margin:0}}
 header{{background:#2d2d71;color:#fff;padding:18px 26px}} header h1{{margin:0;font-size:20px}}
 header .sub{{opacity:.85;font-size:13px;margin-top:4px}}
 .sources{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px 18px;margin:22px 26px 0;max-width:760px}}
 .sources .stitle{{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#2d2d71;font-weight:700;margin-bottom:6px}}
 .srow{{display:grid;grid-template-columns:150px 1fr;gap:8px;font-size:13px;padding:2px 0}}
 .srow .lbl{{color:#666;font-weight:600}}
 .srow .val{{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#33367a;overflow-wrap:anywhere}}
 .srow .none{{color:#999;font-style:italic;font-family:-apple-system,Segoe UI,Roboto,sans-serif}}
 .sources .snote{{font-size:12px;color:#666;margin:8px 0 6px;line-height:1.5}}
 .sources{{max-width:920px}}
 ul{{list-style:none;padding:16px 26px 22px;max-width:760px}}
 li{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 18px;margin-bottom:12px}}
 li a{{font-size:16px;font-weight:600;color:#2d2d71;text-decoration:none}} li a:hover{{text-decoration:underline}}
 .d{{color:#666;font-size:13px;margin-top:3px}}
{partial_css}
</style></head><body>
<header><h1>Snapchat Auto v{get_version()} &mdash; Report index</h1><div class="sub">Generated {generated}</div></header>
{banner}{provenance}
{sources}
<ul>{''.join(items)}</ul>
</body></html>"""
    with open(os.path.join(root_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def _map_timezone(tzval):
    tzval = (tzval or "local").strip()
    if tzval.lower() in ("", "local time", "local"):
        return "local"
    if tzval.upper() == "UTC":
        return "utc"
    if tzval.upper().startswith("UTC") and len(tzval) > 3 and tzval[3] in "+-":
        return tzval[3:]                                   # "UTC-04:00" -> "-04:00"
    return tzval


def run(zip_path, keychain="", workdir=".", os_mode="ios", padding="both", tz="local",
        tile_server="", run_name=None, pause=False, hash_zip=False, partial=None):
    """Do one extraction + report run. Shared by the GUI and the command line.

    Everything for the run lives under a single ``Snapchat_Auto-<timestamp>`` folder inside
    ``workdir``: ``ExtractedData/``, ``SnapFixedVideos/``, ``Reports/`` and ``index.html``.
    ``run_name`` pins that folder name instead of using a timestamp, which is what makes a
    scripted re-run land in the same place. Returns the run folder.

    ``pause`` waits for a keypress at the end — the GUI wants that so the console does not vanish;
    a scripted run must not, or it hangs forever with nobody there to press a key.

    ``partial`` (a :class:`partial_report.Request`) makes this a **partial** run: the same pipeline,
    rendering only the rows the examiner's selection names plus the related items they asked for, into
    ``Reports_partial_<stamp>/`` — never over the reports the selection was made in. The extraction
    itself is unchanged, and ``ExtractedData/`` from the full run is reused as it always is, so a
    partial run into the same run folder skips unzipping entirely.
    """
    started = os.getcwd()
    # The keychain read is cached for the length of a run (the legacy and current Memories
    # reports both ask for it). Drop it here so a second run in the same process — the GUI stays
    # open between extractions — never reuses another case's keys, and writes its own
    # decrypted_keychain.plist into its own run folder.
    from scripts import DecryptLocalMemories_iOS as _memkeys
    _memkeys.clear_keychain_cache()
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)
    run_root = run_name or ("Snapchat_Auto-"
                            + datetime.datetime.today().strftime('%Y%m%d_%H%M%S'))
    os.makedirs(run_root, exist_ok=True)
    os.chdir(run_root)
    run_folder = os.path.abspath(".")
    add_log_file(".")
    logger.info(f"Run folder: {run_folder}")

    try:
        if os_mode == "ios":
            logger.info("You chose iOS")
            extracted_files_dir = extract_zip.extract(zip_path, 'ios', dest="ExtractedData")
            if not os.path.exists("SnapFixedVideos"):
                parseSnapvideos_PREFETCH.main(extracted_files_dir[0])
            else:
                logger.info("Found SnapFixedVideos folder, skipping that step")
            # A partial extract gets its own folder. Overwriting the reports the examiner ticked rows
            # in would destroy the very thing the extract is a subset of.
            reports_subdir = "Reports"
            if partial is not None:
                reports_subdir = os.path.basename(partial_report.partial_dir("."))
                if not partial.links_dir:
                    # last resort: this run folder's own full reports. True when a partial run is
                    # pointed at the same --run-name as the full one, which is the fast path.
                    partial.links_dir = os.path.abspath("Reports")
                logger.info(f"Partial report: {os.path.abspath(reports_subdir)}")
                partial_report.check_links_dir(partial.links_dir)
            ParseSnapchat_iOS.main(extracted_files_dir[0], extracted_files_dir[1], keychain,
                                   padding=padding, tz=tz, report_dir="./" + reports_subdir,
                                   tile_server=tile_server,
                                   zip_path=os.path.abspath(zip_path) if zip_path else "",
                                   hash_zip=hash_zip, partial=partial)
            if partial is not None and partial.dry_run:
                logger.info("--dry-run: no report was written")
                return run_folder
            # Write the report index BEFORE the pause, so index.html exists when the "press any
            # key" prompt appears (previously the pause lived inside the parser and blocked this).
            if partial is None:
                write_index(".", "Reports", zip_path=zip_path, keychain_path=keychain)
                index_path = "index.html"
            else:
                # inside the extract, not beside it: the folder is the deliverable, so it carries its
                # own index, its own provenance and its own manifest and can be handed over as it is
                write_index(reports_subdir, ".", zip_path=zip_path, keychain_path=keychain,
                            closure=partial.closure, prov=partial.prov)
                index_path = os.path.join(reports_subdir, "index.html")
            logger.info(f"Report index: {os.path.abspath(index_path)}")
            if pause:
                os.system("pause")
        else:
            logger.info("You chose Android")
            extracted_files_dir = extract_zip.extract(zip_path, 'android', dest="ExtractedData")
            getCacheAndroid.main(extracted_files_dir)
    finally:
        os.chdir(started)
    return run_folder


def diag_keychain(path):
    """`--diag-keychain <file>`: read a keychain and report what it holds, without running an
    extraction. Lets a keychain be checked in seconds on the machine that holds the case data,
    instead of inferring it afterwards from a run log."""
    from scripts import DecryptLocalMemories_iOS as memkeys
    if not path:
        # A CLI flag with a missing argument should fail fast, not pop a GUI dialog and hang a
        # script (or a console with no one watching it).
        print("--diag-keychain requires a path: Snapchat_Auto.exe --diag-keychain <keychain file>")
        return 2
    # Everything goes to the console (the built app keeps its console window), so the check stays
    # scriptable — no dialog to dismiss.
    res = memkeys.diagnose_keychain(path)
    logger.info(f"Format: {res['format'] or 'not recognized'} - {res['items']} item(s), "
                f"{res['snap_items']} in the Snapchat access group")
    return 0 if res["status"] == "ok" else 1


def print_usage():
    print(f"Snapchat Auto v{get_version()}\n\n"
          "usage: Snapchat_Auto.exe [options]\n\n"
          "  (no arguments)          Launch the GUI.\n\n"
          "Run an extraction without the GUI (everything below is optional except --zip):\n"
          "  --zip <file>            Extraction ZIP to process. Implies a headless run.\n"
          "  --keychain <file>       Keychain plist / objection JSON (iOS only).\n"
          "  --workdir <dir>         Where the run folder is created (default: current dir).\n"
          "  --os ios|android        Which parser to use (default: ios).\n"
          "  --tz <spec>             local | utc | <IANA name> | <+/-HH:MM>  (default: local).\n"
          "  --padding both|strip|keep   Memories media padding (default: both).\n"
          "  --tile-server <url>     Offline map tile server, {z}/{x}/{y} template.\n"
          "  --run-name <name>       Use this run-folder name instead of a timestamp, so a\n"
          "                          repeated run lands in the same place.\n"
          "  --hash-zip yes          Also record the extraction ZIP's MD5 and SHA-256. Off by\n"
          "                          default: tens of GB is a long read, and it is the database\n"
          "                          hashes that bind what the reports contain.\n\n"
          "Build a partial report from a saved selection (only the ticked rows, plus what you\n"
          "ask for with them). Add --selection to a normal run; it writes its own folder,\n"
          "Reports_partial_<stamp>/, and never touches the reports the selection was made in:\n"
          "  --selection <file>      The selection.json (or .js) an examiner saved. Required for\n"
          "                          everything else in this section.\n"
          "  --relations <spec>      Which related items to bring in with the ticked rows:\n"
          "                          minimal (only what a row cannot be shown without),\n"
          "                          recommended (default), all, or a list - 'mem_cache,msg_cache'\n"
          "                          to name them, or '-mem_group' for the recommended set minus\n"
          "                          one. Add 'transitive' to keep following them, and\n"
          "                          'legacy_reports' to include the two legacy reports whole\n"
          "                          (neither has row selection, so they are all-or-nothing).\n"
          "  --case-ref <text>       Case / exhibit reference, stamped on every page.\n"
          "  --dry-run yes           Resolve the selection, work out what the extract would hold,\n"
          "                          print it, and write nothing.\n"
          "  --unresolved refuse|drop    A ticked row this run has no match for. Default refuse:\n"
          "                          an extract quietly missing evidence is worse than one that\n"
          "                          will not build.\n"
          "  --sources-mismatch refuse|proceed   The source artifacts differ from the run the\n"
          "                          selection was made in. Default refuse; proceed builds it and\n"
          "                          states the mismatch in the banner of every page.\n"
          "  --version-mismatch refuse|resolve   A different build produced the selection.\n"
          "                          Default refuse; resolve builds it, re-derives everything, and\n"
          "                          re-resolves the ticked rows against this run.\n"
          "  --no-reuse yes          Re-derive everything from the evidence, reusing nothing an\n"
          "                          earlier run produced.\n"
          "  --max-rows <n>          Warn above this many included rows (default 5000).\n"
          "  --links-dir <dir>       The full report folder the selection was made in, which is\n"
          "                          where the cross-report manifests are read from (default:\n"
          "                          the Reports folder of this run folder).\n\n"
          "Selections (the rows an examiner ticked in the reports):\n"
          "  --install-selection <file>   Put a saved selection.json (or .js) where the reports\n"
          "                          load it, as <report folder>/selection.js. Browsers refuse to\n"
          "                          save a .js, and renaming a .json will NOT work - the reports\n"
          "                          load it as a script and bare JSON fails silently. This does\n"
          "                          the conversion. Any existing file is backed up.\n"
          "  --report-dir <dir>      Which report folder to install into (default: ./Reports).\n"
          "  --force yes             Install even when the selection names a different run.\n\n"
          "For another tool producing a selection (see docs/selection_format.md, and the\n"
          "dependency-free 'snapchat-auto-selection' package if it can import Python):\n"
          "  --describe-selection-api     What this build supports, as JSON: the schema range it\n"
          "                          reads and writes, every kind and the identifiers it takes, and\n"
          "                          the relation vocabulary. Ask this BEFORE writing a file -\n"
          "                          pinning a package version does not prevent a mismatch with the\n"
          "                          build an examiner has installed.\n"
          "  --validate-selection <file>  Report every problem with a selection file, or confirm it\n"
          "                          is valid. Exit code 0 when there are none.\n"
          "  --make-selection <out.json> --items <items.json> [--relations <spec>] [--note <text>]\n"
          "                          Build a selection from identifiers, without importing anything:\n"
          "                          items.json is a list of {\"kind\": ..., <identifiers>} objects.\n\n"
          "Other:\n"
          "  --diag-keychain <file>  Check a keychain file and report what it holds, without\n"
          "                          running an extraction. Exit code 0 if egocipher was\n"
          "                          recovered, 1 otherwise.\n"
          "  --help, -h              Show this message.\n\n"
          "A headless run never pauses for a keypress, so it is safe to call from a script.")


# The headless options, and whether each takes a value. Every one does, which is what `_parse_cli`'s
# idiom requires — hence `--dry-run yes` rather than a bare `--dry-run`.
_CLI_OPTIONS = {"zip": True, "keychain": True, "workdir": True, "os": True, "tz": True,
                "padding": True, "tile-server": True, "run-name": True, "hash-zip": True,
                # a partial run: the same pipeline, rendering only the rows a selection names
                "selection": True, "relations": True, "case-ref": True, "unresolved": True,
                "sources-mismatch": True, "version-mismatch": True, "no-reuse": True,
                "max-rows": True, "links-dir": True, "dry-run": True}


def _yes(value):
    return str(value or "").strip().lower() in ("yes", "y", "true", "1", "on")


def _partial_request(values):
    """Build the :class:`partial_report.Request` a partial run needs. Returns (request, error).

    Everything is settled here, before the pipeline starts, so the CLI and the GUI hand the parser the
    same object and neither can wire up a subtly different run.
    """
    path = values["selection"]
    if not os.path.isfile(path):
        return None, f"selection file not found: {path}"
    try:
        payload, unattributed = partial_report.load_selection(path)
    except selection_file.SelectionFormatError as error:
        return None, str(error)
    if unattributed:
        # A schema-1 file's bare `msg-12.0` ids name a message number with no conversation, and that
        # number restarts in every chat. Promoting them would invent a fact; the examiner re-ticks.
        logger.warning(f"{len(unattributed)} message selection(s) in {os.path.basename(path)} name a "
                       f"message number with no conversation, so they cannot be attributed and are "
                       f"left out. Re-tick those messages in this run's reports and save again.")

    options = partial_report.default_options()
    try:
        options["relations"] = partial_report.parse_relations(values.get("relations"))
    except ValueError as error:
        return None, str(error)
    options.update(partial_report.parse_policy(values.get("relations")))
    for name, key in (("unresolved", "unresolved"), ("sources-mismatch", "sources_mismatch"),
                      ("version-mismatch", "version_mismatch")):
        if values.get(name):
            options[key] = values[name].strip().lower()
    if options["unresolved"] not in ("refuse", "drop"):
        return None, "--unresolved must be 'refuse' or 'drop'"
    if options["sources_mismatch"] not in ("refuse", "proceed"):
        return None, "--sources-mismatch must be 'refuse' or 'proceed'"
    if options["version_mismatch"] not in ("refuse", "resolve"):
        return None, "--version-mismatch must be 'refuse' or 'resolve'"
    options["no_reuse"] = _yes(values.get("no-reuse"))
    if values.get("max-rows"):
        try:
            options["max_rows"] = int(values["max-rows"])
        except ValueError:
            return None, f"--max-rows must be a number, not '{values['max-rows']}'"

    counts = selection_file.selection_counts(payload)
    prov = {"selection": {"name": os.path.basename(path),
                          "sha256": selection_file.file_sha256(path),
                          "digest": selection_file.selection_digest(payload),
                          "exported": payload.get("exported") or "",
                          "schema": payload.get("schema"),
                          "tool_version": payload.get("tool_version") or "",
                          "counts": counts},
            "case_ref": (values.get("case-ref") or "").strip(),
            "tool_version": get_version()}
    logger.info(f"Selection {os.path.basename(path)}: "
                + (", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()) if n)
                   or "nothing ticked"))
    # Where the cross-report manifests come from. Derived from the selection file's own location
    # unless named: the GUI makes a new run folder for every run, so this run's own Reports/ is not
    # the folder the selection was made in and guessing it loses every cross-report link.
    links_dir = (values.get("links-dir") or "").strip()
    if not links_dir:
        links_dir = partial_report.find_links_dir(path, values.get("workdir", ""))
        if links_dir:
            logger.info(f"Cross-report links will be resolved against {links_dir} "
                        f"(the report folder this selection was saved from)")
    return partial_report.Request(payload, options, prov, links_dir=links_dir,
                                  dry_run=_yes(values.get("dry-run"))), None


def _parse_cli(args):
    """Parse the headless options. Returns (values, error message or None)."""
    return _parse_options(args, _CLI_OPTIONS)


def run_cli(args):
    """`--zip …`: run headlessly and return an exit code."""
    values, error = _parse_cli(args)
    if error:
        print(f"Snapchat Auto: {error}\n")
        print_usage()
        return 2
    os_mode = (values.get("os") or "ios").lower()
    if os_mode not in ("ios", "android"):
        print(f"Snapchat Auto: --os must be 'ios' or 'android', not '{os_mode}'")
        return 2
    zip_path = values["zip"]
    if not os.path.isfile(zip_path):
        print(f"Snapchat Auto: extraction ZIP not found: {zip_path}")
        return 2
    keychain = values.get("keychain", "")
    if keychain and not os.path.isfile(keychain):
        print(f"Snapchat Auto: keychain not found: {keychain}")
        return 2
    padding = (values.get("padding") or "both").lower()
    if padding not in ("both", "strip", "keep"):
        print(f"Snapchat Auto: --padding must be both, strip or keep, not '{padding}'")
        return 2

    logger.info(f"Snapchat Auto v{get_version()}")
    partial = None
    if values.get("selection"):
        if os_mode != "ios":
            print("Snapchat Auto: --selection is iOS only for now")
            return 2
        partial, error = _partial_request(values)
        if error:
            print(f"Snapchat Auto: {error}")
            return 2

    try:
        folder = run(zip_path=zip_path, keychain=keychain,
                     workdir=values.get("workdir", "."), os_mode=os_mode, padding=padding,
                     tz=_map_timezone(values.get("tz", "local")),
                     tile_server=(values.get("tile-server") or "").strip(),
                     run_name=values.get("run-name"), pause=False,
                     hash_zip=(values.get("hash-zip") or "").lower()
                              in ("yes", "y", "true", "1"),
                     partial=partial)
    except partial_report.EvidenceMismatch as error:
        # Its own exit code: a script driving several extractions needs to tell "this is the wrong
        # evidence for that selection" apart from "the run broke".
        logger.error(str(error))
        return 3
    except (LookupError, partial_report.AmbiguousSelection) as error:
        logger.error(f"Selection could not be resolved against this run: {error}")
        return 4
    except Exception as error:
        logger.error(f"Run failed: {error}")
        return 1
    logger.info(f"Done: {folder}")
    return 0


# Its own option table, kept out of `_CLI_OPTIONS` so `run_cli`'s "--zip is required" rule is
# untouched. Every option takes a value, which is what `_parse_cli`'s idiom requires.
_SELECTION_OPTIONS = {"install-selection": True, "report-dir": True, "force": True}


def _parse_options(args, table):
    """Parse an option table where every option takes a value. Returns (values, error or None).

    A value may begin with a single ``-``: ``--relations -mem_group`` means "the recommended relations
    minus that one", and rejecting it as a missing value made the documented form unusable. Only a
    ``--`` prefix still reads as another option, which is what catches the real mistake of writing two
    options in a row and forgetting the value between them.
    """
    values, index = {}, 0
    while index < len(args):
        token = args[index]
        name = token.lstrip("-/").lower()
        if name not in table:
            return values, f"unknown option '{token}'"
        index += 1
        if index >= len(args) or args[index].startswith("--"):
            return values, f"'{token}' requires a value"
        values[name] = args[index]
        index += 1
    return values, None


def describe_selection_api():
    """`--describe-selection-api`: what this installed build supports, as JSON on stdout.

    The handshake an external tool runs **before** writing a selection. Pinning a version of the
    ``snapchat_auto_selection`` package does not remove version mismatch — the examiner's installed
    build may read an older schema than the pinned one writes — it only relocates it, so the question
    has to be asked of the executable that will consume the file.

    The format half comes from the package (schemas, kinds, the identifiers each accepts). The rest is
    what only this build knows: its own version, and the relation vocabulary a partial run understands.
    """
    from snapchat_auto_selection import api as selection_api

    payload = dict(selection_api.describe())
    payload["tool_version"] = get_version()
    payload["relations"] = {
        relation.key: {"label": relation.label, "from": relation.src, "to": relation.dst,
                       "default": relation.default, "basis": relation.basis}
        for relation in partial_report.RELATIONS}
    payload["relation_presets"] = {name: sorted(k for k, on in preset.items() if on)
                                   for name, preset in partial_report.PRESETS.items()}
    payload["relation_switches"] = list(partial_report.POLICY_TOKENS)
    payload["containment"] = list(partial_report.CONTAINMENT)
    print(json.dumps(payload, indent=1, sort_keys=True))
    return 0


def run_validate_selection(args):
    """`--validate-selection <file>`: report every problem with a selection file, or say it is valid."""
    from snapchat_auto_selection import api as selection_api

    path = args[0] if args else ""
    if not path or not os.path.isfile(path):
        print("--validate-selection needs a file: Snapchat_Auto --validate-selection <selection.json>")
        return 2
    try:
        payload = selection_file.read_selection(path)
    except selection_file.SelectionFormatError as error:
        print(f"{os.path.basename(path)}: not a selection file this build can read\n  {error}")
        return 1
    problems = selection_api.validate(payload)
    counts = selection_file.selection_counts(payload)
    print(f"{os.path.basename(path)}: schema {payload.get('schema')}, "
          + (", ".join(f"{n} {kind}" for kind, n in sorted(counts.items())) or "nothing selected"))
    if payload.get("sources"):
        print("  carries source fingerprints, so a partial run can verify the evidence")
    else:
        print("  no source fingerprints — a partial run will report that verification was not "
              "possible, which is not an error")
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    return 1 if problems else 0


_MAKE_OPTIONS = {"make-selection": True, "items": True, "relations": True, "note": True}


def run_make_selection(args):
    """`--make-selection <out.json> --items <items.json>`: build a selection from identifiers.

    The two-process route for an integrator that cannot import Python: describe the items in a flat
    JSON list, get a selection file back, then run it with ``--selection``. ``items.json`` is a list of
    objects, each naming a ``kind`` and the identifiers that kind takes — the same names
    :class:`snapchat_auto_selection.api.SelectionBuilder` accepts, and the ones
    ``--describe-selection-api`` reports.
    """
    from snapchat_auto_selection import api as selection_api

    values, error = _parse_options(args, _MAKE_OPTIONS)
    if error:
        print(f"Snapchat Auto: {error}\n")
        print_usage()
        return 2
    out_path, items_path = values["make-selection"], values.get("items")
    if not items_path or not os.path.isfile(items_path):
        print("--make-selection needs --items <items.json>: a list of {\"kind\": …, identifiers…}")
        return 2
    try:
        with open(items_path, encoding="utf-8-sig") as fh:
            items = json.load(fh)
    except (OSError, ValueError) as error:
        print(f"Could not read {items_path}: {error}")
        return 2
    if isinstance(items, dict):                      # tolerate {"items": [...]}
        items = items.get("items")
    if not isinstance(items, list):
        print(f"{items_path} should hold a list of items (or an object with an 'items' list)")
        return 2

    adders = {"conv": "add_conversation", "msg": "add_message", "ct": "add_contact",
              "mem": "add_memory", "cc": "add_cache_entry", "cm": "add_cached_file"}
    builder = selection_api.SelectionBuilder(note=values.get("note", ""))
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict) or item.get("kind") not in adders:
            print(f"item {n}: needs a 'kind' of {', '.join(sorted(adders))}")
            return 2
        fields = {k: v for k, v in item.items() if k != "kind"}
        try:
            getattr(builder, adders[item["kind"]])(**fields)
        except TypeError as error:
            print(f"item {n} ({item['kind']}): {error}")
            return 2
        except ValueError as error:
            print(f"item {n}: {error}")
            return 2
    if values.get("relations"):
        try:
            partial_report.parse_relations(values["relations"])       # reject a typo now, not later
        except ValueError as error:
            print(f"Snapchat Auto: {error}")
            return 2
        builder.set_relations(values["relations"])

    problems = selection_api.validate(builder.to_payload())
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if problems:
        return 1
    builder.write_json(out_path)
    counts = builder.count()
    print(f"{out_path}: " + (", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))
                             or "nothing selected"))
    return 0


def run_install_selection(args):
    """`--install-selection <file>`: place a saved selection where the reports load it."""
    values, error = _parse_options(args, _SELECTION_OPTIONS)
    if error:
        print(f"Snapchat Auto: {error}\n")
        print_usage()
        return 2
    source = values["install-selection"]
    if not os.path.isfile(source):
        print(f"Snapchat Auto: selection file not found: {source}")
        return 2
    report_dir = values.get("report-dir") or os.path.join(".", "Reports")
    if not os.path.isdir(report_dir):
        print(f"Snapchat Auto: report folder not found: {report_dir}\n"
              "Point --report-dir at the Reports folder of the run these selections were made in.")
        return 2
    force = (values.get("force") or "").lower() in ("yes", "y", "true", "1")

    logger.info(f"Snapchat Auto v{get_version()}")
    try:
        target, info = selection_file.install_selection(report_dir, source, force=force)
    except selection_file.SelectionFormatError as error:
        logger.error(str(error))
        return 1

    counts = info["counts"]
    total = sum(counts.values())
    logger.info(f"Installed {total} selection(s) into {target}")
    for kind in sorted(counts):
        logger.info(f"  {kind:<5} {counts[kind]}")
    logger.info(f"  selection digest {info['digest']}")
    if info["backup"]:
        logger.info(f"  previous file kept as {os.path.basename(info['backup'])}")
    if info["run_id_mismatch"]:
        logger.warning(f"Installed despite a run mismatch: the selection names run "
                       f"{info['run_id']!r}, these reports are run {info['report_run_id']!r}. "
                       f"Some of its ids may name nothing here.")
    if info["unattributed_msg_ids"]:
        logger.warning(
            f"{len(info['unattributed_msg_ids'])} message selection(s) were dropped: they predate "
            f"per-conversation message ids and name a message number with no conversation, and the "
            f"same number exists in several chats. Re-tick those messages and save again.")
    return 0


def _hint(text, width=88):
    """One of the small explanatory lines under a settings field.

    The text is wrapped here rather than by the widget because FreeSimpleGUI only wraps a Text
    element whose size spans several rows, and then at a pixel width it derives from the font — so
    the row count has to be guessed right or the text is silently clipped. Wrapping the string
    keeps every hint inside the width the rest of the layout already needs, instead of stretching
    the window to fit one long line. The colour is near-white: grey text was all but invisible on
    this theme's mid-blue background, and the smaller font is what marks a hint as secondary.
    """
    return sg.Text(textwrap.fill(text, width), font=("", 9), text_color="#eef3fa")


def _relations_spec(state):
    """The dialog's checkboxes as a ``--relations`` spec, so both front ends speak one vocabulary."""
    tokens = [key for key, on in state["relations"].items() if on]
    if state.get("transitive"):
        tokens.append("transitive")
    if state.get("legacy_reports"):
        tokens.append("legacy_reports")
    return ",".join(tokens) if tokens else "minimal"


def _confirm_selection(path, workdir):
    """Check a selection against the reports it was made in, before a long extraction starts.

    A cheap up-front check, and not the authoritative one: it compares the fingerprints the selection
    carries against the ones the **full report folder** recorded, which answers "does this selection
    belong to that run". Whether the ZIP about to be processed is that same evidence can only be
    answered once it is unpacked, and the pipeline checks that too. Asking here means the examiner
    settles the question before waiting rather than after.

    Returns True to go ahead.
    """
    try:
        payload, _unattributed = partial_report.load_selection(path)
    except selection_file.SelectionFormatError as error:
        sg.popup_error(str(error), keep_on_top=True)
        return False

    version = source_fingerprint.check_version(payload.get("tool_version"))
    problems = [] if version.ok or not version.comparable else [version.summary]

    # the full reports of the run folder the partial one will be written into
    reports = os.path.join(workdir or ".", "Reports")
    recorded = source_fingerprint.read_sources(reports)
    verdict = None
    if recorded:
        verdict = source_fingerprint.verify(payload.get("sources") or {}, recorded)
        if not verdict.ok and verdict.comparable:
            problems.append(verdict.summary)
    if not problems:
        return True

    detail = "\n".join(f"  - {p}" for p in problems)
    if verdict is not None:
        detail += "\n\n" + source_fingerprint.verdict_text(verdict)
    answer = sg.popup_yes_no(
        "This selection was not made from what is about to be processed:\n\n" + detail
        + "\n\nBuild the partial report anyway? The mismatch will be stated in the banner of every "
          "page and recorded in partial_manifest.json.",
        title="Selection does not match this evidence", keep_on_top=True)
    return answer == "Yes"


def _describe_selection(window, path, values):
    """Say what a chosen selection file holds, and offer back the paths it was made from.

    The paths are the point: the examiner ticked those rows against one extraction with one keychain,
    and finding both again months later is the tedious part of building a partial report. A recorded
    path that no longer exists is *not* filled in — it is reported as a hint, because a pre-filled path
    that does not resolve is worse than an empty field.
    """
    note = window["selection_note"]
    if not path:
        note.update("")
        return
    if not os.path.isfile(path):
        note.update("No such file.", text_color="#ffb0b0")
        return
    try:
        payload, unattributed = partial_report.load_selection(path)
    except selection_file.SelectionFormatError as error:
        note.update(f"Not a selection file this build can read: {error}", text_color="#ffb0b0")
        return

    counts = selection_file.selection_counts(payload)
    bits = [", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()) if n) or "nothing ticked"]
    if payload.get("tool_version"):
        bits.append(f"saved by {payload['tool_version']}")
    if unattributed:
        bits.append(f"{len(unattributed)} message tick(s) name a message number with no "
                    f"conversation and cannot be attributed — re-tick those in this run's reports")

    sources = payload.get("sources") or {}
    for field, key in (("zip", "zip"), ("keychain", "keychain"), ("workdir", "workdir")):
        recorded = (sources.get(key) or {}).get("path") if isinstance(sources.get(key), dict) \
            else sources.get(key)
        if not recorded:
            continue
        exists = os.path.exists(recorded)
        if exists and not (values.get(field) or "").strip():
            window[field].update(recorded)
        elif not exists:
            bits.append(f"the {field} it was made from is not at {recorded}")
    note.update(" · ".join(bits), text_color="#eef3fa")


def _relations_dialog(state):
    """Which related items to bring in with the ticked rows. Edits *state* in place.

    Each checkbox carries the basis of its association, because an examiner deciding whether to
    include something needs to know what the association rests on, not only its name.
    """
    by_src = {}
    for relation in partial_report.RELATIONS:
        by_src.setdefault(relation.src, []).append(relation)
    label = {"conv": "From a selected conversation", "msg": "From an included message",
             "ct": "From a selected contact", "mem": "From a selected Memory",
             "cc": "From a selected cache_controller entry",
             "cm": "From a selected Library/Caches file"}

    rows = [[sg.Text("Always included, and not optional:")]]
    for line in partial_report.CONTAINMENT:
        rows.append([sg.Text(f"   • {line}", font=("", 9))])
    for src, relations in by_src.items():
        rows.append([sg.Text(label.get(src, src), font=("", 10, "bold"), pad=((0, 0), (10, 0)))])
        for relation in relations:
            rows.append([sg.Checkbox(relation.label, default=bool(state["relations"].get(relation.key)),
                                     key=f"rel_{relation.key}")])
            rows.append([_hint("      " + relation.basis, width=100)])
    rows.append([sg.Checkbox("Keep following these until nothing new is added (transitive)",
                             default=state["transitive"], key="transitive",
                             pad=((0, 0), (12, 0)))])
    rows.append([_hint('      Off by default, and worth leaving off: with everything on, one message '
                       'reaches its cache entry, then that entry\'s Memory, then that Memory\'s other '
                       'entries, then their messages — which is most of a case. One hop from each '
                       'ticked row is predictable and explainable.', width=100)])
    rows.append([sg.Checkbox("Include the two legacy reports whole (Communications, Local Memories)",
                             default=state["legacy_reports"], key="legacy_reports")])
    rows.append([_hint('      Neither has row selection, so they are all-or-nothing. Left out by '
                       'default: the legacy Memories report decrypts every Memory on the device.',
                       width=100)])
    rows.append([sg.Push(), sg.Button("Minimal"), sg.Button("Recommended"), sg.Button("Everything"),
                 sg.Button("Ok"), sg.Button("Cancel")])

    window = sg.Window("Related items to include", [[sg.Column(rows, scrollable=True,
                                                               vertical_scroll_only=True,
                                                               size=(760, 560))]],
                       modal=True, keep_on_top=True)
    try:
        while True:
            event, values = window.read()
            if event in (sg.WIN_CLOSED, "Cancel"):
                return
            if event in ("Minimal", "Recommended", "Everything"):
                preset = {"Minimal": "minimal", "Recommended": "recommended",
                          "Everything": "all"}[event]
                for relation in partial_report.RELATIONS:
                    window[f"rel_{relation.key}"].update(
                        bool(partial_report.PRESETS[preset].get(relation.key)))
                window["transitive"].update(preset == "all")
                continue
            if event == "Ok":
                state["relations"] = {r.key: bool(values[f"rel_{r.key}"])
                                     for r in partial_report.RELATIONS}
                state["transitive"] = bool(values["transitive"])
                state["legacy_reports"] = bool(values["legacy_reports"])
                return
    finally:
        window.close()


def main(args):
    flag = args[0].lstrip("-/").lower() if args else ""
    # Re-entry as the poster-frame worker. A packaged build has no interpreter to run
    # "python -m scripts.data.poster_worker" with — sys.executable IS this program — so the report
    # spawns this executable with this flag instead. It must be handled before anything else:
    # without it the packaged app would launch a copy of its own GUI per cached video.
    if flag in ("poster-worker", "posterworker"):
        from scripts.data import poster_worker
        sys.exit(poster_worker.main(args[1:]))
    if flag in ("help", "h", "?"):
        print_usage()
        sys.exit(0)
    if flag in ("diag-keychain", "diagkeychain"):
        sys.exit(diag_keychain(args[1] if len(args) > 1 else ""))
    if flag in ("install-selection", "installselection"):
        sys.exit(run_install_selection(args))
    if flag in ("describe-selection-api", "describeselectionapi"):
        sys.exit(describe_selection_api())
    if flag in ("validate-selection", "validateselection"):
        sys.exit(run_validate_selection(args[1:]))
    if flag in ("make-selection", "makeselection"):
        sys.exit(run_make_selection(args))
    if flag in _CLI_OPTIONS:                                  # a headless run
        sys.exit(run_cli(args))
    if flag:                                                  # an argument was given but not
        print_usage()                                         # recognized — don't silently fall
        sys.exit(2)                                           # through to the GUI

    logger.info(f"Snapchat Auto v{get_version()}")
    cfg = load_config()
    # Only for an examiner who pointed the tool at a folder of newer builds (GUI field below).
    # It runs before anything else because accepting an update launches the installer and ends
    # this process; a headless run never gets here, and must not — nobody is there to answer.
    if (updater := _updater()):
        updater.check_for_update(get_project_name(), cfg.get("installer_dir", ""), get_version())
    show_disclaimer(cfg)

    def _browse_start(this_val, other_val, saved_key):
        """Start a file dialog in the folder of the other field, else this field's saved dir."""
        for candidate in (other_val, this_val, cfg.get(saved_key, "")):
            if candidate and os.path.dirname(candidate):
                return os.path.dirname(candidate)
        return "."

    has_zip, has_kc = bool(cfg.get("zip")), bool(cfg.get("keychain"))
    # The relation policy for a partial run, remembered between runs (the selection file and the case
    # reference are not — see the hints below the fields).
    relation_state = {"relations": dict(cfg.get("partial", {}).get("relations")
                                        or partial_report.PRESETS["recommended"]),
                      "transitive": bool(cfg.get("partial", {}).get("transitive")),
                      "legacy_reports": bool(cfg.get("partial", {}).get("legacy_reports"))}
    layout = [
        [sg.Text("Select Settings")],
        [sg.Radio('IOS', 'OS', default=True), sg.Radio('Android', 'OS')],
        [sg.Text('Extraction zip')],
        [sg.In("", key="zip"), sg.Button('Browse', key="zip_browse"),
         sg.Button('Use previous', key="zip_prev", visible=has_zip, tooltip=cfg.get("zip", ""))],
        [sg.Text('Keychain (iOS Only)')],
        [sg.In("", key="keychain"), sg.Button('Browse', key="keychain_browse"),
         sg.Button('Use previous', key="keychain_prev", visible=has_kc, tooltip=cfg.get("keychain", ""))],
        [sg.Text('Working/Temp/Report directory (required)')],
        [sg.In(cfg.get("workdir", ""), key="workdir"),
         sg.FolderBrowse(target="workdir", initial_folder=cfg.get("workdir") or ".")],
        [sg.Text('Memories media hashes (iOS)'),
         sg.Combo(PADDING_OPTIONS, default_value=cfg.get("padding", PADDING_OPTIONS[0]), key="padding", readonly=True, size=(30, 1))],
        [sg.Text('Timestamp timezone (iOS)'),
         sg.Combo(TZ_OPTIONS, default_value=cfg.get("timezone", "Local time"), key="timezone", size=(30, 1)),
         sg.Text('(or type an IANA name / ±HH:MM)')],
        [_hint('Daylight saving time is applied automatically for named zones '
               '(e.g. America/Toronto).')],
        [sg.Text('Offline map tile server (optional)')],
        [sg.In(cfg.get("tile_server", ""), key="tile_server"),
         sg.Button('Test', key="tile_test")],
        [_hint('Your own XYZ tile server, e.g. http://localhost:8080 or '
               'http://host/tiles/{z}/{x}/{y}.png. When set, each geolocated Memory gets a small '
               'map on its detail page. Nothing is downloaded when this is empty.')],
        [sg.Text('Selection file — build a partial report (optional, iOS)')],
        [sg.In("", key="selection", enable_events=True),
         sg.Button('Browse', key="selection_browse"),
         sg.Button('Related items…', key="relations_edit")],
        [_hint('A selection.json an examiner saved from the reports. With one, this run renders only '
               'the rows it names plus the related items you choose, into its own '
               'Reports_partial_<stamp>/ folder — the full reports are never touched. Leave empty '
               'for a normal, complete run.')],
        [sg.Text('', key="selection_note", font=("", 9), text_color="#eef3fa")],
        [sg.Text('Case / exhibit reference (stamped on every page of a partial report)')],
        [sg.In("", key="case_ref")],
        [_hint('Not remembered between runs: carrying one case reference onto another case is a real '
               'error, and a saved default is how that happens.')],
        [sg.Text('Folder with newer builds, for update checks (optional)')],
        [sg.In(cfg.get("installer_dir", ""), key="installer_dir"),
         sg.FolderBrowse(target="installer_dir", initial_folder=cfg.get("installer_dir") or "."),
         sg.Button('Check', key="installer_check")],
        [_hint('A folder where your organization publishes new builds of this tool (e.g. a '
               'shared drive). At each start, a newer installer found there is offered. The '
               'folder is only checked at the next start, and only when this is not empty.')],
        [sg.Button('Ok'), sg.Button('Cancel')]]

    window = sg.Window(f'Snapchat Auto v{get_version()}', layout)
    while True:
        event, values = window.read()
        if event in (sg.WIN_CLOSED, "Cancel"):
            window.close()
            sys.exit()
        if event == "zip_prev":
            window["zip"].update(cfg.get("zip", ""))
        elif event == "keychain_prev":
            window["keychain"].update(cfg.get("keychain", ""))
        elif event == "zip_browse":
            picked = sg.popup_get_file("Select extraction ZIP", no_window=True, keep_on_top=True,
                                       initial_folder=_browse_start(values["zip"], values["keychain"], "zip"),
                                       file_types=(("All Files", "*.*"),))
            if picked:
                window["zip"].update(picked)
        elif event == "keychain_browse":
            picked = sg.popup_get_file("Select keychain", no_window=True, keep_on_top=True,
                                       initial_folder=_browse_start(values["keychain"], values["zip"], "keychain"),
                                       file_types=(("Keychain (plist/json)", "*.plist *.json"), ("All Files", "*.*")))
            if picked:
                window["keychain"].update(picked)
        elif event == "tile_test":
            if not values["tile_server"].strip():
                sg.popup("Enter a tile server URL first (or leave it empty for no maps).",
                         title="Offline map tile server", keep_on_top=True)
                continue
            ok, message = offline_maps.test_server(values["tile_server"])
            (sg.popup if ok else sg.popup_error)(message, title="Offline map tile server",
                                                 keep_on_top=True)
        elif event == "installer_check":
            # Report a mistyped path or a disconnected share while the examiner is still looking
            # at the field, instead of silently never offering an update.
            ok, message = check_installer_dir(values["installer_dir"])
            (sg.popup if ok else sg.popup_error)(message, title="Update checks", keep_on_top=True)
        elif event == "selection_browse":
            picked = sg.popup_get_file("Select a saved selection", no_window=True, keep_on_top=True,
                                       file_types=(("Selection", "*.json *.js"), ("All", "*.*")),
                                       initial_folder=_browse_start(values["selection"],
                                                                   values["zip"], "workdir"))
            if picked:
                window["selection"].update(picked)
                _describe_selection(window, picked, values)
        elif event == "selection":
            _describe_selection(window, values["selection"], values)
        elif event == "relations_edit":
            _relations_dialog(relation_state)
        elif event == "Ok":
            if not values["zip"] or not os.path.isfile(values["zip"]):
                sg.popup_error("Please select a valid extraction ZIP file.")
                continue
            if not values["workdir"]:
                sg.popup_error("Please select a Working/Temp/Report directory (required).")
                continue
            # A tile server is tested before the run starts, so a typo is caught now rather than
            # after a long extraction — but the examiner stays in charge of continuing without maps.
            if values["tile_server"].strip():
                ok, message = offline_maps.test_server(values["tile_server"])
                logger.info(f"Offline map tile server: {message}")
                if not ok and sg.popup_yes_no(
                        message + "\n\nRun anyway, without offline maps?",
                        title="Offline map tile server", keep_on_top=True) != "Yes":
                    continue
                if not ok:
                    values["tile_server"] = ""
            if values["selection"].strip():
                if not os.path.isfile(values["selection"].strip()):
                    sg.popup_error("That selection file does not exist.", keep_on_top=True)
                    continue
                if not _confirm_selection(values["selection"].strip(), values["workdir"]):
                    continue
            break
    window.close()

    # merge into cfg so other saved settings (e.g. hide_disclaimer) are preserved
    cfg.update({"zip": values["zip"], "keychain": values["keychain"], "workdir": values["workdir"],
                "padding": values.get("padding", PADDING_OPTIONS[0]),
                "timezone": values.get("timezone", "Local time"),
                "tile_server": values.get("tile_server", "").strip(),
                # The relation policy is a working preference and is remembered. The selection file and
                # the case reference deliberately are not: both belong to one case.
                "partial": relation_state,
                # Never committed and never bundled: this repository is public, so an internal
                # share path may only live in this examiner's own settings file.
                "installer_dir": values.get("installer_dir", "").strip()})
    save_config(cfg)

    # values[0]/values[1] are the iOS/Android radios. One is always selected (iOS is the default),
    # but pick explicitly rather than treating "not iOS" as Android.
    if not (values[0] or values[1]):
        logger.error("Choose iOS or Android")
        return

    partial = None
    if values["selection"].strip():
        # The same options table the CLI fills in, so the GUI cannot wire up a different run. The two
        # mismatch answers were settled in the confirmation dialog above.
        cli_values = {"selection": values["selection"].strip(),
                      "relations": _relations_spec(relation_state),
                      "case-ref": values.get("case_ref", "").strip(),
                      "sources-mismatch": "proceed", "version-mismatch": "resolve"}
        partial, error = _partial_request(cli_values)
        if error:
            logger.error(error)
            sg.popup_error(error, keep_on_top=True)
            return

    try:
        run(zip_path=values["zip"], keychain=values["keychain"], workdir=values["workdir"],
            os_mode="ios" if values[0] else "android",
            padding=PADDING_MAP.get(values.get("padding"), "both"),
            tz=_map_timezone(values.get("timezone")),
            tile_server=values.get("tile_server", "").strip(),
            pause=True, partial=partial)
    except (partial_report.EvidenceMismatch, partial_report.AmbiguousSelection, LookupError) as error:
        # A refused partial run reaches here. Without this it left a traceback on the console and
        # **nothing in the log**, so the examiner saw a run that simply stopped: the reason has to be
        # in the log next to the run it belongs to, and in front of the person who asked for it.
        logger.error(str(error))
        sg.popup_error(f"The partial report was not built.\n\n{error}",
                       title="Partial report refused", keep_on_top=True)
        os.system("pause")


if __name__ == '__main__':
    main(sys.argv[1:])
