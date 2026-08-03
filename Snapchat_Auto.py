import sys
import FreeSimpleGUI as sg
from scripts import ParseSnapchat_iOS
from scripts import getCacheAndroid
from scripts.data import extract_zip
from scripts import parseSnapvideos_PREFETCH
from scripts import offline_maps
import os
import json
import logging
import datetime
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


def _pyproject_candidates():
    """Locations pyproject.toml may live in across run modes: source tree, a Nuitka onefile bundle
    (dirname(__file__)), a PyInstaller bundle (sys._MEIPASS), and beside the built binary."""
    seen, out = set(), []
    for base in (app_path,
                 os.path.dirname(os.path.abspath(__file__)),
                 getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else None):
        if not base:
            continue
        cand = os.path.join(base, "pyproject.toml")
        if cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def get_version():
    """Return the project version — from a bundled/source pyproject.toml, else package metadata.

    The build bundles pyproject.toml (see build_nuitka.cmd) so the frozen GUI shows the real version;
    Nuitka sets neither sys.frozen nor sys._MEIPASS, so we probe several candidate locations.
    """
    try:
        import tomllib
        for path in _pyproject_candidates():
            try:
                with open(path, "rb") as f:
                    v = tomllib.load(f).get("project", {}).get("version")
                if v:
                    return v
            except FileNotFoundError:
                continue
            except Exception:
                continue
    except Exception:
        pass
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("Snapchat_Auto")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    return "unknown"

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


def write_index(root_dir, reports_subdir="Reports", zip_path=None, keychain_path=None):
    """Write <root_dir>/index.html linking to whichever sub-reports were produced under
    <root_dir>/<reports_subdir>/, with the source extraction / keychain paths at the top."""
    # Each report opens in its own *named* tab (target), shared with the cross-report links inside
    # the reports, so navigating between reports reuses one tab per report instead of piling up new
    # ones. Ctrl/Shift/middle-click still force a new tab/window (browser default).
    reports = [
        ("Conversations", f"{reports_subdir}/Conversations/Conversations_report.html",
         "Every conversation, with a detail page per conversation: messages, senders, timestamps "
         "and cached chat media.", "scauto_convs"),
        ("Contacts", f"{reports_subdir}/Contacts/Contacts_report.html",
         "Every contact recovered from the friends artifact, linked to their conversation.",
         "scauto_contacts"),
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
    sources = (f'<div class="sources"><div class="stitle">Sources</div>'
               f'{_src_row("Extraction", zip_path)}'
               f'{_src_row("Keychain / keystore", keychain_path)}</div>')
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
 ul{{list-style:none;padding:16px 26px 22px;max-width:760px}}
 li{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 18px;margin-bottom:12px}}
 li a{{font-size:16px;font-weight:600;color:#2d2d71;text-decoration:none}} li a:hover{{text-decoration:underline}}
 .d{{color:#666;font-size:13px;margin-top:3px}}
</style></head><body>
<header><h1>Snapchat Auto v{get_version()} &mdash; Report index</h1><div class="sub">Generated {generated}</div></header>
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
        tile_server="", run_name=None, pause=False):
    """Do one extraction + report run. Shared by the GUI and the command line.

    Everything for the run lives under a single ``Snapchat_Auto-<timestamp>`` folder inside
    ``workdir``: ``ExtractedData/``, ``SnapFixedVideos/``, ``Reports/`` and ``index.html``.
    ``run_name`` pins that folder name instead of using a timestamp, which is what makes a
    scripted re-run land in the same place. Returns the run folder.

    ``pause`` waits for a keypress at the end — the GUI wants that so the console does not vanish;
    a scripted run must not, or it hangs forever with nobody there to press a key.
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
            ParseSnapchat_iOS.main(extracted_files_dir[0], extracted_files_dir[1], keychain,
                                   padding=padding, tz=tz, report_dir="./Reports",
                                   tile_server=tile_server)
            # Write the report index BEFORE the pause, so index.html exists when the "press any
            # key" prompt appears (previously the pause lived inside the parser and blocked this).
            write_index(".", "Reports", zip_path=zip_path, keychain_path=keychain)
            logger.info(f"Report index: {os.path.abspath('index.html')}")
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
    logger.info(f"Format: {res['format'] or 'not recognized'} — {res['items']} item(s), "
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
          "  --tz <spec>             local | utc | <IANA name> | <±HH:MM>  (default: local).\n"
          "  --padding both|strip|keep   Memories media padding (default: both).\n"
          "  --tile-server <url>     Offline map tile server, {z}/{x}/{y} template.\n"
          "  --run-name <name>       Use this run-folder name instead of a timestamp, so a\n"
          "                          repeated run lands in the same place.\n\n"
          "Other:\n"
          "  --diag-keychain <file>  Check a keychain file and report what it holds, without\n"
          "                          running an extraction. Exit code 0 if egocipher was\n"
          "                          recovered, 1 otherwise.\n"
          "  --help, -h              Show this message.\n\n"
          "A headless run never pauses for a keypress, so it is safe to call from a script.")


# The headless options, and whether each takes a value.
_CLI_OPTIONS = {"zip": True, "keychain": True, "workdir": True, "os": True, "tz": True,
                "padding": True, "tile-server": True, "run-name": True}


def _parse_cli(args):
    """Parse the headless options. Returns (values, error message or None)."""
    values, index = {}, 0
    while index < len(args):
        token = args[index]
        name = token.lstrip("-/").lower()
        if name not in _CLI_OPTIONS:
            return values, f"unknown option '{token}'"
        index += 1
        if index >= len(args) or args[index].startswith("-"):
            return values, f"'{token}' requires a value"
        values[name] = args[index]
        index += 1
    return values, None


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
    try:
        folder = run(zip_path=zip_path, keychain=keychain,
                     workdir=values.get("workdir", "."), os_mode=os_mode, padding=padding,
                     tz=_map_timezone(values.get("tz", "local")),
                     tile_server=(values.get("tile-server") or "").strip(),
                     run_name=values.get("run-name"), pause=False)
    except Exception as error:
        logger.error(f"Run failed: {error}")
        return 1
    logger.info(f"Done: {folder}")
    return 0


def main(args):
    flag = args[0].lstrip("-/").lower() if args else ""
    if flag in ("help", "h", "?"):
        print_usage()
        sys.exit(0)
    if flag in ("diag-keychain", "diagkeychain"):
        sys.exit(diag_keychain(args[1] if len(args) > 1 else ""))
    if flag in _CLI_OPTIONS:                                  # a headless run
        sys.exit(run_cli(args))
    if flag:                                                  # an argument was given but not
        print_usage()                                         # recognized — don't silently fall
        sys.exit(2)                                           # through to the GUI

    logger.info(f"Snapchat Auto v{get_version()}")
    cfg = load_config()
    show_disclaimer(cfg)

    def _browse_start(this_val, other_val, saved_key):
        """Start a file dialog in the folder of the other field, else this field's saved dir."""
        for candidate in (other_val, this_val, cfg.get(saved_key, "")):
            if candidate and os.path.dirname(candidate):
                return os.path.dirname(candidate)
        return "."

    has_zip, has_kc = bool(cfg.get("zip")), bool(cfg.get("keychain"))
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
        [sg.Text('Daylight saving time is applied automatically for named zones (e.g. America/Toronto).',
                 font=("", 8), text_color="gray")],
        [sg.Text('Offline map tile server (optional)')],
        [sg.In(cfg.get("tile_server", ""), key="tile_server"),
         sg.Button('Test', key="tile_test")],
        [sg.Text('Your own XYZ tile server, e.g. http://localhost:8080 or '
                 'http://host/tiles/{z}/{x}/{y}.png. When set, each geolocated Memory gets a small '
                 'map on its detail page. Nothing is downloaded when this is empty.',
                 font=("", 8), text_color="gray")],
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
            break
    window.close()

    # merge into cfg so other saved settings (e.g. hide_disclaimer) are preserved
    cfg.update({"zip": values["zip"], "keychain": values["keychain"], "workdir": values["workdir"],
                "padding": values.get("padding", PADDING_OPTIONS[0]),
                "timezone": values.get("timezone", "Local time"),
                "tile_server": values.get("tile_server", "").strip()})
    save_config(cfg)

    # values[0]/values[1] are the iOS/Android radios. One is always selected (iOS is the default),
    # but pick explicitly rather than treating "not iOS" as Android.
    if not (values[0] or values[1]):
        logger.error("Choose iOS or Android")
        return
    run(zip_path=values["zip"], keychain=values["keychain"], workdir=values["workdir"],
        os_mode="ios" if values[0] else "android",
        padding=PADDING_MAP.get(values.get("padding"), "both"),
        tz=_map_timezone(values.get("timezone")),
        tile_server=values.get("tile_server", "").strip(),
        pause=True)


if __name__ == '__main__':
    main(sys.argv[1:])
