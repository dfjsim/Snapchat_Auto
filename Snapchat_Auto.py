import sys
import FreeSimpleGUI as sg
from scripts import ParseSnapchat_iOS
from scripts import getCacheAndroid
from scripts.data import extract_zip
from scripts import parseSnapvideos_PREFETCH
import os
import json
import logging
import datetime

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


def get_version():
    """Return the project version — from pyproject.toml (source) or package metadata."""
    try:
        import tomllib
        with open(os.path.join(app_path, "pyproject.toml"), "rb") as f:
            return tomllib.load(f).get("project", {}).get("version") or "unknown"
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
    "incompletely, or in rare cases incorrectly.\n\n"
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


def write_index(root_dir, reports_subdir="Reports"):
    """Write <root_dir>/index.html linking to whichever sub-reports were produced under
    <root_dir>/<reports_subdir>/."""
    reports = [
        ("Communications", f"{reports_subdir}/Communications/Communications_report.html",
         "Chats, contacts, groups and cached chat media."),
        ("Memories", f"{reports_subdir}/Memories/Memories_report.html",
         "Snapchat Memories with all associated media (SCContent + caching-media) and geolocation."),
        ("Local Memories (legacy)", f"{reports_subdir}/LocalMemories_legacy/LocalMemories_legacy_report.html",
         "Legacy Memories / My Eyes Only decryption report."),
    ]
    items = []
    for title, rel, desc in reports:
        if os.path.exists(os.path.join(root_dir, rel)):
            items.append(f'<li><a href="{rel}">{title}</a><div class="d">{desc}</div></li>')
    if not items:
        return
    generated = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Snapchat Auto report</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f4f8;color:#1b1b1f;margin:0}}
 header{{background:#2d2d71;color:#fff;padding:18px 26px}} header h1{{margin:0;font-size:20px}}
 header .sub{{opacity:.85;font-size:13px;margin-top:4px}}
 ul{{list-style:none;padding:22px 26px;max-width:760px}}
 li{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 18px;margin-bottom:12px}}
 li a{{font-size:16px;font-weight:600;color:#2d2d71;text-decoration:none}} li a:hover{{text-decoration:underline}}
 .d{{color:#666;font-size:13px;margin-top:3px}}
</style></head><body>
<header><h1>Snapchat Auto &mdash; Report index</h1><div class="sub">Generated {generated}</div></header>
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


def main(args):

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
        [sg.Button('Ok'), sg.Button('Cancel')]]

    window = sg.Window('Snapchat Auto', layout)
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
        elif event == "Ok":
            if not values["zip"] or not os.path.isfile(values["zip"]):
                sg.popup_error("Please select a valid extraction ZIP file.")
                continue
            if not values["workdir"]:
                sg.popup_error("Please select a Working/Temp/Report directory (required).")
                continue
            break
    window.close()

    # merge into cfg so other saved settings (e.g. hide_disclaimer) are preserved
    cfg.update({"zip": values["zip"], "keychain": values["keychain"], "workdir": values["workdir"],
                "padding": values.get("padding", PADDING_OPTIONS[0]),
                "timezone": values.get("timezone", "Local time")})
    save_config(cfg)

    padding = PADDING_MAP.get(values.get("padding"), "both")
    tz = _map_timezone(values.get("timezone"))
    run_dt = datetime.datetime.today().strftime('%Y%m%d_%H%M%S')

    # Everything for this run lives under a single Snapchat_Auto-<dt> folder inside the chosen
    # Working/Temp/Report directory: ExtractedData/, SnapFixedVideos/, Reports/ and index.html.
    os.makedirs(values["workdir"], exist_ok=True)
    os.chdir(values["workdir"])
    run_root = "Snapchat_Auto-" + run_dt
    os.makedirs(run_root, exist_ok=True)
    os.chdir(run_root)
    add_log_file(".")
    logger.info(f"Run folder: {os.path.abspath('.')}")

    if values[0]:
        logger.info("You chose iOS")
        extracted_files_dir = extract_zip.extract(values['zip'], 'ios', dest="ExtractedData")
        if not os.path.exists("SnapFixedVideos"):
            parseSnapvideos_PREFETCH.main(extracted_files_dir[0])
        else:
            logger.info("Found SnapFixedVideos folder, skipping that step")
        ParseSnapchat_iOS.main(extracted_files_dir[0], extracted_files_dir[1], values["keychain"],
                               padding=padding, tz=tz, report_dir="./Reports")
        write_index(".", "Reports")
        logger.info(f"Report index: {os.path.abspath('index.html')}")
    elif values[1]:
        logger.info("You chose Android")
        extracted_files_dir = extract_zip.extract(values['zip'], 'android', dest="ExtractedData")
        getCacheAndroid.main(extracted_files_dir)


if __name__ == '__main__':
    main(sys.argv[1:])
